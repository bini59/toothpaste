"""Select queued work and start or resume its agent."""
from __future__ import annotations

import fcntl
from http.client import HTTPException
from pathlib import Path
from subprocess import TimeoutExpired

from . import milestone as M
from . import notion, notion_tickets, review
from . import runner as R
from .briefs import render_agent_brief, slug
from .core import (
    ROOT, GitHubNotConnected, asana_comments, db, event, gh_json,
    github_token, meta_get, meta_set, repo_rules, repos_for_project, settings,
)
from .pr_tracking import latest_run, repo_paths
from .ticket_sync import issue_ref


def chosen_repo(con, t, repos=None):
    if repos is None:
        repos = repos_for_project(con, t["project_gid"])
    if t["repo_override"]:
        r = con.execute("SELECT * FROM repos WHERE name=?", (t["repo_override"],)).fetchone()
        if r:
            base = next((x for x in repos if x["repo"] == r["name"]), {})
            return {"repo": r["name"], "role": base.get("role"), "orca_repo_id": r["orca_repo_id"], "path": r["path"],
                    "github": r["github"], "description": r["description"]}
        # Older Notion rows can still name a deleted repo until their next sync.
        if not t["gid"].startswith("notion:"):
            return None
    return repos[0] if len(repos) == 1 else None


def build_brief(con, t, repo: dict) -> str:
    from .pipeline import deps_status

    tpl = (ROOT / "prompts" / "work_brief.md").read_text()
    ref = issue_ref(t["gid"])
    is_notion = t["gid"].startswith("notion:")
    if is_notion:
        if err := M.staging_config_error(t, repo["repo"]):
            raise notion.NotionError(err)  # deploy settings may have changed since the ticket was queued
        t, markdown = notion_tickets.queued_page(con, t, repo)
        notes = "\n\n".join(filter(None, (t["notes"], markdown)))
    else:
        notes = t["notes"] or ""
    try:
        if ref:
            j = gh_json(["issue", "view", ref[1], "--repo", ref[0], "--json", "comments"])
            comments = [{"created_by": {"name": (c.get("author") or {}).get("login")}, "created_at": c.get("createdAt"),
                         "text": c.get("body") or ""} for c in j.get("comments") or []]
        elif is_notion:
            comments = []
        else:
            comments = asana_comments(t["gid"])
    except (HTTPException, OSError, RuntimeError, TimeoutExpired, ValueError):
        comments = []
    ctext = "\n".join(
        f"- {(c.get('created_by') or {}).get('name', '?')} ({(c.get('created_at') or '')[:10]}): {c.get('text', '').strip()}"
        for c in comments[-20:]) or "(no comments)"
    siblings = [r for r in repos_for_project(con, t["project_gid"]) if r["repo"] != repo["repo"]]
    stext = "\n".join(f"- {r['repo']} ({r['role']}): {r['path']} — {r['description']}" for r in siblings) or "(none)"
    deps = deps_status(con, t)
    dstate = lambda d: "done" if d["completed"] else f"merged into {d['landed']}" if d["landed"] else "OPEN"  # noqa: E731
    dtext = "\n".join(f"- {d['name']} ({dstate(d)})" for d in deps) or "(none)"
    vals = {
        "KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "PROJECT": t["project_name"] or "",
        "SECTION": t["section"] or "", "PARENT": t["parent_name"] or "(none)", "DUE": t["due_on"] or "(none)",
        "NOTES": (notes or "(empty)").strip(), "COMMENTS": ctext, "DEPS": dtext,
        "REPO": repo["repo"], "ROLE": repo.get("role") or "", "REPO_DESC": repo.get("description") or "",
        "GITHUB": repo.get("github") or "", "SIBLINGS": stext,
    }
    # train repos (and pr_into_staging repos, e.g. main + dev) take feature PRs into their staging branch only
    # (or, for a milestone subtask, into the milestone branch)
    fb = M.base_for(t, repo["repo"])
    if fb:
        prod = repo_rules(repo["repo"])["target"]
        vals["BASE_FLAG"] = f"--base {fb} "
        vals["BASE_NOTE"] = (f"The PR base must be `{fb}` (never `{prod}`); "
                             f"branch from `origin/{fb}` if you have not committed yet.")
        if fb.startswith(M.PREFIX):
            vals["BASE_NOTE"] = (f"This ticket belongs to the milestone {t['parent_name']}. The PR base must be `{fb}` "
                                 f"(never `{M.staging_of(repo['repo'])}` or `{prod}`). Branch from `origin/{fb}` "
                                 f"if you have not committed yet. ticket-pilot merges it into `{fb}` "
                                 f"after the review, and the milestone goes to staging as a whole.")
    else:
        vals["BASE_FLAG"], vals["BASE_NOTE"] = "", ""
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return render_agent_brief(tpl, t)


def resume_staging_error(con, t):
    """A retry uses the existing worktree's repo even if the project mapping has changed."""
    if (M.notion_milestone(t) or M.github_milestone(t)) and (r := latest_run(con, t["gid"])):
        return M.staging_config_error(t, r["repo"])
    return None


def _start_guard(con, t, repo, base):
    from .pipeline import compute_stage

    notion_guard = notion_tickets.worktree_guard(con, t, repo)

    def check(*, lock=True):
        if notion_guard:
            notion_guard(lock=lock)
        elif lock and not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT * FROM tickets WHERE gid=?", (t["gid"],)).fetchone()
        repo_name = repo["repo"]
        if (not current or current["project_gid"] != t["project_gid"] or current["parent_gid"] != t["parent_gid"]
                or (not notion_guard and (compute_stage(con, current) != "queued" or chosen_repo(con, current) != repo))
                or (M.base_for(current, repo_name) or R.worktree_base(repo_name)) != base):
            raise RuntimeError("워크트리를 준비하는 동안 대기열이나 기준 브랜치가 바뀌었어요")

    return check


def dispatch(dry_run=False, log=print, only: str | None = None) -> list[str]:
    """Run a dispatch pass and return the ticket keys it handled."""
    con = db()
    # Serialize manual and scheduled dispatch from slot selection through run registration.
    # Take a separate lock so sync can still update tickets during worktree and page reads.
    db_path = con.execute("PRAGMA database_list").fetchone()[2]
    if dry_run or not db_path:
        return _dispatch(con, dry_run, log, only)
    with Path(db_path + ".dispatch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _dispatch(con, dry_run, log, only)


def _dispatch(con, dry_run, log, only):
    from .pipeline import _insert_run, compute_stage, prepare_worktree, restage

    st = settings()
    active = con.execute("SELECT count(*) c FROM tickets WHERE stage IN ('dispatched','in_progress','blocked')").fetchone()["c"]
    slots = max(0, st["max_active_runs"] - active)
    where = " AND (gid=? OR lower(key)=lower(?))" if only else ""
    params = (only, only) if only else ()
    cands = con.execute("SELECT * FROM tickets WHERE stage IN ('conflict','rework','queued') AND milestone_error IS NULL"
                        + where + " ORDER BY stage='queued', due_on IS NULL, due_on", params).fetchall()
    log(f"dispatch: active={active} slots={slots} conflict={sum(t['stage'] == 'conflict' for t in cands)} "
        f"rework={sum(t['stage'] == 'rework' for t in cands)} "
        f"queued={sum(t['stage'] == 'queued' for t in cands)}")
    done = []
    existing = None
    answered = con.execute("SELECT * FROM tickets WHERE stage='blocked' AND reply IS NOT NULL AND milestone_error IS NULL"
                           + where, params).fetchall()
    # no agent starts without tp's own GitHub token (TP-112); escalation to the owner and triage still run.
    # The error event is written once per disconnect, not every precheck.
    gh_ok = dry_run or github_token() is not None
    if not gh_ok and (cands or answered):
        log(f"dispatch: {GitHubNotConnected()}")
        if not meta_get(con, "github_missing_logged"):
            event(con, "error", f"dispatch: {GitHubNotConnected()}")
            meta_set(con, "github_missing_logged", True)
    elif not dry_run and gh_ok and meta_get(con, "github_missing_logged"):
        meta_set(con, "github_missing_logged", False)

    def retry_error(t):
        return notion_tickets.retry_error(con, t) or resume_staging_error(con, t)

    # answered BLOCKED tickets already hold a slot, so they go before the slot limit
    for t in answered if gh_ok else ():
        if err := retry_error(t):
            log(f"{t['key']}: {err}")
        elif review.unblock(con, t, dry_run=dry_run, log=log):
            done.append(t["key"])
    # rework past the round cap goes to the owner instead of the agent; that needs no slot
    def cap_of(t):
        r = latest_run(con, t["gid"])
        return review.max_rework_rounds(r["repo"] if r else None)
    escalated = {t["gid"] for t in cands if t["stage"] == "rework" and (cap := cap_of(t))
                 and review.escalate_rework(con, t, cap, dry_run=dry_run, log=log)}
    done += [t["key"] for t in cands if t["gid"] in escalated]
    cands = [t for t in cands if t["gid"] not in escalated]
    for t in cands if gh_ok else ():
        if not slots:
            break
        if t["stage"] != "queued":
            if err := retry_error(t):
                log(f"{t['key']}: {err}")
                continue
            handler = review.resolve_conflict if t["stage"] == "conflict" else review.rework
            slots -= 1
            if handler(con, t, dry_run=dry_run, log=log):
                done.append(t["key"])
            continue
        # A stored queue entry can outlive a dependency, repo or run update.
        t = con.execute("SELECT * FROM tickets WHERE gid=?", (t["gid"],)).fetchone()
        if not t:
            continue
        repo = chosen_repo(con, t)
        if not repo:
            continue
        if err := M.staging_config_error(t, repo["repo"]):
            log(f"{t['key']}: {err}")
            continue
        if compute_stage(con, t) != "queued":
            continue
        if not dry_run and con.in_transaction:
            con.commit()  # release earlier writes before reading the ticket body and comments
        pr_base = M.base_for(t, repo["repo"])
        base = pr_base or R.worktree_base(repo["repo"])
        try:
            guard = _start_guard(con, t, repo, base)
            brief = build_brief(con, t, repo)
        except notion.NotionError as e:
            log(f"{t['key']}: {e}")
            if not dry_run:
                event(con, "error", f"{t['key']}: {e}", t["gid"])
            continue
        name = slug(t["key"])
        if dry_run:
            slots -= 1
            log(f"--- would create {repo['repo']}/{name}\n{brief}")
            continue
        if existing is None:
            existing = R.worktrees(repo_paths(con))
        clash = next((w for w in existing if w["repo"] == repo["repo"] and w["path"].rstrip("/").endswith("/" + name)), None)
        pending = meta_get(con, f"dispatch_pending:{clash['path']}") if clash else None
        retry = pending and pending["ticket_gid"] == t["gid"]
        adopt = clash and not retry
        wt = None
        try:
            guard(lock=bool(clash))
            if clash and con.execute("SELECT 1 FROM runs WHERE state='active' AND removed_at IS NULL AND worktree_path=?",
                                     (clash["path"],)).fetchone():
                raise RuntimeError("다른 작업이 이미 이 워크트리를 사용 중이에요")
            if pending and not retry:
                raise RuntimeError("다른 작업이 이 워크트리에서 시작을 재시도할 예정이에요")
            if not adopt:
                if retry:
                    if (pending["base"], pending["branch"]) != (base, clash["branch"]):
                        raise RuntimeError("시작에 실패한 워크트리의 기준 브랜치 또는 작업 브랜치가 바뀌었어요")
                    wt = clash
                else:
                    if (pr_base or "").startswith(M.PREFIX):
                        M.ensure(con, t, repo["repo"])  # created on the first subtask
                        con.commit()  # release milestone.ensure's writes before the remote fetch
                    wt = R.create(repo["repo"], repo["path"], name, base, before_add=guard)
                (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
                (ROOT / "data" / "briefs" / f"{name}.md").write_text(brief)
                setup, prep = prepare_worktree(repo["repo"], repo["path"], wt["path"])
                agent = R.repo_extra(repo["repo"]).get("agent")
                description = R.describe(agent)
                handle = R.launch(wt["path"], brief, name, setup=setup, repo_path=repo["path"], agent=agent)
        except notion.NotionError as e:
            event(con, "error", f"{t['key']}: {e}", t["gid"])
            con.commit()
            log(f"{t['key']}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            if wt:
                # Keep a worktree created by dispatch eligible for launch on the next attempt.
                meta_set(con, f"dispatch_pending:{wt['path']}", {
                    "ticket_gid": t["gid"], "branch": wt["branch"], "base": base,
                })
            event(con, "error", f"{t['key']}: worktree {'start' if wt else 'create'} failed: {e}", t["gid"])
            con.commit()
            log(f"ERROR {t['key']}: {e}")
            continue
        if adopt:
            _insert_run(con, t, repo, clash, None, "adopt")
            event(con, "adopt", f"{t['key']}: existing worktree {name} linked", t["gid"])
        else:
            _insert_run(con, t, repo, wt | {"workspaceStatus": "in-progress", "comment": f"{t['key']} {t['name']}"[:120]},
                        handle, "dispatch")
            con.execute("DELETE FROM meta WHERE k=?", (f"dispatch_pending:{wt['path']}",))
            event(con, "dispatch", f"{t['key']} -> {repo['repo']}/{name} ({description}, "
                                   f"{wt['branch']}{'; ' + prep if prep else ''})", t["gid"])
        restage(con, ticket_gid=t["gid"])
        con.commit()
        if not adopt:
            log(f"dispatched {t['key']} -> {repo['repo']}/{name} agent={handle}")
        done.append(t["key"])
        slots -= 1
    if not dry_run:
        restage(con)
        con.commit()
    return done
