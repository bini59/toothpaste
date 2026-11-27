"""TP-131: global.owner_name fills {{OWNER}} in agent prompts; launchd/install.sh drops this checkout's old
com.bini59.ticket-pilot.* agents. launchctl is a stub here, never the real one. Run: python3 -m unittest discover tests"""
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import asana_write, briefs, chat, core, review

ROOT = Path(__file__).resolve().parent.parent


class OwnerNameTest(unittest.TestCase):
    def prompts(self, st):
        with mock.patch.object(briefs, "settings", lambda: st), mock.patch.object(chat, "settings", lambda: st), \
                mock.patch.object(asana_write, "settings", lambda: st):
            t = {"key": "TP-1", "name": "n", "url": "u"}
            brief = review.build_conflict_brief(t, {"branch": "b"}, {"url": "p", "number": 1, "head_sha": "abc", "base": "main"})
            msg = chat.prompt({"name": "P", "repos": [], "repo": "r", "github": "o/r", "path": "/x"}, "hi {{OWNER}}")
        return brief, msg

    def test_default_is_the_user(self):
        brief, msg = self.prompts(core.DEFAULT_SETTINGS)
        self.assertIn("question for the user>", brief)
        self.assertIn("the user's first message", msg)
        for s in (brief, msg.replace("hi {{OWNER}}", "")):
            self.assertNotIn("{{OWNER}}", s)
        self.assertTrue(msg.endswith("hi {{OWNER}}\n"))  # the user's own text is not a template

    def test_setting(self):
        brief, msg = self.prompts({**core.DEFAULT_SETTINGS, "owner_name": "Alex"})
        self.assertIn("question for Alex>", brief)
        self.assertIn("Alex's first message", msg)
        self.assertIn("only when Alex or the ticket asks", brief)  # {{ASANA}} rules too


@unittest.skipUnless(sys.platform == "darwin", "install.sh reads plists with plutil")
class InstallOldLabelsTest(unittest.TestCase):
    def test_old_agents_of_this_checkout_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            home, stubs, la = tmp / "home", tmp / "bin", tmp / "home" / "Library" / "LaunchAgents"
            la.mkdir(parents=True)
            stubs.mkdir()
            calls = tmp / "launchctl.log"
            # launchctl only logs; print fails so install_agent takes the bootstrap path
            (stubs / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> {calls}\n[ "$1" != print ]\n')
            (stubs / "curl").write_text("#!/bin/sh\nexit 0\n")
            for f in stubs.iterdir():
                f.chmod(0o755)
            mine = {"tick": str(ROOT), "dashboard": "/some/other/checkout"}
            for job, wd in mine.items():
                (la / f"com.bini59.ticket-pilot.{job}.plist").write_bytes(plistlib.dumps(
                    {"Label": f"com.bini59.ticket-pilot.{job}", "WorkingDirectory": wd}))
            env = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "TP_LOG_DIR": str(tmp / "logs"),
                   "TP_DB": str(tmp / "tp.db")}  # install.sh reads dashboard_port (TP-144)
            p = subprocess.run([str(ROOT / "launchd" / "install.sh")], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            log = calls.read_text().splitlines()
            dom = f"gui/{os.getuid()}"
            self.assertFalse((la / "com.bini59.ticket-pilot.tick.plist").exists())
            self.assertTrue((la / "com.bini59.ticket-pilot.dashboard.plist").exists())  # another checkout's: kept
            self.assertIn(f"bootout {dom}/com.bini59.ticket-pilot.tick", log)
            self.assertNotIn(f"bootout {dom}/com.bini59.ticket-pilot.dashboard", log)
            # the old tick goes down before the new one comes up
            self.assertLess(log.index(f"bootout {dom}/com.bini59.ticket-pilot.tick"),
                            log.index(f"bootstrap {dom} {la}/com.ticket-pilot.tick.plist"))
            for job in mine:
                d = plistlib.loads((la / f"com.ticket-pilot.{job}.plist").read_bytes())
                self.assertEqual((d["Label"], d["WorkingDirectory"]), (f"com.ticket-pilot.{job}", str(ROOT)))
            self.assertEqual(plistlib.loads((la / "com.ticket-pilot.dashboard.plist").read_bytes())
                             ["EnvironmentVariables"]["TP_PORT"], "3032")


if __name__ == "__main__":
    unittest.main()
