"""Notion OAuth connections and data API (2026-03-11)."""
from __future__ import annotations

import base64
import fcntl
import http.client
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager

from . import core

API = "https://api.notion.com/v1"
SERVICE = "ticket-pilot-notion"
CALLBACK = "/notion/callback"
TTL = 600
_pending: dict[str, tuple[float, str, str, str]] = {}
_state_lock = threading.Lock()


class NotionError(RuntimeError):
    pass


class PageNotFound(NotionError):
    """A referenced page is absent from the selected data source."""


class UncertainWrite(NotionError):
    """The connection may have applied this mutation before the response failed."""


class APIError(NotionError):
    def __init__(self, status, capability="콘텐츠 읽기"):
        hints = {400: "요청 값과 OAuth 앱 설정을 확인하세요", 401: "계정을 다시 연결하세요",
                 403: f"Notion OAuth 앱에서 {capability} 권한을 켜고 전역 설정 → Notion → Notion으로 로그인에서 다시 승인하세요",
                 404: "선택한 항목이 연결에 공유돼 있는지 확인하세요",
                 429: "잠시 뒤 다시 시도하세요"}
        super().__init__(f"Notion HTTP {status} · {hints.get(status, '잠시 뒤 다시 시도하세요')}")
        self.status = status


def _request(path, authorization, body=None, method=None, capability="콘텐츠 읽기"):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": authorization, "Notion-Version": "2026-03-11", "Content-Type": "application/json",
    })
    # Search, queries and OAuth use implicit POST. An explicit POST creates a page or comment.
    uncertain = ("Notion 요청 결과를 확인할 수 없어요. 다시 요청하기 전에 프로젝트 DB에서 페이지나 댓글을 확인하세요"
                 if method == "POST" else "Notion 요청 결과를 확인할 수 없어요. 페이지에서 변경 내용을 확인하세요")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.load(response)
                if not isinstance(result, dict):
                    raise ValueError("Notion 응답 형식을 확인할 수 없어요")
                return result
        except urllib.error.HTTPError as e:
            e.close()
            delay = e.headers.get("Retry-After", "1")
            if e.code == 429 and attempt < 2 and delay.isdecimal() and int(delay) <= 10:
                time.sleep(int(delay))
                continue
            if method and e.code >= 500:
                raise UncertainWrite(f"Notion HTTP {e.code} · {uncertain}") from None
            raise APIError(e.code, capability) from None  # an OAuth error body can contain credentials
        except (OSError, ValueError, http.client.HTTPException) as e:
            if method:
                raise UncertainWrite(uncertain) from e
            raise NotionError("Notion 응답을 읽을 수 없어요. 잠시 뒤 다시 시도하세요") from e


# Store each rotating token pair as one keychain item (keychain values use a restricted alphabet).
def _store(key, value):
    try:
        core.secret_set(SERVICE, key, base64.b64encode(json.dumps(value).encode()).decode())
    except (OSError, RuntimeError) as e:
        raise NotionError(f"Notion 자격 증명을 저장할 수 없어요. {core.secret_store()}을 확인하세요") from e


def _stored(key):
    try:
        raw = core.secret_get(SERVICE, key)
        return json.loads(base64.b64decode(raw)) if raw else None
    except (OSError, RuntimeError, ValueError) as e:
        raise NotionError(f"Notion 자격 증명을 읽을 수 없어요. {core.secret_store()}을 확인하세요") from e


@contextmanager
def _credential_lock():
    # The dashboard and tick are separate processes. Only one may exchange a given refresh token.
    core.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with (core.DB_PATH.parent / "notion-oauth.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_app(client_id, secret):
    if not all(isinstance(v, str) and v.strip() and len(v) <= 512 for v in (client_id, secret)):
        raise NotionError("OAuth 앱의 클라이언트 ID와 시크릿을 입력하세요")
    with _credential_lock():
        _store("oauth-app", [client_id.strip(), secret.strip()])


def accounts(con):
    return {"configured": bool(_stored("oauth-app")), "connections": [dict(r) for r in con.execute(
        "SELECT * FROM notion_connections ORDER BY workspace_name, user_name, id")]}


def _app():
    app = _stored("oauth-app")
    if not app:
        raise NotionError("먼저 Notion OAuth 앱을 설정하세요")
    return app


def start(actor, redirect):
    client_id, _ = _app()
    state, proof = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.monotonic()
    with _state_lock:
        for key in [k for k, entry in _pending.items() if entry[0] <= now]:
            del _pending[key]
        _pending[state] = (now + TTL, actor, redirect, proof)
    params = {"owner": "user", "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "state": state}
    return "https://api.notion.com/v1/oauth/authorize?" + urllib.parse.urlencode(params), state, proof


def _exchange(body):
    auth = base64.b64encode(":".join(_app()).encode()).decode()
    result = _request("/oauth/token", f"Basic {auth}", body)
    if not result.get("access_token"):
        raise NotionError("Notion 인증 응답에 액세스 토큰이 없어요")
    return result


def _save_tokens(key, result):
    tokens = [result["access_token"], result.get("refresh_token")]
    _store(key, tokens)
    return tokens


def finish(con, actor, state, cookie, code):
    if not isinstance(state, str) or not cookie:
        raise NotionError("연결을 시작한 브라우저에서 다시 시도하세요")
    with _state_lock:
        pending = _pending.get(state)
        if (not pending or pending[0] <= time.monotonic() or pending[1] != actor
                or not secrets.compare_digest(pending[3].encode(), cookie.encode())):
            raise NotionError("연결을 시작한 브라우저에서 다시 시도하세요")
        del _pending[state]
    if not isinstance(code, str) or not code:
        raise NotionError("인증 요청이 만료되었거나 취소됐어요. 다시 연결하세요")
    with _credential_lock():
        result = _exchange({"grant_type": "authorization_code", "code": code, "redirect_uri": pending[2]})
        user = (result.get("owner") or {}).get("user") or {}
        if not user.get("id") or not result.get("workspace_id"):
            raise NotionError("연결한 사용자와 워크스페이스를 확인할 수 없어요")
        workspace, owner = str(uuid.UUID(result["workspace_id"])), str(uuid.UUID(user["id"]))
        key = f"{workspace}:{owner}"
        _save_tokens(key, result)
    con.execute("""INSERT INTO notion_connections(id,workspace_id,workspace_name,user_id,user_name) VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET workspace_name=excluded.workspace_name,user_name=excluded.user_name""",
                (key, workspace, result.get("workspace_name") or workspace, owner, user.get("name") or owner))
    con.commit()
    return {"id": key}


def valid_id(value):
    try:
        return isinstance(value, str) and value == str(uuid.UUID(value))
    except ValueError:
        return False


class Client:
    def __init__(self, con, connection_id):
        self.account = con.execute("SELECT * FROM notion_connections WHERE id=?", (connection_id,)).fetchone()
        if not self.account:
            raise NotionError("프로젝트에 사용할 Notion 계정을 연결하세요")
        self.key = self.account["id"]
        self.tokens = _stored(self.key)
        if not self.tokens:
            raise NotionError("선택한 Notion 계정을 다시 연결하세요")

    def _call(self, path, body, method, capability):
        try:
            return _request(path, f"Bearer {self.tokens[0]}", body, method, capability)
        except APIError as e:
            if e.status != 401 or not self.tokens[1]:
                raise
        with _credential_lock():
            latest = _stored(self.key)
            if not latest:
                raise NotionError("선택한 Notion 계정을 다시 연결하세요")
            if latest == self.tokens:
                refreshed = _exchange({"grant_type": "refresh_token", "refresh_token": latest[1]})
                latest = _save_tokens(self.key, refreshed)
            self.tokens = latest
        return _request(path, f"Bearer {self.tokens[0]}", body, method, capability)

    def read(self, path, body=None):
        capability = "사용자 정보 읽기(이메일 제외)" if path.partition("?")[0] == "/users" else "콘텐츠 읽기"
        return self._call(path, body, None, capability)

    def write(self, method, path, body, capability="콘텐츠 수정"):
        return self._call(path, body, method, capability)

    def page(self, page_id, source):
        page = self.read(f"/pages/{page_id}")
        if page.get("id") != page_id or not isinstance(page.get("properties"), dict):
            raise NotionError("Notion 페이지의 속성을 확인할 수 없어요")
        if (page.get("parent") or {}).get("data_source_id") != source:
            raise PageNotFound("페이지가 프로젝트에 연결된 Notion 데이터베이스에 없어요")
        if page.get("in_trash") or page.get("is_archived") or page.get("archived"):
            raise PageNotFound("휴지통에 있는 Notion 페이지예요")
        return page

    def items(self, path, body=None):
        params = {"page_size": 100}
        cursors = set()
        while True:
            result = (self.read(path + "?" + urllib.parse.urlencode(params)) if body is None
                      else self.read(path, {**body, **params}))
            if (result.get("request_status") or {}).get("type") == "incomplete":
                raise NotionError("Notion 조회 한도를 넘었어요. 가져오기 조건을 좁혀 주세요")
            yield from result["results"]
            if not result["has_more"]:
                return
            cursor = result.get("next_cursor")
            if not cursor or cursor in cursors:
                raise NotionError("Notion의 다음 결과를 확인할 수 없어요")
            cursors.add(cursor)
            params["start_cursor"] = cursor

    def markdown(self, page_id):
        """Fetch the page and any subtrees Notion omitted. Only return a complete brief body."""
        parts, seen = [], set()
        pending = [(page_id, frozenset())]
        while pending:
            block_id, ancestors = pending.pop()
            if block_id in ancestors:
                raise NotionError(f"Notion이 누락된 블록 {block_id}을 반복해서 반환해 작업을 보류해요")
            if block_id in seen:
                continue
            try:
                result = self.read(f"/pages/{block_id}/markdown")
            except NotionError as e:
                raise NotionError(f"Notion 본문 {block_id}을 읽을 수 없어 작업을 보류해요 ({e})") from e
            body, unknown = result.get("markdown"), result.get("unknown_block_ids", [])
            if not isinstance(body, str) or not isinstance(unknown, list) or not all(valid_id(i) for i in unknown):
                raise NotionError(f"Notion 본문 {block_id}의 응답을 확인할 수 없어 작업을 보류해요")
            if result.get("truncated") and not unknown:
                raise NotionError(f"Notion 본문 {block_id}이 잘렸고 추가 조회할 블록 ID가 없어 작업을 보류해요")
            parts.append(body if block_id == page_id else f"#### Notion 블록 {block_id}\n\n{body}")
            seen.add(block_id)
            path = ancestors | {block_id}
            pending.extend((child, path) for child in reversed(unknown))
        return "\n\n".join(parts)

    def sources(self):
        rows = self.items("/search", {"filter": {"property": "object", "value": "data_source"}})
        return [{"id": r["id"], "name": plain(r.get("title")) or r["id"]} for r in rows if r["object"] == "data_source"]

    def rows(self, source, *, query_filter=None):
        if not valid_id(source):
            raise NotionError("공유한 데이터베이스를 골라 주세요")
        body = {"result_type": "page"}
        if query_filter:
            body["filter"] = query_filter
        pages = self.items(f"/data_sources/{source}/query", body)
        return [{"id": p["id"], "name": page_title(p) or p["id"]} for p in pages if active_page(p)]

    def relation_ids(self, page, column):
        prop = page["properties"].get(column["name"], {})
        if prop.get("has_more"):
            # Notion property IDs are already URL encoded.
            items = self.items(f"/pages/{page['id']}/properties/{column['id']}")
            return [item["relation"]["id"] for item in items]
        return [row["id"] for row in prop.get("relation", [])]

    def people(self):
        return [{"id": r["id"], "name": r.get("name") or r["id"]} for r in self.items("/users") if r.get("type") == "person"]

    def properties(self, source):
        if not valid_id(source):
            raise NotionError("공유한 데이터베이스를 골라 주세요")
        schema = self.read(f"/data_sources/{source}")["properties"]
        supported = ("title", "date", "people", "select", "multi_select", "status", "relation", "rich_text", "formula", "unique_id")
        return [{"id": p["id"], "name": name, "type": p["type"],
                 **({"data_source_id": p["relation"].get("data_source_id")} if p["type"] == "relation" else {}),
                 "options": [{"id": o["id"], "name": o["name"]} for o in p[p["type"]].get("options", [])]}
                for name, p in schema.items() if p["type"] in supported]


def plain(rich_text):
    return "".join(part.get("plain_text") or "" for part in rich_text or [])


def page_title(page):
    return next((plain(p["title"]) for p in page["properties"].values() if p["type"] == "title"), "")


def active_page(page):
    return page["object"] == "page" and not (page.get("in_trash") or page.get("is_archived") or page.get("archived"))
