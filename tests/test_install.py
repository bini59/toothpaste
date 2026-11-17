"""TP-162 install scripts: launchd/install.sh writes the same plists as before on macOS; on Linux it hands over to
systemd/install.sh, which fills the unit templates (checkout, python, port, host, log folder). launchctl, systemctl,
loginctl and curl are fakes; nothing is installed. Run: python3 -m unittest discover tests"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = ("ticket-pilot-dashboard.service", "ticket-pilot-tick.service", "ticket-pilot-tick.timer")


class InstallScript(unittest.TestCase):
    def prepare(self, lan: bool, linger: str = "yes") -> tuple[Path, dict]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "tp.db"
        subprocess.run([str(ROOT / "bin" / "tp"), "config", "set", "global.lan_share",
                        json.dumps({"enabled": lan, "org": "example-org"})], env={**os.environ, "TP_DB": str(db)},
                       check=True, capture_output=True)
        fake = tmp / "bin"
        fake.mkdir()
        for name, out in (("launchctl", ""), ("systemctl", ""), ("curl", ""), ("loginctl", linger)):
            (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp}/calls"\necho "{out}"\nexit 0\n')
            (fake / name).chmod(0o755)
        (fake / "python3").symlink_to(sys.executable)  # systemd/install.sh takes python3 from PATH
        self.tmp, self.home, self.logs = tmp, tmp / "home", tmp / "logs"
        return tmp, {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(self.home), "TP_DB": str(db), "TP_LOG_DIR": str(self.logs)}

    def run_sh(self, script: str, env: dict) -> str:
        p = subprocess.run(["/bin/sh", str(ROOT / script)], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout


class SystemdTest(InstallScript):
    def test_units_are_filled_and_started(self):
        tmp, env = self.prepare(lan=True, linger="no")
        out = self.run_sh("systemd/install.sh", env)
        unit_dir = self.home / ".config" / "systemd" / "user"
        units = {u: (unit_dir / u).read_text() for u in UNITS}
        self.assertEqual(sorted(p.name for p in unit_dir.iterdir()), sorted(UNITS))  # no .new left
        for u, text in units.items():
            self.assertNotIn("__TP_", text, u)
        dash, tick = units["ticket-pilot-dashboard.service"], units["ticket-pilot-tick.service"]
        self.assertIn(f"WorkingDirectory={ROOT}\n", dash)
        self.assertIn(f'ExecStart="{tmp}/bin/python3" -m ticket_pilot.cli serve\n', dash)
        self.assertIn('Environment="TP_PORT=3032"\n', dash)
        self.assertIn('Environment="TP_HOST=0.0.0.0"\n', dash)  # lan_share on
        self.assertIn(f'Environment="TP_LOG_DIR={self.logs}"\n', dash)
        self.assertIn(f'Environment="PYTHONPATH={ROOT}"\n', dash)
        self.assertIn(f"StandardOutput=append:{self.logs}/dashboard.log\n", dash)
        self.assertIn("Restart=on-failure\n", dash)
        self.assertIn(f"StandardOutput=append:{self.logs}/tick.log\n", tick)
        self.assertIn(f'ExecStart="{tmp}/bin/python3" -P -m ticket_pilot.cli tick\n', tick)
        self.assertIn("OnUnitActiveSec=60\n", units["ticket-pilot-tick.timer"])
        calls = (tmp / "calls").read_text()
        for c in ("systemctl --user daemon-reload", "systemctl --user enable --now ticket-pilot-tick.timer",
                  "systemctl --user restart ticket-pilot-dashboard.service", "curl -fs -o /dev/null --max-time 5 http://127.0.0.1:3032/api/state"):
            self.assertIn(c, calls)
        self.assertIn("sudo loginctl enable-linger", out)  # linger off: a hint, never run
        self.assertNotIn("launchctl", calls)

    def test_lan_off_and_linger_on(self):
        _, env = self.prepare(lan=False)
        out = self.run_sh("systemd/install.sh", env)
        dash = (self.home / ".config/systemd/user/ticket-pilot-dashboard.service").read_text()
        self.assertIn('Environment="TP_HOST=127.0.0.1"\n', dash)
        self.assertNotIn("linger", out)

    def test_relative_log_dir_is_refused(self):
        _, env = self.prepare(lan=False)
        p = subprocess.run(["/bin/sh", str(ROOT / "systemd/install.sh")], capture_output=True, text=True,
                           env={**env, "TP_LOG_DIR": "logs"})
        self.assertEqual(p.returncode, 1)
        self.assertFalse((self.home / ".config").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "the launchd/install.sh handover happens on Linux")
    def test_launchd_install_hands_over_on_linux(self):
        tmp, env = self.prepare(lan=False)
        self.run_sh("launchd/install.sh", env)  # what tp update, tp setup and 로컬망 공유 run
        self.assertTrue((self.home / ".config/systemd/user/ticket-pilot-dashboard.service").is_file())
        self.assertNotIn("launchctl", (tmp / "calls").read_text())


@unittest.skipUnless(sys.platform == "darwin", "launchd/install.sh is macOS only")
class LaunchdTest(InstallScript):
    def test_plists_match_the_templates(self):
        """macOS output is unchanged by TP-162: each plist is its template with exactly the old substitutions."""
        tmp, env = self.prepare(lan=False)
        out = self.run_sh("launchd/install.sh", env)
        py = re.search(r"launchd agents use (\S+)", out).group(1)
        subs = {"__TP_DIR__": str(ROOT), "__TP_PYTHON__": py, "__TP_LOG_DIR__": str(self.logs),
                "__TP_HOME__": str(self.home), "__TP_PORT__": "3032", "__TP_HOST__": "127.0.0.1"}
        for label in ("com.ticket-pilot.dashboard", "com.ticket-pilot.tick"):
            want = (ROOT / "launchd" / f"{label}.plist").read_text()
            for k, v in subs.items():
                want = want.replace(k, v)
            self.assertEqual((self.home / "Library/LaunchAgents" / f"{label}.plist").read_text(), want)
        self.assertFalse((self.home / ".config").exists())
        self.assertNotIn("systemctl", (tmp / "calls").read_text())


if __name__ == "__main__":
    unittest.main()
