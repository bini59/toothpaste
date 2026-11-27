"""Dashboard routing through real HTTP requests against a temporary database."""
import http.client
import json
import os
import queue
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from ticket_pilot import chat, config, core, server
from ticket_pilot.http_api import operations
from ticket_pilot.storage import connection


class DashboardHTTPTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, TP_MODE="host"))
        self.enterContext(mock.patch.object(core, "DB_PATH", self.root / "tp.db"))
        self.enterContext(mock.patch.object(connection, "_main_connections", {}))
        self.enterContext(mock.patch.object(server, "ROOT", self.root))
        self.enterContext(mock.patch.object(operations, "ROOT", self.root))
        self.enterContext(mock.patch.object(chat, "DATA", self.root))
        self.enterContext(mock.patch.object(config, "backup_dir", return_value=self.root / "backups"))
        core.config_cache_clear()
        self.addCleanup(core.config_cache_clear)
        config.update_many([
            {"scope": "global", "value": {"owner_name": "Owner", "asana_enabled": False}},
            {"scope": "repo.dashboard", "value": {"path": str(self.root), "github": "example/dashboard"}},
            {"scope": "project.demo", "value": {"name": "Demo", "source": "github",
                                               "repos": [{"repo": "dashboard", "role": "app"}]}},
        ], actor="cli")
        self.con = core.db()
        self.addCleanup(core.close_db, self.con)

        self.actors = queue.Queue()
        actors = self.actors

        class Handler(server.H):
            def finish(self):
                actors.put(core.current_actor())
                super().finish()

        self.srv = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    def call(self, method, path, body=None, **headers):
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        with closing(http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)) as conn:
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            raw = response.read()
            data = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, data, dict(response.getheaders())

    def test_chat_projects_tickets_and_monitoring(self):
        code, data, _ = self.call("GET", "/api/projects")
        self.assertEqual(code, 200)
        self.assertIn("demo", {project["id"] for project in data["projects"]})
        code, data, _ = self.call("POST", "/api/chat/new", {"project": "demo"})
        self.assertEqual(code, 200)
        ident = data["id"]
        code, data, _ = self.call("GET", f"/api/chat/messages?id={ident}")
        self.assertEqual(code, 200)
        self.assertEqual(data["conversation"]["id"], ident)
        self.assertEqual(data["messages"], [])

        self.con.execute("INSERT INTO tickets(gid,key,name,assigned) VALUES ('demo-1','DEMO-1','Sample',1)")
        self.con.commit()
        self.assertEqual(self.call("POST", "/api/skip", {"ticket": "DEMO-1"})[:2], (200, {"ok": True}))
        code, data, _ = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(next(row for row in data["tickets"] if row["gid"] == "demo-1")["skip"], 1)

        code, data, _ = self.call("POST", "/api/metrics/mark", {"label": "Snapshot"})
        self.assertEqual(code, 201)
        self.assertEqual(data["label"], "Snapshot")
        self.assertEqual(self.call("GET", "/api/metrics?days=7")[0], 200)
        self.assertEqual(self.call("GET", "/api/metrics?days=bad")[0], 400)
        self.assertEqual(self.call("GET", "/api/reviews")[:2], (200, {"reviews": []}))

    def test_methods_and_query_matching(self):
        for method, path in (
            ("GET", "/api/chat/messages"), ("GET", "/api/state?unused=1"),
            ("GET", "/api/lan/me?unused=1"), ("GET", "/api/update/health"),
            ("GET", "/api/missing"), ("POST", "/api/reviews"),
            ("POST", "/api/chat/new?unused=1"), ("POST", "/api/notion/missing"),
            ("POST", "/notion/callback"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path)[:2], (404, {"error": "not found"}))
        self.assertEqual(self.call("GET", "/api/chat/compactions?")[:2], (200, {"rows": []}))
        self.assertEqual(self.call("GET", "/api/onboard/clone?id=bad")[:2], (404, {"error": "not found"}))

    def test_callbacks_static_assets_and_downloads(self):
        dist = self.root / "web" / "dist"
        self.assertEqual(self.call("GET", "/")[0], 503)
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_bytes(b"<html>dashboard</html>")
        (dist / "assets" / "app.js").write_bytes(b"console.log('dashboard');")
        (dist.parent / "private.txt").write_bytes(b"outside")
        for path in ("/", "/notion/callback?code=fixture&state=fixture", "/../private.txt"):
            code, raw, headers = self.call("GET", path)
            self.assertEqual((code, raw, headers["Cache-Control"]), (200, b"<html>dashboard</html>", "no-store"))
        code, raw, headers = self.call("GET", "/assets/app.js?v=1")
        self.assertEqual((code, raw), (200, b"console.log('dashboard');"))
        self.assertIn("immutable", headers["Cache-Control"])

        code, data, headers = self.call("GET", "/api/config/export")
        self.assertEqual((code, data), (200, config.export()))
        self.assertTrue(headers["Content-Disposition"].startswith('attachment; filename="ticket-pilot-config-'))
        self.assertEqual(headers["Cache-Control"], "no-store")

        ident = self.call("POST", "/api/chat/new", {"project": "demo"})[1]["id"]
        directory = chat._dir(ident)
        directory.mkdir(parents=True)
        for filename, content_type, disposition in (
            ("key-note.txt", "text/plain; charset=utf-8", "inline"),
            ("key-그림.svg", "application/octet-stream", "attachment"),
        ):
            with self.subTest(filename=filename):
                (directory / filename).write_bytes(b"attachment bytes")
                code, data, headers = self.call("GET", "/api/chat/file?" + urlencode({"id": ident, "name": filename}))
                self.assertEqual((code, data), (200, b"attachment bytes"))
                self.assertEqual(headers["Content-Type"], content_type)
                self.assertTrue(headers["Content-Disposition"].startswith(f"{disposition}; filename*=UTF-8''"))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Cache-Control"], "private, max-age=86400")
        self.assertEqual(self.call("GET", f"/api/chat/file?id={ident}&name=../private.txt")[0], 404)

    def test_docker_probe_precedes_maintenance_and_session_gate(self):
        with mock.patch.dict(os.environ, TP_MODE="docker"), mock.patch.object(server.DU, "maintenance", return_value=True):
            self.assertEqual(self.call("GET", "/api/update/health")[:2], (503, {"ready": False}))
            dist = self.root / "web" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").touch()
            self.assertEqual(self.call("GET", "/api/update/health")[:2], (200, {"ready": True}))
            self.assertEqual(self.call("GET", "/api/update/health?unused=1")[0], 503)
            self.assertEqual(self.call("GET", "/api/lan/me")[0], 503)
            self.assertEqual(self.call("POST", "/api/lan/login", {})[0], 503)

    def test_actor_stays_through_event_and_clears_after_each_request(self):
        self.assertEqual(self.call("GET", "/api/lan/me")[1]["actor"], "Owner")
        self.assertIsNone(self.actors.get(timeout=2))
        self.assertEqual(self.call("POST", "/api/view", {"tab": "board"})[0], 200)
        self.assertIsNone(self.actors.get(timeout=2))
        self.assertEqual(self.call("POST", "/api/assign", {})[0], 500)
        self.assertIsNone(self.actors.get(timeout=2))
        self.assertEqual(self.call("POST", "/api/view", {"tab": "board"}, Origin="https://other.example")[0], 403)
        self.assertIsNone(self.actors.get(timeout=2))
        row = self.con.execute("SELECT message, actor FROM events WHERE kind='view'").fetchone()
        self.assertEqual(tuple(row), ("board", "Owner"))
