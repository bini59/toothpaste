"""Docker mode (TP-164): every visit logs in (127.0.0.1 too), TP_ADMINS / TP_ORG decide who may, admin-only actions,
host install steps off, and tp run's tick loop. GitHub is faked. Run: python3 -m unittest discover tests"""
import contextlib
import io
import os
import threading
import unittest
import urllib.error
from unittest import mock

import test_lan  # module import: its LanTest must not be collected twice

from ticket_pilot import cli, core, lan as L, server as S, sysjobs as SJ

LOCAL = "127.0.0.1"


class DockerAuthTest(unittest.TestCase):
    setUp_lan = test_lan.LanTest.setUp
    call = test_lan.LanTest.call
    _login = test_lan.LanTest._login

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TP_MODE": "docker", "TP_ADMINS": "Admin1, admin2", "TP_ORG": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.setUp_lan()

    def session(self, login, org=""):
        return L.new_session(login, org, LOCAL)

    def test_localhost_needs_a_login(self):
        self.assertEqual(self.call("GET", "/api/state", ip=LOCAL)[0], 401)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=LOCAL)[0], 401)
        code, me, _ = self.call("GET", "/api/lan/me", ip=LOCAL)
        self.assertEqual((code, me), (200, {"local": False, "login": None, "actor": None, "docker": True, "admin": False,
                                       "public_url": None}))
        self.assertNotIn(self.call("GET", "/", ip=LOCAL)[0], (401, 403))  # the login screen's bundle

    def test_admin_session_passes_without_org_and_lan_share(self):
        core.config_cache_clear()
        from ticket_pilot import config as G
        G.update("global", {"lan_share": {"enabled": False, "org": "example-org"}}, actor="cli")
        sid = self.session("admin1")
        self.assertEqual(self.call("GET", "/api/state", ip=LOCAL, cookie=sid)[0], 200)
        me = self.call("GET", "/api/lan/me", ip="172.17.0.1", cookie=sid)[1]  # the docker bridge
        self.assertEqual((me["login"], me["admin"], me["actor"]), ("admin1", True, "admin1"))
        # Origin must match Host, as on the LAN
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=LOCAL, cookie=sid, origin=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=LOCAL, cookie=sid,
                                   origin="http://localhost:1")[0], 403)
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=LOCAL, cookie=sid)[0], 200)
        row = core.db().execute("SELECT actor FROM events WHERE kind='view' ORDER BY id DESC").fetchone()
        self.assertEqual(row[0], "admin1")

    def test_session_ends_when_dropped_from_admins(self):
        sid = self.session("admin2")
        self.assertEqual(self.call("GET", "/api/state", ip=LOCAL, cookie=sid)[0], 200)
        with mock.patch.dict(os.environ, {"TP_ADMINS": "admin1"}):
            self.assertEqual(self.call("GET", "/api/state", ip=LOCAL, cookie=sid)[0], 401)

    def test_org_member_session_and_admin_only_actions(self):
        os.environ["TP_ORG"] = "example-org"
        member, admin = self.session("bob", "example-org"), self.session("admin1", "example-org")
        self.assertEqual(self.call("GET", "/api/state", ip=LOCAL, cookie=member)[0], 200)
        st = self.call("GET", "/api/lan/status", ip=LOCAL, cookie=member)[1]
        self.assertEqual((st["docker"], st["local"], st["enabled"]), (True, False, False))
        bob = next(s["id"] for s in st["sessions"] if s["login"] == "bob")
        self.assertEqual(self.call("POST", "/api/lan/sessions/delete", {"id": bob}, ip=LOCAL, cookie=member)[0], 403)
        self.assertEqual(self.call("POST", "/api/config", {"scope": "global", "value": {
            "lan_share": {"enabled": True, "org": "x"}}}, ip=LOCAL, cookie=member)[0], 403)
        self.assertTrue(self.call("GET", "/api/lan/status", ip=LOCAL, cookie=admin)[1]["local"])
        self.assertEqual(self.call("POST", "/api/lan/sessions/delete", {"id": bob}, ip=LOCAL, cookie=admin)[0], 200)
        self.assertEqual(self.call("GET", "/api/state", ip=LOCAL, cookie=member)[0], 401)

    def test_lan_share_and_update_are_off(self):
        sid = self.session("admin1")
        code, out, _ = self.call("POST", "/api/lan/share", {"enabled": True, "org": "x"}, ip=LOCAL, cookie=sid)
        self.assertEqual((code, out["error"]), (400, core.DOCKER_OFF))
        with mock.patch.object(S.subprocess, "Popen") as popen:
            code, out, _ = self.call("POST", "/api/update", {}, ip=LOCAL, cookie=sid)
        popen.assert_not_called()
        self.assertEqual(code, 400)
        self.assertIn("docker compose", out["error"])
        with mock.patch.object(S.UP, "current", return_value="v1.0.0"), mock.patch.object(S.UP, "blocked", return_value=None):
            info = self.call("GET", "/api/update", ip=LOCAL, cookie=sid)[1]
        self.assertEqual(info["command"], S.DOCKER_UPDATE)
        self.assertEqual(SJ.collect()["jobs"], [])

    def test_login_admin_org_member_and_stranger(self):
        L._hits.clear()
        code, out, ck = self._login(AssertionError("no org check for an admin"), {"login": "Admin1"})
        self.assertEqual((code, out), (200, {"login": "Admin1"}))
        L._hits.clear()
        code, out, ck = self._login(AssertionError("no org to check"), {"login": "bob"})
        self.assertEqual(code, 400)
        self.assertIn("TP_ADMINS", out["error"])
        self.assertIsNone(ck)
        os.environ["TP_ORG"] = "example-org"
        L._hits.clear()
        self.assertEqual(self._login({"state": "active"}, {"login": "bob"})[:2], (200, {"login": "bob"}))
        L._hits.clear()
        code, out, _ = self._login(urllib.error.HTTPError("u", 404, "nf", {}, None), {"login": "eve"})
        self.assertEqual(code, 400)
        self.assertIn("멤버가 아니에요", out["error"])


class DockerPublicTest(unittest.TestCase):
    """TP-166: TP_PUBLIC_URL (the cloudflared tunnel's address), Secure cookie, Origin list, Cf-Connecting-IP, tunnel."""
    PUB = "https://tp.example.com"
    BRIDGE = "172.18.0.3"  # cloudflared on the compose network

    setUp_lan, call, _login = DockerAuthTest.setUp_lan, DockerAuthTest.call, DockerAuthTest._login
    session = DockerAuthTest.session

    def setUp(self):
        env = mock.patch.dict(os.environ, {"TP_MODE": "docker", "TP_ADMINS": "admin1", "TP_ORG": "",
                                           "TP_PUBLIC_URL": self.PUB + "/"})
        env.start()
        self.addCleanup(env.stop)
        self.setUp_lan()

    def test_public_url_parsing(self):
        self.assertEqual(L.public_url(), self.PUB)
        os.environ["TP_PUBLIC_URL"] = "https://TP.example.com:443"
        self.assertEqual(L.public_url(), self.PUB)
        for bad in ("http://tp.example.com", "https://tp.example.com/x", "tp.example.com", "https://u@tp.example.com",
                    "https://tp.example.com:99999"):
            os.environ["TP_PUBLIC_URL"] = bad
            self.assertIsNone(L.public_url(), bad)
            self.assertIn("TP_PUBLIC_URL", L.docker_problem(), bad)
        with mock.patch.dict(os.environ, {"TP_MODE": "", "TP_PUBLIC_URL": self.PUB}):
            self.assertIsNone(L.public_url())  # host install: never public

    def test_login_cookie_is_secure(self):
        code, out, ck = self._login(AssertionError("admin"), {"login": "admin1"})
        self.assertEqual(code, 200, out)
        self.assertIn("; Secure", ck)
        sid = ck.split(";")[0].split("=", 1)[1]
        self.assertIn("; Secure", self.call("POST", "/api/lan/logout", {}, cookie=sid)[2])
        with mock.patch.dict(os.environ, {"TP_PUBLIC_URL": ""}):
            L._hits.clear()
            self.assertNotIn("Secure", self._login(AssertionError("admin"), {"login": "admin1"})[2])

    def test_cf_ip_is_recorded_only(self):
        mock.patch.object(S, "_github_form", side_effect=lambda url, data: {
            "device_code": "d", "user_code": "U", "interval": 5, "expires_in": 900} if url.endswith("/device/code")
            else {"access_token": "t"}).start()
        mock.patch.object(L, "_api", return_value={"login": "admin1"}).start()
        cf = {"Cf-Connecting-IP": "203.0.113.7"}
        start = self.call("POST", "/api/lan/login", {}, ip=self.BRIDGE, origin=self.PUB, headers=cf)[1]
        code, out, _ = self.call("POST", "/api/lan/login/poll", {"id": start["id"]}, ip=self.BRIDGE, origin=self.PUB,
                                 headers=cf)
        self.assertEqual(code, 200, out)
        self.assertEqual(L.sessions()[0]["ip"], "203.0.113.7")
        ev = core.db().execute("SELECT message FROM events WHERE kind='login'").fetchone()[0]
        self.assertIn("203.0.113.7", ev)
        # the header lets nobody in, from the tunnel or 127.0.0.1, whatever it says
        for ip in (self.BRIDGE, LOCAL):
            for v in ("127.0.0.1", "203.0.113.7"):
                h = {"Cf-Connecting-IP": v}
                self.assertEqual(self.call("GET", "/api/state", ip=ip, headers=h)[0], 401)
                self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=ip, origin=self.PUB, headers=h)[0], 401)
                self.assertEqual(self.call("GET", "/api/lan/me", ip=ip, headers=h)[1]["login"], None)
        self.assertEqual(L.seen_ip("172.18.0.3", "junk<script>"), "172.18.0.3")
        self.assertEqual(L.seen_ip(LOCAL, "203.0.113.9"), LOCAL)  # 127.0.0.1 cannot forge the record
        sid = self.session("admin1")  # a session works the same with any header
        for v in (None, "127.0.0.1", "8.8.8.8"):
            self.assertEqual(self.call("GET", "/api/state", ip=self.BRIDGE, cookie=sid,
                                       headers=v and {"Cf-Connecting-IP": v})[0], 200, v)

    def test_login_rate_limit_is_per_visitor_behind_the_tunnel(self):
        mock.patch.object(S, "_github_form", return_value={"device_code": "d", "user_code": "U"}).start()
        mock.patch.object(S, "_client_id", return_value="cid").start()
        for i in range(L.LOGINS_PER_MIN):
            self.call("POST", "/api/lan/login", {}, ip=self.BRIDGE, origin=self.PUB, headers={"Cf-Connecting-IP": "203.0.113.1"})
        code = lambda ip: self.call("POST", "/api/lan/login", {}, ip=self.BRIDGE, origin=self.PUB,
                                    headers={"Cf-Connecting-IP": ip})[0]
        self.assertEqual(code("203.0.113.1"), 400)  # this visitor is limited
        self.assertEqual(code("203.0.113.2"), 200)  # another one is not

    def test_origin_list(self):
        sid = self.session("admin1")
        port = self.srv.server_port
        for origin, want in [(self.PUB, 200), (f"http://127.0.0.1:{port}", 200), (None, 403),
                             ("http://tp.example.com", 403), ("https://evil.example", 403),
                             (f"http://localhost:{port}", 403), (f"http://127.0.0.1:{port + 1}", 403)]:
            self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=self.BRIDGE, cookie=sid, origin=origin)[0],
                             want, origin)
        # a Host the browser sent for another name does not make its Origin allowed
        self.assertEqual(self.call("POST", "/api/view", {"tab": "x"}, ip=self.BRIDGE, cookie=sid,
                                   origin="http://evil.example", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/lan/me", ip=LOCAL, cookie=sid)[1]["public_url"], self.PUB)

    def test_tunnel_status(self):
        os.environ["TP_ORG"] = "example-org"
        member = self.session("bob", "example-org")
        admin = self.session("admin1", "example-org")
        self.assertEqual(self.call("GET", "/api/lan/tunnel", ip=LOCAL, cookie=member)[0], 403)
        self.assertEqual(self.call("GET", "/api/lan/tunnel", ip=LOCAL)[0], 401)

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass
        with mock.patch.object(L.urllib.request, "urlopen", return_value=Resp(b'{"status":200,"readyConnections":4}')) as u:
            code, st, _ = self.call("GET", "/api/lan/tunnel", ip=LOCAL, cookie=admin)
        self.assertEqual((code, st), (200, {"public_url": self.PUB, "running": True, "connections": 4}))
        self.assertEqual(u.call_args[0][0], L.TUNNEL_READY)
        err = urllib.error.HTTPError(L.TUNNEL_READY, 503, "x", {}, io.BytesIO(b'{"status":503,"readyConnections":0}'))
        with mock.patch.object(L.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(L.tunnel_status(), {"public_url": self.PUB, "running": True, "connections": 0})
        with mock.patch.object(L.urllib.request, "urlopen", side_effect=urllib.error.URLError("no host")):
            self.assertEqual(L.tunnel_status(), {"public_url": self.PUB, "running": False, "connections": 0})
        with mock.patch.dict(os.environ, {"TP_MODE": ""}):
            self.assertEqual(self.call("GET", "/api/lan/tunnel", ip=LOCAL)[:2], (200, {"docker": False}))


class DockerStartTest(unittest.TestCase):
    def test_refuses_to_start_without_admins_or_org(self):
        with mock.patch.dict(os.environ, {"TP_MODE": "docker", "TP_ADMINS": " , ", "TP_ORG": ""}):
            self.assertIn("TP_ADMINS", L.docker_problem())
            with mock.patch.object(S, "ThreadingHTTPServer") as srv, self.assertRaises(SystemExit) as e:
                S.serve(1, block=False)
            srv.assert_not_called()
            self.assertIn("TP_ADMINS", str(e.exception))
        with mock.patch.dict(os.environ, {"TP_MODE": "docker", "TP_ADMINS": "", "TP_ORG": "example-org"}):
            self.assertIsNone(L.docker_problem())
        with mock.patch.dict(os.environ, {"TP_MODE": "", "TP_ADMINS": ""}):
            self.assertIsNone(L.docker_problem())

    def test_setup_migrate_update_are_off(self):
        for argv in (["setup"], ["migrate", "--to", "/tmp/x"], ["update"]):
            with mock.patch.dict(os.environ, {"TP_MODE": "docker"}), mock.patch.object(cli.S, "setup") as su, \
                    mock.patch.object(cli.M, "migrate") as mg, mock.patch.object(cli.U, "main") as up, \
                    self.assertRaises(SystemExit) as e:
                cli.main(argv)
            self.assertIn(core.DOCKER_OFF, str(e.exception.code), argv)
            for m in (su, mg, up):
                m.assert_not_called()


class FakeProc:
    def __init__(self, runs: int):
        self.runs, self.waited = runs, False

    def poll(self):
        if self.runs:
            self.runs -= 1
            return None
        return 0

    def wait(self):
        self.waited = True
        return 0


class RunLoopTest(unittest.TestCase):
    def test_skips_while_a_tick_runs_and_waits_for_it_on_stop(self):
        stop, procs = threading.Event(), []
        long_tick = FakeProc(runs=2)  # still running at the next two loop turns
        script = [FakeProc(0), long_tick]

        def spawn():
            p = script.pop(0) if script else FakeProc(10)
            procs.append(p)
            return p
        turns = []
        real_wait = stop.wait

        def wait(_t):
            turns.append(len(procs))
            if len(turns) == 5:
                stop.set()
            return real_wait(0)
        stop.wait = wait
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run_loop(stop, spawn, every=60)
        # turn 1 spawns, 2 spawns the long tick, 3 and 4 skip it, 5 spawns again; stop waits for that last one
        self.assertEqual(turns, [1, 2, 2, 2, 3])
        self.assertEqual(out.getvalue().count("still running"), 2)
        self.assertTrue(procs[-1].waited)
        self.assertFalse(long_tick.waited)

    def test_stop_before_the_first_turn_spawns_nothing(self):
        stop = threading.Event()
        stop.set()
        spawn = mock.Mock()
        cli.run_loop(stop, spawn)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
