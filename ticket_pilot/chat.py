"""프로젝트 page 채팅: the owner talks to a headless agent about one project (TP-91: a project has 1..N repos).

Each turn is one detached agent (runner.launch) in the project's default repo checkout, the other repos opened with
--add-dir, with the default repo's agent settings (repo.<name>.agent, else the global agent). The first turn sends prompts/chat.md + the message and records the
agent's session; later turns resume it with only the new message.
The reply is the agent's final message (runner.last_message), read when the process has exited; the dashboard
polls messages(), which finalizes finished turns. One running turn per conversation. The tick also finalizes
turns and summarizes idle Claude sessions so the next turn can start fresh with the summary.

Attachments (TP-72): files sent with a message are stored in data/chat-uploads/<conversation>/ (outside every repo
checkout) and listed in chat_messages.attachments; the agent gets their absolute paths at the end of the message
(claude reads them with Read; codex also gets the images with --image). Deleting the conversation deletes them.
"""
from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import runner as R
from .core import DATA, ROOT, UNSORTED, db, now, project_map, settings

MAX_TEXT = 32000
REPLY_LIMIT = 200_000
COMPACT_IDLE = timedelta(minutes=50)
COMPACT_TIMEOUT = timedelta(minutes=5)
COMPACT_LIMIT = 2  # keep a backlog of old conversations from starting all at once
SUMMARY_LIMIT = 6000
_USAGE_UPDATE = """input_tokens=?, cache_read_input_tokens=?, cache_creation_input_tokens=?,
                  output_tokens=?, context_tokens=?"""
COMPACT_PROMPT = ("/compact Keep a handoff for a fresh project chat in under 4,000 characters. "
                  "Retain the user's goals, preferences, decisions, completed work, open tasks, and relevant paths or ticket IDs.")
UPLOADS = DATA / "chat-uploads"
MAX_FILE = 20 * 1024 * 1024
MAX_FILES = 10
IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # what codex --image and the thumbnails take
_BAD_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def _layout() -> list[dict]:
    """Projects in settings order with their repos (default repo first), then 미분류 (UNSORTED) with the repos no
    project links. A repo in two projects is in both. repo/path/agent = the default repo the chat runs in."""
    pm = project_map()
    out, linked = [], set()

    def repo(name, rr):
        e = pm["repos"].get(name) or {}
        path = os.path.expanduser(e.get("path") or "")
        return {"name": name, "role": rr.get("role"), "default": bool(rr.get("default")),
                "description": e.get("description") or "", "github": e.get("github") or "", "path": path,
                "path_ok": bool(path) and os.path.isdir(path), "agent_conf": e.get("agent")}

    def project(pid, name, source, gid, repos, description=""):
        repos.sort(key=lambda r: not r["default"])  # stable: settings order after the default repo
        d = repos[0] if repos else {}
        return {"id": pid, "name": name, "source": source, "asana_gid": gid, "unsorted": pid == UNSORTED,
                "repos": repos, "repo": d.get("name"), "description": description or d.get("description") or "",
                "github": d.get("github") or "", "path": d.get("path") or "", "path_ok": bool(d.get("path_ok")),
                "agent": R.describe(d.get("agent_conf"))}

    for p in pm["projects"]:
        repos = [repo(rr["repo"], rr) for rr in p.get("repos") or [] if rr.get("repo") in pm["repos"]]
        linked |= {r["name"] for r in repos}
        out.append(project(p["id"], p["name"], p.get("source"), p.get("asana_gid"), repos))
    rest = [repo(n, {}) for n in pm["repos"] if n not in linked]
    out.append(project(UNSORTED, "미분류", "none", None, rest, "어느 프로젝트에도 속하지 않은 레포"))
    return out


def _focus(p: dict, repo: str | None) -> dict:
    """p as one conversation sees it: the conversation's repo first while it is still in the project (a session lives
    in the checkout it started in), else the default repo. 미분류 opens only that repo, not the unrelated others."""
    main = next((r for r in p["repos"] if r["name"] == repo), p["repos"][0] if p["repos"] else None)
    if not main:
        return p
    others = [] if p["unsorted"] else [r for r in p["repos"] if r is not main]
    return {**p, "repos": [main, *others], "repo": main["name"], "github": main["github"], "path": main["path"],
            "path_ok": main["path_ok"]}


def _owner(c, layout) -> str:
    """The project a conversation shows under: its project_id while that project exists, else the first project with
    its repo (the project was deleted, or the conversation predates TP-91), else 미분류."""
    ids = {p["id"] for p in layout if not p["unsorted"]}
    if c["project_id"] in ids:
        return c["project_id"]
    return next((p["id"] for p in layout if any(r["name"] == c["repo"] for r in p["repos"])), UNSORTED)


# TP-103: finished replies (failed ones too) after the last one the owner saw (chat_conversations.read_id)
UNREAD = """(SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id=c.id AND m.role='assistant'
             AND m.text IS NOT NULL AND m.id > COALESCE(c.read_id, 0))"""


def _finalize_all(con) -> None:
    """Finish turns whose process ended, so the sidebar's running dot and unread counts move without opening that chat."""
    rows = con.execute("SELECT * FROM chat_messages WHERE role IN ('assistant', 'compaction') AND text IS NULL").fetchall()
    for r in rows:
        _finalize(con, r)
    if rows:
        con.commit()


def projects() -> list[dict]:
    """Projects (see _layout) with their chat activity; 미분류 only while it has repos or conversations."""
    con = db()
    _finalize_all(con)
    layout = _layout()
    stats: dict = {}
    for c in con.execute(
            f"""SELECT c.id, c.repo, c.project_id, c.updated_at, {UNREAD} AS unread,
                      EXISTS(SELECT 1 FROM chat_messages m WHERE m.conversation_id=c.id AND m.role IN ('assistant', 'compaction')
                             AND m.text IS NULL) AS running FROM chat_conversations c"""):
        st = stats.setdefault(_owner(c, layout), {"conversations": 0, "running": 0, "unread": 0, "last_at": None})
        st["conversations"] += 1
        st["running"] += c["running"]
        st["unread"] += c["unread"]
        st["last_at"] = max(st["last_at"] or "", c["updated_at"] or "") or None
    out = []
    for p in layout:
        st = stats.get(p["id"]) or {}
        if p["unsorted"] and not p["repos"] and not st:
            continue
        for r in p["repos"]:
            r.pop("agent_conf")
        out.append({**p, "conversations": st.get("conversations") or 0, "running": st.get("running") or 0,
                    "unread": st.get("unread") or 0, "last_at": st.get("last_at")})
    return out


def inbox() -> list[dict]:
    """수신함 (TP-103): conversations of every project with unread replies, latest reply first. last_id = the newest
    reply listed (read_all marks only up to what was shown)."""
    con = db()
    _finalize_all(con)
    layout = _layout()
    names = {p["id"]: p["name"] for p in layout}
    out = []
    for c in con.execute(
            f"""SELECT * FROM (SELECT c.id, c.repo, c.project_id, c.title, {UNREAD} AS unread,
                          (SELECT MAX(m.id) FROM chat_messages m WHERE m.conversation_id=c.id AND m.role='assistant'
                           AND m.text IS NOT NULL) AS last_id FROM chat_conversations c) WHERE unread > 0"""):
        m = con.execute("SELECT substr(text,1,140) AS preview, failed, COALESCE(finished_at, ts) AS at "
                        "FROM chat_messages WHERE id=?", (c["last_id"],)).fetchone()
        owner = _owner(c, layout)
        out.append({"id": c["id"], "title": c["title"], "project_id": owner, "project": names.get(owner, owner),
                    "unread": c["unread"], "last_id": c["last_id"], "preview": m["preview"], "failed": bool(m["failed"]),
                    "at": m["at"]})
    out.sort(key=lambda r: (r["at"] or "", r["last_id"]), reverse=True)
    return out


def _upto(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise SystemExit("잘못된 메시지 id예요")
    return v


def read(ident: str, upto) -> None:
    """The owner saw the conversation's replies up to message id upto (the newest one the open thread showed)."""
    read_all([{"id": ident, "upto": upto}])


def read_all(items) -> None:
    """Marks [{id, upto}] read: the open thread, or 수신함 모두 읽음 with each listed conversation's newest reply.
    Per conversation: a reply's id is taken when its turn starts, so one global id could swallow a turn still running."""
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise SystemExit("잘못된 요청이에요")
    con = db()
    marks = [(i.get("id"), _upto(i.get("upto"))) for i in items]
    for ident, _ in marks:
        _conv(con, ident)
    for ident, upto in marks:
        con.execute("UPDATE chat_conversations SET read_id=? WHERE id=? AND COALESCE(read_id, 0) < ?", (upto, ident, upto))
    con.commit()


def _project(pid: str) -> dict:
    p = next((p for p in _layout() if p["id"] == pid), None)
    if not p:
        raise SystemExit(f"설정에 없는 프로젝트예요: {pid}")
    return p


def _conv(con, ident: str) -> dict:
    row = con.execute("SELECT * FROM chat_conversations WHERE id=?", (ident,)).fetchone()
    if not row:
        raise SystemExit("없는 대화예요")
    return dict(row)


def _dir(ident: str):
    if not re.fullmatch(r"c[0-9a-f]{16}", ident or ""):
        raise SystemExit("없는 대화예요")
    return UPLOADS / ident


def _save(ident: str, files: list) -> list[dict]:
    """Validates every file first (name, size, base64), then writes them; returns the attachment list.
    Stored as <8 hex>-<name> so two pastes named image.png do not collide."""
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise SystemExit(f"첨부는 한 번에 {MAX_FILES}개까지 보낼 수 있어요")
    todo = []
    for f in files:
        name = f.get("name") if isinstance(f, dict) else None
        name = name.strip() if isinstance(name, str) else ""
        if not name or name.startswith(".") or len(name) > 120 or _BAD_NAME.search(name):
            raise SystemExit(f"잘못된 파일 이름이에요: {str(name)[:120]!r} (/ \\ : * ? \" < > | 와 제어 문자, "
                             "점으로 시작하는 이름은 쓸 수 없어요)")
        try:
            data = base64.b64decode(f.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise SystemExit(f"{name}: 파일 내용을 읽지 못했어요") from None
        if len(data) > MAX_FILE:
            raise SystemExit(f"{name}: 파일이 너무 커요 ({len(data) / 1048576:.1f}MB, 파일당 {MAX_FILE // 1048576}MB까지)")
        todo.append((name, data))
    d = _dir(ident)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for name, data in todo:
        stored = f"{uuid.uuid4().hex[:8]}-{name.replace(',', '_')}"  # codex splits --image values on commas
        (d / stored).write_bytes(data)
        out.append({"name": name, "file": stored, "size": len(data),
                    "type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    return out


def file_path(ident: str, name: str):
    """An uploaded file of the conversation (GET /api/chat/file), or None; never anything outside its directory."""
    try:
        d = _dir(ident).resolve()
    except SystemExit:
        return None
    if not name or "/" in name or "\\" in name:
        return None
    f = (d / name).resolve()
    return f if f.parent == d and f.is_file() else None


def with_files(text: str, ident: str, atts: list[dict]) -> str:
    """The message as the agent gets it: the text plus the absolute paths of the attachments."""
    if not atts:
        return text
    d = _dir(ident).resolve()
    lines = "\n".join(f"- {d / a['file']}" + (" (이미지)" if a["type"] in IMAGES else "") for a in atts)
    return (f"{text}\n\n" if text else "") + ("첨부 파일 (사용자가 이 메시지에 붙인 파일, Read 도구로 직접 열어 확인하세요; "
                                                f"이미지는 내용을 보고 답하세요):\n{lines}")


def title_of(text: str) -> str:
    return " ".join(text.split())[:60] or "새 대화"


def conversations(project: str) -> list[dict]:
    con = db()
    layout = _layout()
    ids = [c["id"] for c in con.execute("SELECT id, repo, project_id FROM chat_conversations") if _owner(c, layout) == project]
    _finalize_ids(con, ids)
    return [dict(r) for r in con.execute(
        f"""SELECT c.id, c.repo, c.project_id, c.title, c.tool, c.created_at, c.updated_at,
                  EXISTS(SELECT 1 FROM chat_messages m WHERE m.conversation_id=c.id AND m.role IN ('assistant', 'compaction')
                         AND m.text IS NULL) AS running, {UNREAD} AS unread,
                  coalesce((SELECT substr(m.text,1,140) FROM chat_messages m WHERE m.conversation_id=c.id
                            AND m.role != 'compaction' AND m.text IS NOT NULL ORDER BY m.id DESC LIMIT 1), '') AS preview
           FROM chat_conversations c WHERE c.id IN ({",".join("?" * len(ids))})
           ORDER BY c.updated_at DESC, c.rowid DESC""", ids)]


def create(project: str, repo: str | None = None) -> str:
    """A new conversation of project, in its default repo or repo (one of its repos; 미분류 picks one this way)."""
    p = _project(project)
    if repo and not any(r["name"] == repo for r in p["repos"]):
        raise SystemExit(f"{p['name']}에 없는 레포예요: {repo}")
    p = _focus(p, repo)
    con = db()
    ident = "c" + uuid.uuid4().hex[:16]
    con.execute("INSERT INTO chat_conversations(id,project_id,repo,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (ident, p["id"], p["repo"], "새 대화", now(), now()))
    con.commit()
    return ident


def rename(ident: str, title: str) -> None:
    title = " ".join((title or "").split())[:80]
    if not title:
        raise SystemExit("제목을 입력해 주세요")
    con = db()
    _conv(con, ident)
    con.execute("UPDATE chat_conversations SET title=? WHERE id=?", (title, ident))
    con.commit()


def delete(ident: str) -> None:
    """Stops a running turn, then removes the conversation (the agent's own session files stay)."""
    con = db()
    con.execute("BEGIN IMMEDIATE")
    with con:
        _conv(con, ident)
        handles = con.execute("SELECT handle FROM chat_messages WHERE conversation_id=? AND text IS NULL AND handle IS NOT NULL",
                              (ident,)).fetchall()
        con.execute("DELETE FROM chat_messages WHERE conversation_id=?", (ident,))
        con.execute("DELETE FROM chat_conversations WHERE id=?", (ident,))
    for r in handles:
        R.stop(r["handle"])
    shutil.rmtree(_dir(ident), ignore_errors=True)


def stop(ident: str) -> None:
    con = db()
    _conv(con, ident)
    for r in con.execute("SELECT id, handle FROM chat_messages WHERE conversation_id=? AND role='assistant' AND text IS NULL",
                         (ident,)).fetchall():
        R.stop(r["handle"])
        _, end = R.window(r["handle"])
        usage = R.claude_turn_usage(r["handle"], end)
        con.execute(f"UPDATE chat_messages SET text=?, failed=1, finished_at=?, {_USAGE_UPDATE} WHERE id=? AND text IS NULL",
                    (R.last_message(r["handle"], REPLY_LIMIT) or "(중단했어요)", now(), *usage, r["id"]))
    con.commit()


def prompt(p: dict, text: str) -> str:
    others = "\n".join(f"  - {r['name']} ({r['role'] or '-'}, GitHub {r['github'] or '-'}): {r['path']}"
                       for r in p["repos"][1:] if r["path_ok"]) or "  (없음)"
    return ((ROOT / "prompts" / "chat.md").read_text().replace("{{PROJECT}}", p["name"]).replace("{{OTHER_REPOS}}", others)
            .replace("{{REPO}}", p["repo"]).replace("{{GITHUB}}", p["github"] or "(GitHub 정보 없음)")
            .replace("{{PATH}}", p["path"]).replace("{{OWNER}}", settings().get("owner_name", "the user")).replace("{{MESSAGE}}", text))


def _check_idle(con, ident: str) -> None:
    row = con.execute("SELECT role FROM chat_messages WHERE conversation_id=? AND text IS NULL", (ident,)).fetchone()
    if row:
        raise SystemExit("대화를 압축하는 중이에요" if row["role"] == "compaction" else "이전 답을 기다리는 중이에요")


def _previous_result(con, ident: str):
    return con.execute("""SELECT id, role, text, compaction_id FROM chat_messages
                          WHERE conversation_id=? AND role IN ('assistant', 'compaction')
                          AND failed=0 AND text IS NOT NULL ORDER BY id DESC LIMIT 1""", (ident,)).fetchone()


def send(ident: str, text: str, files: list | None = None) -> int:
    """Records the owner's message (and its attachments, see _save) and starts the agent turn; returns the assistant
    message id."""
    text = (text or "").strip()
    if (not text and not files) or len(text) > MAX_TEXT:
        raise SystemExit(f"메시지는 1~{MAX_TEXT}자로 입력해 주세요")
    con = db()
    c = _conv(con, ident)
    p = _focus(_project(_owner(c, _layout())), c["repo"])
    if not p["repo"]:
        raise SystemExit(f"{p['name']}에 연결된 레포가 없어요 (설정에서 레포를 추가해 주세요)")
    if not p["path_ok"]:
        raise SystemExit(f"{p['repo']} 체크아웃이 없어요: {p['path'] or '경로 미설정'} (설정에서 경로를 확인해 주세요)")
    _finalize_ids(con, [ident])
    _check_idle(con, ident)  # checked again under the lock; this one skips the upload
    agent = p["repos"][0]["agent_conf"]
    tool = R.resolve(agent)["tool"]
    atts = _save(ident, files) if files else []
    con.execute("BEGIN IMMEDIATE")
    try:
        c = _conv(con, ident)
        _check_idle(con, ident)
        # A session lives in the checkout it started in. A different repo or tool starts a new session.
        resume = c["session"] if c["session"] and c["tool"] == tool and c["repo"] == p["repo"] else None
        prior = _previous_result(con, ident)
        handoff = prior if prior and prior["role"] == "compaction" and not c["session"] else None
        compaction_id = None
        if tool == "claude":
            compaction_id = handoff["id"] if handoff else (prior["compaction_id"] if resume and prior else None)
        if not con.execute("SELECT 1 FROM chat_messages WHERE conversation_id=? AND role='user'", (ident,)).fetchone() \
                and c["title"] == "새 대화":
            con.execute("UPDATE chat_conversations SET title=? WHERE id=?",
                        (title_of(text or " ".join(a["name"] for a in atts)), ident))
        con.execute("INSERT INTO chat_messages(conversation_id,role,text,ts,attachments) VALUES(?,?,?,?,?)",
                    (ident, "user", text, now(), json.dumps(atts, ensure_ascii=False) if atts else None))
        con.execute("INSERT INTO chat_messages(conversation_id,role,ts,compaction_id) VALUES(?,?,?,?)",
                    (ident, "assistant", now(), compaction_id))
        turn = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("UPDATE chat_conversations SET updated_at=? WHERE id=?", (now(), ident))
        con.commit()  # the running row is the lock: a second tab gets "이전 답을 기다리는 중"
    except BaseException:
        con.rollback()
        for a in atts:
            (_dir(ident) / a["file"]).unlink(missing_ok=True)
        raise
    msg = with_files(text, ident, atts)
    if handoff:
        msg = (f"Earlier chat summary (background only):\n{handoff['text']}\n\n"
               f"Current user message:\n{msg}")
    images = [str(_dir(ident).resolve() / a["file"]) for a in atts if a["type"] in IMAGES]
    dirs = [r["path"] for r in p["repos"][1:] if r["path_ok"]]
    try:
        handle = R.launch(p["path"], msg if resume else prompt(p, msg), f"chat-{p['id']}-{ident}", agent=agent,
                          resume=resume, split_stderr=True, images=images, add_dirs=dirs)
    except Exception as e:  # noqa: BLE001
        con.execute("UPDATE chat_messages SET text=?, failed=1, finished_at=? WHERE id=?",
                    (f"(에이전트를 시작하지 못했어요: {e})", now(), turn))
        con.commit()
        raise SystemExit(f"에이전트를 시작하지 못했어요: {e}") from e
    con.execute("UPDATE chat_messages SET handle=? WHERE id=?", (handle, turn))
    if not resume:
        con.execute("UPDATE chat_conversations SET session=NULL, tool=?, repo=?, project_id=? WHERE id=?",
                    (tool, p["repo"], p["id"], ident))
    con.commit()
    return turn


def _finalize(con, row) -> None:
    """Fills in a finished turn's reply and binds the agent session to the conversation."""
    if row["role"] == "compaction":
        _finalize_compaction(con, row)
        return
    if not row["handle"]:
        return  # launch still in progress (send() sets the handle right after)
    alive, code = R.agent_state(row["handle"])
    if alive:
        return
    reply = R.last_message(row["handle"], REPLY_LIMIT)
    failed = code != 0 or not reply
    if not reply:
        reply = f"(답을 받지 못했어요: exit {code})"
        err = _tail(row["handle"], 1500).strip()
        if err:
            reply += f"\n\n```\n{err}\n```"
    _, end = R.window(row["handle"])
    finished_at = _finished_at(end)
    usage = R.claude_turn_usage(row["handle"], end)
    agent, sid = R.session(row["handle"])
    finished = con.execute(f"""UPDATE chat_messages SET text=?, failed=?, finished_at=?, claude_session_id=?, {_USAGE_UPDATE}
                               WHERE id=? AND text IS NULL""",
                           (reply, int(failed), finished_at, sid if agent == "claude" else None, *usage, row["id"]))
    if not finished.rowcount:  # a dashboard request or the tick may have finalized the same turn
        return
    if sid and not failed:
        con.execute("UPDATE chat_conversations SET session=? WHERE id=?", (sid, row["conversation_id"]))
    con.execute("UPDATE chat_conversations SET updated_at=? WHERE id=?", (finished_at, row["conversation_id"]))


def _finalize_compaction(con, row) -> None:
    alive, code = R.agent_state(row["handle"])
    if alive:
        if datetime.fromisoformat(now()) - datetime.fromisoformat(row["ts"]) < COMPACT_TIMEOUT:
            return
        R.stop(row["handle"])
    summary = R.compacted_summary(row["handle"], SUMMARY_LIMIT) if code == 0 else ""
    usage = R.compacted_usage(row["handle"])
    _, end = R.window(row["handle"])
    done = con.execute(f"""UPDATE chat_messages SET text=?, failed=?, finished_at=?, {_USAGE_UPDATE}
                           WHERE id=? AND text IS NULL""", (summary, int(not summary), _finished_at(end), *usage, row["id"]))
    if done.rowcount and summary:
        con.execute("UPDATE chat_conversations SET session=NULL WHERE id=?", (row["conversation_id"],))


def _finished_at(end: float | None) -> str:
    return datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="seconds") if end is not None else now()


_COMPACTION_DUE = """c.tool='claude' AND c.session IS NOT NULL AND datetime(c.updated_at) <= datetime(?)
    AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.conversation_id=c.id AND m.text IS NULL)
    AND (SELECT m.role FROM chat_messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1)='assistant'"""


def compact_idle(con) -> None:
    """Start summaries in the background and let subsequent ticks or chat reads record their result."""
    _finalize_all(con)
    active = con.execute("SELECT COUNT(*) FROM chat_messages WHERE role='compaction' AND text IS NULL").fetchone()[0]
    slots = COMPACT_LIMIT - active
    if slots <= 0:
        return
    cutoff = (datetime.fromisoformat(now()) - COMPACT_IDLE).isoformat()
    candidates = con.execute(f"""SELECT id FROM chat_conversations c WHERE {_COMPACTION_DUE}
                                 ORDER BY c.updated_at DESC""", (cutoff,)).fetchall()
    if not candidates:
        return
    repos = project_map()["repos"]
    for candidate in candidates:
        con.execute("BEGIN IMMEDIATE")
        with con:
            c = con.execute(f"SELECT * FROM chat_conversations c WHERE c.id=? AND {_COMPACTION_DUE}",
                            (candidate["id"], cutoff)).fetchone()
            if not c:
                continue
            path = os.path.expanduser((repos.get(c["repo"]) or {}).get("path") or "")
            if not os.path.isdir(path):
                continue
            last = con.execute("""SELECT id, handle, context_tokens FROM chat_messages WHERE conversation_id=?
                                  AND role='assistant' AND failed=0 ORDER BY id DESC LIMIT 1""", (c["id"],)).fetchone()
            prior = R.settings_of(last["handle"]) if last else {}
            # A stopped or failed turn can still have added context to the session being compacted.
            failed_turns = con.execute("""SELECT handle, context_tokens FROM chat_messages WHERE conversation_id=?
                                          AND id>? AND role='assistant' AND failed=1 AND context_tokens IS NOT NULL
                                          ORDER BY id DESC""", (c["id"], last["id"] if last else 0))
            before = next((r["context_tokens"] for r in failed_turns if R.session(r["handle"]) == ("claude", c["session"])),
                          last["context_tokens"] if last else None)
            agent = {"tool": "claude", "model": prior.get("model"), "effort": prior.get("effort"),
                     "ultracode": prior.get("ultracode")}
            tag = f"chat-compact-{c['id']}"
            try:
                handle = R.launch(path, COMPACT_PROMPT, tag, agent=agent, resume=c["session"],
                                  split_stderr=True, compact=True)
            except OSError as e:
                print(f"{tag}: could not start summarization: {e}", flush=True)
                con.execute("""INSERT INTO chat_messages(conversation_id,role,ts,text,failed,finished_at,before_context_tokens)
                               VALUES(?,'compaction',?,'',1,?,?)""", (c["id"], now(), now(), before))
                continue
            con.execute("""INSERT INTO chat_messages(conversation_id,role,ts,handle,before_context_tokens)
                           VALUES(?,'compaction',?,?,?)""", (c["id"], now(), handle, before))
            slots -= 1
        if not slots:
            break


def compaction_report(days: int | None) -> list[dict]:
    con = db()
    _finalize_all(con)
    cutoff = (datetime.fromisoformat(now()) - timedelta(days=days)).isoformat() if days else ""
    # A successful handoff reply establishes the session that future turns can resume.
    rows = con.execute("""WITH attempts AS (
                            SELECT m.*, (SELECT a.claude_session_id FROM chat_messages a
                                         WHERE m.failed=0 AND a.conversation_id=m.conversation_id
                                           AND a.id>m.id AND a.compaction_id=m.id AND a.role='assistant'
                                           AND a.failed=0 AND a.text IS NOT NULL AND a.claude_session_id IS NOT NULL
                                         ORDER BY a.id LIMIT 1) AS handoff_session
                            FROM chat_messages m
                            WHERE m.role='compaction' AND m.finished_at >= ? AND m.text IS NOT NULL
                          ), followups AS (
                            SELECT a.compaction_id, MIN(CASE WHEN a.failed=0 OR a.context_tokens IS NOT NULL
                                                            THEN a.id END) AS first_id, COUNT(*) AS turns,
                                   COUNT(a.context_tokens) AS measured, SUM(a.context_tokens) AS context_sum
                            FROM attempts m JOIN chat_messages a ON a.conversation_id=m.conversation_id
                              AND a.id>m.id AND a.compaction_id=m.id AND a.role='assistant'
                              AND a.claude_session_id=m.handoff_session AND a.text IS NOT NULL
                            GROUP BY a.compaction_id
                          )
                          SELECT m.id, c.title, m.finished_at, m.failed, m.before_context_tokens,
                                 first.context_tokens AS first_context_tokens,
                                 m.input_tokens + m.cache_read_input_tokens + m.cache_creation_input_tokens
                                   + m.output_tokens AS cost_tokens,
                                 COALESCE(f.turns, 0) AS turns, COALESCE(f.measured, 0) AS measured,
                                 m.before_context_tokens * f.measured - f.context_sum AS gross_tokens,
                                 f.turns IS NOT NULL AS continued
                          FROM attempts m JOIN chat_conversations c ON c.id=m.conversation_id
                          LEFT JOIN followups f ON f.compaction_id=m.id
                          LEFT JOIN chat_messages first ON first.id=f.first_id
                          ORDER BY m.finished_at DESC, m.id DESC""", (cutoff,))
    report = []
    for row in rows:
        item = dict(row)
        gross = item.pop("gross_tokens")
        cost = item["cost_tokens"]
        before, first = item["before_context_tokens"], item["first_context_tokens"]
        item["first_savings_tokens"] = before - first if before is not None and first is not None else None
        item["failed"] = bool(item["failed"])
        item["continued"] = bool(item["continued"])
        if item["failed"] or not item["continued"]:
            gross = 0
        item["net_savings_tokens"] = gross - cost if gross is not None and cost is not None else None
        report.append(item)
    return report


def _finalize_ids(con, ids: list[str]) -> None:
    rows = con.execute(f"""SELECT * FROM chat_messages WHERE conversation_id IN ({",".join("?" * len(ids))})
                           AND role IN ('assistant', 'compaction') AND text IS NULL""", ids).fetchall()
    for r in rows:
        _finalize(con, r)
    if rows:
        con.commit()


def _tail(handle: str, n: int = 4000) -> str:
    """The end of a running turn's stderr (codex prints its steps there; claude -p prints only at the end)."""
    _, log = R._parts(handle)
    try:
        err = f"{log}.err"
        size = os.path.getsize(err)
        with open(err, "rb") as f:
            f.seek(max(0, size - n))
            return f.read().decode(errors="replace")
    except (OSError, TypeError):
        return ""


def messages(ident: str) -> dict:
    con = db()
    _conv(con, ident)
    _finalize_ids(con, [ident])
    c = _conv(con, ident)
    rows = [dict(r) for r in con.execute(
        "SELECT id, role, text, ts, handle, failed, finished_at, attachments FROM chat_messages "
        "WHERE conversation_id=? AND (role!='compaction' OR failed=0) ORDER BY id",
        (ident,))]
    busy = None
    for m in rows:
        if m["text"] is None:
            busy = {"since": m["ts"], "log": _tail(m["handle"]) if m["role"] == "assistant" and m["handle"] else ""}
        elif m["role"] == "compaction":
            m["text"] = "대화가 압축됐어요"
        handle = m.pop("handle")
        m["agent"] = R.settings_of(handle).get("agent") if m["role"] == "assistant" else None
        m["failed"] = bool(m["failed"])
        m["attachments"] = [{"name": a["name"], "size": a["size"], "type": a["type"], "image": a["type"] in IMAGES,
                             "url": f"/api/chat/file?id={ident}&name={quote(a['file'])}"}
                            for a in json.loads(m["attachments"] or "[]")]
    return {"conversation": c, "messages": rows, "busy": busy}
