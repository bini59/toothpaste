"""Import tickets from Asana, GitHub Issues and Notion into SQLite."""
from __future__ import annotations

import json
import re
from http.client import HTTPException

from . import milestone as M
from . import notion_tickets
from .core import (
    AsanaError, TASK_FIELDS, asana, asana_all, asana_workspace, db, event, load_map, meta_set, now, project_map, run, settings,
)


def field(task: dict, name: str):
    for c in task.get("custom_fields") or []:
        if c.get("name") == name and c.get("display_value"):
            return c["display_value"]
    return None


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
        except AsanaError as e:
            if e.status not in (403, 404):
                raise  # A failed lookup cannot tell us whether the ticket is still assigned.
            con.execute("UPDATE tickets SET assigned=0 WHERE gid=?", (gid,))
    return len(seen), len(gone)


def sync(log=print) -> dict:
    from .pipeline import restage

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
    hidden = list(mapping.get("hidden_projects") or {})
    con.executemany("UPDATE tickets SET assigned=0 WHERE completed=0 AND project_gid=?", [(g,) for g in hidden])
    ts, new_tagged = now(), []
    incomplete, dropped, asana_error = 0, 0, None
    if st.get("asana_enabled", True):
        con.execute("SAVEPOINT asana_sync")
        try:
            incomplete, dropped = _sync_asana(con, st, mapped, names, hidden, ts, new_tagged)
            con.execute("RELEASE asana_sync")
        except (AsanaError, OSError, HTTPException, ValueError) as e:
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
