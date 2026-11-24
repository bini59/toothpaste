"""Notion OAuth, schema/filter discovery and the dashboard preview -> saved project -> sync/dispatch path."""
import base64
import http.client
import io
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

from ticket_pilot import cli, core, mode, notion, pipeline, release, server


def uid(n):
    return str(uuid.UUID(int=n))


OWNER, MEMBER, PERSONAL = (uid(n) for n in (1, 2, 3))
TEAM_DB, HOME_DB, DOCS_DB = uid(20), uid(21), uid(22)
TAG_IDS = {"tp": "tp-id", "문서": "docs-id", "버그": "bug-id"}
STATUS_IDS = {"할 일": "todo-id", "진행 중": "doing-id", "완료": "done-id"}


def page(n, title, people, tags, status):
    return {"id": uid(n), "object": "page", "url": f"https://www.notion.so/{uid(n)}", "last_edited_time": "2026-01-01",
            "properties": {"제목": {"type": "title", "title": [{"plain_text": title}]},
                           "개발 담당": {"type": "people", "people": [{"id": i} for i in people]},
                           "라벨": {"type": "multi_select", "multi_select": [{"name": t, "id": TAG_IDS[t]} for t in tags]},
                           "상태": {"type": "status", "status": {"name": status, "id": STATUS_IDS[status]}},
                           "기한": {"type": "date", "date": None}}}


class NotionFixture:
    """Wire-format fixtures. Each account sees its shared sources. Force pagination on every listing."""
    names = {"team": ("회사", OWNER, uid(10)), "home": ("개인", PERSONAL, uid(11))}
    shared = {"team": {TEAM_DB: "회사 티켓", DOCS_DB: "문서 티켓"}, "home": {HOME_DB: "개인 티켓"}}

    def __init__(self):
        self.pages = {TEAM_DB: [page(101, "계정 연결", [OWNER, MEMBER], ["tp"], "할 일"),
                                page(102, "사용 설명", [MEMBER], ["문서"], "할 일"),
                                page(103, "배포 완료", [MEMBER], ["tp"], "완료"),
                                page(104, "팀 보드", [OWNER], ["tp"], "할 일"),
                                page(105, "설정 저장", [MEMBER], ["tp"], "할 일")],
                      HOME_DB: [page(201, "개인 일정", [PERSONAL], ["tp"], "할 일")],
                      DOCS_DB: [page(301, "가이드 발행", [MEMBER], ["문서"], "완료"),
                                page(302, "가이드 초안", [MEMBER], ["문서"], "할 일")]}
        self.props = {"제목": {"type": "title", "id": "title", "title": {}}, "기한": {"type": "date", "id": "due-id", "date": {}},
                      "개발 담당": {"type": "people", "id": "people-id", "people": {}},
                      "라벨": {"type": "multi_select", "id": "tag-id", "multi_select": {
                          "options": [{"id": i, "name": n} for n, i in TAG_IDS.items()]}},
                      "상태": {"type": "status", "id": "status-id", "status": {
                          "options": [{"id": i, "name": n} for n, i in STATUS_IDS.items()]}}}
        self.writes = []
        self.deny_writes = set()
        self.created = 0
        self.queries = []
        self.fail_cursor = False
        self.on_query = None
        self.after_listing = None
        self.on_markdown = None
        self.markdown = {}
        self.truncate_path = None
        self.truncate_method = None
        self.expired = False
        self.refreshes = 0

    def apply_properties(self, row, props):
        for prop_id, field in props.items():
            name, col = next((name, col) for name, col in self.props.items() if col["id"] == prop_id)
            kind = col["type"]
            value = field[kind]
            if kind == "title":
                value = [{"plain_text": t["text"]["content"]} for t in value]
            elif kind in ("select", "multi_select", "status") and value:
                options = col[kind]["options"]
                selected = []
                for option in (value if kind == "multi_select" else [value]):
                    match = next((o for o in options if option.items() <= o.items()), None)
                    if match is None:
                        assert kind != "status" and set(option) == {"name"}
                        match = {**option, "id": uid(500 + len(options))}
                        options.append(match)
                    selected.append(match.copy())
                value = selected if kind == "multi_select" else selected[0]
            row["properties"][name] = {"type": kind, kind: value}

    def respond(self, method, path, headers, body):
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
            workspace, owner, wid = self.names[name]
            return 200, {"access_token": "access-" + name, "refresh_token": "refresh-" + name,
                         "owner": {"type": "user", "user": {"id": owner, "name": "민지" if name == "team" else "하늘"}},
                         "workspace_name": workspace, "workspace_id": wid, "bot_id": uid(900)}
        token = headers["Authorization"].removeprefix("Bearer ")
        name = token.split("-")[-1]
        if name not in self.names or (self.expired and token.startswith("access-")):
            return 401, {}
        shared = self.shared[name]
        if (method, url.path) in self.deny_writes:
            return 403, {}
        if url.path == "/pages" and method == "POST":
            parent = body["parent"]
            source = parent.get("data_source_id")
            if parent["type"] != "data_source_id" or source not in shared:
                return 404, {}
            self.created += 1
            row = page(1000 + self.created, "", [], [], "할 일")
            self.apply_properties(row, body["properties"])
            self.pages[source].append(row)
            self.markdown[row["id"]] = (200, {"markdown": body.get("markdown", "")})
            self.writes.append((method, path, body))
            return 200, row
        if url.path.startswith("/pages/") and (method == "PATCH" or not url.path.endswith("/markdown")):
            page_id = url.path.split("/")[2]
            for source in shared:
                row = next((r for r in self.pages[source] if r["id"] == page_id), None)
                if row:
                    if method == "PATCH":
                        if url.path.endswith("/markdown"):
                            assert body["type"] == "replace_content"
                            self.markdown[page_id] = (200, {"markdown": body["replace_content"]["new_str"]})
                        else:
                            self.apply_properties(row, body["properties"])
                        self.writes.append((method, path, body))
                    elif method != "GET":
                        return 405, {}
                    return 200, {**row, "parent": {"type": "data_source_id", "data_source_id": source}}
            return 404, {}
        if url.path == "/comments" and method == "POST":
            self.writes.append((method, path, body))
            return 200, {"object": "comment", "id": uid(401)}
        args = body or {k: vs[0] for k, vs in parse_qs(url.query).items()}
        start = int(args.get("start_cursor", "0"))
        if url.path == "/search" and body["filter"] == {"property": "object", "value": "data_source"}:
            rows = [{"id": source, "object": "data_source", "title": [{"plain_text": title}]} for source, title in shared.items()]
        elif url.path == "/users":
            rows = [{"id": uid(900), "type": "bot"}, {"id": self.names[name][1], "name": "연결 사용자", "type": "person"},
                    {"id": MEMBER, "name": "서준", "type": "person"}]
        elif url.path.startswith("/data_sources/"):
            source, _, action = url.path.removeprefix("/data_sources/").partition("/")
            if source not in shared:
                return 404, {}
            if not action:
                return 200, {"properties": self.props}
            if action != "query":
                return 404, {}
            if self.on_query:
                fn, self.on_query = self.on_query, None
                fn()
            if start and self.fail_cursor in (True, source):
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
                    kind = "status" if "status" in f else "select"
                    rows = [r for r in rows if f[kind]["equals"] == (r["properties"][column][kind] or {}).get("name")]
        elif url.path.startswith("/pages/") and url.path.endswith("/markdown"):
            if self.on_markdown:
                fn, self.on_markdown = self.on_markdown, None
                fn()
            return self.markdown.get(url.path.split("/")[2], (200, {
                "markdown": "페이지 본문에서 가져온 작업 설명", "truncated": False, "unknown_block_ids": []}))
        else:
            return 404, {}
        result = {"results": rows[start:start + 1], "has_more": start + 1 < len(rows),
                  "next_cursor": str(start + 1) if start + 1 < len(rows) else None}
        if self.after_listing and url.path.endswith("/query") and not result["has_more"]:
            fn, self.after_listing = self.after_listing, None
            fn()
        return 200, result


class FixtureHandler(BaseHTTPRequestHandler):
    def handle_api(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, result = self.server.fixture.respond(self.command, self.path, self.headers, json.loads(data) if data else None)
        if (urlsplit(self.path).path == self.server.fixture.truncate_path
                and self.server.fixture.truncate_method in (None, self.command)):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"results":')
            return
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    do_GET = do_POST = do_PATCH = handle_api

    def log_message(self, *args):
        pass


class NotionDashboard(unittest.TestCase):
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
        self.assertEqual(params["client_id"], ["app-id"])
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

    def dispatch_log(self):
        lines = []
        pipeline.dispatch(dry_run=True, log=lines.append)
        return lines


class NotionAppTest(NotionDashboard):
    def test_server_provisioning_and_one_login_for_two_project_databases(self):
        self.assertEqual(self.post("accounts"), {"configured": False, "connections": [], "admin": True})
        self.assertEqual(self.call("connect")[0], 400)
        self.assertEqual(self.call("app", {"client_id": "old", "client_secret": "unused"})[0], 404)
        with mock.patch("sys.stdin", io.StringIO("app-secret\n")), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main(["notion-app", "--client-id", "app-id"])
        self.assertNotIn("app-secret", out.getvalue())
        self.assertEqual(self.post("accounts"), {"configured": True, "connections": [], "admin": True})

        connection = self.connect("team")
        self.assertEqual(len(self.post("accounts")["connections"]), 1)
        sources = self.post("sources", {"connection_id": connection})
        self.assertEqual({s["id"] for s in sources}, {TEAM_DB, DOCS_DB})
        team = {"connection_id": connection, "data_source_id": TEAM_DB, "people_property": "people-id",
                "tag_property": "tag-id", "tag_option": "tp-id", "status_property": "status-id", "status_option": "todo-id"}
        docs = {**team, "data_source_id": DOCS_DB, "assignee": MEMBER, "tag_option": "docs-id", "status_option": "done-id"}
        expected = {"team": {"계정 연결", "팀 보드"}, "docs": {"가이드 발행"}}
        for project, cfg in (("team", team), ("docs", docs)):
            self.assertEqual({t["title"] for t in self.preview(cfg, pid=project)}, expected[project])
            self.save(cfg, project)
        self.assertNotIn("notion_errors", pipeline.sync(log=lambda *_: None))
        for project, titles in expected.items():
            rows = self.con.execute("SELECT name FROM tickets WHERE assigned=1 AND project_gid=?", ("notion:" + project,))
            self.assertEqual({r["name"] for r in rows}, titles)

    def test_existing_app_connection_and_project_survive_update(self):
        # Records as written by the former dashboard app form and OAuth callback.
        connection = f"{uid(10)}:{OWNER}"
        for key, pair in (("oauth-app", ["app-id", "app-secret"]), (connection, ["access-team", "refresh-team"])):
            core.secret_set("ticket-pilot-notion", key, base64.b64encode(json.dumps(pair).encode()).decode())
        self.con.execute("INSERT INTO notion_connections(id, workspace_id, workspace_name, user_id, user_name) VALUES(?,?,?,?,?)",
                         (connection, uid(10), "회사", OWNER, "민지"))
        self.con.commit()
        cfg = {"connection_id": connection, "data_source_id": TEAM_DB, "assignee": MEMBER, "people_property": "people-id",
               "tag_property": "tag-id", "tag_option": "docs-id", "status_property": "status-id", "status_option": "todo-id"}
        self.save(cfg)
        accounts = self.post("accounts")
        self.assertTrue(accounts["configured"])
        self.assertEqual([c["id"] for c in accounts["connections"]], [connection])
        self.fixture.expired = True
        self.assertNotIn("notion_errors", pipeline.sync(log=lambda *_: None))
        self.assertEqual(self.fixture.refreshes, 1)
        self.assertEqual(self.stages(), {"사용 설명": "open"})
        self.assertEqual(core.project_map()["projects"][0]["notion"], cfg)


class NotionTest(NotionDashboard):
    def setUp(self):
        super().setUp()
        notion.save_app("app-id", "app-secret")
        self.team = self.connect("team")
        self.cfg = {"connection_id": self.team, "data_source_id": TEAM_DB, "people_property": "people-id",
                    "tag_property": "tag-id", "tag_option": "tp-id", "status_property": "status-id", "status_option": "todo-id"}

    def test_member_can_use_the_shared_connection_while_login_requires_admin(self):
        with mock.patch.dict(os.environ, TP_MODE="docker", TP_ADMINS="owner", TP_DOCKER_UPDATE_DIR=""), \
                mock.patch.object(server.L, "session_login", return_value="member"):
            accounts = self.post("accounts")
            self.assertFalse(accounts["admin"])
            self.assertEqual([c["id"] for c in accounts["connections"]], [self.team])
            self.assertEqual(self.call("connect")[0], 403)
            self.assertEqual(self.call("finish")[0], 403)
            self.assertEqual({t["title"] for t in self.preview(self.cfg)}, {"계정 연결", "팀 보드"})

    def test_parallel_account_authorizations_in_one_browser(self):
        flows, cookies = [], []
        for name in ("team", "home"):
            status, r, cookie = self.call("connect")
            self.assertEqual(status, 200)
            state = parse_qs(urlsplit(r["url"]).query)["state"][0]
            flows.append({"state": state, "code": name})
            cookies.append(cookie.split(";")[0])
        self.assertNotEqual(cookies[0].partition("=")[0], cookies[1].partition("=")[0])
        self.assertEqual(self.call("finish", flows[0], cookies[1])[0], 400)
        for flow in reversed(flows):
            self.assertEqual(self.call("finish", flow, "; ".join(cookies))[0], 200)
        self.assertEqual(len(self.post("accounts")["connections"]), 2)

    def test_account_scoped_schema_members_and_rotating_tokens(self):
        home = self.connect("home")
        accounts = self.post("accounts")
        self.assertEqual({r["workspace_name"] for r in accounts["connections"]}, {"회사", "개인"})
        self.assertNotIn("access-", json.dumps(accounts))
        self.assertEqual(self.post("sources", {"connection_id": self.team}), [{"name": "회사 티켓", "id": TEAM_DB},
                                                                            {"name": "문서 티켓", "id": DOCS_DB}])
        self.assertEqual(self.post("sources", {"connection_id": home}), [{"name": "개인 티켓", "id": HOME_DB}])
        self.assertEqual(self.post("people", {"connection_id": self.team})[-1], {"name": "서준", "id": MEMBER})
        properties = self.post("properties", self.cfg)
        self.assertEqual([p["name"] for p in properties], ["제목", "기한", "개발 담당", "라벨", "상태"])
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
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
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

    def test_deleted_manual_repo_matches_preview_and_dispatch(self):
        self.save(self.cfg)
        self.post("/api/config", {"updates": [
            {"scope": "global", "value": {"max_active_runs": 1}},
            {"scope": "repo.web", "value": {"path": str(self.root)}}]})
        gid, other = "notion:" + uid(101), "notion:" + uid(104)

        def ticket(ticket_gid=gid):
            return self.con.execute("SELECT * FROM tickets WHERE gid=?", (ticket_gid,)).fetchone()

        def assert_route(repos, stage):
            match = next(t for t in self.preview(self.cfg, repos) if t["gid"] == gid)
            self.assertEqual((match["stage"], match["auto_start"]), (stage, stage == "queued"))
            self.assertEqual(pipeline.compute_stage(self.con, ticket()), stage)
            starts = [s.splitlines()[0] for s in self.dispatch_log() if "would create" in s]
            expected = ticket(gid if match["auto_start"] else other)["key"].lower()
            self.assertEqual(starts, [f"--- would create app/{expected}"])

        for repos, stage in (([], "unmapped"), (["app", "web"], "needs_repo"), (["app"], "queued")):
            with self.subTest(repos=repos):
                # A ticket with no mapped repos can be manually assigned any registered repo.
                before = [*repos, "manual"] if repos else []
                project = {"source": "notion", "name": "example", "notion": self.cfg,
                           "repos": [{"repo": r, "role": r} for r in before]}
                self.post("/api/config", {"updates": [
                    {"scope": "repo.manual", "value": {"path": str(self.root)}},
                    {"scope": "project.example", "value": project}]})
                pipeline.sync(log=lambda *_: None)
                self.post("/api/assign", {"ticket": gid, "repo": "manual"})
                self.post("/api/assign", {"ticket": other, "repo": "app"})
                self.con.execute("UPDATE tickets SET due_on='2000-01-01' WHERE gid=?", (gid,))
                self.con.commit()
                pipeline.sync(log=lambda *_: None)
                self.assertEqual(ticket()["repo_override"], "manual")
                self.assertTrue(next(t for t in self.preview(self.cfg, before) if t["gid"] == gid)["auto_start"])

                self.post("/api/config", {"scope": "repo.manual", "value": None})
                self.assertIsNone(ticket()["repo_override"])
                assert_route(repos, stage)
                # A database from before the fix may still have the deleted override and its old queued stage.
                self.con.execute("UPDATE tickets SET repo_override='manual', stage='queued' WHERE gid=?", (gid,))
                self.con.commit()
                assert_route(repos, stage)
                self.assertEqual(ticket()["repo_override"], "manual")
                pipeline.sync(log=lambda *_: None)
                self.assertIsNone(ticket()["repo_override"])
                self.assertEqual(ticket()["stage"], stage)
                assert_route(repos, stage)

    def test_changed_source_does_not_dispatch_or_reserve_an_old_page(self):
        self.save(self.cfg, "first")
        pipeline.sync(log=lambda *_: None)
        self.save({**self.cfg, "data_source_id": HOME_DB}, "first")  # team account cannot read the new source
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        self.save(self.cfg, "second")
        preview = self.preview(self.cfg, pid="second")
        self.assertTrue(all(t["auto_start"] for t in preview))
        self.assertIn("first", pipeline.sync(log=lambda *_: None)["notion_errors"])
        self.assertEqual(self.stages(), {t["title"]: "queued" for t in preview})
        owners = {r[0] for r in self.con.execute("SELECT project_gid FROM tickets WHERE assigned=1")}
        self.assertEqual(owners, {"notion:second"})
        self.assertEqual(sum("would create" in s for s in self.dispatch_log()), 2)

    def test_filter_edit_during_query_discards_outdated_results(self):
        self.save(self.cfg)
        pipeline.sync(log=lambda *_: None)
        change = {**self.cfg, "assignee": MEMBER}
        self.fixture.on_query = lambda: self.save(change)
        self.assertIn("example", pipeline.sync(log=lambda *_: None)["notion_errors"])
        self.assertEqual(self.stages(), {})
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.stages(), {"설정 저장": "queued", "계정 연결": "queued"})

    def test_older_sync_finishing_last_does_not_requeue_removed_page(self):
        self.save(self.cfg)

        def newer_sync():
            self.fixture.pages[TEAM_DB].pop(0)
            pipeline.sync(log=lambda *_: None)

        self.fixture.after_listing = newer_sync
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(self.stages(), {"팀 보드": "queued"})

    def test_filter_change_and_sync_during_markdown_read(self):
        self.save(self.cfg)
        pipeline.sync(log=lambda *_: None)

        def new_filter():
            self.save({**self.cfg, "assignee": "all", "status_option": "done-id"})
            pipeline.sync(log=lambda *_: None)

        self.fixture.on_markdown = new_filter
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        self.assertEqual(self.stages(), {"배포 완료": "queued"})
        self.assertTrue(any("배포 완료" in s for s in self.dispatch_log() if "would create" in s))

    def test_interrupted_http_body_is_scoped_to_its_project(self):
        self.save(self.cfg)
        home = {**self.cfg, "connection_id": self.connect("home"), "data_source_id": HOME_DB}
        self.save(home, "second")
        self.fixture.truncate_path = f"/data_sources/{TEAM_DB}/query"
        self.assertIn("example", pipeline.sync(log=lambda *_: None)["notion_errors"])
        self.assertEqual(self.stages(), {"개인 일정": "queued"})

    def test_page_moved_between_databases_wins_over_an_old_failed_listing(self):
        self.save({**self.cfg, "assignee": "all"}, "first")
        pipeline.sync(log=lambda *_: None)
        home = {**self.cfg, "connection_id": self.connect("home"), "data_source_id": HOME_DB, "assignee": "all"}
        self.save(home, "second")
        self.fixture.pages[HOME_DB].append(self.fixture.pages[TEAM_DB].pop(0))
        self.fixture.fail_cursor = TEAM_DB
        preview = self.preview(home, pid="second")
        self.assertEqual({t["title"] for t in preview if t["auto_start"]}, {"계정 연결", "개인 일정"})
        self.assertIn("first", pipeline.sync(log=lambda *_: None)["notion_errors"])
        rows = self.con.execute("SELECT name FROM tickets WHERE project_gid='notion:second' AND stage='queued'")
        self.assertEqual({r[0] for r in rows}, {"계정 연결", "개인 일정"})

    def test_unreadable_first_page_leaves_the_slot_for_the_next_ticket(self):
        self.save(self.cfg)
        self.post("/api/config", {"scope": "global", "value": {"max_active_runs": 1}})
        pipeline.sync(log=lambda *_: None)
        self.fixture.markdown[uid(101)] = (403, {})
        self.con.execute("UPDATE tickets SET due_on='2000-01-01' WHERE gid=?", ("notion:" + uid(101),))
        self.con.commit()
        lines = self.dispatch_log()
        self.assertTrue(any("Notion HTTP 403" in s for s in lines), lines)
        starts = [s for s in lines if "would create" in s]
        self.assertEqual(len(starts), 1)
        self.assertIn("팀 보드", starts[0])
        self.fixture.truncate_path = f"/pages/{uid(101)}/markdown"
        starts = [s for s in self.dispatch_log() if "would create" in s]
        self.assertEqual(len(starts), 1)
        self.assertIn("팀 보드", starts[0])

    def test_dispatch_recovers_nested_markdown_blocks(self):
        self.save(self.cfg)
        pipeline.sync(log=lambda *_: None)
        root, child, leaf = uid(101), uid(501), uid(502)
        self.fixture.markdown = {
            root: (200, {"markdown": "본문 요약", "truncated": True, "unknown_block_ids": [child, leaf]}),
            child: (200, {"markdown": "하위 요구사항", "truncated": True, "unknown_block_ids": [leaf]}),
            leaf: (200, {"markdown": "세부 완료 기준", "truncated": False, "unknown_block_ids": []}),
        }
        brief = next(s for s in self.dispatch_log() if "would create" in s and "본문 요약" in s)
        self.assertIn("하위 요구사항", brief)
        self.assertEqual(brief.count("세부 완료 기준"), 1)

    def test_incomplete_markdown_holds_only_the_affected_ticket(self):
        self.save(self.cfg)
        self.post("/api/config", {"scope": "global", "value": {"max_active_runs": 1}})
        pipeline.sync(log=lambda *_: None)
        root, child = uid(101), uid(501)
        self.con.execute("UPDATE tickets SET due_on='2000-01-01' WHERE gid=?", ("notion:" + root,))
        self.con.commit()
        cycle = {"markdown": "하위 블록", "truncated": True, "unknown_block_ids": [root]}
        cases = [("HTTP 404", [child], {child: (404, {})}), ("블록 ID", [], {}), ("반복", [child], {child: (200, cycle)})]
        for reason, unknown, blocks in cases:
            with self.subTest(reason=reason):
                self.fixture.markdown = {
                    root: (200, {"markdown": "일부만 읽은 본문", "truncated": True, "unknown_block_ids": unknown}),
                    **blocks,
                }
                lines = self.dispatch_log()
                self.assertTrue(any("보류" in s and reason in s for s in lines if "would create" not in s))
                starts = [s for s in lines if "would create" in s]
                self.assertEqual(len(starts), 1)
                self.assertIn("팀 보드", starts[0])

    def test_select_columns_and_renamed_options_use_stored_ids(self):
        for old, new, kind in (("라벨", "분류", "multi_select"), ("상태", "진행", "status")):
            prop = self.fixture.props.pop(old)
            options = prop[kind]["options"]
            if old == "상태":
                options[0]["name"] = "예정"
            self.fixture.props[new] = {"id": prop["id"], "type": "select", "select": {"options": options}}
            for row in self.fixture.pages[TEAM_DB]:
                value = row["properties"].pop(old)[kind]
                selected = value[0] if isinstance(value, list) else value
                row["properties"][new] = {"type": "select", "select": {
                    "name": "예정" if selected["name"] == "할 일" else selected["name"]}}
        tickets = self.preview(self.cfg)
        self.assertEqual([t["title"] for t in tickets], ["계정 연결", "팀 보드"])
        self.assertTrue(all(t["auto_start"] for t in tickets))
        self.assertEqual(self.fixture.queries[-1][1][1:], [{"property": "분류", "select": {"equals": "tp"}},
                                                          {"property": "진행", "select": {"equals": "예정"}}])

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
