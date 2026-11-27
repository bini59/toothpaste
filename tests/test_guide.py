"""TP-150: `tp guide [topic]`, GET /api/guide, and the guide line in the agent prompts.
Run: python3 -m unittest discover tests"""
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ticket_pilot import accounts, asana_write, briefs, chat, core, guide, review, runner
from ticket_pilot import server as S

ROOT = Path(__file__).resolve().parent.parent
TP = str(ROOT / "bin" / "tp")
LINE = f"`{TP} guide`로 주제를 보고 `{TP} guide <주제>`로 읽으세요."
TOPICS = ["start", "metrics", "ticket", "milestone", "review", "blocked", "deploy", "agent", "update", "lan", "theme"]


def tp(*args):
    return subprocess.run([TP, "guide", *args], capture_output=True, text=True, timeout=30)


class GuideCliTest(unittest.TestCase):
    def test_topics(self):
        self.assertEqual(list(guide.sections()), TOPICS)
        p = tp()
        self.assertEqual(p.returncode, 0, p.stderr)
        for k in TOPICS:
            self.assertRegex(p.stdout, rf"(?m)^  {k} +\S")
        self.assertIn("마일스톤: 하위 태스크를 모아", p.stdout)

    def test_one_topic(self):
        p = tp("milestone")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("## milestone · 마일스톤\n"))
        self.assertIn("tp-milestone", p.stdout)
        self.assertNotIn("## ", p.stdout[3:])  # only that section

    def test_unknown_topic(self):
        p = tp("nope")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("모르는 주제: nope", p.stdout)
        self.assertIn("  deploy ", p.stdout)

    def test_help_mentions_guide(self):
        p = subprocess.run([TP, "--help"], capture_output=True, text=True, timeout=30)
        self.assertIn("tp guide [주제]", p.stdout)


class GuidePromptTest(unittest.TestCase):
    def test_templates(self):
        for f in ("chat", "work_brief", "rework_brief", "unblock_brief", "conflict_brief"):
            s = (ROOT / "prompts" / f"{f}.md").read_text()
            self.assertIn("{{ASANA}}\n- ticket-pilot 사용법(마일스톤, 단계, 배포 등)이 필요하면 `{{TP}} guide`", s, f)

    def test_brief_and_chat_prompt_have_the_path(self):
        st = core.DEFAULT_SETTINGS
        with mock.patch.object(briefs, "settings", lambda: st), mock.patch.object(chat, "settings", lambda: st), \
                mock.patch.object(asana_write, "settings", lambda: st), mock.patch.object(runner, "settings", lambda: st):
            brief = review.build_conflict_brief({"key": "TP-1", "name": "n", "url": "u"}, {"branch": "b"},
                                                  {"url": "p", "number": 1, "head_sha": "abc", "base": "main"})
            self.assertIn(LINE, brief)
            # the chat prompt gets {{TP}} where runner.launch writes the prompt file
            msg = chat.prompt({"name": "P", "repos": [], "repo": "r", "github": "o/r", "path": "/x"}, "hi")
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner, "LOGS", Path(tmp)), \
                    mock.patch.object(runner, "gh_env", lambda env, required: env), \
                    mock.patch.object(accounts, "agent_env", lambda t: {}), \
                    mock.patch.object(runner.subprocess, "Popen") as po:
                po.return_value.pid = 1
                handle = runner.launch(tmp, msg, "chat-test", agent={"tool": "claude"})
                written = Path(handle.split(" ", 1)[1]).with_suffix(".prompt.md").read_text()
        self.assertIn(LINE, written)
        self.assertNotIn("{{TP}}", written)


class GuideApiTest(unittest.TestCase):
    def test_api_guide(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
        c.request("GET", "/api/guide")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read())["text"], (ROOT / "docs" / "guide.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
