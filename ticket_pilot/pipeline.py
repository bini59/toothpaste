"""sync -> dispatch -> scan -> review pipeline."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .core import (
    ROOT, AsanaError, TASK_FIELDS, asana, asana_all, asana_comments, asana_workspace, db, event, load_map,
    meta_get, meta_set, now, orca_json, project_map, repo_rules, repos_for_project, run, settings, gh_json,
    feature_base, GitHubNotConnected, github_token,
)
from . import runner as R
from . import milestone as M
from . import notifications as N
from . import notion, notion_stages, notion_tickets

# briefs call ticket-pilot's CLI by absolute path from inside the target repo's worktree
TP_BIN = str(ROOT / "bin" / "tp")
# ws_comment prefix of a run that broke a brief rule (audit_pushes / merged during the agent); shows as 막힘
VIOLATION = "BLOCKED: 규칙 위반"
ACTIVE_STAGES = ("dispatched", "in_progress")
STAGE_ORDER = [
    "conflict", "rework", "queued", "needs_repo", "unmapped", "waiting_dep", "ms_hold", "collecting", "dispatched",
    "in_progress",
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


def latest_run(con, gid):
    return con.execute("SELECT * FROM runs WHERE ticket_gid=? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()


def run_pr(con, run_id):
    return con.execute("SELECT * FROM prs WHERE run_id=? ORDER BY number DESC LIMIT 1", (run_id,)).fetchone()


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


def review_overridden(t, pr) -> bool:
    """The owner approved this head despite an AI 'blocked'/'changes' verdict, with a reason (TP-99; that commit only)."""
    return bool("review_override" in t.keys() and t["review_override"] and t["review_override_sha"] == pr["head_sha"])


def compute_stage(con, t, *, repo_count=None) -> str:
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
        if pr:
            if pr["release_id"] or pr["prod_at"]:
                return "done"  # verified in production (deploy.py / release train / track.py)
            if active_deploy(con, t["gid"]):
                return "deploying"
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
    if t["open_deps"] and unmet_deps(con, t):
        return "waiting_dep"
    if t["repo_override"] and (not t["gid"].startswith("notion:") or chosen_repo(con, t)):
        return "queued"
    if repo_count is None:  # preview can supply the project's unsaved repo selection
        repo_count = len(repos_for_project(con, t["project_gid"]))
    if not repo_count:
        return "unmapped"
    if repo_count > 1:
        return "needs_repo"
    return "queued"


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
            issues = _gh_issue_json(["issue", "list", "--repo", gh, "--state", "open", "--limit", "500",
                                     "--json", "number,title,body,url,updatedAt,labels"])
        except Exception as e:  # noqa: BLE001  (keep the Asana sync going; do not close tickets on a failed read)
            event(con, "error", f"github issues {gh}: {str(e)[:300]}")
            continue
        seen, still_open = set(), set()
        for i in issues:
            gid, key = f"gh:{gh}#{i['number']}", f"{prefix}-{i['number']}"
            still_open.add(gid)
            labels = [lb["name"] for lb in i.get("labels") or []]
            if label and label not in [x.lower() for x in labels]:
                continue
            seen.add(gid)
            if not con.execute("SELECT 1 FROM tickets WHERE gid=? AND completed=0 AND assigned=1", (gid,)).fetchone():
                new_tagged.append(key)
            con.execute("""
              INSERT INTO tickets(gid,key,name,url,project_name,notes,tags,has_tag,deps_json,open_deps,completed,assigned,
                                  asana_modified_at,first_seen,last_seen,repo_override)
              VALUES(?,?,?,?,?,?,?,1,'[]',0,0,1,?,?,?,?)
              ON CONFLICT(gid) DO UPDATE SET name=excluded.name,notes=excluded.notes,tags=excluded.tags,completed=0,
                assigned=1,asana_modified_at=excluded.asana_modified_at,last_seen=excluded.last_seen
            """, (gid, key, i["title"], i["url"], f"GitHub {gh}", i.get("body") or "",
                  json.dumps(labels), i.get("updatedAt"), ts, ts, repo))
        for row in con.execute("SELECT gid FROM tickets WHERE completed=0 AND gid LIKE ?", (f"gh:{gh}#%",)).fetchall():
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
    notion_stages.flush(con)
    res = {"incomplete": incomplete, "dropped": dropped, "new_tagged": new_tagged}
    if asana_error:
        res["asana_error"] = asana_error
    if notion_errors:
        res["notion_errors"] = notion_errors
    log(f"sync: {res}")
    return res


# ---------------------------------------------------------------- dispatch

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _agent_brief(template, ticket):
    gid = ticket["project_gid"] if "project_gid" in ticket.keys() else None
    pid = gid.removeprefix("notion:") if gid and gid.startswith("notion:") else None
    return (template.replace("{{NOTION}}", notion_tickets.agent_rules(pid)).replace("{{ASANA}}", R.asana_rules())
            .replace("{{TP}}", TP_BIN).replace("{{OWNER}}", settings().get("owner_name", "the user")))


def build_brief(con, t, repo: dict) -> str:
    tpl = (ROOT / "prompts" / "work_brief.md").read_text()
    ref = issue_ref(t["gid"])
    is_notion = t["gid"].startswith("notion:")
    if is_notion:
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
            vals["BASE_NOTE"] = (f"This ticket is a subtask of the milestone {t['parent_name']}: the PR base must be "
                                 f"`{fb}` (never `{M.staging_of(repo['repo'])}` or `{prod}`); branch from "
                                 f"`origin/{fb}` if you have not committed yet. ticket-pilot merges it into `{fb}` "
                                 f"after the review, and the milestone goes to staging as a whole.")
    else:
        vals["BASE_FLAG"], vals["BASE_NOTE"] = "", ""
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return _agent_brief(tpl, t)


def chosen_repo(con, t):
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


def dispatch(dry_run=False, log=print, only: str | None = None) -> list[str]:
    st = settings()
    con = db()
    active = con.execute("SELECT count(*) c FROM tickets WHERE stage IN ('dispatched','in_progress','blocked')").fetchone()["c"]
    slots = max(0, st["max_active_runs"] - active)
    q = "SELECT * FROM tickets WHERE stage IN ('conflict','rework','queued')" + (" AND (gid=? OR lower(key)=lower(?))" if only else "") + \
        " ORDER BY stage='queued', due_on IS NULL, due_on"
    cands = con.execute(q, (only, only) if only else ()).fetchall()
    log(f"dispatch: active={active} slots={slots} conflict={sum(t['stage'] == 'conflict' for t in cands)} "
        f"rework={sum(t['stage'] == 'rework' for t in cands)} "
        f"queued={sum(t['stage'] == 'queued' for t in cands)}")
    done = []
    existing = None
    answered = con.execute("SELECT * FROM tickets WHERE stage='blocked' AND reply IS NOT NULL" +
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
    # answered BLOCKED tickets already hold a slot, so they go before the slot limit
    for t in answered if gh_ok else ():
        if unblock(con, t, dry_run=dry_run, log=log):
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
        if t["gid"].startswith("notion:") and con.in_transaction:
            con.commit()  # release earlier candidates' writes before reading the page over HTTP
        try:
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
        if clash:
            _insert_run(con, t, repo, clash, None, "adopt")
            event(con, "adopt", f"{t['key']}: existing worktree {name} linked", t["gid"])
            done.append(t["key"])
            continue
        (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "briefs" / f"{name}.md").write_text(brief)
        try:
            base = worktree_base(repo["repo"])
            if (M.base_for(t, repo["repo"]) or "").startswith(M.PREFIX):
                base = M.ensure(con, t, repo["repo"])  # the milestone branch (created on the first subtask)
            wt = R.create(repo["repo"], repo["path"], name, base)
            setup, prep = prepare_worktree(repo["repo"], repo["path"], wt["path"])
            handle = R.launch(wt["path"], brief, name, setup=setup, repo_path=repo["path"],
                              agent=repo_extra(repo["repo"]).get("agent"))
        except Exception as e:  # noqa: BLE001
            event(con, "error", f"{t['key']}: worktree create failed: {e}", t["gid"])
            con.commit()
            log(f"ERROR {t['key']}: {e}")
            continue
        _insert_run(con, t, repo, wt | {"workspaceStatus": "in-progress", "comment": f"{t['key']} {t['name']}"[:120]},
                    handle, "dispatch")
        event(con, "dispatch", f"{t['key']} -> {repo['repo']}/{name} ({R.describe(repo_extra(repo['repo']).get('agent'))}, "
                               f"{wt['branch']}{'; ' + prep if prep else ''})", t["gid"])
        log(f"dispatched {t['key']} -> {repo['repo']}/{name} agent={handle}")
        done.append(t["key"])
    restage(con)
    con.commit()
    if done and not dry_run:
        notion_stages.flush(con)
    return done


def max_rework_rounds(repo: str | None = None) -> int:
    """Fix rounds the AI reviewer may send one PR back to the agent before the owner decides (0 = no cap).
    A repo's own review.max_rework_rounds (설정 page) overrides the global value."""
    own = ((project_map().get("repos") or {}).get(repo or "") or {}).get("review") or {}
    if own.get("max_rework_rounds") is not None:
        return int(own["max_rework_rounds"])
    return int((settings().get("review") or {}).get("max_rework_rounds", 3))


def rework_rounds(con, t, pr) -> int:
    """Fix rounds already sent back to the agent on this PR since the owner last answered a BLOCKED question."""
    since = con.execute("SELECT max(ts) m FROM events WHERE ticket_gid=? AND kind='unblock'", (t["gid"],)).fetchone()["m"]
    return con.execute("SELECT count(*) c FROM reviews WHERE pr_url=? AND rework_at IS NOT NULL AND rework_at > ?",
                       (pr["url"], since or "")).fetchone()["c"]


def escalate_rework(con, t, cap, dry_run=False, log=print) -> bool:
    """The review still wants changes after `cap` fix rounds: stop the agent/reviewer loop and ask the owner.

    An agent author and an agent reviewer can cycle forever, mostly re-finding old code. The workspace gets a
    BLOCKED comment, so the ticket shows as blocked; the owner's dashboard reply relaunches the agent (unblock())
    and restarts the count."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not (r and pr and r["worktree_path"]) or rework_rounds(con, t, pr) < cap:
        return False
    rv = con.execute("SELECT * FROM reviews WHERE pr_url=? AND head_sha=? ORDER BY id DESC LIMIT 1",
                     (pr["url"], pr["head_sha"])).fetchone()
    if not rv:
        return False
    what = (f"{cap}번 고친 뒤에도 tp 검사 실패" if rv["source"] == "check" else f"AI 리뷰가 {cap}번 고친 뒤에도 수정 요청")
    q = (f"BLOCKED: {what} (PR #{pr['number']}). "
         f"계속 / 방향 변경 / 그만두기? 최근 {'검사' if rv['source'] == 'check' else '리뷰'}: {rv['summary'] or ''}")[:240]
    if dry_run:
        log(f"--- would ask the owner instead of rework round {cap + 1}: {q}")
        return False
    mirror_orca(r, "todo", q, unread=True)
    ts = now()
    # rework_at marks this review handled, so the reply relaunches only one agent (unblock), not a rework too
    con.execute("UPDATE reviews SET rework_at=? WHERE id=?", (ts, rv["id"]))
    con.execute("UPDATE runs SET ws_status='todo', ws_comment=?, updated_at=? WHERE id=?", (q, ts, r["id"]))
    event(con, "rework_cap", f"{t['key']}: PR #{pr['number']} still needs changes after {cap} rounds -> asked the owner",
          t["gid"])
    N.blocked(con, t["gid"], q, pr["repo"])
    con.commit()
    log(f"rework cap {t['key']}: asked the owner")
    return True


def build_rework_brief(t, run, pr, rv, round_no=1) -> str:
    tpl = (ROOT / "prompts" / "rework_brief.md").read_text()
    brief = ROOT / "data" / "briefs" / f"{slug(t['key'])}.md"
    sha7 = (pr["head_sha"] or "")[:7]
    vals = {
        "OPENING": (f"ticket-pilot's own repo checks (`tp check`, run by the host on head `{sha7}`) failed on your draft "
                    f"PR {pr['url']} (#{pr['number']})." if rv["source"] == "check" else
                    f"The ticket-pilot AI review of your draft PR {pr['url']} (#{pr['number']}, head `{sha7}`) "
                    "requested changes."),
        "KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "PR_URL": pr["url"], "PR_NUMBER": pr["number"],
        "HEAD_SHA": (pr["head_sha"] or "")[:7], "BRANCH": run["branch"] or "", "SUMMARY": rv["summary"] or "",
        "REVIEW": (rv["body"] or "").strip(), "BRIEF_FILE": str(brief) if brief.exists() else "(not available)",
        "ROUND": round_no, "MAX_ROUNDS": max_rework_rounds(pr["repo"]) or "unlimited",
    }
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return _agent_brief(tpl, t)


def rework(con, t, dry_run=False, log=print) -> bool:
    """'changes' verdict -> relaunch the agent in the same worktree/branch with the review as its brief."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    rv = pr and con.execute("SELECT * FROM reviews WHERE pr_url=? AND head_sha=? ORDER BY id DESC LIMIT 1",
                            (pr["url"], pr["head_sha"])).fetchone()
    if not (r and pr and rv and r["worktree_path"]):
        log(f"rework {t['key']}: missing run/PR/review, skipped")
        return False
    brief = build_rework_brief(t, r, pr, rv, rework_rounds(con, t, pr) + 1)
    if dry_run:
        log(f"--- would relaunch {st_agent()} in {r['worktree_path']}\n{brief}")
        return False
    (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "briefs" / f"{slug(t['key'])}-rework-{(pr['head_sha'] or '')[:7]}.md").write_text(brief)
    why = "tp checks failed" if rv["source"] == "check" else "AI review requested changes"
    comment = f"rework: {why} on PR #{pr['number']}"[:120]
    try:
        handle = relaunch(con, r, brief, f"{slug(t['key'])}-rework", comment)
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"{t['key']}: rework launch failed: {e}", t["gid"])
        con.commit()
        log(f"ERROR rework {t['key']}: {e}")
        return False
    ts = now()
    con.execute("UPDATE reviews SET rework_at=? WHERE id=?", (ts, rv["id"]))
    # back to in-progress so the review automation waits for the agent's in-review signal
    con.execute("UPDATE runs SET ws_status='in-progress', ws_comment=?, terminal=?, updated_at=? WHERE id=?",
                (comment, handle, ts, r["id"]))
    event(con, "rework", f"{t['key']}: PR #{pr['number']} {rv['summary'] if rv['source'] == 'check' else 'changes requested'}"
                         f" -> agent relaunched in {r['worktree_name']}", t["gid"])
    con.commit()
    log(f"rework {t['key']} -> {r['repo']}/{r['worktree_name']} terminal={handle}")
    return True


def build_conflict_brief(t, run, pr) -> str:
    tpl = (ROOT / "prompts" / "conflict_brief.md").read_text()
    vals = {
        "KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "PR_URL": pr["url"], "PR_NUMBER": pr["number"],
        "HEAD_SHA": (pr["head_sha"] or "")[:7], "BRANCH": run["branch"] or "", "BASE": pr["base"] or "",
    }
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return _agent_brief(tpl, t)


def resolve_conflict(con, t, dry_run=False, log=print) -> bool:
    """충돌 대기 -> relaunch the agent in the same worktree to merge the PR's base branch in (prompts/conflict_brief.md).
    Stage 'conflict' is set mechanically by compute_stage for any open PR GitHub reports CONFLICTING (any repo/base);
    tp dispatch picks it up like rework (ahead of new tickets, within max_active_runs). Once per head commit."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not (r and pr and r["worktree_path"] and pr["base"]):
        log(f"conflict {t['key']}: missing run/PR/base, skipped")
        return False
    brief = build_conflict_brief(t, r, pr)
    if dry_run:
        log(f"--- would relaunch {st_agent()} in {r['worktree_path']}\n{brief}")
        return False
    (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "briefs" / f"{slug(t['key'])}-conflict-{(pr['head_sha'] or '')[:7]}.md").write_text(brief)
    comment = f"conflict: PR #{pr['number']} conflicts with {pr['base']}, merging it in"[:120]
    try:
        handle = relaunch(con, r, brief, f"{slug(t['key'])}-conflict", comment)
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"{t['key']}: conflict relaunch failed: {e}", t["gid"])
        con.commit()
        log(f"ERROR conflict {t['key']}: {e}")
        return False
    ts = now()
    con.execute("UPDATE prs SET conflict_sha=?, conflict_at=? WHERE url=?", (pr["head_sha"], ts, pr["url"]))
    con.execute("UPDATE runs SET ws_status='in-progress', ws_comment=?, terminal=?, updated_at=? WHERE id=?",
                (comment, handle, ts, r["id"]))
    # a pending dashboard approval cannot merge a conflicting head; the new head needs a fresh review + approval
    con.execute("UPDATE tickets SET approve_requested_at=NULL, approve_error=NULL WHERE gid=?", (t["gid"],))
    event(con, "conflict_rework", f"{t['key']}: PR #{pr['number']} ({(pr['head_sha'] or '')[:7]}) conflicts with "
                                  f"{pr['base']} -> agent relaunched in {r['worktree_name']}", t["gid"])
    con.commit()
    log(f"conflict {t['key']} -> {r['repo']}/{r['worktree_name']} terminal={handle}")
    return True


def unblock(con, t, dry_run=False, log=print) -> bool:
    """BLOCKED + the owner's dashboard reply -> relaunch the agent in the same worktree with their answer."""
    r = latest_run(con, t["gid"])
    if not (r and r["worktree_path"]):
        log(f"unblock {t['key']}: missing run, skipped")
        return False
    brief = (ROOT / "prompts" / "unblock_brief.md").read_text()
    orig = ROOT / "data" / "briefs" / f"{slug(t['key'])}.md"
    for k, v in {"KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "BRANCH": r["branch"] or "",
                 "BRIEF_FILE": str(orig) if orig.exists() else "(not available)",
                 "QUESTION": r["ws_comment"] or "", "REPLY": t["reply"]}.items():
        brief = brief.replace("{{" + k + "}}", str(v))
    brief = _agent_brief(brief, t)
    if dry_run:
        log(f"--- would relaunch {st_agent()} in {r['worktree_path']}\n{brief}")
        return False
    comment = f"answered: {t['reply']}"[:120]
    try:
        handle = relaunch(con, r, brief, f"{slug(t['key'])}-unblock", comment)
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"{t['key']}: unblock launch failed: {e}", t["gid"])
        con.commit()
        log(f"ERROR unblock {t['key']}: {e}")
        return False
    ts = now()
    con.execute("UPDATE runs SET ws_status='in-progress', ws_comment=?, terminal=?, updated_at=? WHERE id=?",
                (comment, handle, ts, r["id"]))
    con.execute("UPDATE tickets SET reply=NULL WHERE gid=?", (t["gid"],))
    event(con, "unblock", f"{t['key']}: the owner answered -> agent relaunched in {r['worktree_name']}", t["gid"])
    con.commit()
    log(f"unblock {t['key']} -> {r['repo']}/{r['worktree_name']} terminal={handle}")
    return True


def st_agent() -> str:
    return R.resolve(None)["tool"]


def repo_paths(con) -> list[tuple[str, str]]:
    return [(r["name"], r["path"]) for r in con.execute("SELECT name, path FROM repos")]


def repo_extra(repo: str | None) -> dict:
    """A repo's settings entry (repo.<name>): setup, worktree_base, checks, ..."""
    return ((project_map().get("repos") or {}).get(repo or "") or {})


def worktree_base(repo: str) -> str | None:
    """Branch new ticket worktrees start from: the feature base (PRs target it), else the repo's worktree_base
    setting (what Orca's per-repo base ref was, e.g. staging or dev), else origin's default branch."""
    return feature_base(repo) or (repo_extra(repo).get("worktree_base") or "").removeprefix("origin/") or None


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


def relaunch(con, r, brief: str, tag: str, comment: str) -> str:
    """Start the agent again in a run's worktree (rework / conflict / unblock); returns the new handle.
    An agent still running there is stopped first: two agents on one branch fight over it."""
    if R.running(r["terminal"]):
        R.stop(r["terminal"])
    if (r["branch"] or "").startswith(M.PREFIX) and r["worktree_path"]:
        # milestone PR worktree: subtask merges may have moved the branch on GitHub since the checkout
        run(["git", "-C", r["worktree_path"], "pull", "-q", "--ff-only"], timeout=180, check=False)
    mirror_orca(r, "in-progress", comment)
    rp = con.execute("SELECT path FROM repos WHERE name=?", (r["repo"],)).fetchone()
    return R.launch(r["worktree_path"], brief, tag, repo_path=rp["path"] if rp else None,
                    agent=repo_extra(r["repo"]).get("agent"))


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


def _insert_run(con, t, repo, wt: dict, handle, origin, raw=None):
    branch = (wt.get("branch") or "").replace("refs/heads/", "")
    ts = now()
    con.execute("""INSERT INTO runs(ticket_gid,repo,worktree_id,worktree_name,worktree_path,branch,terminal,ws_status,
                   ws_comment,state,origin,created_at,updated_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t["gid"], repo["repo"], wt.get("id"), wt.get("displayName") or Path(wt.get("path", "")).name,
                 wt.get("path"), branch, handle, wt.get("workspaceStatus"), wt.get("comment"), "active", origin, ts, ts,
                 json.dumps(raw or wt)[:20000]))


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


# ---------------------------------------------------------------- scan (worktrees + agents + GitHub)

def scan(log=print) -> dict:
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
    notion_stages.flush(con)
    res = {"active_runs": len(runs), "prs": found_prs}
    log(f"scan: {res}")
    return res


def refresh_prs(con, r, branch) -> int:
    """Re-read the run's PRs from GitHub (head, mergeable, state) into prs, retargeting feature PRs opened against
    the default branch. Returns how many it found."""
    if not r["github"] or not branch:
        return 0
    found_prs = 0
    try:
        prs = gh_json(["pr", "list", "--repo", r["github"], "--head", branch, "--state", "all",
                       "--json", "number,url,state,isDraft,headRefOid,title,createdAt,updatedAt,baseRefName,mergedAt,mergeCommit,mergeable"])
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"gh pr list {r['github']} {branch}: {e}", r["ticket_gid"])
        return 0
    rel = settings().get("release", {})
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (r["ticket_gid"],)).fetchone()
    fb = M.base_for(t, r["repo"])  # a milestone subtask's PR goes into the milestone branch instead
    ms = (fb or "").startswith(M.PREFIX)
    rr = repo_rules(r["repo"]) if fb else None
    # per-repo retarget_since (a pr_into_staging repo: PRs opened before dev existed stay on main), else the global one
    since = (rr.get("retarget_since") if rr else None) or rel.get("retarget_since", "")
    for p in prs or []:
        found_prs += 1
        if p["state"] == "MERGED" and p.get("mergedAt"):
            old = con.execute("SELECT state FROM prs WHERE url=?", (p["url"],)).fetchone()
            if (not old or old["state"] != "MERGED") and _during_agent(r["terminal"], p["mergedAt"]):
                flag_violation(con, r, f"PR #{p['number']}이 에이전트 실행 중에 머지됐어요 (tp 승인 머지가 아님)",
                               f"PR #{p['number']} merged")
        # safety net: agents sometimes open the PR against the GitHub default branch (main).
        # These repos take feature PRs into their staging branch only, so retarget (the worktree branches from it).
        # Milestone subtasks: also PRs on staging (opened before the parent got the milestone tag, or by mistake).
        was = p.get("baseRefName")
        if (fb and p["state"] == "OPEN" and was != fb
                and ((was == rr["target"] and p["createdAt"] >= since)  # "" = all open PRs
                     or (ms and was == M.staging_of(r["repo"])))):
            try:
                if ms:
                    M.ensure(con, t, r["repo"])
                # REST, not `gh pr edit`: the latter fails on the Projects (classic) GraphQL deprecation
                run(["gh", "api", "-X", "PATCH", f"repos/{r['github']}/pulls/{p['number']}", "-f",
                     f"base={fb}"], timeout=120)
                p["baseRefName"] = fb
                event(con, "retarget", f"PR #{p['number']} base {was} -> {fb}", r["ticket_gid"])
            except Exception as e:  # noqa: BLE001
                event(con, "error", f"retarget PR #{p['number']}: {e}", r["ticket_gid"])
        con.execute("""INSERT INTO prs(url,run_id,ticket_gid,repo,github,number,title,state,is_draft,head_sha,created_at,updated_at,
                       base,merged_at,merge_sha,mergeable)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                       state=excluded.state,is_draft=excluded.is_draft,updated_at=excluded.updated_at,
                       base=excluded.base,merged_at=excluded.merged_at,merge_sha=excluded.merge_sha,
                       -- GitHub computes mergeability lazily: UNKNOWN now (the query itself starts the computation),
                       -- a real answer next scan. Keep the last known value while the head is unchanged.
                       mergeable=CASE WHEN excluded.mergeable IS NULL AND prs.head_sha=excluded.head_sha AND prs.base IS excluded.base
                                      THEN prs.mergeable ELSE excluded.mergeable END,
                       head_sha=excluded.head_sha""",
                    (p["url"], r["id"], r["ticket_gid"], r["repo"], r["github"], p["number"], p["title"], p["state"],
                     1 if p["isDraft"] else 0, p["headRefOid"], p["createdAt"], p["updatedAt"], p.get("baseRefName"),
                     p.get("mergedAt"), (p.get("mergeCommit") or {}).get("oid"),
                     p.get("mergeable") if p["state"] == "OPEN" and p.get("mergeable") in ("MERGEABLE", "CONFLICTING") else None))
        if p["state"] == "OPEN" and p.get("mergeable") == "CONFLICTING":
            t = con.execute("SELECT key, stage FROM tickets WHERE gid=?", (r["ticket_gid"],)).fetchone()
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
              feature_base(repo), worktree_base(repo)):
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


def ws_set(status: str, comment: str, path: str = ".") -> None:
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
        if status == "todo" and comment.upper().startswith("BLOCKED"):
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


# ---------------------------------------------------------------- review

def review_queue(con=None) -> list[dict]:
    """PRs waiting for an AI review of their head. Repos with tp checks (checks.py) only enter once every check of
    the head passed (or tp could not run it: 'error'); a failed head goes back to the agent instead, unless the owner
    overrode its checks with a reason (check_override)."""
    from . import checks as K
    con = con or db()
    rows = con.execute("""
      SELECT p.*, t.key, t.name tname, t.url turl, t.check_override, t.check_override_sha,
             r.worktree_path, r.ws_status, r.branch, rp.path repo_path
      FROM prs p JOIN runs r ON r.id=p.run_id JOIN tickets t ON t.gid=p.ticket_gid LEFT JOIN repos rp ON rp.name=p.repo
      WHERE p.state='OPEN' AND r.ws_status IN ('in-review','completed') AND r.state != 'abandoned'
        -- a milestone PR whose subtasks are still coming in is reviewed once they are all in
        AND COALESCE(t.stage, '') NOT IN ('collecting', 'ms_hold')
        AND NOT EXISTS (SELECT 1 FROM reviews v WHERE v.pr_url=p.url AND v.head_sha=p.head_sha
                        -- a reworked review whose agent marked in-review again without pushing needs a new pass
                        AND NOT (v.rework_at IS NOT NULL
                                 AND v.id=(SELECT max(id) FROM reviews WHERE pr_url=p.url)))
    """).fetchall()
    out = []
    for p in rows:
        cs = K.check_state(con, p)
        over = K.overridden(con, p)
        if cs["state"] in ("pending", "fail") and not over:
            continue
        # rounds and the previous review count AI reviews only; a tp check failure is not a review of the code
        prev = con.execute("SELECT verdict, summary, head_sha, body FROM reviews WHERE pr_url=? AND source IS NULL "
                           "ORDER BY id DESC LIMIT 1", (p["url"],)).fetchone()
        rounds = con.execute("SELECT count(*) c FROM reviews WHERE pr_url=? AND source IS NULL", (p["url"],)).fetchone()["c"]
        brief = ROOT / "data" / "briefs" / f"{slug(p['key'])}.md"
        out.append({
            "pr_url": p["url"], "github": p["github"], "number": p["number"], "title": p["title"],
            "head_sha": p["head_sha"], "ticket": p["key"], "ticket_title": p["tname"], "asana_url": p["turl"],
            "worktree_path": p["worktree_path"], "repo_path": p["repo_path"],
            "brief_file": str(brief) if brief.exists() else None,
            "round": rounds + 1,  # 1 = first review; later rounds review the diff since previous_review.head_sha
            "previous_review": dict(prev) if prev else None,
            # tp's own results for this head ([] = the repo has no checks: run its tests yourself as before)
            "checks": [{k: c[k] for k in ("name", "status", "cmd", "log_path")} for c in cs["rows"]],
            # The owner's reason for ignoring the checks of this head (they did not pass); null otherwise
            "check_override": p["check_override"] if over else None,
        })
    return out


VERDICT_LABEL = {"approve": "Looks good", "changes": "Changes requested", "blocked": "Blocked"}
VERDICT_KO = {"approve": "승인", "changes": "수정 요청", "blocked": "막힘"}
SHOT_BRANCH = "ticket-pilot-screenshots"


def gh_api(method: str, path: str, body: dict | None = None) -> dict:
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:  # via file: base64 images can exceed argv limits
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(body, f)
        cmd += ["--input", f.name]
    return json.loads(run(cmd, timeout=120).stdout or "{}")


def upload_shots(github: str, prefix: str, files: list[str]) -> list[tuple[str, str]]:
    """Commit images to an orphan branch (PR branch untouched) and return (name, url) for Markdown embeds."""
    if run(["gh", "api", f"repos/{github}/branches/{SHOT_BRANCH}"], check=False).returncode != 0:
        tree = gh_api("POST", f"repos/{github}/git/trees", {"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
                      "content": "Screenshots attached to ticket-pilot AI reviews.\n"}]})
        commit = gh_api("POST", f"repos/{github}/git/commits",
                        {"message": "ticket-pilot screenshots [skip ci]", "tree": tree["sha"], "parents": []})
        gh_api("POST", f"repos/{github}/git/refs", {"ref": f"refs/heads/{SHOT_BRANCH}", "sha": commit["sha"]})
    out = []
    for f in files:
        path = f"{prefix}/{Path(f).name}"
        gh_api("PUT", f"repos/{github}/contents/{path}", {"message": f"screenshot {path} [skip ci]", "branch": SHOT_BRANCH,
               "content": base64.b64encode(Path(f).read_bytes()).decode()})
        out.append((Path(f).stem, f"https://github.com/{github}/blob/{SHOT_BRANCH}/{path}?raw=true"))
    return out


def screenshot(url: str, out: str, wait_ms: int = 3000, device: str | None = None, js: str | None = None,
               repo: str = "."):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    mods = Path(repo).resolve() / "node_modules"
    if (mods / "playwright").is_dir():
        p = subprocess.run(["node", str(Path(__file__).with_name("shot.cjs")), url, out, device or "Desktop Chrome HiDPI",
                            str(wait_ms), js or ""],
                           env={**os.environ, "NODE_PATH": str(mods)}, capture_output=True, text=True, timeout=180)
        if p.returncode:
            raise SystemExit(f"screenshot failed: {p.stderr.strip()[-800:]}")
        print(json.dumps({"saved": out}))
        return
    if device:
        raise SystemExit(f"--device needs Playwright installed in {mods} (pass --repo <worktree>)")
    if not shutil.which("orca"):
        raise SystemExit(f"no Playwright in {mods} (pass --repo <worktree with Playwright installed>)")
    # shortcut: Orca fallback is desktop-only and subject to CORS; install Playwright in the repo for more
    page = orca_json(["tab", "create", "--url", url])["browserPageId"]
    try:
        orca_json(["wait", "--page", page, "--timeout", str(wait_ms)])
        if js:  # e.g. scroll to the area under review
            orca_json(["eval", "--expression", js, "--page", page])
            orca_json(["wait", "--page", page, "--timeout", "1000"])
        data = orca_json(["screenshot", "--format", "png", "--page", page])["data"]
        Path(out).write_bytes(base64.b64decode(data))
    finally:
        orca_json(["tab", "close", "--page", page])
    print(json.dumps({"saved": out}))


def review_save(pr_url: str, sha: str, verdict: str, file: str, summary: str, post: bool = True, shots: list[str] = ()):
    if verdict not in VERDICT_LABEL:
        raise SystemExit("verdict must be approve|changes|blocked")
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):  # a bad sha (e.g. "undefined") hides the approve button
        raise SystemExit(f"--sha must be the full 40-char head sha from review-queue, got {sha!r}")
    con = db()
    p = con.execute("SELECT p.*, t.key FROM prs p JOIN tickets t ON t.gid=p.ticket_gid WHERE p.url=?", (pr_url,)).fetchone()
    if not p:
        raise SystemExit(f"unknown PR {pr_url}; run `tp scan` first")
    body = Path(file).read_text().strip()
    for f in shots:
        if not Path(f).is_file():
            raise SystemExit(f"screenshot not found: {f}")
    comment_url = None
    if post:
        if shots:  # upload before posting so a failure leaves the PR unreviewed and the next run retries
            links = upload_shots(p["github"], f"pr-{p['number']}/{sha[:7]}-{int(time.time())}", list(shots))
            body += "\n\n## 스크린샷\n\n" + "\n\n".join(f"**{n}**\n\n![{n}]({u})" for n, u in links)
        text = (f"## AI review (ticket-pilot): {VERDICT_LABEL[verdict]}\n\n**Summary:** {summary}\n\n{body}\n\n---\n"
                f"<sub>Automated review for {p['key']} at `{sha[:7]}`. Not a merge approval; a human decides.</sub>\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
        out = run(["gh", "pr", "comment", pr_url, "--body-file", f.name], timeout=120)
        comment_url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else None
    con.execute("""INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict,summary,body,comment_url,posted,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (pr_url, p["ticket_gid"], sha, verdict, summary, body, comment_url, 1 if comment_url else 0, now()))
    event(con, "review", f"{p['key']} PR #{p['number']}: {verdict}", p["ticket_gid"])
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (p["ticket_gid"],)).fetchone()
    if not (M.is_ms(t["gid"]) or M.in_milestone(t)):  # milestone work merges / waits on its own (TP-105)
        N.add(con, "review", f"{p['key']} PR #{p['number']} AI 리뷰: {VERDICT_KO[verdict]}", summary, pr_url, t["gid"])
    restage(con)
    con.commit()
    print(json.dumps({"saved": True, "comment_url": comment_url}))


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
