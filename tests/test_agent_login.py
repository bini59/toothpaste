"""에이전트 로그인 buttons (TP-179): codex Device Flow, claude setup-token / pasted token / API key, the agent env rule
and who may press them. The CLIs are fake scripts, secrets go to a temp secrets.json, Anthropic is mocked.
Run: python3 -m unittest discover tests"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import test_lan  # module import: its LanTest must not be collected twice

from ticket_pilot import accounts as A, core, devserver, lan as L, runner

TOKEN = "sk-ant-oat01-" + "T" * 80
KEY = "sk-ant-api03-" + "K" * 80
CODE = "WXYZ-12AB3"

# codex: prints the Device Flow screen (with colors), waits for $DIR/approve, then logs in; status / logout use $DIR/ok
FAKE_CODEX = r"""#!/bin/sh
D="$FAKE_DIR"
case "$1 $2" in
  "login --device-auth")
    printf '\033[94mhttps://auth.example.test/codex/device\033[0m\n\n2. Enter this one-time code \033[90m(expires in 15 minutes)\033[0m\n   \033[94m%s\033[0m\n' "$FAKE_CODE"
    i=0; while [ ! -f "$D/approve" ]; do sleep 0.1; i=$((i+1)); [ $i -gt 300 ] && exit 1; done
    [ "$(cat "$D/approve")" = deny ] && { echo "Error: denied"; exit 1; }
    touch "$D/ok"; exit 0 ;;
  "login status") [ -f "$D/ok" ] && { echo "Logged in using ChatGPT"; exit 0; }; echo "Not logged in"; exit 1 ;;
  "logout ") rm -f "$D/ok"; echo "Successfully logged out" ;;
  *) echo "codex 9.9" ;;
esac
"""

# claude setup-token: needs a terminal like the real one, prints the link as an OSC 8 hyperlink, reads the code
FAKE_CLAUDE = r"""#!PYTHON
import sys
if sys.argv[1:] == ["--version"]:
    print("claude 9.9"); sys.exit(0)
if not sys.stdin.isatty():
    sys.exit(3)
sys.stdout.write("Browser didn't open? Use the url below\r\n\x1b]8;id=1;https://claude.example.test/oauth/authorize?code=true&state=s\x07link\x1b]8;;\x07\r\nPaste code here if prompted >")
sys.stdout.flush()
code = sys.stdin.readline().strip()
if code == "good#state":
    print("\r\nYour OAuth token (valid for 1 year):\r\n\r\n" + "TOKEN" + "\r\n\r\nStore this token securely.")
else:
    print("\r\n****" + code[-6:] + "\r\nOAuth error: That code wasn't accepted.\r\nPress Enter to retry.")
    sys.stdin.readline()
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.fake = self.tmp / "bin", self.tmp / "fake"
        self.bin.mkdir()
        self.fake.mkdir()
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE.replace("PYTHON", sys.executable)
                                                          .replace('"TOKEN"', repr(TOKEN)))):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        mock.patch.object(core, "KEYCHAIN", False).start()
        mock.patch.object(core, "SECRETS_FILE", self.tmp / "secrets" / "secrets.json").start()
        mock.patch.object(A, "agent_path", lambda: f"{self.bin}:/usr/bin:/bin").start()
        mock.patch.object(A.U, "_creds", lambda: []).start()
        mock.patch.object(A.Path, "home", lambda: self.tmp / "home").start()
        mock.patch.dict(os.environ, {"FAKE_DIR": str(self.fake), "FAKE_CODE": CODE,
                                     "CODEX_HOME": str(self.tmp / "home" / ".codex")}).start()
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CLAUDE_CONFIG_DIR", "TP_MODE"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: [A.cancel(t) for t in ("claude", "codex")])


class CodexLoginTest(Base):
    def wait(self, want, secs=10):
        end = time.time() + secs
        while time.time() < end:
            st, done = A.codex_status()
            if st["state"] != "pending":
                return st, done
            time.sleep(0.1)
        self.fail("still pending")

    def test_code_then_done(self):
        r = A.codex_start()
        self.assertEqual((r["state"], r["url"], r["code"]), ("pending", "https://auth.example.test/codex/device", CODE))
        self.assertEqual(r["expires_in"] // 60, 14)
        self.assertEqual(A.codex_start()["code"], CODE)  # one login at a time: a second press shows the same one
        self.assertEqual(A.codex_status()[0]["state"], "pending")
        (self.fake / "approve").write_text("ok")
        self.assertEqual(self.wait("done"), ({"state": "done"}, True))
        self.assertEqual(A.codex_status(), ({"state": "idle"}, False))
        A.logout("codex")
        self.assertFalse((self.fake / "ok").exists())

    def test_failed_does_not_echo_the_code(self):
        A.codex_start()
        (self.fake / "approve").write_text("deny")
        st, done = self.wait("failed")
        self.assertEqual((st["state"], done), ("failed", False))
        self.assertIn("denied", st["error"])
        self.assertNotIn(CODE, st["error"])

    def test_expired_and_cancel_kill_the_process(self):
        A.codex_start()
        p = A._logins["codex"]["p"]
        A._logins["codex"]["exp"] = time.time() - 1
        self.assertEqual(A.codex_status()[0]["state"], "expired")
        self.assertIsNotNone(p.poll())
        A.codex_start()
        p = A._logins["codex"]["p"]
        A.cancel("codex")
        self.assertIsNotNone(p.poll())
        self.assertNotIn("codex", A._logins)


class ClaudeLoginTest(Base):
    def test_setup_token_flow_saves_token(self):
        r = A.claude_start()
        self.assertEqual(r, {"state": "pending", "url": "https://claude.example.test/oauth/authorize?code=true&state=s"})
        self.assertEqual(A.claude_code("good#state"), A.OAUTH)
        self.assertEqual(core.secret_get(A.CLAUDE_SVC, A.OAUTH), TOKEN)
        self.assertNotIn("claude", A._logins)
        t = A.detect()["tools"]["claude"]
        self.assertEqual((t["logged_in"], t["method"], t["saved"]), (True, A.OAUTH, A.OAUTH))
        self.assertNotIn(TOKEN, json.dumps(A.detect()))

    def test_wrong_code_saves_nothing_and_hides_the_code(self):
        A.claude_start()
        with self.assertRaises(RuntimeError) as e:
            A.claude_code("bad-code#abcdef")
        self.assertIn("wasn't accepted", str(e.exception))
        self.assertNotIn("abcdef", str(e.exception))
        self.assertIsNone(A.claude_saved())
        with self.assertRaises(RuntimeError):  # the login ended: start again
            A.claude_code("good#state")

    def test_pasted_token_and_api_key(self):
        with self.assertRaises(RuntimeError):
            A.claude_token("not a token")
        A.claude_token(f"  {TOKEN}\n")
        self.assertEqual(A.claude_saved(), A.OAUTH)
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                A.ANTHROPIC_MODELS, 401, "Unauthorized", {}, io.BytesIO(b"{}"))):
            with self.assertRaises(RuntimeError) as e:
                A.claude_api_key(KEY)
        self.assertNotIn(KEY, str(e.exception))
        self.assertEqual(A.claude_saved(), A.OAUTH)  # a refused key changes nothing
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertRaises(RuntimeError, A.claude_api_key, KEY)
        self.assertIsNone(core.secret_get(A.CLAUDE_SVC, A.API_KEY))
        ok = mock.MagicMock()
        with mock.patch.object(A.urllib.request, "urlopen", return_value=ok) as u:
            A.claude_api_key(KEY)
        self.assertEqual(u.call_args[0][0].get_header("X-api-key"), KEY)
        self.assertEqual((A.claude_saved(), core.secret_get(A.CLAUDE_SVC, A.OAUTH)), (A.API_KEY, None))  # one at a time
        A.logout("claude")
        self.assertIsNone(A.claude_saved())


class AgentEnvTest(Base):
    def test_rule(self):
        self.assertEqual(A.agent_env("claude"), {})  # nothing saved
        A.claude_token(TOKEN)
        self.assertEqual(A.agent_env("claude"), {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
        self.assertEqual(A.agent_env("codex"), {})
        with mock.patch.object(A.U, "_creds", lambda: [{"accessToken": "x"}]):  # the CLI's own login wins
            self.assertEqual(A.agent_env("claude"), {})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}):
            self.assertEqual(A.agent_env("claude"), {})
        with mock.patch.object(A.urllib.request, "urlopen"):
            A.claude_api_key(KEY)
        self.assertEqual(A.agent_env("claude"), {"ANTHROPIC_API_KEY": KEY})

    def test_launch_passes_it_and_dev_servers_drop_it(self):
        A.claude_token(TOKEN)
        mock.patch.object(runner, "LOGS", self.tmp / "logs").start()
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            popen.return_value.pid = 1
            runner.launch(str(self.tmp), "hi", "t", agent={"tool": "claude"})
            self.assertEqual(popen.call_args.kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
            self.assertNotIn(TOKEN, json.dumps(popen.call_args.args))  # never on a command line
            runner.launch(str(self.tmp), "hi", "t", agent={"tool": "codex"})
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", popen.call_args.kwargs["env"])
        self.assertTrue({"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"} <= set(devserver.SECRET_ENV))


class AgentLoginApiTest(Base):
    call = test_lan.LanTest.call

    def setUp(self):
        super().setUp()
        test_lan.LanTest.setUp(self)

    def events(self):
        con = core.db()
        try:
            return [dict(r) for r in con.execute("SELECT kind,message,actor FROM events WHERE kind='agent_login'")]
        finally:
            con.close()

    def test_local_flow_leaves_no_secret(self):
        code, r, _ = self.call("POST", "/api/agents/claude/login", {}, ip="127.0.0.1")
        self.assertEqual((code, r["state"]), (200, "pending"))
        code, r, _ = self.call("POST", "/api/agents/claude/login/code", {"code": "good#state"}, ip="127.0.0.1")
        self.assertEqual((code, r), (200, {"ok": True}))
        self.assertEqual(core.secret_get(A.CLAUDE_SVC, A.OAUTH), TOKEN)
        code, acc, _ = self.call("GET", "/api/agents/accounts", ip="127.0.0.1")
        self.assertEqual((acc["admin"], acc["tools"]["claude"]["method"]), (True, A.OAUTH))
        self.assertNotIn(TOKEN, json.dumps(acc))
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                A.ANTHROPIC_MODELS, 401, "Unauthorized", {}, io.BytesIO(b"{}"))):
            code, r, _ = self.call("POST", "/api/agents/claude/apikey", {"key": KEY}, ip="127.0.0.1")
        self.assertEqual(code, 400)
        self.assertNotIn(KEY, json.dumps(r))
        self.assertEqual(self.call("POST", "/api/agents/claude/logout", {}, ip="127.0.0.1")[0], 200)
        self.assertIsNone(A.claude_saved())
        self.assertEqual(self.call("POST", "/api/agents/codex/token", {"token": TOKEN}, ip="127.0.0.1")[0], 404)
        ev = self.events()
        self.assertEqual([e["message"] for e in ev], ["claude 로그인 (tp 토큰)", "claude 로그아웃 (tp에 저장한 토큰·키만 지움)"])
        self.assertEqual({e["actor"] for e in ev}, {"Owner"})
        self.assertNotIn(TOKEN, json.dumps(ev))
        code, r, _ = self.call("POST", "/api/agents/claude/login", {}, ip="127.0.0.1", origin="http://evil.test")
        self.assertEqual(code, 403)  # the write-request Origin rule still applies
        self.assertNotIn("claude", A._logins)

    def test_lan_user_and_docker_non_admin_get_403(self):
        sid = L.new_session("alice", "example-org", test_lan.LAN_IP)
        for method, path in (("POST", "/api/agents/claude/login"), ("POST", "/api/agents/claude/token"),
                             ("POST", "/api/agents/codex/login"), ("GET", "/api/agents/codex/login"),
                             ("POST", "/api/agents/codex/logout"), ("POST", "/api/agents/claude/apikey")):
            # a GET body is never read: the server would close with it unread and reset the connection
            self.assertEqual(self.call(method, path, {"token": TOKEN} if method == "POST" else None, cookie=sid)[0], 403, path)
        self.assertFalse(self.call("GET", "/api/agents/accounts", cookie=sid)[1]["admin"])
        with mock.patch.dict(os.environ, {"TP_MODE": "docker", "TP_ADMINS": "admin1", "TP_ORG": "example-org"}):
            member, admin = L.new_session("bob", "example-org", "127.0.0.1"), L.new_session("admin1", "example-org", "127.0.0.1")
            self.assertEqual(self.call("POST", "/api/agents/claude/token", {"token": TOKEN}, ip="127.0.0.1", cookie=member)[0], 403)
            self.assertIsNone(A.claude_saved())
            self.assertEqual(self.call("POST", "/api/agents/claude/token", {"token": TOKEN}, ip="127.0.0.1", cookie=admin)[0], 200)
        self.assertEqual(self.events()[-1]["actor"], "admin1")


if __name__ == "__main__":
    unittest.main()
