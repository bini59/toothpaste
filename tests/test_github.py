"""tp's own GitHub token (TP-112): gh / git get it through core.gh_env, the dashboard connects it with the OAuth Device Flow (TP-143).
Run: python3 -m unittest discover tests"""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from ticket_pilot import core
from ticket_pilot import server as S

TOK = "gho_abc123"


class GhEnvTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, "GH_CONFIG_DIR", Path(tempfile.mkdtemp()) / "gh").start()

    def test_token_and_own_config_dir(self):
        with mock.patch.object(core, "github_token", return_value=TOK):
            env = core.gh_env({"PATH": "/bin", "GITHUB_TOKEN": "ci", "GH_TOKEN": "personal"})
        self.assertEqual(env["GH_TOKEN"], TOK)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(core.GH_CONFIG_DIR))
        self.assertTrue(core.GH_CONFIG_DIR.is_dir())
        self.assertEqual(env["PATH"], "/bin")
        # The owner's credential helpers are reset, then gh's (which reads GH_TOKEN) is the only one
        self.assertEqual([env["GIT_CONFIG_VALUE_0"], env["GIT_CONFIG_VALUE_1"]], ["", "!gh auth git-credential"])

    def test_not_connected(self):
        with mock.patch.object(core, "github_token", return_value=None):
            with self.assertRaisesRegex(core.GitHubNotConnected, "대시보드"):
                core.gh_env()
            env = core.gh_env({"GH_TOKEN": "personal"}, required=False)
        self.assertNotIn("GH_TOKEN", env)  # never falls back to another token
        # gh's credential helper would read the owner's keychain entry without GH_TOKEN: only the reset is set
        self.assertEqual((env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_VALUE_0"]), ("1", ""))
        self.assertNotIn("GIT_CONFIG_VALUE_1", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(core.GH_CONFIG_DIR))

    def test_run_gh_requires_token_git_does_not(self):
        with mock.patch.object(core, "github_token", return_value=None), \
             mock.patch.object(core.subprocess, "run", return_value=mock.Mock(returncode=0)) as sp:
            with self.assertRaises(core.GitHubNotConnected):
                core.run(["gh", "pr", "list"])
            sp.assert_not_called()
            core.run(["git", "status"])
            self.assertEqual(sp.call_args.kwargs["env"]["GH_CONFIG_DIR"], str(core.GH_CONFIG_DIR))
            core.run(["ls"])
            self.assertIsNone(sp.call_args.kwargs["env"])


class ClientIdTest(unittest.TestCase):
    """built-in client id unless data/github-oauth.json or the keychain names another OAuth App."""
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.f = Path(tempfile.mkdtemp()) / "github-oauth.json"
        mock.patch.object(S, "GITHUB_OAUTH_FILE", self.f).start()
        self.kc = mock.patch.object(S, "secret_get", return_value=None).start()

    def test_order(self):
        self.assertEqual(S._client_id(), S.GITHUB_CLIENT_ID)
        self.kc.return_value = "kc-id"
        self.assertEqual(S._client_id(), "kc-id")
        self.assertEqual(self.kc.call_args.args, (S.GITHUB_OAUTH_KEYCHAIN, "client_id"))
        self.f.write_text('{"client_id": "file-id", "client_secret": "old"}')
        self.assertEqual(S._client_id(), "file-id")
        for bad in ("not json", "[]", "{}"):
            self.f.write_text(bad)
            self.assertEqual(S._client_id(), "kc-id")


def _resp(j: dict):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(j).encode()
    return r


DEVICE = {"device_code": "dev-secret-code", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
          "expires_in": 900, "interval": 5}


class DeviceFlowTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        S._device.clear()
        mock.patch.object(S, "_client_id", return_value="cid").start()
        self.kc = {}
        mock.patch.object(S, "github_token", side_effect=lambda: self.kc.get("tok")).start()
        mock.patch.object(core, "KEYCHAIN", True).start()  # the macOS path on every OS; `security` is faked below
        mock.patch.object(core, "secret_get", side_effect=lambda s, a=None: self.kc.get("tok")).start()
        self.sp = mock.patch.object(S.subprocess, "run", side_effect=self._security).start()
        self.answers = []
        self.op = mock.patch.object(S.urllib.request, "urlopen",
                                    side_effect=lambda req, timeout: _resp(self.answers.pop(0))).start()

    def _security(self, argv, input=None, **kw):
        self.kc["tok"] = input.split("-w ")[1].strip()
        return mock.Mock(returncode=0)

    def form(self, i):
        req = self.op.call_args_list[i].args[0]
        return req.full_url, {k: v[0] for k, v in parse_qs(req.data.decode()).items()}

    def start(self):
        self.answers.append(DEVICE)
        return S.github_device_start()

    def test_pending_then_token_is_stored(self):
        out = self.start()
        self.assertEqual(out, {k: DEVICE[k] for k in ("user_code", "verification_uri", "expires_in", "interval")})
        self.assertNotIn("dev-secret-code", json.dumps(out))
        self.assertEqual(self.form(0), ("https://github.com/login/device/code", {"client_id": "cid", "scope": "repo workflow"}))
        self.answers += [{"error": "authorization_pending"}, {"access_token": TOK, "token_type": "bearer"}]
        self.assertEqual(S.github_device_poll(), {"pending": True, "interval": 5})
        self.sp.assert_not_called()
        out = S.github_device_poll()
        self.assertEqual(out, {"connected": True})
        self.assertEqual(self.form(2), ("https://github.com/login/oauth/access_token", {
            "client_id": "cid", "device_code": "dev-secret-code",
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}))
        self.assertEqual(self.sp.call_args.args[0], ["security", "-i"])  # token not in argv
        self.assertIn(f"-s ticket-pilot-github -a github -w {TOK}", self.sp.call_args.kwargs["input"])
        self.assertEqual(self.kc["tok"], TOK)
        with self.assertRaisesRegex(RuntimeError, "진행 중인 연결이 없어요"):
            S.github_device_poll()  # one use only

    def test_slow_down_raises_interval(self):
        self.start()
        self.answers += [{"error": "slow_down", "interval": 10}, {"error": "slow_down"}, {"error": "authorization_pending"}]
        self.assertEqual(S.github_device_poll()["interval"], 10)
        self.assertEqual(S.github_device_poll()["interval"], 15)
        self.assertEqual(S.github_device_poll()["interval"], 15)

    def test_expired_or_denied_stores_nothing(self):
        for err, msg in (("expired_token", "만료"), ("access_denied", "거부"), ("device_flow_disabled", "device_flow_disabled")):
            self.start()
            self.answers.append({"error": err})
            with self.assertRaisesRegex(RuntimeError, msg) as e:
                S.github_device_poll()
            self.assertNotIn("dev-secret-code", str(e.exception))
            self.assertEqual(S._device, {})
        self.sp.assert_not_called()

    def test_network_error_keeps_waiting(self):
        self.start()
        self.op.side_effect = OSError("timed out")
        self.assertEqual(S.github_device_poll(), {"pending": True, "interval": 5})
        self.assertEqual(S._device["code"], "dev-secret-code")

    def test_stale_poll_leaves_new_code(self):
        self.start()
        def answer(req, timeout):  # 새 코드 받기 lands while the old poll waits on GitHub
            S._device.update(code="new-code")
            return _resp({"error": "expired_token"})
        self.op.side_effect = answer
        with self.assertRaisesRegex(RuntimeError, "만료"):
            S.github_device_poll()
        self.assertEqual(S._device["code"], "new-code")

    def test_local_expiry_skips_github(self):
        self.start()
        S._device["exp"] = 0
        with self.assertRaisesRegex(RuntimeError, "만료"):
            S.github_device_poll()
        self.assertEqual(self.op.call_count, 1)
        self.sp.assert_not_called()

    def test_start_error(self):
        self.answers.append({"error": "device_flow_disabled", "error_description": "Device Flow must be explicitly enabled"})
        with self.assertRaisesRegex(RuntimeError, "explicitly enabled"):
            S.github_device_start()
        self.assertEqual(S._device, {})

    def test_odd_token_is_not_stored(self):
        self.start()
        self.answers.append({"access_token": TOK + " -s x"})
        with self.assertRaisesRegex(RuntimeError, "형식"):
            S.github_device_poll()
        self.sp.assert_not_called()


class DeviceHttpTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        S._device.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, path, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        self.addCleanup(c.close)
        c.request("POST", path, "{}", {"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def test_foreign_origin_forbidden(self):
        with mock.patch.object(S.urllib.request, "urlopen") as op:
            for path in ("/api/github/device", "/api/github/device/poll"):
                self.assertEqual(self.call(path, "https://evil.example")[0], 403)
            op.assert_not_called()

    def test_routes(self):
        with mock.patch.object(S.urllib.request, "urlopen", side_effect=[_resp(DEVICE), _resp({"error": "authorization_pending"})]):
            st, j = self.call("/api/github/device", "http://127.0.0.1:3032")
            self.assertEqual((st, j["user_code"]), (200, "ABCD-1234"))
            self.assertNotIn("device_code", j)
            self.assertEqual(self.call("/api/github/device/poll"), (200, {"pending": True, "interval": 5}))
        S._device.clear()
        st, j = self.call("/api/github/device/poll")
        self.assertEqual(st, 500)
        self.assertIn("다시 연결", j["error"])


class StatusTest(unittest.TestCase):
    def test_status_never_returns_token(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"login": "alice"}'
        resp.__enter__.return_value.headers = {"X-OAuth-Scopes": "repo, workflow"}
        with mock.patch.object(S, "github_token", return_value=TOK), \
             mock.patch.object(S.urllib.request, "urlopen", return_value=resp):
            st = S.github_status()
        self.assertEqual(st, {"connected": True, "login": "alice", "scopes": ["repo", "workflow"]})
        self.assertNotIn(TOK, str(st))
        with mock.patch.object(S, "github_token", return_value=None):
            self.assertEqual(S.github_status(), {"connected": False})


if __name__ == "__main__":
    unittest.main()
