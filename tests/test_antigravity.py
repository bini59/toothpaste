import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_lan

from ticket_pilot import accounts, antigravity as AGY, chat, core, runner

SID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"
NEON = "gemini-4-neon-high"
KEY = "fixture." + "x" * 28
USAGE = {
    "status": "SUCCESS",
    "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"name": "Weekly Limit", "remaining_fraction": 0.74, "reset_time": "2026-11-23T10:00:00Z"},
            {"name": "Five Hour Limit", "remaining_fraction": 0, "remaining_amount": 0},
        ]},
        {"name": "Claude and GPT Models", "buckets": [
            {"name": "Weekly Limit", "remaining_fraction": 1},
            {"name": "Five Hour Limit", "disabled": True, "remaining_fraction": 0},
        ]},
    ]}},
}
FAKE_AGY = """
import json, os, pathlib, sys
args = sys.argv[1:]
login = pathlib.Path.home() / "agy-login"
if args == ["--version"]:
    print("1.5.12")
elif args[-1] == "models":
    if not (login.exists() or os.environ.get("GEMINI_API_KEY")):
        print("Please sign in to view available models", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("GEMINI_API_KEY"):
        assert any("modelProvider" in a for a in args)
    print("gemini-3.8-flash-high\\tGemini 3.8 Flash (High)")
    if os.environ.get("GEMINI_API_KEY") or login.read_text() == "neon":
        print("gemini-4-neon-high\\tGemini 4 Neon (High)")
elif "/model" in args:
    assert sys.stdin.isatty()
    print("https://accounts.google.com/o/oauth2/auth?state=fixture", flush=True)
    if input().strip() == "fixture-code":
        login.write_text("plus")
    else:
        sys.exit(1)
elif "/usage" in args:
    print(json.dumps(USAGE_FIXTURE))
else:
    assert "--input-format" in args and "--output-format" in args
    assert args[args.index("--model") + 1] == "gemini-4-neon-high"
    text = json.loads(sys.stdin.readline())["message"]["content"]
    if text == "check saved key":
        assert os.environ.get("GEMINI_API_KEY") == KEY_FIXTURE
        assert json.loads(args[args.index("--settings") + 1])["modelProvider"] == "gemini"
    if text == "fail":
        print(json.dumps({"event": "result", "result": {"status": "ERROR", "error": "model unavailable"}}))
        sys.exit(0)
    if "--conversation" in args:
        assert args[args.index("--conversation") + 1] == "CONVERSATION_ID"
        assert text == "second turn"
    elif "second turn" in text:
        sys.exit(1)
    result = {"status": "SUCCESS", "conversation_id": "CONVERSATION_ID", "response": text.splitlines()[-1]}
    print("non-interactive CLI diagnostic", file=sys.stderr)
    print(json.dumps({"event": "result", "result": result}))
"""
FAKE_SECURITY = """
import json, os, pathlib, sys
store = pathlib.Path(os.environ["TP_SECRETS_FILE"])
if sys.argv[1:] == ["find-generic-password", "-s", "ticket-pilot-antigravity", "-a", "api_key", "-w"] and store.exists():
    key = json.loads(store.read_text()).get("ticket-pilot-antigravity", {}).get("api_key")
    if key:
        print(key)
        sys.exit(0)
sys.exit(1)
"""


class TestAntigravity(unittest.TestCase):
    call = test_lan.LanTest.call

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        agy = self.work / "agy"
        agy.write_text(f"#!{sys.executable}\n" + FAKE_AGY.replace("USAGE_FIXTURE", repr(USAGE))
                       .replace("CONVERSATION_ID", SID).replace("KEY_FIXTURE", repr(KEY)))
        agy.chmod(0o755)
        # The detached adapter has its own process. Stub its keychain reads on macOS.
        if sys.platform == "darwin":
            security = self.work / "security"
            security.write_text(f"#!{sys.executable}\n" + FAKE_SECURITY)
            security.chmod(0o755)
        for tool in ("codex", "claude"):
            exe = self.work / tool
            exe.write_text("#!/bin/sh\necho 1.0\n")
            exe.chmod(0o755)
        secrets = self.work / "secrets.json"
        for patch in (
            mock.patch.dict(os.environ, {"HOME": tmp.name, "PATH": f"{tmp.name}:/usr/bin:/bin", "CODEX_HOME": tmp.name,
                                         "TP_SECRETS_FILE": str(secrets)}, clear=True),
            mock.patch.object(core, "KEYCHAIN", False),
            mock.patch.object(core, "SECRETS_FILE", secrets),
            mock.patch.object(runner, "LOGS", self.work / "logs"),
            mock.patch.object(runner, "asana_rules", lambda: ""),
            mock.patch.object(runner, "gh_env", lambda env, required: env),
            mock.patch.object(accounts.U, "_creds", lambda: []),
            mock.patch.object(AGY, "_model_cache", None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(accounts.cancel, "agy")

    def test_accounts_usage_and_logout_via_http(self):
        test_lan.LanTest.setUp(self)
        self.work.joinpath("auth.json").write_text('{"tokens":{"access_token":"fixture"}}')
        # Both Claude and Codex have saved usage. Only the installed and authenticated tool is returned.
        con = core.db()
        for tool in ("claude", "codex"):
            core.meta_set(con, f"{tool}_usage", {"checked_at": core.now(), "cached": True})
        con.commit()
        status, body, _ = self.call("GET", "/api/agents/usage", ip="127.0.0.1")
        self.assertEqual((status, list(body)), (200, ["codex"]))
        base = "/api/agents/agy"
        st, login, _ = self.call("POST", base + "/login", {}, ip="127.0.0.1")
        self.assertEqual((st, login["state"]), (200, "pending"))
        st, _, _ = self.call("POST", base + "/login/code", {"code": "fixture-code"}, ip="127.0.0.1")
        self.assertEqual(st, 200)
        st, body, _ = self.call("GET", "/api/agents/accounts", ip="127.0.0.1")
        agy = body["tools"]["agy"]
        self.assertEqual((st, agy["installed"], agy["logged_in"], agy["method"]), (200, True, True, "cli"))
        self.assertEqual([m["id"] for m in agy["models"]], ["gemini-3.8-flash-high"])
        self.assertIn("Plus", agy["neon_reason"])
        _, body, _ = self.call("GET", "/api/agents/usage", ip="127.0.0.1")
        self.assertEqual(set(body), {"codex", "agy"})
        self.assertEqual([g["name"] for g in body["agy"]["groups"]], ["Gemini Models", "Claude and GPT Models"])
        self.assertEqual(body["agy"]["groups"][0]["limits"][1],
                         {"name": "Five Hour Limit", "remaining": 0, "reset_at": None, "used": 100})
        self.work.joinpath("agy-login").unlink()
        _, body, _ = self.call("GET", "/api/agents/usage", ip="127.0.0.1")
        self.assertEqual(list(body), ["codex"])
        self.work.joinpath("auth.json").unlink()
        _, body, _ = self.call("GET", "/api/agents/usage", ip="127.0.0.1")
        self.assertEqual(body, {})

    def test_api_key_verification_model_catalog_and_disconnect(self):
        test_lan.LanTest.setUp(self)
        api_reply = {"models": [{"name": "models/gemini-4-neon", "supportedGenerationMethods": ["generateContent"]}]}
        with mock.patch.object(AGY.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(api_reply).encode())) as request:
            self.assertEqual(self.call("POST", "/api/agents/agy/apikey", {"key": KEY}, ip="127.0.0.1")[0], 200)
        self.assertEqual(request.call_args.args[0].get_header("X-goog-api-key"), KEY)
        _, body, _ = self.call("GET", "/api/agents/accounts", ip="127.0.0.1")
        agy = body["tools"]["agy"]
        self.assertEqual((agy["method"], agy["saved"], agy["neon_reason"]), ("api_key", "api_key", None))
        self.assertEqual([m["id"] for m in agy["models"]], [NEON])
        self.assertNotIn(KEY, json.dumps(body))
        _, body, _ = self.call("GET", "/api/agents/usage", ip="127.0.0.1")
        self.assertEqual(body, {"agy": {"error": "unsupported_auth"}})
        self.assertEqual(self.call("POST", "/api/agents/agy/logout", {}, ip="127.0.0.1")[0], 200)
        self.assertIsNone(core.secret_get(AGY.SERVICE, AGY.API_KEY))

    def wait_for(self, handle):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            alive, code = runner.agent_state(handle)
            if not alive:
                return code
            time.sleep(0.05)
        self.fail("agy did not exit")

    def test_saved_key_is_only_in_agy_environment(self):
        core.secret_set(AGY.SERVICE, AGY.API_KEY, KEY)
        setup = 'if [ "${GEMINI_API_KEY+x}" = x ]; then echo present; else echo absent; fi > setup-key; exit 17'
        with mock.patch.object(runner, "settings", return_value=core.DEFAULT_SETTINGS):
            handle = runner.launch(str(self.work), "check saved key", "fixture", setup=setup,
                                   agent={"tool": "agy", "model": NEON})
            self.assertEqual(self.wait_for(handle), 0)
        self.assertEqual((self.work / "setup-key").read_text().strip(), "absent")
        self.assertEqual(runner.last_message(handle), "check saved key")
        _, log = runner._parts(handle)
        self.assertIn("worktree setup failed (17)", log.read_text())
        for path in (log, Path(f"{log}.json")):
            self.assertNotIn(KEY, path.read_text())

    def test_runner_roles_and_two_chat_turns(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        core._migrate(con)
        agent = {"tool": "agy", "model": NEON}
        config = {**core.DEFAULT_SETTINGS, "agents": dict.fromkeys(("work", "review"), agent)}
        pm = {"repos": {"r": {"path": str(self.work), "agent": agent}}, "projects": [
            {"id": "p", "name": "Project", "source": "none", "repos": [{"repo": "r", "default": True}]}]}
        with mock.patch.object(runner, "settings", return_value=config), mock.patch.object(chat, "db", return_value=con), \
                mock.patch.object(chat, "project_map", return_value=pm):
            for role, response in (("work", "ticket output"), ("review", "review output")):
                handle = runner.launch(str(self.work), response, "fixture", role=role)
                self.assertEqual(self.wait_for(handle), 0)
                self.assertEqual(runner.last_message(handle), response)
                self.assertEqual(runner.resume_cmd(handle), f"agy --conversation {SID}")
            handle = runner.launch(str(self.work), "fail", "fixture", role="review", split_stderr=True)
            self.assertEqual(self.wait_for(handle), 1)
            self.assertEqual(runner.last_message(handle), "")
            self.assertEqual(runner.session(handle), ("agy", None))

            cid = chat.create("p")
            for prompt in ("first turn", "second turn"):
                turn = chat.send(cid, prompt)
                handle = con.execute("SELECT handle FROM chat_messages WHERE id=?", (turn,)).fetchone()[0]
                self.assertEqual(self.wait_for(handle), 0)
                self.assertEqual(chat.messages(cid)["messages"][-1]["text"], prompt)
                self.assertEqual(con.execute("SELECT session FROM chat_conversations WHERE id=?", (cid,)).fetchone()[0], SID)


if __name__ == "__main__":
    unittest.main()
