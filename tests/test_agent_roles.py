"""TP-168: per-role agent cards (global agents.{work,review,check}) and the 활성 계정 check.
Run: python3 -m unittest discover tests"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import accounts, automation, config as G, core, runner


def st(**kw):
    return {**core.DEFAULT_SETTINGS, **kw}


class ResolveTest(unittest.TestCase):
    def resolve(self, s, conf=None, role="work"):
        with mock.patch.object(runner, "settings", lambda: s):
            return runner.resolve(conf, role)

    def test_empty_cards_follow_global_agent(self):
        s = st(agent="claude")
        for role in ("work", "review", "check"):
            self.assertEqual(self.resolve(s, role=role), {"tool": "claude", "model": None, "effort": None, "ultracode": False})

    def test_repo_over_role_over_global(self):
        s = st(agent="codex", agents={"work": {"tool": "claude", "model": "opus", "effort": "high"},
                                      "review": {"tool": "claude", "model": "sonnet"}})
        self.assertEqual(self.resolve(s)["model"], "opus")  # role card
        a = self.resolve(s, {"tool": "codex", "model": "gpt-5"})  # repo wins as a whole
        self.assertEqual((a["tool"], a["model"], a["effort"]), ("codex", "gpt-5", None))
        a = self.resolve(s, {"effort": "low"})  # repo without a tool: the role card's tool
        self.assertEqual((a["tool"], a["model"], a["effort"]), ("claude", None, "low"))
        self.assertEqual(self.resolve(s, {}, "review")["model"], "sonnet")
        self.assertEqual(self.resolve(s, None, "check")["tool"], "codex")  # no check card: global
        self.assertEqual(self.resolve(s, {"model": None}, "check")["tool"], "codex")  # all-empty repo entry = none

    def test_each_automation_runs_its_roles_tool(self):
        s = st(agent="codex", agents={"review": {"tool": "claude"}, "check": {"tool": "claude", "model": "haiku"}})
        self.assertEqual({a[0] for a in automation.AUTOMATIONS}, set(runner.AUTOMATION_ROLES))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner, "settings", lambda: s), \
                mock.patch.object(runner, "LOGS", Path(tmp)), mock.patch.object(runner, "gh_env", lambda e, required: e), \
                mock.patch.object(runner, "asana_rules", lambda: ""), mock.patch.object(accounts, "agent_env", lambda t: {}), \
                mock.patch.object(runner.subprocess, "Popen") as po:
            po.return_value.pid = 1
            got = {}
            for aid, _name, _prompt, _every in automation.AUTOMATIONS:
                h = runner.launch(tmp, "p", f"auto-{aid}", role=runner.AUTOMATION_ROLES[aid])
                meta = json.loads(Path(h.split(" ", 1)[1] + ".json").read_text())
                got[aid] = (meta["agent"], meta["model"])
        self.assertEqual(got, {"dispatch": ("claude", "haiku"), "review": ("claude", None),
                               "diff": ("claude", "haiku"), "cleanup": ("claude", "haiku")})


class ValidateTest(unittest.TestCase):
    def errors(self, agents, repo_agent=None):
        doc = core.effective_config({"settings": {"agents": agents},
                                     "project_map": {"repos": {"r": {"path": "/tmp", "agent": repo_agent}} if repo_agent else {}}})
        with mock.patch.object(G, "agent_models", lambda: {"codex": [], "claude": []}):
            return [e for e in G.validate(doc) if "agent" in e]

    def test_valid(self):
        self.assertEqual(self.errors({}), [])
        self.assertEqual(self.errors({"work": {"tool": "claude", "model": "opus", "effort": "high", "ultracode": True},
                                      "check": {"effort": "low"}}), [])
        self.assertEqual(self.errors({"work": {"tool": "agy", "model": "gemini-4-neon-high"}}), [])

    def test_invalid(self):
        self.assertTrue(any("global.agents:" in e for e in self.errors([])))
        self.assertTrue(any("global.agents:" in e for e in self.errors({"chat": {"tool": "codex"}})))
        self.assertTrue(any("global.agents.review.tool" in e for e in self.errors({"review": {"tool": "gemini"}})))
        self.assertTrue(any("global.agents.work.ultracode" in e for e in self.errors({"work": {"tool": "codex", "ultracode": True}})))
        self.assertTrue(any("global.agents.check:" in e for e in self.errors({"check": "claude"})))
        self.assertTrue(any("global.agents.review.effort" in e for e in self.errors(
            {"review": {"tool": "agy", "model": "gemini-4-neon-high", "effort": "low"}})))

    def test_repo_without_tool_checks_against_the_work_card(self):
        # ultracode needs claude: the repo has no tool, the work card says claude, the global agent is codex
        self.assertEqual(self.errors({"work": {"tool": "claude"}}, {"ultracode": True}), [])
        self.assertTrue(self.errors({}, {"ultracode": True}))


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")})
        self.env.start()
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TP_MODE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()

    def fake(self, tool):
        f = self.bin / tool
        f.write_text(f"#!/bin/sh\necho '{tool} 9.9'\n")
        f.chmod(0o755)

    def detect(self, creds=()):
        with mock.patch.object(accounts, "agent_path", lambda: str(self.bin)), \
                mock.patch.object(accounts.U, "_creds", lambda: list(creds)), \
                mock.patch.object(accounts, "claude_saved", lambda: None), \
                mock.patch.object(accounts.Path, "home", lambda: self.home):
            return accounts.detect()

    def test_cli_missing(self):
        t = self.detect()["tools"]
        self.assertEqual(t["claude"], {"installed": False, "version": None, "logged_in": False, "account": [],
                                       "method": None, "saved": None})
        self.assertFalse(t["codex"]["installed"])

    def test_installed_not_logged_in(self):
        self.fake("claude")
        self.fake("codex")
        t = self.detect()["tools"]
        self.assertEqual((t["claude"]["installed"], t["claude"]["version"], t["claude"]["logged_in"]), (True, "claude 9.9", False))
        self.assertEqual((t["codex"]["logged_in"], t["codex"]["method"]), (False, None))

    def test_logged_in_shows_account_never_token(self):
        self.fake("claude")
        self.fake("codex")
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "a@x.test", "organizationName": "Org"}}))
        claims = base64.urlsafe_b64encode(json.dumps({"email": "b@x.test", "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}).encode()).decode().rstrip("=")
        (self.home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"h.{claims}.s", "access_token": "SECRET-A"}}))
        d = self.detect([{"accessToken": "SECRET-C", "subscriptionType": "max"}])
        self.assertEqual(d["tools"]["claude"]["account"], ["a@x.test", "Org", "max 플랜"])
        self.assertEqual(d["tools"]["codex"]["account"], ["b@x.test", "ChatGPT pro"])
        self.assertEqual({tool for tool, t in d["tools"].items() if t["logged_in"] and t["method"] == "cli"},
                         {"claude", "codex"})
        self.assertNotIn("SECRET", json.dumps(d))

    def test_docker_flag(self):
        self.fake("codex")
        with mock.patch.dict(os.environ, {"TP_MODE": "docker"}):
            d = self.detect()
        self.assertTrue(d["docker"])

    def test_current_cli_status_overrides_leftover_credentials(self):
        self.fake("claude")
        self.fake("codex")
        (self.bin / "claude").write_text('#!/bin/sh\nprintf \'{"loggedIn": false}\\n\'\nexit 1\n')
        (self.bin / "codex").write_text('#!/bin/sh\necho "Error checking login status: invalid credential JSON" >&2\nexit 1\n')
        (self.home / ".codex" / "auth.json").write_text('{"tokens":{"access_token":"stale"}}')
        tools = self.detect([{"accessToken": "stale", "expiresAt": 1}])["tools"]
        self.assertFalse(tools["claude"]["logged_in"])
        self.assertFalse(tools["codex"]["logged_in"])


if __name__ == "__main__":
    unittest.main()
