"""Shared ticket stages and pipeline entry points."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .core import (
    ROOT, db, dispatch_lock, event,
    meta_get, meta_set, now, project_map, repo_rules, repos_for_project, run, settings,
    feature_base,
)
from . import runner as R
from . import metrics_usage as MU
from . import milestone as M
from . import notifications as N
from . import notion, notion_stages, notion_tickets, pr_tracking, review
from .briefs import slug
from .dispatch import chosen_repo, dispatch, resume_staging_error
from .pr_tracking import VIOLATION, latest_run, legacy, repo_paths, run_pr
from .review import escalate_rework, max_rework_rounds, resolve_conflict, review_overridden, review_queue, rework, unblock
from .runner import repo_extra
from .ticket_sync import ISSUE_LABEL, field, issue_ref, issue_repos, sync

ACTIVE_STAGES = ("dispatched", "in_progress")
STAGE_ORDER = [
    "conflict", "rework", "queued", "needs_repo", "unmapped", "waiting_dep", "milestone_error", "needs_staging",
    "ms_hold", "collecting", "dispatched", "in_progress",
    "blocked", "agent_done", "pr_open", "reviewed", "approving", "milestone", "deploying", "staged", "merged", "done",
    "closed", "skipped", "open",
]


# ---------------------------------------------------------------- helpers

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


def restage(con, *, ticket_gid=None):
    tickets = (con.execute("SELECT * FROM tickets").fetchall() if ticket_gid is None
               else con.execute("SELECT * FROM tickets WHERE gid=?", (ticket_gid,)).fetchall())
    for t in tickets:
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
    with dispatch_lock(con):
        t = find_ticket(con, ticket_ref)
        ref = os.path.realpath(worktree_ref) if os.path.exists(worktree_ref) else worktree_ref
        wt = next((w for w in R.worktrees(repo_paths(con)) if w["path"] == ref or w["displayName"] == worktree_ref), None)
        if not wt:
            raise SystemExit(f"worktree not found: {worktree_ref}")
        r = con.execute("SELECT * FROM repos WHERE name=?", (repo_name or wt["repo"],)).fetchone()
        if not r:
            raise SystemExit("worktree repo is not in the repo settings (tp config get repo)")
        _insert_run(con, t, dict(r) | {"repo": r["name"]}, wt, None, "adopt")
        con.execute("DELETE FROM meta WHERE k=?", (f"dispatch_pending:{wt['path']}",))
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
