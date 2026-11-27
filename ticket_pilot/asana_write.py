"""`tp asana comment|create|update|tag`: Asana writes for the chat and ticket agents (TP-97, the owner).

tp holds the PAT (core.asana_token); agents only get these commands. An agent process carries TP_AGENT (its launch
tag, runner.launch); there the commands need global.agent_asana_write (default on) and automation steps (auto-*) are
always refused. Every write is an `asana_write` event (who, which task, what) without comment / notes text.
Not a sandbox: agents run unsandboxed as the owner, so the switch and the keychain token rely on the agents following
their prompts.
"""
from __future__ import annotations

import os
import sqlite3

from .core import NO_WORKSPACE, asana, db, event, settings


class CreateRecordError(RuntimeError):
    def __init__(self, task):
        super().__init__(f"Asana 태스크는 생성됐어요 ({task.get('permalink_url') or task['gid']}). 로컬 DB에 이벤트를 기록할 수 없어요")
        self.task = task

# runner.launch fills {{ASANA}} in every agent prompt with one of these (on / off)
RULES_ON = """- Edit existing Asana tasks only through `{{TP}} asana` (it holds the token; never look for or use an Asana token yourself),
  and only when {{OWNER}} or the ticket asks for it, or for a short note on your own ticket:
  `{{TP}} asana comment <ticket key|task gid> "<text>"`
  `{{TP}} asana update <ticket key|task gid> [--completed true|false] [--assignee me|<user gid>|none] [--title ...] [--notes ...]`
  `{{TP}} asana tag <ticket key|task gid> "<tag name>"`
  The tag `{tag}` dispatches the task to a ticket agent right away. A ticket in ticket-pilot's pipeline (it has a run)
  cannot be completed or reassigned: ticket-pilot completes it once its change is live in production."""
RULES_OFF = "- Ticket and milestone creation is disabled. Asana is read only for you."


def rules() -> str:
    st = settings()
    return RULES_ON.replace("{tag}", st["asana_tag"]) if st.get("agent_asana_write", True) else RULES_OFF


class WriteDisabled(SystemExit):
    pass


def write_actor(*, agents_only=True) -> str:
    """Who writes: the agent's launch tag (TP_AGENT, e.g. tp-97, tp-97-rework, chat-<project>-<id>) or the CLI.
    Refuses where agents may not write."""
    tag = os.environ.get("TP_AGENT")
    if tag and tag.startswith("auto-"):
        raise WriteDisabled("automation steps cannot write tickets")
    if (tag or not agents_only) and not settings().get("agent_asana_write", True):
        raise WriteDisabled("ticket writes are off (global.agent_asana_write)")
    return f"agent {tag}" if tag else "cli"


def _task(con, ref: str) -> tuple[str, str]:
    """(task gid, label) for a ticket key or an Asana task gid."""
    r = con.execute("SELECT gid, key FROM tickets WHERE gid=? OR lower(key)=lower(?)", (ref, ref)).fetchone()
    if r:
        if not r["gid"].isdigit():  # gh:<repo>#<n> issues, ms:<gid>:<repo> milestone tickets
            raise SystemExit(f"{r['key']} is not an Asana task")
        return r["gid"], f"{r['key']} ({r['gid']})"
    if ref.isdigit():
        return ref, ref
    raise SystemExit(f"ticket not found: {ref} (use a ticket key or an Asana task gid)")


def _project(con, ref: str) -> str:
    r = con.execute("SELECT asana_gid FROM config_projects WHERE id=? OR asana_gid=?", (ref, ref)).fetchone()
    if r:
        if not r["asana_gid"]:
            raise SystemExit(f"project {ref} has no Asana project")
        return r["asana_gid"]
    if ref.isdigit():
        return ref
    raise SystemExit(f"project not found: {ref} (use a project id from `tp config get` or an Asana project gid)")


def _assignee(v: str | None):
    return None if v in (None, "none") else v  # "me" and user gids go to Asana as they are


def _log(con, actor: str, what: str, gid: str, log=print):
    event(con, "asana_write", f"{actor}: {what}", gid)
    con.commit()
    if log:
        log(what)


def comment(ref: str, text: str):
    actor, con = write_actor(), db()
    if not text.strip():
        raise SystemExit("empty comment")
    gid, label = _task(con, ref)
    asana("POST", f"/tasks/{gid}/stories", body={"text": text})
    _log(con, actor, f"comment on {label} ({len(text)} chars)", gid)


def _tag_gid(name: str) -> tuple[str, bool]:
    """The workspace tag with this name (case-insensitive), created when there is none. (gid, created)."""
    ws = settings().get("asana_workspace_gid")
    if not ws:
        raise SystemExit(NO_WORKSPACE)
    found = asana("GET", f"/workspaces/{ws}/typeahead",
                  {"resource_type": "tag", "query": name, "count": 100, "opt_fields": "name"}).get("data") or []
    hit = next((t for t in found if (t.get("name") or "").strip().lower() == name.strip().lower()), None)
    if hit:
        return hit["gid"], False
    return asana("POST", "/tags", body={"name": name.strip(), "workspace": ws})["data"]["gid"], True


def tag(ref: str, name: str):
    actor, con = write_actor(), db()
    gid, label = _task(con, ref)
    if not name.strip():
        raise SystemExit("empty tag name")
    tg, created = _tag_gid(name)
    asana("POST", f"/tasks/{gid}/addTag", body={"tag": tg})
    _log(con, actor, _tag_note(name, label, created), gid)


def _tag_note(name, label, created):
    note = " (new tag)" if created else ""
    if name.strip().lower() == settings()["asana_tag"].lower():
        note += " (auto-dispatch tag)"
    return f"tag '{name.strip()}' on {label}{note}"


def create(project: str, title: str, notes: str | None = None, assignee: str | None = None,
           tags: list[str] | None = None, parent: str | None = None, *, log=print):
    actor, con = write_actor(), db()
    if not title.strip():
        raise SystemExit("empty title")
    pg = _project(con, project)
    body = {"name": title.strip(), "projects": [pg]}
    pgid = _task(con, parent)[0] if parent else None
    if pgid:  # a subtask, e.g. of a milestone parent; it stays in the project too
        body["parent"] = pgid
    if notes:
        body["notes"] = notes
    if _assignee(assignee):
        body["assignee"] = assignee
    if any(not t.strip() for t in tags or []):
        raise SystemExit("empty tag name")
    resolved = {t: _tag_gid(t) for t in dict.fromkeys(tags or [])}
    if resolved:
        body["tags"] = list(dict.fromkeys(gid for gid, _ in resolved.values()))
    d = asana("POST", "/tasks", {"opt_fields": "name,permalink_url,custom_fields.name,custom_fields.display_value"}, body)["data"]
    created_note = (f"created task {d['gid']} '{title.strip()[:60]}' in project {pg}"
                    + (f" under {pgid}" if pgid else "")
                    + (f", assignee {assignee}" if _assignee(assignee) else "") + (" with notes" if notes else ""))
    try:
        with con:
            tag_notes = [_tag_note(name, d["gid"], created) for name, (_, created) in resolved.items()]
            for note in [created_note, *tag_notes]:
                event(con, "asana_write", f"{actor}: {note}", d["gid"])
    except sqlite3.Error as e:
        raise CreateRecordError(d) from e
    if log:
        log(created_note)
        log(d.get("permalink_url") or d["gid"])
        for note in tag_notes:
            log(note)
    return d


def update(ref: str, completed: str | None = None, assignee: str | None = None, title: str | None = None,
           notes: str | None = None):
    actor, con = write_actor(), db()
    gid, label = _task(con, ref)
    body: dict = {}
    if completed is not None:
        body["completed"] = completed == "true"
    if assignee is not None:
        body["assignee"] = _assignee(assignee)
    # Refused, not warned: sync would close (completed) or drop (assignee) the ticket while its change is still on the
    # way to production (merged runs are 'finished' before the release). The owner can still do it in Asana.
    if (body.get("completed") or "assignee" in body) and con.execute(
            "SELECT 1 FROM runs r JOIN tickets t ON t.gid=r.ticket_gid WHERE r.ticket_gid=? AND r.state!='abandoned' "
            "AND t.completed=0", (gid,)).fetchone():
        raise SystemExit(f"{label} is in ticket-pilot's pipeline: it is completed once its change is live in production, "
                         "and keeps its assignee until then. Comment on it instead.")
    if title is not None:
        if not title.strip():
            raise SystemExit("empty title")
        body["name"] = title.strip()
    if notes is not None:
        body["notes"] = notes
    if not body:
        raise SystemExit("nothing to update (--completed, --assignee, --title, --notes)")
    asana("PUT", f"/tasks/{gid}", body=body)
    shown = [f"{k}={v}" for k, v in body.items() if k in ("completed", "assignee")]
    shown += [f"title '{body['name'][:60]}'"] if "name" in body else []
    shown += ["notes"] if "notes" in body else []
    _log(con, actor, f"updated {label}: {', '.join(shown)}", gid)
