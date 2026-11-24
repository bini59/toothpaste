"""Notion OAuth, schema/filter discovery and the dashboard preview -> saved project -> sync/dispatch path."""
import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from ticket_pilot import core, mode, notion, pipeline, release, server


def uid(n):
    return str(uuid.UUID(int=n))


OWNER, MEMBER, PERSONAL = (uid(n) for n in (1, 2, 3))
TEAM_DB, HOME_DB = uid(20), uid(21)


def page(n, title, people, tags, status):
    return {"id": uid(n), "object": "page", "url": f"https://www.notion.so/{uid(n)}", "last_edited_time": "2026-01-01",
            "properties": {"제목": {"type": "title", "title": [{"plain_text": title}]},
                           "개발 담당": {"type": "people", "people": [{"id": i} for i in people]},
                           "라벨": {"type": "multi_select", "multi_select": [{"name": t} for t in tags]},
                           "상태": {"type": "status", "status": {"name": status}}}}


class NotionFixture:
    """Wire-format fixtures. A connection can only see its own data source. Force pagination on every listing."""
    names = {"team": ("회사", OWNER, uid(10), TEAM_DB), "home": ("개인", PERSONAL, uid(11), HOME_DB)}

    def __init__(self):
        self.pages = {TEAM_DB: [page(101, "계정 연결", [OWNER, MEMBER], ["tp"], "할 일"),
                                page(102, "사용 설명", [MEMBER], ["문서"], "할 일"),
                                page(103, "배포 완료", [MEMBER], ["tp"], "완료"),
                                page(104, "팀 보드", [OWNER], ["tp"], "할 일"),
                                page(105, "설정 저장", [MEMBER], ["tp"], "할 일")],
                      HOME_DB: [page(201, "개인 일정", [PERSONAL], ["tp"], "할 일")]}
        self.props = {"개발 담당": {"type": "people", "id": "people-id", "people": {}},
                      "라벨": {"type": "multi_select", "id": "tag-id", "multi_select": {"options": [
                          {"id": "tp-id", "name": "tp"}, {"id": "docs-id", "name": "문서"}]}},
                      "상태": {"type": "status", "id": "status-id", "status": {"options": [
                          {"id": "todo-id", "name": "할 일"}, {"id": "done-id", "name": "완료"}]}}}
        self.queries = []
        self.fail_cursor = False
        self.expired = False
        self.refreshes = 0

    def respond(self, path, headers, body):
        url = urlsplit(path)
        if headers["Notion-Version"] != "2026-03-11":
            return 400, {}
        if url.path == "/oauth/token":
            basic = base64.b64encode(b"app-id:app-secret").decode()
            if headers["Authorization"] != "Basic " + basic:
                return 401, {}
            if body["grant_type"] == "refresh_token":
                name = body["refresh_token"].removeprefix("refresh-")
                self.refreshes += 1
                return 200, {"access_token": "fresh-" + name, "refresh_token": "rotated-" + name}
            name = body["code"]
            if name not in self.names or not body["redirect_uri"].endswith("/notion/callback"):
                return 400, {}
            workspace, owner, wid, _ = self.names[name]
            return 200, {"access_token": "access-" + name, "refresh_token": "refresh-" + name,
                         "owner": {"type": "user", "user": {"id": owner, "name": "민지" if name == "team" else "하늘"}},
                         "workspace_name": workspace, "workspace_id": wid, "bot_id": uid(900)}
        token = headers["Authorization"].removeprefix("Bearer ")
        name = token.split("-")[-1]
        if name not in self.names or (self.expired and token.startswith("access-")):
            return 401, {}
        source = self.names[name][3]
        args = body or {k: vs[0] for k, vs in parse_qs(url.query).items()}
        start = int(args.get("start_cursor", "0"))
        if url.path == "/search" and body["filter"] == {"property": "object", "value": "data_source"}:
            rows = [{"id": source, "object": "data_source", "title": [{"plain_text": self.names[name][0] + " 티켓"}]}]
        elif url.path == "/users":
            rows = [{"id": uid(900), "type": "bot"}, {"id": self.names[name][1], "name": "연결 사용자", "type": "person"},
                    {"id": MEMBER, "name": "서준", "type": "person"}]
        elif url.path == "/data_sources/" + source:
            return 200, {"properties": self.props}
        elif url.path == f"/data_sources/{source}/query":
            if start and self.fail_cursor:
                return 503, {}
            conditions = body.get("filter", {})
            filters = conditions.get("and", [conditions] if conditions else [])
            self.queries.append((name, filters))
            rows = self.pages[source]
            for f in filters:
                column = f["property"]
                if "people" in f:
                    rows = [r for r in rows if f["people"]["contains"] in [a["id"] for a in r["properties"][column]["people"]]]
                elif "multi_select" in f:
                    rows = [r for r in rows if f["multi_select"]["contains"] in [v["name"] for v in r["properties"][column]["multi_select"]]]
                else:
                    rows = [r for r in rows if f["status"]["equals"] == r["properties"][column]["status"]["name"]]
        elif url.path.startswith("/pages/") and url.path.endswith("/markdown"):
            return 200, {"markdown": "페이지 본문에서 가져온 작업 설명"}
        else:
            return 404, {}
        return 200, {"results": rows[start:start + 1], "has_more": start + 1 < len(rows),
                     "next_cursor": str(start + 1) if start + 1 < len(rows) else None}


class FixtureHandler(BaseHTTPRequestHandler):
    def handle_api(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, result = self.server.fixture.respond(self.path, self.headers, json.loads(data) if data else None)
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    do_GET = do_POST = handle_api

    def log_message(self, *args):
        pass


class NotionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(mock.patch.stopall)
        for patch in (mock.patch.object(core, "DB_PATH", self.root / "tp.db"), mock.patch.object(core, "_CON", None),
                      mock.patch.object(core, "KEYCHAIN", False), mock.patch.object(core, "SECRETS_FILE", self.root / "keys" / "secrets.json"),
                      mock.patch.dict(os.environ, TP_MODE="", TP_PUBLIC_URL="")):
            patch.start()
        core.config_cache_clear()
        self.addCleanup(core.config_cache_clear)
        self.con = core.db()
        self.addCleanup(self.con.close)
        self.fixture = NotionFixture()
        upstream = self.serve(FixtureHandler)
        upstream.fixture = self.fixture
        mock.patch.object(notion, "API", f"http://127.0.0.1:{upstream.server_port}").start()
        self.port = self.serve(server.H).server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.post("app", {"client_id": "app-id", "client_secret": "app-secret"})
        self.team = self.connect("team")
        self.cfg = {"connection_id": self.team, "data_source_id": TEAM_DB, "people_property": "people-id",
                    "tag_property": "tag-id", "tag_option": "tp-id", "status_property": "status-id", "status_option": "todo-id"}

    def serve(self, handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def call(self, action, body=None, cookie=""):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("POST", action if action.startswith("/") else "/api/notion/" + action, json.dumps(body or {}),
                      {"Content-Type": "application/json", "Origin": self.origin, "Cookie": cookie})
            res = c.getresponse()
            return res.status, json.loads(res.read()), res.getheader("Set-Cookie")
        finally:
            c.close()

    def post(self, action, body=None):
        status, result, _ = self.call(action, body)
        self.assertEqual(status, 200, result)
        return result

    def connect(self, name):
        status, r, cookie = self.call("connect")
        self.assertEqual(status, 200, r)
        params = parse_qs(urlsplit(r["url"]).query)
        self.assertEqual(params["redirect_uri"], [self.origin + "/notion/callback"])
        self.assertIn("HttpOnly", cookie)
        body = {"state": params["state"][0], "code": name}
        self.assertEqual(self.call("finish", body)[0], 400)  # only the initiating browser
        status, result, _ = self.call("finish", body, cookie.split(";")[0])
        self.assertEqual(status, 200, result)
        self.assertEqual(self.call("finish", body, cookie.split(";")[0])[0], 400)  # one use
        return result["id"]

    def preview(self, cfg, repos=("app",), pid="example"):
        return self.post("preview", {"project_id": pid, "repos": list(repos), "notion": cfg})["tickets"]

    def save(self, cfg, pid="example"):
        self.post("/api/config", {"updates": [
            {"scope": "global", "value": {"asana_enabled": False}},
            {"scope": "repo.app", "value": {"path": str(self.root), "github": "example/app"}},
            {"scope": "project." + pid, "value": {"name": pid, "source": "notion", "notion": cfg,
                                                  "repos": [{"repo": "app", "role": "app"}]}}]})

    def stages(self):
        return {r["name"]: r["stage"] for r in self.con.execute("SELECT name,stage FROM tickets WHERE assigned=1")}

    def test_account_scoped_schema_members_and_rotating_tokens(self):
        home = self.connect("home")
        accounts = self.post("accounts")
        self.assertEqual({r["workspace_name"] for r in accounts["connections"]}, {"회사", "개인"})
        self.assertNotIn("access-", json.dumps(accounts))
        self.assertEqual(self.post("sources", {"connection_id": self.team}), [{"name": "회사 티켓", "id": TEAM_DB}])
        self.assertEqual(self.post("sources", {"connection_id": home}), [{"name": "개인 티켓", "id": HOME_DB}])
        self.assertEqual(self.post("people", {"connection_id": self.team})[-1], {"name": "서준", "id": MEMBER})
        properties = self.post("properties", self.cfg)
        self.assertEqual([p["name"] for p in properties], ["개발 담당", "라벨", "상태"])
        self.assertEqual(properties[-1]["options"][0], {"name": "할 일", "id": "todo-id"})
        self.assertEqual(self.call("properties", {**self.cfg, "connection_id": home})[0], 400)
        mine = self.preview({**self.cfg, "connection_id": home, "data_source_id": HOME_DB})
        self.assertEqual([r["title"] for r in mine], ["개인 일정"])
        self.assertEqual(self.fixture.queries[-1][1][0]["people"], {"contains": PERSONAL})
        first, second = notion.Client(self.con, self.team), notion.Client(self.con, self.team)
        self.fixture.expired = True
        self.assertEqual(first.sources(), second.sources())
        self.assertEqual(self.fixture.refreshes, 1)
        self.assertNotEqual(mode.host_secrets()[notion.SERVICE][home], mode.host_secrets()[notion.SERVICE][self.team])

    def test_all_filters_preview_save_and_idempotent_queue(self):
        def counts():
            return [self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                    ("tickets", "runs", "config_projects", "events", "settings_history")]
        before = counts()
        self.assertEqual({x["title"] for x in self.preview(self.cfg)}, {"계정 연결", "팀 보드"})
        self.assertEqual(self.fixture.queries[-1][1][0]["people"], {"contains": OWNER})
        cfg = {**self.cfg, "assignee": MEMBER}
        only_member = self.preview(cfg)
        self.assertEqual({t["title"] for t in only_member}, {"계정 연결", "설정 저장"})
        self.assertEqual(self.fixture.queries[-1][1], [{"property": "개발 담당", "people": {"contains": MEMBER}},
                         {"property": "라벨", "multi_select": {"contains": "tp"}}, {"property": "상태", "status": {"equals": "할 일"}}])
        self.assertEqual({x["title"] for x in self.preview({**cfg, "assignee": "all"})}, {"계정 연결", "팀 보드", "설정 저장"})
        self.assertEqual(len(self.fixture.queries[-1][1]), 2)
        self.assertEqual(counts(), before)

        cfg["tag_option"] = ""  # import other labels while only the work tag starts an agent
        tickets = self.preview(cfg)
        self.assertEqual({t["title"]: t["auto_start"] for t in tickets}, {"계정 연결": True, "설정 저장": True, "사용 설명": False})
        self.save(cfg)
        self.assertEqual(self.stages(), {})
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.stages(), {t["title"]: t["stage"] for t in tickets})
        self.assertEqual({t["title"] for t in tickets if t["auto_start"]}, {n for n, s in self.stages().items() if s == "queued"})
        self.assertTrue(all(t["stage"] != "queued" for t in self.preview(cfg, repos=["app", "web"])))
        with mock.patch.object(pipeline, "asana_comments", side_effect=AssertionError("Notion is not Asana")):
            log = []
            pipeline.dispatch(dry_run=True, log=log.append)
            self.assertEqual(sum("페이지 본문에서 가져온 작업 설명" in s for s in log), 2)

        self.fixture.pages[TEAM_DB][0]["properties"]["제목"]["title"] = [{"plain_text": "계정 다시 연결"}]
        self.con.execute("UPDATE tickets SET skip=1 WHERE gid=?", ("notion:" + uid(101),))
        self.con.commit()
        next_preview = self.preview(cfg)
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.stages(), {t["title"]: t["stage"] for t in next_preview})
        self.assertEqual(self.stages()["계정 다시 연결"], "skipped")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 3)

    def test_reconciliation_completion_and_read_failures(self):
        cfg = {**self.cfg, "assignee": "all"}
        self.save(cfg)
        pipeline.sync(log=lambda *_: None)
        before = self.stages()
        self.fixture.fail_cursor = True
        self.assertIn("example", pipeline.sync(log=lambda *_: None)["notion_errors"])
        self.assertEqual(self.stages(), before)
        self.fixture.fail_cursor = False
        del self.fixture.props["상태"]
        self.assertIn("example", pipeline.sync(log=lambda *_: None)["notion_errors"])
        self.assertEqual(self.stages(), before)
        cfg.update(status_property="", status_option="", assignee=MEMBER)
        self.save(cfg)
        pipeline.sync(log=lambda *_: None)
        self.assertNotIn("팀 보드", self.stages())
        with mock.patch.object(release, "asana", side_effect=AssertionError("Notion is read only")):
            self.assertIsNone(release.complete_asana(self.con, "notion:" + uid(101)))
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.stages()["계정 연결"], "done")
        self.post("/api/config", {"scope": "project.example", "value": None})
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.stages(), {})

    def test_same_page_in_two_projects_gets_one_owner(self):
        self.save(self.cfg, "first")
        cfg = {**self.cfg, "assignee": MEMBER}
        self.save(cfg, "second")
        tickets = self.preview(cfg, pid="second")
        self.assertEqual({t["title"]: t["stage"] for t in tickets}, {"계정 연결": "other_project", "설정 저장": "queued"})
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 3)
        row = self.con.execute("SELECT project_gid FROM tickets WHERE gid=?", ("notion:" + uid(101),)).fetchone()
        self.assertEqual(row[0], "notion:first")

    def test_draft_cannot_save_partial_filters(self):
        for cfg in ({**self.cfg, "people_property": ""}, {**self.cfg, "status_property": ""}, {**self.cfg, "token": "no"}):
            value = {"name": "x", "source": "notion", "notion": cfg, "repos": []}
            self.assertEqual(self.call("/api/config", {"scope": "project.x", "value": value})[0], 400)
        self.assertEqual(self.con.execute("SELECT count(*) FROM config_projects").fetchone()[0], 0)
