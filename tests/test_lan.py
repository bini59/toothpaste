"""로컬망 공유 (TP-152): private-network access with a GitHub login (org members), the actor on every action, and
launchd/install.sh binding the dashboard to 0.0.0.0 only with lan_share on. GitHub is faked.
Run: python3 -m unittest discover tests"""
import http.client
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ticket_pilot import config as G, core, lan as L, release as R
from ticket_pilot import server as S, setup as SU, usage
from ticket_pilot.http_api import connections
from ticket_pilot.storage import connection

ROOT = Path(__file__).resolve().parent.parent
LAN_IP = "192.168.0.23"


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


class LanTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        mock.patch.object(core, "DB_PATH", self.tmp / "tp.db").start()
        mock.patch.object(connection, "_main_connections", {}).start()
        # /api/state would start a real claude/codex usage fetch in a thread that outlives the test and writes to
        # whatever DB a later test has patched in (it recreated test_migrate's moved data/tp.db)
        mock.patch.object(usage, "cached", lambda con, key="claude_usage", fn=None: core.meta_get(con, key)).start()
        mock.patch.object(G, "backup_dir", lambda: self.tmp / "backups").start()
        core.config_cache_clear()
        self.addCleanup(core.config_cache_clear)
        L._pending.clear()
        L._hits.clear()
        G.update("global", {"lan_share": {"enabled": True, "org": "example-org"}, "owner_name": "Owner"}, actor="cli")
        self.ip = ["127.0.0.1"]
        ip = self.ip

        class Handler(S.H):  # the client address the gate sees
            def setup(self):
                super().setup()
                self.client_address = (ip[0], self.client_address[1])

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.host = f"127.0.0.1:{self.srv.server_port}"

    def call(self, method, path, body=None, ip=LAN_IP, cookie=None, origin="same", headers=None):
        self.ip[0] = ip
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        self.addCleanup(c.close)
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = f"http://{self.host}" if origin == "same" else origin
        if cookie:
            h["Cookie"] = f"{L.COOKIE}={cookie}"
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read().decode()
        try:
            out = json.loads(raw)
        except ValueError:
            out = raw
        return r.status, out, r.getheader("Set-Cookie")

    def session(self, login="alice"):
        return L.new_session(login, "example-org", LAN_IP)

    def test_classify(self):
        for a, want in [("127.0.0.1", "local"), ("::1", "local"), ("::ffff:127.0.0.1", "local"), ("10.1.2.3", "lan"),
                        ("172.20.0.5", "lan"), ("192.168.1.9", "lan"), ("fd00::1", "lan"), ("fe80::1%en0", "lan"),
                        ("::ffff:192.168.1.9", "lan"), ("8.8.8.8", None), ("172.32.0.1", None), ("100.64.0.1", None),
                        ("2001:db8::1", None), ("junk", None)]:
            self.assertEqual(L.classify(a), want, a)

    def test_public_ip_is_refused(self):
        self.assertEqual(self.call("GET", "/api/state", ip="8.8.8.8", cookie=self.session())[0], 403)
        self.assertEqual(self.call("GET", "/", ip="8.8.8.8")[0], 403)

    def test_lan_refused_while_sharing_is_off(self):
        sid = self.session()
        G.update("global", {"lan_share": {"enabled": False, "org": "example-org"}}, actor="cli")
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 403)

    def test_no_cookie_401_but_login_screen_loads(self):
        for path in ("/api/state", "/api/reviews", "/api/events"):
            self.assertEqual(self.call("GET", path)[0], 401, path)
        self.assertEqual(self.call("GET", "/api/config")[0], 401)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "board"})[0], 401)
        self.assertEqual(self.call("GET", "/api/state", cookie="forged")[0], 401)
        code, me, _ = self.call("GET", "/api/lan/me")
        self.assertEqual((code, me), (200, {"local": False, "login": None, "actor": None}))
        self.assertNotIn(self.call("GET", "/")[0], (401, 403))  # the app bundle (or 503 without web/dist)

    def test_valid_cookie_passes(self):
        sid = self.session()
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 200)
        self.assertEqual(self.call("GET", "/api/lan/me", cookie=sid)[1]["login"], "alice")

    def test_monitoring_endpoints_keep_review_bodies_and_the_recent_non_view_events(self):
        sid = self.session()
        with sqlite3.connect(core.DB_PATH) as con:
            con.executemany("INSERT INTO reviews(summary,body) VALUES (?,?)", [("First", "Body A"), ("Second", "Body B")])
            con.executemany("INSERT INTO events(kind,message) VALUES ('review',?)", [(str(i),) for i in range(510)])
            con.execute("INSERT INTO events(kind,message) VALUES ('view','latest tab visit')")

        code, data, _ = self.call("GET", "/api/reviews", cookie=sid)
        self.assertEqual(code, 200)
        self.assertEqual([r["body"] for r in data["reviews"]], ["Body B", "Body A"])
        code, data, _ = self.call("GET", "/api/events", cookie=sid)
        self.assertEqual(code, 200)
        self.assertEqual([e["message"] for e in data["events"]], [str(i) for i in range(509, 9, -1)])

    def test_session_cookie_among_foreign_cookies(self):  # TP-178
        sid = self.session()
        jar = '__sec_id={"a":"b,c"}; _ga=GA1.1.1'
        for cookie, want in [(f"{jar}; {L.COOKIE}={sid}", 200), (f"{jar};  {L.COOKIE} = {sid} ; x=1", 200),
                             (jar, 401), (f"{jar}; {L.COOKIE}=", 401), (f"{jar}; {L.COOKIE}= ", 401)]:
            self.assertEqual(self.call("GET", "/api/state", headers={"Cookie": cookie})[0], want, cookie)

    def test_post_needs_matching_origin(self):
        sid = self.session()
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, cookie=sid, origin=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, cookie=sid, origin="http://evil.example")[0], 403)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, cookie=sid,
                                   origin=f"http://{LAN_IP}:{self.srv.server_port}")[0], 403)  # != Host
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, cookie=sid)[0], 200)

    def test_localhost_unchanged(self):
        code, me, _ = self.call("GET", "/api/lan/me", ip="127.0.0.1")
        self.assertEqual((code, me), (200, {"local": True, "login": None, "actor": "Owner"}))
        self.assertEqual(self.call("GET", "/api/state", ip="127.0.0.1")[0], 200)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip="127.0.0.1", origin=None)[0], 200)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip="127.0.0.1", origin="http://evil.example")[0], 403)

    def test_lan_user_cannot_change_lan_share(self):
        sid = self.session()
        for body in ({"scope": "global", "value": {"lan_share": {"enabled": False, "org": "example-org"}}},
                     {"scope": "global", "value": {"lan_share": {"enabled": True, "org": "other"}}, "dry_run": True},
                     {"updates": [{"scope": "global", "value": {"lan_share": None}}]}):
            self.assertEqual(self.call("POST", "/api/config", body, cookie=sid)[0], 403, body)
        doc = G.export()
        doc["settings"].pop("lan_share")  # an import that drops it would turn sharing off
        self.assertEqual(self.call("POST", "/api/config/import", {"doc": doc}, cookie=sid)[0], 403)
        self.assertEqual(self.call("POST", "/api/lan/share", {"enabled": False, "org": "x"}, cookie=sid)[0], 403)
        self.assertEqual(self.call("POST", "/api/lan/sessions/delete", {"id": 1}, cookie=sid)[0], 403)
        self.assertTrue(core.settings()["lan_share"]["enabled"])
        # other settings are fine, recorded with the GitHub login
        code, out, _ = self.call("POST", "/api/config", {"scope": "global", "value": {"max_active_runs": 5}}, cookie=sid)
        self.assertEqual(code, 200, out)
        con = core.db()
        self.assertEqual(tuple(con.execute("SELECT actor, key FROM settings_history ORDER BY id DESC").fetchone()),
                         ("alice", "max_active_runs"))
        self.assertEqual(con.execute("SELECT actor FROM events WHERE kind='config' ORDER BY id DESC").fetchone()[0], "alice")

    def test_read_only_keys_stay_read_only_whatever_the_login(self):
        sid = self.session("cli")  # a GitHub login can be any name
        code, out, _ = self.call("POST", "/api/config", {"scope": "global", "value": {"dashboard_port": 4000}}, cookie=sid)
        self.assertEqual(code, 400, out)
        self.assertEqual(core.settings()["dashboard_port"], 3032)

    def test_localhost_turns_sharing_off_and_actor_is_owner(self):
        code, out, _ = self.call("POST", "/api/lan/share", {"enabled": True, "org": "other-org"}, ip="127.0.0.1")
        self.assertEqual((code, out), (200, {"ok": True, "restart": False}))  # org only: no restart
        self.assertEqual(core.settings()["lan_share"], {"enabled": True, "org": "other-org"})
        self.assertEqual(self.call("POST", "/api/lan/share", {"enabled": True, "org": "bad org!"}, ip="127.0.0.1")[0], 400)
        # no default org (open-source default): off may keep it empty, turning sharing on needs one
        self.assertEqual(self.call("POST", "/api/lan/share", {"enabled": False, "org": ""}, ip="127.0.0.1")[0], 200)
        self.assertEqual(self.call("POST", "/api/lan/share", {"enabled": True, "org": " "}, ip="127.0.0.1")[0], 400)
        con = core.db()
        self.assertEqual(con.execute("SELECT actor FROM settings_history ORDER BY id DESC").fetchone()[0], "Owner")

    def test_sharing_toggle_reruns_install_only_for_this_checkouts_launchd(self):
        mock.patch.object(connections, "_plist_dir", lambda: self.tmp).start()
        mock.patch.object(SU, "UNIT", self.tmp / "ticket-pilot-dashboard.service").start()  # Linux
        mock.patch.dict(os.environ, {"TP_LOG_DIR": str(self.tmp / "logs")}).start()
        pop = mock.patch.object(subprocess, "Popen").start()
        r = S.lan_share_set(False, "example-org", "Owner")
        self.assertFalse(r["restart"])  # no plist of this checkout: a dev server
        pop.assert_not_called()
        (self.tmp / "com.ticket-pilot.dashboard.plist").write_bytes(plistlib.dumps({"WorkingDirectory": str(ROOT)}))
        (self.tmp / "ticket-pilot-dashboard.service").write_text(f"[Service]\nWorkingDirectory={ROOT}\n")
        self.assertEqual(S.lan_share_set(True, "example-org", "Owner"), {"ok": True, "restart": True})
        self.assertEqual(pop.call_args.args[0], ["/bin/sh", str(ROOT / "launchd" / "install.sh")])
        self.assertTrue(pop.call_args.kwargs["start_new_session"])

    def test_session_lists_and_force_logout_from_localhost(self):
        sid = self.session()
        st = self.call("GET", "/api/lan/status", cookie=sid)[1]
        self.assertEqual([s["login"] for s in st["sessions"]], ["alice"])
        self.assertNotIn(sid, json.dumps(st))
        self.assertNotIn(L._hash(sid), json.dumps(st))
        self.assertEqual(self.call("POST", "/api/lan/sessions/delete", {"id": st["sessions"][0]["id"]}, ip="127.0.0.1")[0], 200)
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 401)

    def test_logout(self):
        sid = self.session()
        code, _, ck = self.call("POST", "/api/lan/logout", {}, cookie=sid)
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", ck)
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 401)

    def test_expired_sessions_are_refused(self):
        con = core.db()
        for col, ago in (("created_at", {"days": 15}), ("last_seen", {"days": 8})):
            sid = self.session()
            con.execute(f"UPDATE sessions SET {col}=? WHERE id_hash=?", (_ago(**ago), L._hash(sid)))
            con.commit()
            self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 401, col)
            self.assertIsNone(con.execute("SELECT 1 FROM sessions WHERE id_hash=?", (L._hash(sid),)).fetchone())
        sid = self.session()  # sharing now with another org: old sessions do not count
        G.update("global", {"lan_share": {"enabled": True, "org": "other"}}, actor="cli")
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 401)

    def test_daily_membership_recheck(self):
        con = core.db()
        sid, gone = self.session(), self.session("bob")
        con.execute("UPDATE sessions SET checked_at=?", (_ago(days=2),))
        con.commit()
        mock.patch.object(L, "github_token", return_value="gho_tp").start()

        def members(path, tok):
            if path.endswith("/bob"):
                raise urllib.error.HTTPError(path, 404, "nf", {}, None)
            return {}
        api = mock.patch.object(L, "_api", side_effect=members).start()
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 200)
        self.assertEqual(api.call_args.args, ("/orgs/example-org/members/alice", "gho_tp"))
        self.assertEqual(self.call("GET", "/api/state", cookie=gone)[0], 401)
        self.assertEqual([r[0] for r in con.execute("SELECT login FROM sessions")], ["alice"])
        api.reset_mock()
        self.assertEqual(self.call("GET", "/api/state", cookie=sid)[0], 200)
        api.assert_not_called()  # checked today

    # ---- login (Device Flow, read:org; the user's token is only used for the two checks)

    def _login(self, membership, user=None):
        forms = []

        def form(url, data):
            forms.append((url, data))
            if url.endswith("/device/code"):
                return {"device_code": "dev-secret", "user_code": "ABCD-1234", "interval": 5, "expires_in": 900}
            return {"access_token": "gho_user"}

        def api(path, tok):
            self.assertEqual(tok, "gho_user")
            if path == "/user":
                return user or {"login": "alice"}
            if isinstance(membership, Exception):
                raise membership
            return membership
        mock.patch.object(S, "_github_form", side_effect=form).start()
        mock.patch.object(L, "_api", side_effect=api).start()
        code, start, _ = self.call("POST", "/api/lan/login", {})
        self.assertEqual(code, 200, start)
        self.assertEqual(forms[0][1]["scope"], "read:org")
        self.assertNotIn("dev-secret", json.dumps(start))
        return self.call("POST", "/api/lan/login/poll", {"id": start["id"]})

    def test_login_active_member(self):
        sec = mock.patch.object(subprocess, "run").start()  # nothing goes to the keychain
        code, out, ck = self._login({"state": "active", "role": "member"})
        self.assertEqual((code, out), (200, {"login": "alice"}))
        for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, ck)
        sid = ck.split(";")[0].split("=", 1)[1]
        self.assertFalse([c for c in sec.call_args_list if "add-generic-password" in str(c)])
        self.assertEqual(self.call("GET", "/api/lan/me", cookie=sid)[1]["login"], "alice")
        con = core.db()
        row = con.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual((row["login"], row["ip"], row["id_hash"]), ("alice", LAN_IP, L._hash(sid)))
        dump = "\n".join(str(tuple(r)) for t in ("sessions", "events", "meta") for r in con.execute(f"SELECT * FROM {t}"))
        self.assertNotIn("gho_user", dump)
        self.assertNotIn(sid, dump)
        self.assertEqual(con.execute("SELECT actor FROM events WHERE kind='login'").fetchone()[0], "alice")

    def test_login_refused_for_pending_or_non_member(self):
        for m in ({"state": "pending"}, urllib.error.HTTPError("u", 404, "nf", {}, None)):
            L._hits.clear()
            code, out, ck = self._login(m)
            self.assertEqual(code, 400, m)
            self.assertIn("멤버가 아니에요", out["error"])
            self.assertIsNone(ck)
        self.assertEqual(core.db().execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_pending_login_is_per_browser_and_rate_limited(self):
        mock.patch.object(S, "_github_form", return_value={"device_code": "d", "user_code": "U", "interval": 5}).start()
        a = self.call("POST", "/api/lan/login", {})[1]
        b = self.call("POST", "/api/lan/login", {})[1]
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(L._pending), 2)  # the first is not replaced
        self.call("POST", "/api/lan/login", {}, ip="10.0.0.9")  # another IP has its own limit
        with mock.patch.object(S, "_github_form", return_value={"error": "authorization_pending"}):
            self.assertEqual(self.call("POST", "/api/lan/login/poll", {"id": a["id"]}, ip="10.0.0.9")[0], 400)  # not its login
            self.assertEqual(self.call("POST", "/api/lan/login/poll", {"id": a["id"]})[1], {"pending": True, "interval": 5})
        for _ in range(3):
            self.call("POST", "/api/lan/login", {})
        code, out, _ = self.call("POST", "/api/lan/login", {})
        self.assertEqual(code, 400)
        self.assertIn("너무 잦아요", out["error"])

    # ---- who did it

    def test_events_record_the_actor(self):
        sid = self.session()
        self.call("POST", "/api/view", {"tab": "lan"}, cookie=sid)
        self.call("POST", "/api/view", {"tab": "local"}, ip="127.0.0.1")
        con = core.db()
        self.assertEqual({r["message"]: r["actor"] for r in con.execute("SELECT * FROM events WHERE kind='view'")},
                         {"lan": "alice", "local": "Owner"})
        core.set_actor(None)
        core.event(con, "tick", "no request")  # tp itself
        self.assertIsNone(con.execute("SELECT actor FROM events WHERE kind='tick'").fetchone()[0])

    def test_review_override_comment_names_the_actor(self):
        gh = mock.patch.object(R, "_gh").start()
        core.set_actor("alice")
        self.addCleanup(core.set_actor, None)
        R._comment_review_override(core.db(), {"url": "u", "head_sha": "abcdef1234"}, "changes", "ok")
        self.assertIn("**ticket-pilot:** alice approved `abcdef1`", gh.call_args.args[0][4])


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("plutil"), "launchd/install.sh is macOS only")
class InstallHostTest(unittest.TestCase):
    """install.sh puts 0.0.0.0 into the dashboard plist's TP_HOST only with lan_share on (launchctl and curl faked)."""
    def run_install(self, enabled: bool) -> str:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "tp.db"
        env = {**os.environ, "TP_DB": str(db)}
        subprocess.run([str(ROOT / "bin" / "tp"), "config", "set", "global.lan_share",
                        json.dumps({"enabled": enabled, "org": "example-org"})], env=env, check=True, capture_output=True)
        fake = tmp / "bin"
        fake.mkdir()
        for name in ("launchctl", "curl"):
            (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp}/calls"\nexit 0\n')
            (fake / name).chmod(0o755)
        home = tmp / "home"
        p = subprocess.run(["/bin/sh", str(ROOT / "launchd" / "install.sh")], capture_output=True, text=True, timeout=120,
                           env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home), "TP_DB": str(db),
                                "TP_LOG_DIR": str(tmp / "logs")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("launchctl bootstrap", (tmp / "calls").read_text())
        plist = plistlib.loads((home / "Library/LaunchAgents/com.ticket-pilot.dashboard.plist").read_bytes())
        return plist["EnvironmentVariables"]["TP_HOST"]

    def test_host_follows_lan_share(self):
        self.assertEqual(self.run_install(True), "0.0.0.0")
        self.assertEqual(self.run_install(False), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
