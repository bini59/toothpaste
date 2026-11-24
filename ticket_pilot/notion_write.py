"""Project-scoped Notion page commands used by the CLI, chat, ticket agents and host."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from functools import cached_property

from . import core, notion, notion_tickets


@contextmanager
def page_lock(page_id):
    """Serialize tp's read/modify/write of a page across dashboard, tick and CLI processes."""
    if not notion.valid_id(page_id):
        raise notion.NotionError("Notion 페이지 ID를 확인할 수 없어요")
    folder = core.DB_PATH.parent / "notion-page-locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / page_id).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _rich_text(text):
    if not text.strip():
        raise notion.NotionError("빈 텍스트를 입력할 수 없어요")
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, len(text), 2000)]


def _option(column, value, *, new_tag=False, by_id=False):
    for option in column["options"]:
        if value == option["id"] or (not by_id and value == option["name"]):
            return {"id": option["id"]}
    if new_tag and value.strip():
        return {"name": value}
    names = ", ".join(o["name"] for o in column["options"])
    raise notion.NotionError(f"{column['name']} 열에 해당 선택지가 없어요. 현재 선택지: {names}")


class ProjectPages:
    def __init__(self, con, cfg):
        self.con = con
        self.cfg = cfg
        self.client = notion.Client(con, cfg["connection_id"])
        self.source = cfg["data_source_id"]

    @cached_property
    def schema(self):
        return {col["id"]: col for col in self.client.properties(self.source)}

    def column(self, role):
        col = notion_tickets.column(self.schema, self.cfg, role)
        if col:
            return col
        label = {"people": "담당자", "date": "마감일", "tag": "태그", "status": "상태", "title": "제목"}[role]
        raise notion.NotionError(f"프로젝트 설정에서 {label} 열을 확인하세요")

    def fields(self, title, assignee, due):
        props = {}
        if title is not None:
            props[self.column("title")["id"]] = {"title": _rich_text(title)}
        if assignee is not None:
            user = self.client.account["user_id"] if assignee == "me" else assignee
            if user != "none" and not notion.valid_id(user):
                matches = [p["id"] for p in self.client.people() if p["name"] == user]
                if len(matches) != 1:
                    raise notion.NotionError("담당자 이름이 없거나 겹쳐요. Notion 사용자 ID를 지정하세요")
                user = matches[0]
            props[self.column("people")["id"]] = {"people": [] if user == "none" else [{"id": user}]}
        if due is not None:
            try:
                value = None if due == "none" else {"start": date.fromisoformat(due).isoformat()}
            except ValueError:
                raise notion.NotionError("마감일은 YYYY-MM-DD 또는 none으로 입력하세요") from None
            props[self.column("date")["id"]] = {"date": value}
        return props

    def create(self, args):
        props = self.fields(args.title, args.assignee, args.due)
        if args.status is not None:
            col = self.column("status")
            props[col["id"]] = {col["type"]: _option(col, args.status)}
        if args.tag:
            col = self.column("tag")
            if col["id"] in props:
                raise notion.NotionError("상태와 태그가 같은 열이에요. 한 가지 값만 지정하세요")
            tags = [_option(col, tag, new_tag=True) for tag in dict.fromkeys(args.tag)]
            if col["type"] == "select" and len(tags) != 1:
                raise notion.NotionError("단일 선택 열에는 태그를 하나만 지정하세요")
            props[col["id"]] = {col["type"]: tags if col["type"] == "multi_select" else tags[0]}
        body = {"parent": {"type": "data_source_id", "data_source_id": self.source}, "properties": props}
        if args.body:
            body["markdown"] = args.body
        return self.client.write("POST", "/pages", body, "콘텐츠 추가")

    def _remember(self, page_id, page):
        try:
            with self.con:
                notion_tickets.page_edited(self.con, "notion:" + page_id, page, self.source, self.schema)
        except sqlite3.Error as e:
            raise notion.NotionError(f"{notion_tickets.ticket_key(page_id)}의 로컬 DB 저장에 실패했어요. Notion 페이지를 확인하세요") from e

    def apply_properties(self, page, props):
        page_id = page["id"]
        if props:
            try:
                page = self.client.write("PATCH", f"/pages/{page_id}", {"properties": props})
            except notion.UncertainWrite:
                self._remember(page_id, None)  # an older listing cannot safely dispatch this page
                raise
        self._remember(page_id, page)  # an idempotent retry can already see the requested value

    def update(self, page, args):
        props = self.fields(args.title, args.assignee, args.due)
        if not props and args.body is None:
            raise notion.NotionError("수정할 제목, 본문, 담당자 또는 마감일을 지정하세요")
        if props:
            self.apply_properties(page, props)
        if args.body is not None:
            try:
                self.client.write("PATCH", f"/pages/{page['id']}/markdown",
                                  {"type": "replace_content", "replace_content": {"new_str": args.body}})
            except notion.NotionError as e:
                if props:
                    raise notion.NotionError(f"속성은 저장했어요. 본문 변경을 확인하세요. {e}") from e
                raise

    @staticmethod
    def current(page, col):
        prop = page["properties"].get(col["name"])
        if not prop or prop["type"] != col["type"]:
            raise notion.NotionError(f"{col['name']} 열이 바뀌었어요. 다시 시도하세요")
        return prop[col["type"]]

    def status_change(self, page, value, *, by_id=False):
        col = self.column("status")
        choice = _option(col, value, by_id=by_id)
        current = self.current(page, col)
        if current and current["id"] == choice["id"]:
            return {}
        return {col["id"]: {col["type"]: choice}}

    def tag_change(self, page, value, remove):
        col = self.column("tag")
        choice = _option(col, value, new_tag=True)
        current = self.current(page, col)
        multi = col["type"] == "multi_select"
        tags = current if multi else ([current] if current else [])
        field, wanted = next(iter(choice.items()))
        present = any(t[field] == wanted for t in tags)
        if remove:
            if not present:
                return {}
            updated = [{"id": t["id"]} for t in tags if t[field] != wanted] if multi else None
        else:
            if present:
                return {}
            updated = [{"id": t["id"]} for t in tags] + [choice] if multi else choice
        return {col["id"]: {col["type"]: updated}}

    def comment(self, page_id, text):
        result = self.client.write("POST", "/comments", {"parent": {"page_id": page_id}, "rich_text": _rich_text(text)}, "댓글 추가")
        if not result.get("id"):
            raise notion.NotionError("댓글 ID를 확인할 수 없어요. 다시 요청하기 전에 Notion 페이지에서 댓글을 확인하세요")


def run(args):
    if os.environ.get("TP_AGENT", "").startswith("auto-"):
        raise notion.NotionError("자동화 에이전트는 Notion 페이지를 수정할 수 없어요")
    con = core.db()
    pid = args.project
    if args.op != "create":
        ticket = con.execute("""SELECT gid, key, project_gid, url FROM tickets WHERE gid LIKE 'notion:%'
                                AND (lower(key)=lower(?) OR gid=?)""", (args.ticket, args.ticket)).fetchone()
        if not ticket:
            raise notion.NotionError(f"Notion 티켓을 찾을 수 없어요: {args.ticket}")
        # Shared databases can have several projects; dispatch ownership does not select the chat's account.
        if pid is None:
            pid = ticket["project_gid"].removeprefix("notion:")
    cfg = notion_tickets.project_config(con, pid)
    if not cfg:
        raise notion.NotionError(f"프로젝트 {pid}에 연결된 Notion 데이터베이스가 없어요")
    pages = ProjectPages(con, cfg)
    if args.op == "create":
        page = pages.create(args)
        page_id = page.get("id")
        if not notion.valid_id(page_id):
            raise notion.NotionError("생성 응답의 페이지 ID를 확인할 수 없어요. 프로젝트 DB를 확인하세요")
        gid, key = "notion:" + page_id, notion_tickets.ticket_key(page_id)
        url = page.get("url") or "https://www.notion.so/" + page_id.replace("-", "")
    else:
        gid, key, url = ticket["gid"], ticket["key"], ticket["url"]
        page_id = gid.removeprefix("notion:")
        with page_lock(page_id):
            page = pages.client.page(page_id, pages.source)
            if args.op == "update":
                pages.update(page, args)
            elif args.op == "tag":
                pages.apply_properties(page, pages.tag_change(page, args.name, args.remove))
            elif args.op == "status":
                if args.value is None:
                    return {"key": key, "options": pages.column("status")["options"]}
                pages.apply_properties(page, pages.status_change(page, args.value))
            else:
                pages.comment(page_id, args.text)
    actor = os.environ.get("TP_AGENT") or "cli"
    try:
        with con:
            if args.op == "create":
                # Return a usable key before sync, including when the page doesn't match the import filter.
                ts = core.now()
                inserted = con.execute("""INSERT INTO tickets(gid,key,name,url,project_gid,assigned,first_seen,last_seen)
                                          VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(gid) DO NOTHING""",
                                       (gid, key, args.title, url, "notion:" + pid, ts, ts))
                if inserted.rowcount:
                    notion_tickets.page_edited(con, gid, page, pages.source, pages.schema)
            core.event(con, "notion_write", f"{actor} · {pid} · {key} · {args.op}", gid)
    except sqlite3.Error as e:
        raise notion.NotionError(f"Notion 요청은 처리됐어요 ({key} {url}). 로컬 DB 기록을 저장할 수 없어요") from e
    return {"key": key, "url": url}


def command(args):
    try:
        print(json.dumps(run(args), ensure_ascii=False))
    except notion.NotionError as e:
        raise SystemExit(str(e)) from None


def register(parsers):
    group = parsers.add_parser("notion", help="프로젝트에 연결된 Notion DB의 티켓 생성·편집")
    ops = group.add_subparsers(dest="op", required=True)
    for op, help_text in (("create", "페이지를 만들고 티켓 키와 링크 받기"), ("update", "지정한 속성·본문 수정"),
                          ("status", "DB의 상태 선택지 조회 또는 변경"), ("tag", "태그 추가·제거"),
                          ("comment", "티켓 페이지에 댓글 추가")):
        p = ops.add_parser(op, help=help_text)
        p.set_defaults(fn=command)
        p.add_argument("--project", required=op == "create", help="프로젝트 설정의 ID")
        if op != "create":
            p.add_argument("ticket", help="Notion 티켓 키 (NT-…)")
        if op in ("create", "update"):
            p.add_argument("--title", required=op == "create")
            p.add_argument("--body", help="페이지 본문 전체 (Markdown)")
            p.add_argument("--assignee", help="me, 사용자 ID·이름 또는 none")
            p.add_argument("--due", help="YYYY-MM-DD 또는 none")
        if op == "create":
            p.add_argument("--tag", action="append", help="태그 이름 (반복 가능)")
            p.add_argument("--status", help="상태 선택지 이름 또는 ID")
        elif op == "status":
            p.add_argument("value", nargs="?", help="선택지 이름 또는 ID (생략하면 목록)")
        elif op == "tag":
            p.add_argument("name")
            p.add_argument("--remove", action="store_true", help="이 태그만 제거")
        elif op == "comment":
            p.add_argument("text")
