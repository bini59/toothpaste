"""Ticket sync, staging and dispatch, with entry points for tracking and review."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .core import (
    ROOT, AsanaError, TASK_FIELDS, asana, asana_all, asana_comments, asana_workspace, db, event, load_map,
    meta_get, meta_set, now, project_map, repo_rules, repos_for_project, run, settings,
    feature_base, GitHubNotConnected, github_token,
)
from . import runner as R
from . import metrics_usage as MU
from . import milestone as M
from . import notifications as N
from . import notion, notion_stages, notion_tickets, pr_tracking, review
from .briefs import render_agent_brief, slug
from .pr_tracking import VIOLATION, latest_run, legacy, repo_paths, run_pr
from .review import escalate_rework, max_rework_rounds, resolve_conflict, review_overridden, review_queue, rework, unblock
from .runner import repo_extra

ACTIVE_STAGES = ("dispatched", "in_progress")
STAGE_ORDER = [
    "conflict", "rework", "queued", "needs_repo", "unmapped", "waiting_dep", "milestone_error", "needs_staging",
    "ms_hold", "collecting", "dispatched", "in_progress",
    "blocked", "agent_done", "pr_open", "reviewed", "approving", "milestone", "deploying", "staged", "merged", "done",
    "closed", "skipped", "open",
]


# ---------------------------------------------------------------- helpers

def field(task: dict, name: str):
    for c in task.get("custom_fields") or []:
        if c.get("name") == name and c.get("display_value"):
            return c["display_value"]
    return None


def find_ticket(con, ref: str):
    r = con.execute("SELECT * FROM tickets WHERE gid=? OR lower(key)=lower(?)", (ref, ref)).fetchone()
    if not r:
        raise SystemExit(f"ticket not found: {ref}")
    return r


ACTIVE_DEPLOY = ("requested", "running", "pending", "deploying")


def active_release_pr_urls(con) -> set[str]:
    """PRs whose production verification belongs to a running or queued release."""
    rows = con.execute(f"SELECT prs_json FROM releases WHERE status IN ({','.join('?' * len(ACTIVE_DEPLOY))})",
                       ACTIVE_DEPLOY)
    return {pr["url"] for row in rows for pr in json.loads(row["prs_json"] or "[]") if pr.get("url")}


def active_deploy(con, gid: str):
    """A dashboard deploy or the weekday train that includes this ticket and has not finished yet."""
    return con.execute(f"""SELECT * FROM releases WHERE status IN ({','.join('?' * len(ACTIVE_DEPLOY))})
                           AND (ticket_gid=? OR prs_json LIKE ?) ORDER BY id DESC LIMIT 1""",
                       (*ACTIVE_DEPLOY, gid, f'%"ticket_gid": "{gid}"%')).fetchone()


def compute_stage(con, t, *, repos=None) -> str:
    if t["completed"]:
        return "done"
    r = latest_run(con, t["gid"])
    if M.is_ms(t["gid"]):
        # milestone ticket: collecting while its subtasks still go into the milestone branch (milestone.stage)
        live = r if r and r["state"] != "abandoned" else None
        own = M.stage(con, t, run_pr(con, live["id"]) if live else None)
        if own:
            return own
    if r and r["state"] != "abandoned":
        pr = run_pr(con, r["id"])
        if pr and (pr["release_id"] or pr["prod_at"]):
            return "done"  # verified in production (deploy.py / release train / track.py)
        if pr and active_deploy(con, t["gid"]):
            return "deploying"
        if (not pr or pr["state"] == "OPEN") and t["milestone_error"] and notion_tickets.uses_milestone_kind(con, t):
            return "blocked"
        if pr:
            if pr["state"] == "MERGED":
                if (pr["base"] or "").startswith(M.PREFIX):
                    return "milestone"  # in its milestone branch; ships with the milestone PR (milestone.py)
                rr = repo_rules(pr["repo"])
                # staging == target (32_Ticket_pilot: approval merges into main) means the merge already is the deploy
                if rr and rr["staging"] and pr["base"] == rr["staging"]["branch"] != rr["target"]:
                    return "staged"  # in staging; prod via the weekday train (train repos) or the 배포 page
                # merged but not verified in production yet (track.py checks the deployed sha); merge != deploy
                return "merged"
            if pr["state"] == "CLOSED":
                return "closed"
            if (r["ws_comment"] or "").startswith(VIOLATION):
                return "blocked"  # The owner decides (reply or drop) before anything else, even an approved review
            if pr["mergeable"] == "CONFLICTING":
                blocked = (r["ws_comment"] or "").upper().startswith("BLOCKED")
                if pr["conflict_sha"] and pr["conflict_sha"] == pr["head_sha"]:
                    # resolve_conflict() already sent this head back to the agent to merge the base branch in
                    if blocked:
                        return "blocked"
                    if r["ws_status"] == "in-progress":
                        return "in_progress"
                elif r["ws_status"] != "in-progress" and not blocked:
                    # 충돌 대기 (대기 column): the next tp dispatch relaunches the agent when a slot is free
                    return "conflict"
            rv = con.execute("SELECT verdict, rework_at FROM reviews WHERE pr_url=? AND head_sha=? ORDER BY id DESC LIMIT 1",
                             (pr["url"], pr["head_sha"])).fetchone()
            if not rv:
                # a draft PR pushed before the agent stopped with BLOCKED needs the owner's reply, not a review
                if (r["ws_comment"] or "").upper().startswith("BLOCKED"):
                    return "blocked"
                # agent may push before it is done; it signals completion with workspace status in-review
                return "in_progress" if r["ws_status"] == "in-progress" else "pr_open"
            if t["approve_requested_at"] and (rv["verdict"] == "approve" or review_overridden(t, pr)):
                return "approving"  # The owner approved on the dashboard; the host merges it into staging next tick
            if rv["verdict"] == "changes":
                if (r["ws_comment"] or "").upper().startswith("BLOCKED"):
                    return "blocked"  # agent stopped mid-fix, or escalate_rework() handed the loop to the owner
                if rv["rework_at"]:
                    if r["ws_status"] in ("in-review", "completed"):
                        # the agent answered the review without a new commit (it may disagree instead of changing
                        # code, rework_brief step 2); review_queue re-reviews the same head
                        return "agent_done"
                    return "in_progress"  # review sent back to the agent; waiting for its fixes
                # back to the queue: tp dispatch relaunches the agent with the review, or after
                # review.max_rework_rounds fix rounds asks the owner instead (escalate_rework, 2026-10-27)
                return "rework"
            return "reviewed"
        if (r["ws_comment"] or "").upper().startswith("BLOCKED"):
            return "blocked"
        if r["ws_status"] in ("in-review", "completed"):
            return "agent_done"
        if r["ws_status"] == "in-progress":
            return "in_progress"
        return "dispatched"
    if r and t["skip"] and t["gid"].startswith("notion:"):
        # A dropped run still needs skipped-worktree cleanup after its page leaves the import filter.
        return "skipped"
    if not t["assigned"]:
        return "closed"
    if not t["has_tag"]:
        return "open"
    if not t["gid"].startswith("notion:") and M.has_tag(json.loads(t["tags"] or "[]")):
        return "open"  # a milestone parent ships through its milestone tickets (ms:<gid>:<repo>), not an agent
    if t["skip"]:
        return "skipped"
    if t["gid"].startswith("notion:") and t["milestone_error"]:
        return "milestone_error"
    if t["open_deps"] and unmet_deps(con, t):
        return "waiting_dep"
    if t["repo_override"] and not t["gid"].startswith("notion:"):
        return "needs_staging" if M.staging_config_error(t, t["repo_override"]) else "queued"
    if repos is None:  # preview can supply the project's unsaved repo selection
        repos = repos_for_project(con, t["project_gid"])
    repo = chosen_repo(con, t, repos)
    if repo:
        return "needs_staging" if M.staging_config_error(t, repo["repo"]) else "queued"
    return "needs_repo" if repos else "unmapped"


def deps_status(con, t) -> list[dict]:
    """t's deps, each with landed = the feature base the dep's PR is already merged into (TP-20).
    The dep's repo must be t's repo or another repo of t's project (a project with an API repo and web repos: the API change in
    staging unblocks the web ticket built against it). t's worktree branches from that base, or runs against that staging.
    Asana completion still waits for production (deploy.py / release.py / track.py)."""
    deps = json.loads(t["deps_json"] or "[]")
    if not any(not d["completed"] for d in deps):
        return [{**d, "landed": None} for d in deps]
    repo = chosen_repo(con, t)
    allowed = {r["repo"] for r in repos_for_project(con, t["project_gid"])} | ({repo["repo"]} if repo else set())
    for d in deps:
        d["landed"] = None
        if not d["completed"]:
            r = latest_run(con, d["gid"])
            pr = run_pr(con, r["id"]) if r and r["state"] != "abandoned" else None
            if (pr and pr["state"] == "MERGED" and pr["repo"] in allowed and pr["base"]
                    and pr["base"] in (feature_base(pr["repo"]), M.base_for(t, pr["repo"]))):
                d["landed"] = pr["base"]
    return deps


def unmet_deps(con, t) -> list[dict]:
    return [d for d in deps_status(con, t) if not d["completed"] and not d["landed"]]


DONE_STAGES = ("done", "closed")


def restage(con):
    for t in con.execute("SELECT * FROM tickets").fetchall():
        s = compute_stage(con, t)
        done = s in DONE_STAGES
        if s != t["stage"] or done != bool(t["done_at"]):
            # done_at: kept while the ticket stays done/closed, cleared when it is reopened
            con.execute("UPDATE tickets SET stage=?, done_at=? WHERE gid=?",
                        (s, (t["done_at"] or now()) if done else None, t["gid"]))
        if s != t["stage"]:
            if t["gid"].startswith("notion:"):
                notion_stages.enqueue(con, t, s)
            if t["stage"] is not None and s != "open":
                event(con, "stage", f"{t['key']}: {t['stage']} -> {s}", t["gid"])


# ---------------------------------------------------------------- sync

def _gh_issue_json(args: list[str]):
    return json.loads(run(["gh", *args], timeout=120).stdout or "null")


def issue_ref(gid: str) -> tuple[str, str] | None:
    """GitHub issue tickets have gid 'gh:<owner/repo>#<number>'."""
    if not gid.startswith("gh:"):
        return None
    gh, num = gid[3:].rsplit("#", 1)
    return gh, num


ISSUE_LABEL = "tp"


def _open_issues(gh: str) -> list:
    # Fetch the immediate parent in the same listing, including parents in another repository.
    # Finish every page before marking missing issues closed.
    owner, name = gh.split("/")
    query = """query($owner:String!, $name:String!, $endCursor:String) {
      repository(owner:$owner, name:$name) { issues(states:OPEN, first:100, after:$endCursor,
                                                    orderBy:{field:CREATED_AT, direction:DESC}) {
        pageInfo { hasNextPage endCursor }
        nodes { number title body url updatedAt labels(first:100) { nodes { name } }
                parent { number title url repository { nameWithOwner } labels(first:100) { nodes { name } } } }
      } }
    }"""
    data = run(["gh", "api", "graphql", "--paginate", "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"query={query}",
                "--jq", ".data.repository.issues.nodes[] | @json"], timeout=120)
    issues = [json.loads(line) for line in data.stdout.splitlines()]
    for issue in issues:
        issue["labels"] = issue["labels"]["nodes"]
        if parent := issue.get("parent"):
            parent["labels"] = parent["labels"]["nodes"]
    return issues


def issue_repos() -> dict:
    """{repo: (key prefix, label or None)} of the repos whose GitHub issues are tickets.
    settings.github_issues (legacy): every open issue. Repos of a `source: github` project (TP-92): open issues labeled
    `tp` only; the key prefix still comes from github_issues when the repo is listed there, else from the repo name
    (32_Ticket_pilot -> TICKETPILO) so two repos do not both make GH-<n>."""
    gi = settings().get("github_issues") or {}
    out = {repo: (prefix, None) for repo, prefix in gi.items()}
    for p in project_map().get("projects") or []:
        if p.get("source") == "github":
            for r in p.get("repos") or []:
                name = re.sub(r"[^A-Z0-9]", "", r["repo"].upper()).lstrip("0123456789")[:10]
                out[r["repo"]] = (gi.get(r["repo"]) or name or "GH", ISSUE_LABEL)
    return out


def sync_issues(con, ts: str, new_tagged: list) -> None:
    """Open issues of issue_repos() are tagged tickets for their repo. A closed (or deleted) issue completes the
    ticket; ticket-pilot closes it itself once the change is live. An open issue whose `tp` label was removed drops
    out like an Asana task assigned to someone else (assigned=0; re-labeling brings it back)."""
    sources = issue_repos()
    repo_names = {r[0].lower(): r[0] for r in con.execute("SELECT github FROM repos WHERE github IS NOT NULL")}
    # a repo that is no longer an issue source (project switched to asana/none, repo unlinked): its tickets drop out
    con.executemany("UPDATE tickets SET assigned=0 WHERE completed=0 AND assigned=1 AND gid LIKE 'gh:%' AND repo_override=?",
                    [(r[0],) for r in con.execute("SELECT DISTINCT repo_override FROM tickets WHERE gid LIKE 'gh:%'")
                     if r[0] not in sources])
    for repo, (prefix, label) in sources.items():
        r = con.execute("SELECT github FROM repos WHERE name=?", (repo,)).fetchone()
        if not r or not r["github"]:
            continue
        gh = r["github"]
        try:
            # all open issues, label filtered here: an unlabeled open issue must not look closed
            issues = _open_issues(gh)
        except Exception as e:  # noqa: BLE001  (keep the Asana sync going; do not close tickets on a failed read)
            event(con, "error", f"github issues {gh}: {str(e)[:300]}")
            continue
        seen, still_open = set(), set()
        for i in issues:
            gid, key = f"gh:{gh}#{i['number']}", f"{prefix}-{i['number']}"
            still_open.add(gid)
            labels = [lb["name"] for lb in i.get("labels") or []]
            is_parent = M.has_tag(labels)
            if not is_parent and label and label not in [x.lower() for x in labels]:
                continue
            seen.add(gid)
            parent = i.get("parent") or {}
            parent_gid = None
            if parent:
                owner_repo = parent["repository"]["nameWithOwner"]
                parent_gid = f"gh:{repo_names.get(owner_repo.lower(), owner_repo)}#{parent['number']}"
            in_ms = M.has_tag([lb["name"] for lb in parent.get("labels") or []])
            if in_ms:
                M.refresh_parent(con, parent_gid, parent["title"], parent["url"])
            if not is_parent and not con.execute(
                    "SELECT 1 FROM tickets WHERE gid=? AND completed=0 AND assigned=1 AND has_tag=1", (gid,)).fetchone():
                new_tagged.append(key)
            con.execute("""
              INSERT INTO tickets(gid,key,name,url,project_name,notes,tags,has_tag,deps_json,open_deps,completed,assigned,
                                  asana_modified_at,first_seen,last_seen,repo_override,parent_gid,parent_name,parent_url,in_milestone)
              VALUES(?,?,?,?,?,?,?,?,'[]',0,0,1,?,?,?,?,?,?,?,?)
              ON CONFLICT(gid) DO UPDATE SET key=excluded.key,name=excluded.name,notes=excluded.notes,tags=excluded.tags,
                completed=0,assigned=1,has_tag=excluded.has_tag,asana_modified_at=excluded.asana_modified_at,
                last_seen=excluded.last_seen,parent_gid=excluded.parent_gid,parent_name=excluded.parent_name,
                parent_url=excluded.parent_url,in_milestone=excluded.in_milestone
            """, (gid, key, i["title"], i["url"], f"GitHub {gh}", i.get("body") or "",
                  json.dumps(labels), int(not is_parent), i.get("updatedAt"), ts, ts, repo, parent_gid,
                  parent.get("title"), parent.get("url"), int(in_ms)))
        for row in con.execute("SELECT gid FROM tickets WHERE completed=0 AND gid GLOB ?", (f"gh:{gh}#*",)).fetchall():
            if row["gid"] not in seen:
                col = "assigned=0" if row["gid"] in still_open else "completed=1"
                con.execute(f"UPDATE tickets SET {col} WHERE gid=?", (row["gid"],))


def _sync_asana(con, st, mapped, names, hidden, ts, new_tagged):
    tasks = asana_all("/tasks", {
        "assignee": "me", "workspace": asana_workspace(), "completed_since": "now", "opt_fields": TASK_FIELDS,
    })
    dep_cache: dict[str, dict] = {}
    seen = set()
    for t in tasks:
        if t.get("completed"):
            continue
        parent = t.get("parent") or {}
        projects = t.get("projects") or parent.get("projects") or []
        proj = next((p for p in projects if p["gid"] in mapped), projects[0] if projects else None)
        if proj and proj["gid"] in hidden:
            continue
        section = next((m.get("section", {}).get("name") for m in t.get("memberships") or []
                        if proj and (m.get("project") or {}).get("gid") == proj["gid"]), None)
        key = field(t, "ID") or f"AS-{t['gid']}"
        tags = [g["name"] for g in t.get("tags") or []]
        deps = []
        for d in t.get("dependencies") or []:
            if d["gid"] not in dep_cache:
                dep_cache[d["gid"]] = asana("GET", f"/tasks/{d['gid']}", {"opt_fields": "name,completed"})["data"]
            dd = dep_cache[d["gid"]]
            deps.append({"gid": d["gid"], "name": dd.get("name"), "completed": dd.get("completed")})
        has_tag = 1 if st["asana_tag"] in [x.lower() for x in tags] else 0
        in_ms = 1 if parent.get("gid") and M.has_tag([g.get("name") or "" for g in parent.get("tags") or []]) else 0
        old = con.execute("SELECT has_tag FROM tickets WHERE gid=?", (t["gid"],)).fetchone()
        if has_tag and (not old or not old["has_tag"]):
            new_tagged.append(key)
        con.execute("""
          INSERT INTO tickets(gid,key,name,url,project_gid,project_name,section,parent_gid,parent_name,due_on,notes,tags,
                              has_tag,deps_json,open_deps,completed,assigned,asana_modified_at,first_seen,last_seen,
                              in_milestone)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?,?)
          ON CONFLICT(gid) DO UPDATE SET key=excluded.key,name=excluded.name,url=excluded.url,
            project_gid=excluded.project_gid,project_name=excluded.project_name,section=excluded.section,
            parent_gid=excluded.parent_gid,parent_name=excluded.parent_name,due_on=excluded.due_on,notes=excluded.notes,
            tags=excluded.tags,has_tag=excluded.has_tag,deps_json=excluded.deps_json,open_deps=excluded.open_deps,
            completed=0,assigned=1,asana_modified_at=excluded.asana_modified_at,last_seen=excluded.last_seen,
            in_milestone=excluded.in_milestone
        """, (t["gid"], key, t.get("name"), t.get("permalink_url"), proj and proj["gid"],
              proj and (names.get(proj["gid"]) or proj["name"].strip()),  # The owner's map names win over Asana's
              section, parent.get("gid"), parent.get("name"), t.get("due_on"), t.get("notes"), json.dumps(tags),
              has_tag, json.dumps(deps), sum(1 for d in deps if not d["completed"]), t.get("modified_at"), ts, ts, in_ms))
        seen.add(t["gid"])
    gone = [r["gid"] for r in con.execute("SELECT gid FROM tickets WHERE completed=0 AND assigned=1 "
                                          "AND gid NOT LIKE 'gh:%' AND gid NOT LIKE 'ms:%' AND gid NOT LIKE 'notion:%'")
            if r["gid"] not in seen]
    me = asana("GET", "/users/me", {"opt_fields": "gid"})["data"]["gid"] if gone else None
    for gid in gone:
        try:
            d = asana("GET", f"/tasks/{gid}", {"opt_fields": "completed,assignee"})["data"]
            if d.get("completed"):
                con.execute("UPDATE tickets SET completed=1 WHERE gid=?", (gid,))
            elif (d.get("assignee") or {}).get("gid") != me:
                con.execute("UPDATE tickets SET assigned=0 WHERE gid=?", (gid,))
        except AsanaError:
            con.execute("UPDATE tickets SET assigned=0 WHERE gid=?", (gid,))
    return len(seen), len(gone)


def sync(log=print) -> dict:
    st = settings()
    con = db()
    mapping = project_map()
    load_map(con, mapping)
    sources = [p for p in mapping["projects"] if p["source"] == "notion"]
    notion_generation = notion_tickets.begin_sync(con, sources)
    con.commit()
    notion_listings = notion_tickets.fetch(con, sources, st["asana_tag"])
    mapped = {r["project_gid"] for r in con.execute("SELECT project_gid FROM project_map WHERE repo IS NOT NULL")}
    names = {r["project_gid"]: r["project_name"] for r in con.execute("SELECT project_gid, project_name FROM project_map")}
    hidden = list(project_map().get("hidden_projects") or {})
    con.executemany("UPDATE tickets SET assigned=0 WHERE completed=0 AND project_gid=?", [(g,) for g in hidden])
    ts, new_tagged = now(), []
    incomplete, dropped, asana_error = 0, 0, None
    if st.get("asana_enabled", True):
        con.execute("SAVEPOINT asana_sync")
        try:
            incomplete, dropped = _sync_asana(con, st, mapped, names, hidden, ts, new_tagged)
            con.execute("RELEASE asana_sync")
        except (AsanaError, OSError, ValueError) as e:
            con.execute("ROLLBACK TO asana_sync")
            con.execute("RELEASE asana_sync")
            new_tagged.clear()
            asana_error = str(e)
            event(con, "error", f"Asana 동기화: {asana_error}")
    sync_issues(con, ts, new_tagged)
    notion_errors = notion_tickets.sync(con, notion_generation, notion_listings, ts, new_tagged)
    for k in new_tagged:
        event(con, "tagged", f"{k} tagged")
    restage(con)
    meta_set(con, "last_sync", ts)
    con.commit()
    res = {"incomplete": incomplete, "dropped": dropped, "new_tagged": new_tagged}
    if asana_error:
        res["asana_error"] = asana_error
    if notion_errors:
        res["notion_errors"] = notion_errors
    log(f"sync: {res}")
    return res


# ---------------------------------------------------------------- dispatch

def build_brief(con, t, repo: dict) -> str:
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
            j = _gh_issue_json(["issue", "view", ref[1], "--repo", ref[0], "--json", "comments"])
            comments = [{"created_by": {"name": (c.get("author") or {}).get("login")}, "created_at": c.get("createdAt"),
                         "text": c.get("body") or ""} for c in j.get("comments") or []]
        elif is_notion:
            comments = []
        else:
            comments = asana_comments(t["gid"])
    except (AsanaError, RuntimeError):
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


def resume_staging_error(con, t):
    """A retry uses the existing worktree's repo even if the project mapping has changed."""
    if (M.notion_milestone(t) or M.github_milestone(t)) and (r := latest_run(con, t["gid"])):
        return M.staging_config_error(t, r["repo"])
    return None


def dispatch(dry_run=False, log=print, only: str | None = None) -> list[str]:
    st = settings()
    con = db()
    active = con.execute("SELECT count(*) c FROM tickets WHERE stage IN ('dispatched','in_progress','blocked')").fetchone()["c"]
    slots = max(0, st["max_active_runs"] - active)
    q = "SELECT * FROM tickets WHERE stage IN ('conflict','rework','queued') AND milestone_error IS NULL" + (" AND (gid=? OR lower(key)=lower(?))" if only else "") + \
        " ORDER BY stage='queued', due_on IS NULL, due_on"
    cands = con.execute(q, (only, only) if only else ()).fetchall()
    log(f"dispatch: active={active} slots={slots} conflict={sum(t['stage'] == 'conflict' for t in cands)} "
        f"rework={sum(t['stage'] == 'rework' for t in cands)} "
        f"queued={sum(t['stage'] == 'queued' for t in cands)}")
    done = []
    existing = None
    answered = con.execute("SELECT * FROM tickets WHERE stage='blocked' AND reply IS NOT NULL AND milestone_error IS NULL" +
                           (" AND (gid=? OR lower(key)=lower(?))" if only else ""), (only, only) if only else ()).fetchall()
    # no agent starts without tp's own GitHub token (TP-112); escalation to the owner and triage still run.
    # The error event is written once per disconnect, not every precheck.
    gh_ok = dry_run or github_token() is not None
    if not gh_ok and (cands or answered):
        log(f"dispatch: {GitHubNotConnected()}")
        if not meta_get(con, "github_missing_logged"):
            event(con, "error", f"dispatch: {GitHubNotConnected()}")
            meta_set(con, "github_missing_logged", True)
    elif gh_ok and meta_get(con, "github_missing_logged"):
        meta_set(con, "github_missing_logged", False)
    def retry_error(t):
        return notion_tickets.retry_error(con, t) or resume_staging_error(con, t)

    # answered BLOCKED tickets already hold a slot, so they go before the slot limit
    for t in answered if gh_ok else ():
        if err := retry_error(t):
            log(f"{t['key']}: {err}")
        elif unblock(con, t, dry_run=dry_run, log=log):
            done.append(t["key"])
    # rework past the round cap goes to the owner instead of the agent; that needs no slot
    def cap_of(t):
        r = latest_run(con, t["gid"])
        return max_rework_rounds(r["repo"] if r else None)
    escalated = {t["gid"] for t in cands if t["stage"] == "rework" and (cap := cap_of(t))
                 and escalate_rework(con, t, cap, dry_run=dry_run, log=log)}
    done += [t["key"] for t in cands if t["gid"] in escalated]
    cands = [t for t in cands if t["gid"] not in escalated]
    for t in cands if gh_ok else ():
        if not slots:
            break
        if t["stage"] != "queued" and (err := retry_error(t)):
            log(f"{t['key']}: {err}")
            continue
        slots -= 1
        if t["stage"] == "conflict":
            if resolve_conflict(con, t, dry_run=dry_run, log=log):
                done.append(t["key"])
            continue
        if t["stage"] == "rework":
            if rework(con, t, dry_run=dry_run, log=log):
                done.append(t["key"])
            continue
        repo = chosen_repo(con, t)
        if not repo:
            slots += 1
            continue
        if err := M.staging_config_error(t, repo["repo"]):
            slots += 1
            log(f"{t['key']}: {err}")
            continue
        if t["gid"].startswith("notion:") and con.in_transaction:
            con.commit()  # release earlier candidates' writes before reading the page over HTTP
        try:
            guard = notion_tickets.worktree_guard(con, t, repo)
            brief = build_brief(con, t, repo)
        except notion.NotionError as e:
            slots += 1  # an unavailable page must not hold a slot ahead of the next ticket
            log(f"{t['key']}: {e}")
            if not dry_run:
                event(con, "error", f"{t['key']}: {e}", t["gid"])
            continue
        name = slug(t["key"])
        if dry_run:
            log(f"--- would create {repo['repo']}/{name}\n{brief}")
            continue
        if existing is None:
            existing = R.worktrees(repo_paths(con))
        clash = next((w for w in existing if w["repo"] == repo["repo"] and w["path"].rstrip("/").endswith("/" + name)), None)
        try:
            if guard:
                guard(lock=bool(clash))
            if clash:
                _insert_run(con, t, repo, clash, None, "adopt")
                event(con, "adopt", f"{t['key']}: existing worktree {name} linked", t["gid"])
                if guard:
                    con.commit()
                done.append(t["key"])
                continue
            (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
            (ROOT / "data" / "briefs" / f"{name}.md").write_text(brief)
            base = R.worktree_base(repo["repo"])
            if (M.base_for(t, repo["repo"]) or "").startswith(M.PREFIX):
                base = M.ensure(con, t, repo["repo"])  # the milestone branch (created on the first subtask)
            if guard:
                con.commit()  # release milestone.ensure's writes before the remote fetch
                wt = R.create(repo["repo"], repo["path"], name, base, before_add=guard)
            else:
                wt = R.create(repo["repo"], repo["path"], name, base)
            setup, prep = prepare_worktree(repo["repo"], repo["path"], wt["path"])
            handle = R.launch(wt["path"], brief, name, setup=setup, repo_path=repo["path"],
                              agent=repo_extra(repo["repo"]).get("agent"))
        except notion.NotionError as e:
            slots += 1
            event(con, "error", f"{t['key']}: {e}", t["gid"])
            con.commit()
            log(f"{t['key']}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            event(con, "error", f"{t['key']}: worktree create failed: {e}", t["gid"])
            con.commit()
            log(f"ERROR {t['key']}: {e}")
            continue
        _insert_run(con, t, repo, wt | {"workspaceStatus": "in-progress", "comment": f"{t['key']} {t['name']}"[:120]},
                    handle, "dispatch")
        event(con, "dispatch", f"{t['key']} -> {repo['repo']}/{name} ({R.describe(repo_extra(repo['repo']).get('agent'))}, "
                               f"{wt['branch']}{'; ' + prep if prep else ''})", t["gid"])
        if guard:
            con.commit()
        log(f"dispatched {t['key']} -> {repo['repo']}/{name} agent={handle}")
        done.append(t["key"])
    restage(con)
    con.commit()
    return done


def prepare_worktree(repo: str, repo_path: str, wt_path: str) -> tuple[str | None, str]:
    """Automatic worktree prep (repo setting auto_setup, default on): copy the main checkout's .env / .env.* files
    now, and return the setup script for the agent wrapper: the dependency install (pnpm > yarn > npm by lockfile),
    then the repo's own setup script. Second value: a short note for the dispatch event."""
    extra = repo_extra(repo)
    steps, note = [], []
    if extra.get("auto_setup", True) is not False:
        envs = R.copy_env(repo_path, wt_path)
        if envs:
            note.append(f".env {len(envs)}개 복사")
        inst = R.install_cmd(wt_path)
        if inst:
            steps.append(inst)
            note.append(inst)
    if extra.get("setup"):
        steps.append(extra["setup"])
        note.append("준비 스크립트")
    return ("\n".join(steps) or None), ", ".join(note)


def _insert_run(con, t, repo, wt: dict, handle, origin, raw=None):
    branch = (wt.get("branch") or "").replace("refs/heads/", "")
    ts = now()
    con.execute("""INSERT INTO runs(ticket_gid,repo,worktree_id,worktree_name,worktree_path,branch,terminal,ws_status,
                   ws_comment,state,origin,created_at,updated_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t["gid"], repo["repo"], wt.get("id"), wt.get("displayName") or Path(wt.get("path", "")).name,
                 wt.get("path"), branch, handle, wt.get("workspaceStatus"), wt.get("comment"), "active", origin, ts, ts,
                 json.dumps(raw or wt)[:20000]))
    MU.register(con, handle, "work", t["gid"], track_ws_set=True)


def adopt(ticket_ref: str, worktree_ref: str, repo_name: str | None = None):
    con = db()
    t = find_ticket(con, ticket_ref)
    ref = os.path.realpath(worktree_ref) if os.path.exists(worktree_ref) else worktree_ref
    wt = next((w for w in R.worktrees(repo_paths(con)) if w["path"] == ref or w["displayName"] == worktree_ref), None)
    if not wt:
        raise SystemExit(f"worktree not found: {worktree_ref}")
    r = con.execute("SELECT * FROM repos WHERE name=?", (repo_name or wt["repo"],)).fetchone()
    if not r:
        raise SystemExit("worktree repo is not in the repo settings (tp config get repo)")
    _insert_run(con, t, dict(r) | {"repo": r["name"]}, wt, None, "adopt")
    event(con, "adopt", f"{t['key']}: adopted worktree {wt.get('displayName')}", t["gid"])
    restage(con)
    con.commit()
    print(f"adopted {t['key']} -> {r['name']}/{wt.get('displayName')}")


# ---------------------------------------------------------------- scan and review entry points
# Supply the shared stage update so tracking and review can persist atomically without importing pipeline.

def scan(log=print) -> dict:
    return pr_tracking.scan(log=log, restage=restage)


def ws_set(status: str, comment: str, path: str = ".") -> None:
    pr_tracking.ws_set(status, comment, path, restage=restage)


def review_save(pr_url: str, sha: str, verdict: str, file: str, summary: str, post: bool = True, shots: list[str] = (),
                *, manual=False):
    return review.review_save(pr_url, sha, verdict, file, summary, post, shots, manual=manual, restage=restage)


# ---------------------------------------------------------------- triage (multi-repo projects)

def triage_list(mark=True) -> list[dict]:
    con = db()
    tried = set(meta_get(con, "triage_attempted", []))
    rows = con.execute("SELECT * FROM tickets WHERE stage='needs_repo'").fetchall()
    out = []
    for t in rows:
        if t["gid"] in tried:
            continue
        out.append({"ticket": t["key"], "title": t["name"], "project": t["project_name"], "parent": t["parent_name"],
                    "notes": (t["notes"] or "")[:2000], "url": t["url"],
                    "candidates": [{"repo": r["repo"], "role": r["role"], "description": r["description"]}
                                   for r in repos_for_project(con, t["project_gid"])]})
        tried.add(t["gid"])
    if mark:
        meta_set(con, "triage_attempted", sorted(tried))
        con.commit()
    return out


def assign(ticket_ref: str, repo: str | None):
    con = db()
    t = find_ticket(con, ticket_ref)
    if repo and not con.execute("SELECT 1 FROM repos WHERE name=?", (repo,)).fetchone():
        raise SystemExit(f"unknown repo {repo}")
    con.execute("UPDATE tickets SET repo_override=? WHERE gid=?", (repo, t["gid"]))
    event(con, "assign", f"{t['key']} -> {repo}", t["gid"])
    restage(con)
    con.commit()


def set_skip(ticket_ref: str, skip: bool):
    con = db()
    t = find_ticket(con, ticket_ref)
    con.execute("UPDATE tickets SET skip=? WHERE gid=?", (1 if skip else 0, t["gid"]))
    event(con, "skip", f"{t['key']}: {'skipped' if skip else 'unskipped'}", t["gid"])
    restage(con)
    con.commit()


def request_reply(ticket_ref: str, text: str):
    con = db()
    t = find_ticket(con, ticket_ref)
    if t["stage"] != "blocked":
        raise SystemExit(f"{t['key']} is not blocked")
    con.execute("UPDATE tickets SET reply=? WHERE gid=?", (text.strip() or None, t["gid"]))
    if text.strip():
        N.read_blocked(con, t["gid"])
    event(con, "reply", f"{t['key']}: BLOCKED {'answered' if text.strip() else 'answer cleared'}", t["gid"])
    con.commit()


# stages with a live, unmerged run that the owner can throw away from the dashboard
DROPPABLE = ("conflict", "rework", "dispatched", "in_progress", "blocked", "agent_done", "pr_open", "reviewed", "approving")


def drop(ticket_ref: str):
    """Give up on a ticket mid-flight: stop its agent, abandon its run (frees the slot, leaves the review queue)
    and skip it. The worktree, branch and any open PR stay; Asana is untouched."""
    con = db()
    t = find_ticket(con, ticket_ref)
    if t["stage"] not in DROPPABLE:
        raise SystemExit(f"{t['key']} cannot be dropped at stage {t['stage']}")
    r = latest_run(con, t["gid"])
    # best effort: the run is abandoned either way, a still-running agent just finishes into the void
    if R.is_handle(r["terminal"]):
        R.stop(r["terminal"])
    elif legacy(r) and r["worktree_path"]:
        run(["orca", "terminal", "close", "--worktree", f"path:{r['worktree_path']}", "--all", "--json"], timeout=60, check=False)
    con.execute("UPDATE runs SET state='abandoned', updated_at=? WHERE id=?", (now(), r["id"]))
    con.execute("UPDATE tickets SET skip=1, reply=NULL, approve_requested_at=NULL WHERE gid=?", (t["gid"],))
    N.read_blocked(con, t["gid"])
    event(con, "abandoned", f"{t['key']}: the owner dropped the {t['stage']} run ({r['worktree_name']})", t["gid"])
    restage(con)
    con.commit()
