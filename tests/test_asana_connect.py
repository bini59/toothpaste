"""Asana 연결 (TP-115): the 설정 page stores a pasted PAT in the keychain after checking it against users/me.
Run: python3 -m unittest discover tests"""
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock

from ticket_pilot import config as G, core, onboard as OB, pipeline as P
from ticket_pilot import server as S

# built at runtime so secret scanners do not take the fake fixture for a real Asana PAT
PAT = "2/1200000000000001/" + "1200000000000002:" + "0123456789abcdef" * 2
ME = b'{"data": {"gid": "1", "name": "Alex", "email": "alex@example.com", "workspaces": [{"gid": "77", "name": "Acme"}]}}'
ME2 = (b'{"data": {"gid": "1", "name": "Alex", "email": "alex@example.com", '
       b'"workspaces": [{"gid": "77", "name": "Acme"}, {"gid": "88", "name": "Home"}]}}')
WS1 = [{"gid": "77", "name": "Acme"}]


def _resp(body: bytes):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = body
    return r


class AsanaConnectTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        # a throwaway DB: settings are the code defaults (asana_workspace_gid ""), saves go through config.save
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mock.patch.object(core, "DB_PATH", core.Path(tmp.name) / "tp.db").start()
        mock.patch.object(core, "_CON", None).start()
        core.config_cache_clear()
        self.addCleanup(core.config_cache_clear)
        mock.patch.dict(os.environ).start()
        os.environ.pop("ASANA_PAT", None)
        self.kc = {}  # (service, account) -> token, stands in for the keychain
        mock.patch.object(core, "KEYCHAIN", True).start()  # the macOS path on every OS; `security` is faked below
        mock.patch.object(core, "secret_get", side_effect=lambda s, a=None: self.kc.get((s, a or "asana"))).start()
        self.sp = mock.patch.object(S.subprocess, "run", side_effect=self._security).start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def _security(self, argv, input=None, **kw):
        if argv == ["security", "-i"]:
            w = input.split()
            self.kc[(w[w.index("-s") + 1], w[w.index("-a") + 1])] = w[w.index("-w") + 1]
        elif argv[1] == "delete-generic-password":
            self.kc.pop((argv[3], argv[5]), None)
        return mock.Mock(returncode=0)

    def _call(self, method, path, body=None, origin=None):
        # http.client, not urllib: the tests mock urllib.request.urlopen for the Asana side
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        self.addCleanup(c.close)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
        r = c.getresponse()
        return r.status, r.read().decode()

    def test_save_checks_then_stores_via_stdin(self):
        with mock.patch.object(S.urllib.request, "urlopen", return_value=_resp(ME)) as op:
            me = S.asana_save(PAT)
        self.assertEqual(me, {"connected": True, "name": "Alex", "email": "alex@example.com", "workspaces": WS1,
                              "workspace": "77"})
        self.assertIn("workspaces.name", op.call_args.args[0].full_url)
        self.assertEqual(op.call_args.args[0].get_header("Authorization"), f"Bearer {PAT}")
        self.assertEqual(self.sp.call_args.args[0], ["security", "-i"])  # token not in argv
        self.assertEqual(self.kc[("ticket-pilot-asana", "asana")], PAT)
        self.assertEqual(core.asana_token(), PAT)  # what the next scan reads

    def test_rejected_pat_keeps_old_token(self):
        self.kc[("ticket-pilot-asana", "asana")] = "old"
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch.object(S.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "거절") as e:
                S.asana_save(PAT)
        self.assertNotIn(PAT, str(e.exception))
        self.assertEqual(self.kc[("ticket-pilot-asana", "asana")], "old")
        self.sp.assert_not_called()

    def test_rejects_odd_characters_before_any_call(self):
        with mock.patch.object(S.urllib.request, "urlopen") as op:
            for bad in ("", None, 123, "short", PAT + " -s x", PAT + "\nadd-generic-password", "-" + PAT, "a'" + PAT):
                with self.assertRaisesRegex(RuntimeError, "형식"):
                    S.asana_save(bad)
            op.assert_not_called()
        self.sp.assert_not_called()

    def test_routes(self):
        with mock.patch.object(S.urllib.request, "urlopen", return_value=_resp(ME)):
            self.assertEqual(json.loads(self._call("GET", "/api/asana/status")[1]), {"connected": False})
            code, out = self._call("POST", "/api/asana/token", {"token": PAT}, origin="http://127.0.0.1:3032")
            self.assertEqual((code, json.loads(out)["name"]), (200, "Alex"))
            self.assertNotIn(PAT, out)
            code, out = self._call("GET", "/api/asana/status")
            self.assertEqual(json.loads(out), {"connected": True, "name": "Alex", "email": "alex@example.com",
                                               "workspaces": WS1, "workspace": "77"})
            self.assertNotIn(PAT, out)
        self.assertEqual(self._call("POST", "/api/asana/disconnect", {})[0], 200)
        self.assertNotIn(("ticket-pilot-asana", "asana"), self.kc)
        with self.assertRaisesRegex(core.AsanaError, "대시보드 설정에서 Asana PAT를 등록하세요"):
            core.asana_token()

    def _ws(self):
        return core.settings()["asana_workspace_gid"]

    def test_one_workspace_is_chosen_when_empty(self):
        with mock.patch.object(S.urllib.request, "urlopen", return_value=_resp(ME)):
            S.asana_save(PAT)
        self.assertEqual(self._ws(), "77")
        con = core.db()
        row = con.execute("SELECT actor, key, old, new FROM settings_history").fetchone()
        self.assertEqual(tuple(row), ("dashboard", "asana_workspace_gid", '""', '"77"'))

    def test_chosen_workspace_is_kept(self):
        G.update("global", {"asana_workspace_gid": "55"}, actor="cli")
        with mock.patch.object(S.urllib.request, "urlopen", return_value=_resp(ME)):
            self.assertEqual(S.asana_save(PAT)["workspace"], "55")
        self.assertEqual(self._ws(), "55")

    def test_several_workspaces_are_listed_not_chosen(self):
        with mock.patch.object(S.urllib.request, "urlopen", return_value=_resp(ME2)):
            S.asana_save(PAT)
            st = S.asana_status()
        self.assertEqual(self._ws(), "")
        self.assertEqual((st["workspace"], [w["name"] for w in st["workspaces"]]), ("", ["Acme", "Home"]))
        self.assertNotIn(PAT, json.dumps(st))

    def test_pick_from_dashboard_applies_on_next_read(self):
        self.assertEqual(self._ws(), "")
        code, _ = self._call("POST", "/api/config", {"scope": "global", "value": {"asana_workspace_gid": "88"}},
                             origin="http://127.0.0.1:3032")
        self.assertEqual(code, 200)  # no longer read-only on the dashboard
        self.assertEqual(self._ws(), "88")  # settings() right away, no restart
        self.assertEqual(core.asana_workspace(), "88")

    def test_name_saved_after_pat_keeps_workspace(self):
        # 시작하기 order: PAT (auto-picks 77), then 이름 저장 sends only owner_name (Start.tsx saveName)
        with mock.patch.object(S.urllib.request, "urlopen", return_value=_resp(ME)):
            S.asana_save(PAT)
        code, _ = self._call("POST", "/api/config", {"scope": "global", "value": {"owner_name": "Alex"}},
                             origin="http://127.0.0.1:3032")
        self.assertEqual(code, 200)
        self.assertEqual(self._ws(), "77")

    def test_empty_workspace_does_not_block_github_sync(self):
        with mock.patch.object(P, "sync_issues") as github:
            result = P.sync(log=lambda *a: None)
        self.assertIn("워크스페이스", result["asana_error"])
        github.assert_called_once()
        self.assertIn("설정 → Asana 연결에서 워크스페이스를 고르세요", OB.asana_projects(refresh=True)["error"])

    def test_github_issue_is_imported_without_asana_workspace(self):
        con = core.db()
        issue = {"number": 1, "title": "mobile", "body": "", "url": "https://example.test/1",
                 "updatedAt": "now", "labels": [{"name": "tp"}]}
        sources = {"repo": ("REPO", "tp")}
        with mock.patch.object(P, "issue_repos", return_value=sources), \
             mock.patch.object(P, "_gh_issue_json", return_value=[issue]), \
             mock.patch.object(P, "load_map"):
            con.execute("INSERT INTO repos(name, github) VALUES ('repo', 'owner/repo')")
            con.commit()
            result = P.sync(log=lambda *a: None)
        self.assertIn("asana_error", result)
        self.assertEqual(con.execute("SELECT key FROM tickets WHERE gid='gh:owner/repo#1'").fetchone()[0], "REPO-1")

    def test_disabled_asana_preserves_existing_tickets_and_syncs_github(self):
        G.update("global", {"asana_enabled": False}, actor="cli")
        con = core.db()
        con.execute("INSERT INTO tickets(gid,key,name,assigned,completed) VALUES ('123','AS-123','existing',1,0)")
        con.commit()
        with mock.patch.object(P, "asana_all") as api, mock.patch.object(P, "sync_issues") as github:
            result = P.sync(log=lambda *a: None)
        api.assert_not_called()
        github.assert_called_once()
        self.assertNotIn("asana_error", result)
        self.assertEqual(tuple(con.execute("SELECT assigned,completed FROM tickets WHERE gid='123'").fetchone()), (1, 0))
        self.assertEqual(S.asana_status(), {"connected": False, "enabled": False})
        with self.assertRaisesRegex(core.AsanaError, "꺼져"):
            core.asana("GET", "/users/me")

    def test_asana_failure_rolls_back_partial_changes(self):
        G.update("global", {"asana_workspace_gid": "1"}, actor="cli")
        con = core.db()
        con.execute("INSERT INTO tickets(gid,key,name,assigned,completed) VALUES ('123','AS-123','existing',1,0)")
        con.commit()
        def partial(con, st, mapped, names, hidden, ts, tagged):
            con.execute("UPDATE tickets SET assigned=0 WHERE gid='123'")
            tagged.append("AS-123")
            raise core.AsanaError("offline")
        with mock.patch.object(P, "_sync_asana", side_effect=partial), mock.patch.object(P, "sync_issues") as github:
            result = P.sync(log=lambda *a: None)
        self.assertEqual(result["asana_error"], "offline")
        self.assertEqual(result["new_tagged"], [])
        self.assertEqual(con.execute("SELECT assigned FROM tickets WHERE gid='123'").fetchone()[0], 1)
        github.assert_called_once()

    def test_foreign_origin_is_forbidden(self):
        self.kc[("ticket-pilot-asana", "asana")] = "old"
        for path, body in (("/api/asana/token", {"token": PAT}), ("/api/asana/disconnect", {})):
            self.assertEqual(self._call("POST", path, body, origin="https://evil.example")[0], 403)
        self.assertEqual(self.kc[("ticket-pilot-asana", "asana")], "old")
        self.sp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
