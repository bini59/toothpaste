"""Track ticket PRs and run status from worktrees, agents and GitHub."""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime

from . import metrics_usage as MU
from . import milestone as M
from . import notifications as N
from . import runner as R
from .core import db, event, feature_base, gh_json, meta_set, now, orca_json, repo_rules, run, settings


# ws_comment prefix of a run that broke a brief rule (audit_pushes / merged during the agent); shows as 막힘
VIOLATION = "BLOCKED: 규칙 위반"


def latest_run(con, gid):
    return con.execute("SELECT * FROM runs WHERE ticket_gid=? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()


def run_pr(con, run_id):
    return con.execute("SELECT * FROM prs WHERE run_id=? ORDER BY number DESC LIMIT 1", (run_id,)).fetchone()


def repo_paths(con) -> list[tuple[str, str]]:
    return [(r["name"], r["path"]) for r in con.execute("SELECT name, path FROM repos")]


def legacy(r) -> bool:
    """A run whose agent Orca launched before the local runner (runs.terminal is an Orca terminal handle).
    Its status still comes from Orca's workspace status until tp relaunches it (rework, conflict, unblock)."""
    return bool(r["terminal"]) and not R.is_handle(r["terminal"]) and shutil.which("orca") is not None


def mirror_orca(r, status: str, comment: str, unread: bool = False) -> None:
    """Keep a legacy run's Orca workspace status in step with the DB, so scan() does not copy an old value back."""
    if not legacy(r) or not r["worktree_path"]:
        return
    try:
        orca_json(["worktree", "set", "--worktree", f"path:{r['worktree_path']}", "--workspace-status", status,
                   *(["--unread"] if unread else []), "--comment", comment[:240]], timeout=60)
    except Exception:  # noqa: BLE001  (Orca gone or the worktree unknown to it: the DB still has the status)
        pass


def scan(log=print, *, restage) -> dict:
    con = db()
    by_path = {w["path"]: w for w in R.worktrees(repo_paths(con))}
    orca_wts = None
    runs = con.execute("SELECT r.*, rp.github FROM runs r LEFT JOIN repos rp ON rp.name=r.repo WHERE r.state='active'").fetchall()
    found_prs = 0
    for r in runs:
        path = os.path.realpath(r["worktree_path"]) if r["worktree_path"] else None
        w = by_path.get(path)
        if not w:
            con.execute("UPDATE runs SET state='abandoned', updated_at=? WHERE id=?", (now(), r["id"]))
            event(con, "abandoned", f"run {r['id']} worktree gone ({r['worktree_name']})", r["ticket_gid"])
            continue
        branch = w["branch"] or r["branch"]
        con.execute("UPDATE runs SET branch=?, updated_at=? WHERE id=?", (branch, now(), r["id"]))
        if legacy(r):
            if orca_wts is None:
                try:
                    lst = orca_json(["worktree", "list"])
                    orca_wts = {os.path.realpath(x.get("path") or ""): x for x in (lst.get("worktrees", lst) if isinstance(lst, dict) else lst)}
                except Exception:  # noqa: BLE001
                    orca_wts = {}
            ow = orca_wts.get(path)
            if ow:
                con.execute("UPDATE runs SET ws_status=?, ws_comment=? WHERE id=?",
                            (ow.get("workspaceStatus"), ow.get("comment"), r["id"]))
        found_prs += refresh_prs(con, r, branch)
        try:
            audit_pushes(con, r, branch)
        except Exception as e:  # noqa: BLE001  (network/git trouble: try again next scan)
            event(con, "error", f"audit {r['worktree_name']}: {str(e)[:300]}", r["ticket_gid"])
    agent_exits(con)
    restage(con)
    meta_set(con, "last_scan", now())
    con.commit()
    res = {"active_runs": len(runs), "prs": found_prs}
    log(f"scan: {res}")
    return res


def _retarget_pr(con, r, t, pr) -> None:
    repo, old_base = r["repo"], pr.get("baseRefName")
    if pr["state"] != "OPEN" or (t and (t["milestone_error"] or M.staging_config_error(t, repo))):
        return
    target = M.base_for(t, repo)
    into_milestone = (target or "").startswith(M.PREFIX)
    from_milestone = t and M.on_milestone_branch(t, old_base)
    if from_milestone and not target:
        # Unlinked tasks on repos without a feature base can still use staging. When that route is
        # disabled, ordinary PRs use the GitHub default branch (which can differ from prod).
        target = M.staging_of(repo, notion=True) or gh_json(
            ["repo", "view", r["github"], "--json", "defaultBranchRef"])["defaultBranchRef"]["name"]
    if not target or target == old_base:
        return
    if not from_milestone:
        # Correct PRs opened on the default branch, or on staging before a task joined a milestone.
        rr = repo_rules(repo)
        since = (rr and rr.get("retarget_since")) or settings().get("release", {}).get("retarget_since", "")
        from_default = rr and old_base == rr["target"] and pr["createdAt"] >= since
        if not from_default and not (into_milestone and old_base == M.staging_of(repo)):
            return
    if into_milestone:
        M.ensure(con, t, repo)
    if from_milestone:
        M.check_retarget(r["github"], old_base, target, pr["headRefOid"])
    # Use REST because gh pr edit can fail on the Projects (classic) GraphQL deprecation.
    run(["gh", "api", "-X", "PATCH", f"repos/{r['github']}/pulls/{pr['number']}", "-f", f"base={target}"], timeout=120)
    pr["baseRefName"] = target
    pr["mergeable"] = None  # GitHub computed it against the old base.
    event(con, "retarget", f"PR #{pr['number']} base {old_base} -> {target}", r["ticket_gid"])


def refresh_prs(con, r, branch) -> int:
    """Refresh the run's PRs from GitHub, retargeting open PRs as their ticket's base changes."""
    if not r["github"] or not branch:
        return 0
    found_prs = 0
    try:
        prs = gh_json(["pr", "list", "--repo", r["github"], "--head", branch, "--state", "all",
                       "--json", "number,url,state,isDraft,headRefOid,title,createdAt,updatedAt,baseRefName,mergedAt,mergeCommit,mergeable"])
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"gh pr list {r['github']} {branch}: {e}", r["ticket_gid"])
        return 0
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (r["ticket_gid"],)).fetchone()
    for p in prs or []:
        found_prs += 1
        if p["state"] == "MERGED" and p.get("mergedAt"):
            old = con.execute("SELECT state FROM prs WHERE url=?", (p["url"],)).fetchone()
            if (not old or old["state"] != "MERGED") and _during_agent(r["terminal"], p["mergedAt"]):
                flag_violation(con, r, f"PR #{p['number']}이 에이전트 실행 중에 머지됐어요 (tp 승인 머지가 아님)",
                               f"PR #{p['number']} merged")
        try:
            _retarget_pr(con, r, t, p)
        except Exception as e:  # noqa: BLE001
            event(con, "error", f"retarget PR #{p['number']}: {e}", r["ticket_gid"])
        con.execute("""INSERT INTO prs(url,run_id,ticket_gid,repo,github,number,title,state,is_draft,head_sha,created_at,updated_at,
                       base,merged_at,merge_sha,mergeable)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                       state=excluded.state,is_draft=excluded.is_draft,updated_at=excluded.updated_at,
                       base=excluded.base,merged_at=excluded.merged_at,merge_sha=excluded.merge_sha,
                       -- GitHub computes mergeability lazily: UNKNOWN now (the query itself starts the computation),
                       -- a real answer next scan. Keep the last known value for the same head and base.
                       mergeable=CASE WHEN excluded.mergeable IS NULL AND prs.head_sha=excluded.head_sha AND prs.base IS excluded.base
                                      THEN prs.mergeable ELSE excluded.mergeable END,
                       head_sha=excluded.head_sha""",
                    (p["url"], r["id"], r["ticket_gid"], r["repo"], r["github"], p["number"], p["title"], p["state"],
                     1 if p["isDraft"] else 0, p["headRefOid"], p["createdAt"], p["updatedAt"], p.get("baseRefName"),
                     p.get("mergedAt"), (p.get("mergeCommit") or {}).get("oid"),
                     p.get("mergeable") if p["state"] == "OPEN" and p.get("mergeable") in ("MERGEABLE", "CONFLICTING") else None))
        if p["state"] == "OPEN" and p.get("mergeable") == "CONFLICTING":
            was = con.execute("SELECT 1 FROM events WHERE kind='conflict' AND ticket_gid=? AND message LIKE ?",
                              (r["ticket_gid"], f"%{p['headRefOid'][:7]}%")).fetchone()
            if t and not was:  # one event per head commit, not one per scan
                event(con, "conflict", f"{t['key']}: PR #{p['number']} ({p['headRefOid'][:7]}) conflicts with "
                                       f"{p.get('baseRefName')}; resolve it, re-review, then approve again", r["ticket_gid"])
        if p["state"] in ("MERGED", "CLOSED"):
            con.execute("UPDATE runs SET state='finished', updated_at=? WHERE id=?", (now(), r["id"]))
    return found_prs


def agent_exits(con) -> None:
    """An agent whose process ended while its run is still in-progress never ran `tp ws-set`: exit 0 with an open
    PR counts as in-review, anything else as BLOCKED (with the agent's last message) so the owner sees it on the dashboard.
    An agent still running after settings agent_max_min is stopped and counts as BLOCKED too."""
    limit = int(settings().get("agent_max_min") or 0)
    for r in con.execute("SELECT * FROM runs WHERE state='active' AND (ws_status='in-progress' OR ws_status IS NULL) "
                         "AND terminal IS NOT NULL").fetchall():
        if not R.is_handle(r["terminal"]):
            continue  # legacy Orca run: Orca's workspace status (scan) decides
        alive, code = R.agent_state(r["terminal"])
        timed_out = False
        if alive:
            started = R.started_at(r["terminal"])
            if not (limit and started and time.time() - started > limit * 60):
                continue
            R.stop(r["terminal"])
            timed_out, code = True, None
        pr = run_pr(con, r["id"])
        log_path = r["terminal"].split(" ", 1)[-1]
        if code == 0 and pr and pr["state"] == "OPEN" and not timed_out:
            st, msg = "in-review", f"PR #{pr['number']} ready (agent exited)"
        else:
            said = R.last_message(r["terminal"])
            how = (f"agent stopped after {limit} min (agent_max_min) without marking the PR ready; log {log_path}"
                   if timed_out else f"agent exited (code {code}) without marking the PR ready; log {log_path}")
            st, msg = "todo", (f"BLOCKED: {said}" + "\n" + f"({how})" if said else f"BLOCKED: {how}")
        con.execute("UPDATE runs SET ws_status=?, ws_comment=?, updated_at=? WHERE id=?", (st, msg, now(), r["id"]))
        event(con, "agent_timeout" if timed_out else "agent_exit", f"{r['worktree_name']}: {msg[:300]}", r["ticket_gid"])
        MU.workspace_signal(con, r["terminal"], exit_event=True)
        if st == "todo":
            N.blocked(con, r["ticket_gid"], msg, r["repo"])


# ---------------------------------------------------------------- audit (brief rules the agent could still break)

def flag_violation(con, r, what: str, ref: str) -> bool:
    """An agent broke a brief rule (merge, push to a base branch): stop it, block the ticket, log a 'violation'
    event. Once per ref (commit sha / PR), so a later reply from the owner is not undone by the next scan."""
    if con.execute("SELECT 1 FROM events WHERE kind='violation' AND ticket_gid=? AND message LIKE ?",
                   (r["ticket_gid"], f"%({ref})%")).fetchone():
        return False
    if R.running(r["terminal"]):
        R.stop(r["terminal"])
    t = con.execute("SELECT key FROM tickets WHERE gid=?", (r["ticket_gid"],)).fetchone()
    msg = f"{VIOLATION}: {what}. 확인한 뒤 답장(이어서 진행)하거나 그만두기를 눌러 주세요."
    con.execute("UPDATE runs SET ws_status='todo', ws_comment=?, updated_at=? WHERE id=?", (msg, now(), r["id"]))
    N.blocked(con, r["ticket_gid"], msg, r["repo"])
    event(con, "violation", f"{t['key'] if t else r['worktree_name']}: {what} ({ref})", r["ticket_gid"])
    return True


def _during_agent(handle: str | None, merged_at: str) -> bool:
    """Was the PR merged while tp's agent for this run was running? tp's own approval merge and the owner's manual
    merges come after the agent marked the PR ready (and exited)."""
    start, end = R.window(handle)
    if not start:
        return False
    try:
        ts = datetime.fromisoformat(merged_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return start <= ts <= (end or time.time()) + 60


BASE_NAMES = {"main", "master", "staging", "dev", "develop", "production"}


def _base_branches(repo: str) -> set[str]:
    """Branches only tp's merges (or the owner) may change: the usual names plus the repo's deploy branches."""
    rr = repo_rules(repo) or {}
    names = set(BASE_NAMES)
    for b in ((rr.get("staging") or {}).get("branch"), rr.get("via"), (rr.get("prod") or {}).get("branch"),
              feature_base(repo), R.worktree_base(repo)):
        if b:
            names.add(b)
    return names


def audit_pushes(con, r, branch: str | None) -> None:
    """A commit of this ticket ("[KEY] ..." subject, the brief's commit format) on a base branch while its PR is not
    merged = the agent pushed there directly. Checked on every scan of an active run."""
    pr = run_pr(con, r["id"])
    if pr and pr["state"] == "MERGED":
        return
    t = con.execute("SELECT key FROM tickets WHERE gid=?", (r["ticket_gid"],)).fetchone()
    rp = con.execute("SELECT path FROM repos WHERE name=?", (r["repo"],)).fetchone()
    if not (t and t["key"] and rp and rp["path"] and os.path.isdir(rp["path"])):
        return
    heads = run(["git", "-C", rp["path"], "ls-remote", "--heads", "origin"], timeout=60).stdout
    remote = {ln.split("refs/heads/", 1)[1] for ln in heads.splitlines() if "refs/heads/" in ln}
    # milestone branches take subtask PRs through tp's merge only, like a staging branch
    targets = sorted(((_base_branches(r["repo"]) & remote) | {h for h in remote if h.startswith(M.PREFIX)})
                     - {branch or ""})
    if not targets:
        return
    run(["git", "-C", rp["path"], "fetch", "-q", "--no-tags", "origin",
         *[f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in targets]], timeout=120)
    tag = f"[{t['key']}]"
    for b in targets:
        out = run(["git", "-C", rp["path"], "log", "--format=%H%x1f%s", "-F", f"--grep={tag}", "-i",
                   f"--since={r['created_at']}", f"refs/remotes/origin/{b}"], timeout=60).stdout
        for ln in out.splitlines():
            sha, _, subj = ln.partition("\x1f")
            if subj.lower().startswith(tag.lower()):
                flag_violation(con, r, f"{tag} 커밋 {sha[:7]}이 PR 없이 {b} 브랜치에 바로 올라갔어요", sha[:7])
                return


def ws_set(status: str, comment: str, path: str = ".", *, restage) -> None:
    """Completion signal from the agent inside its worktree (replaces Orca's workspace status):
    in-review = PR ready for the AI review, todo + "BLOCKED: ..." = needs the owner."""
    top = os.path.realpath(run(["git", "-C", path, "rev-parse", "--show-toplevel"]).stdout.strip())
    con = db()
    rows = [r for r in con.execute("SELECT r.*, rp.github FROM runs r LEFT JOIN repos rp ON rp.name=r.repo "
                                   "WHERE r.state='active' AND r.worktree_path IS NOT NULL").fetchall()
            if os.path.realpath(r["worktree_path"]) == top]
    if not rows:
        raise SystemExit(f"no active ticket run for {top}")
    for r in rows:
        con.execute("UPDATE runs SET ws_status=?, ws_comment=?, updated_at=? WHERE id=?", (status, comment, now(), r["id"]))
        MU.workspace_signal(con, r["terminal"])
        if status == "todo" and comment.upper().startswith("BLOCKED"):
            event(con, "ws_set_blocked", "ws-set --status todo: BLOCKED", r["ticket_gid"])
            N.blocked(con, r["ticket_gid"], comment, r["repo"])
        else:  # the agent moved on (e.g. in-review after the owner's answer): nothing waits for them
            N.read_blocked(con, r["ticket_gid"])
    con.commit()  # the status is saved even if GitHub is slow or down below
    # read the just-pushed head now: a stale head/mergeable would show the old review until the next scan
    for r in rows:
        try:
            refresh_prs(con, r, r["branch"] or run(["git", "-C", top, "branch", "--show-current"]).stdout.strip())
        except Exception:  # noqa: BLE001  (the next scan reads it)
            pass
    restage(con)
    con.commit()
    print(f"{status}: {comment}")
