"""Host installer entry points, using temporary paths and fake service commands and curl."""
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
    def prepare(self, lan: bool, linger: str = "yes", port: int | None = None, darwin: bool = False) -> tuple[Path, dict]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "tp.db"
        subprocess.run([str(ROOT / "bin" / "tp"), "config", "set", "global.lan_share",
                        json.dumps({"enabled": lan, "org": "example-org"})], env={**os.environ, "TP_DB": str(db)},
                       check=True, capture_output=True)
        if port is not None:
            subprocess.run([str(ROOT / "bin/tp"), "config", "set", "global.dashboard_port", str(port)],
                           env={**os.environ, "TP_DB": str(db)}, check=True, capture_output=True)
        fake = tmp / "bin"
        fake.mkdir()
        commands = {
            "launchctl": '[ "$1" != bootout ]',
            "systemctl": "exit 0",
            "curl": f'[ "$(grep -c "^curl " "{tmp}/calls")" -gt "${{CURL_FAILURES:-0}}" ]',
            "loginctl": f'echo "{linger}"',
            "sleep": "exit 0",
            "docker": "exit 1",  # no legacy container to remove
            "orca": "exit 1",  # no legacy automations to disable
        }
        fake_darwin = darwin and sys.platform != "darwin"
        if fake_darwin:
            commands["uname"] = "echo Darwin"
        for name, body in commands.items():
            (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp}/calls"\n{body}\n')
            (fake / name).chmod(0o755)
        (fake / "python3").symlink_to(sys.executable)  # systemd/install.sh takes python3 from PATH
        self.checkout = ROOT
        if fake_darwin:
            # Exercise the launchd entry point on Linux without writing to macOS's absolute Python paths.
            self.checkout = tmp / "checkout"
            shutil.copytree(ROOT / "launchd", self.checkout / "launchd")
            (self.checkout / "scripts").mkdir()
            shutil.copy(ROOT / "scripts/service-install.sh", self.checkout / "scripts")
            (self.checkout / "ticket_pilot").symlink_to(ROOT / "ticket_pilot", target_is_directory=True)
            install = self.checkout / "launchd/install.sh"
            script = install.read_text()
            path = "/opt/homebrew/bin/python3"
            self.assertEqual(script.count(path), 1)
            install.write_text(script.replace(path, str(fake / "python3")))
        self.home, self.logs = tmp / "home", tmp / "logs"
        return tmp, {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(self.home), "TP_DB": str(db), "TP_LOG_DIR": str(self.logs)}

    def run_sh(self, script: str, env: dict, status: int = 0) -> subprocess.CompletedProcess:
        p = subprocess.run([str(self.checkout / script)], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(p.returncode, status, p.stdout + p.stderr)
        return p


class SystemdTest(InstallScript):
    def test_units_are_filled_and_started(self):
        tmp, env = self.prepare(lan=True, linger="no", port=4312)
        config_dir = tmp / "xdg"
        out = self.run_sh("systemd/install.sh", {**env, "XDG_CONFIG_HOME": str(config_dir), "CURL_FAILURES": "1"}).stdout
        unit_dir = config_dir / "systemd" / "user"
        units = {u: (unit_dir / u).read_text() for u in UNITS}
        self.assertEqual(sorted(p.name for p in unit_dir.iterdir()), sorted(UNITS))  # no .new left
        for u, text in units.items():
            self.assertNotIn("__TP_", text, u)
        dash, tick = units["ticket-pilot-dashboard.service"], units["ticket-pilot-tick.service"]
        self.assertIn(f"WorkingDirectory={ROOT}\n", dash)
        self.assertIn(f'ExecStart="{tmp}/bin/python3" -m ticket_pilot.cli serve\n', dash)
        self.assertIn('Environment="TP_PORT=4312"\n', dash)
        self.assertIn('Environment="TP_HOST=0.0.0.0"\n', dash)  # lan_share on
        self.assertIn(f'Environment="TP_LOG_DIR={self.logs}"\n', dash)
        self.assertIn(f'Environment="PYTHONPATH={ROOT}"\n', dash)
        self.assertIn(f"StandardOutput=append:{self.logs}/dashboard.log\n", dash)
        self.assertIn("Restart=on-failure\n", dash)
        self.assertIn(f"StandardOutput=append:{self.logs}/tick.log\n", tick)
        self.assertIn(f'ExecStart="{tmp}/bin/python3" -P -m ticket_pilot.cli tick\n', tick)
        self.assertIn("OnUnitActiveSec=60\n", units["ticket-pilot-tick.timer"])
        calls = (tmp / "calls").read_text().splitlines()
        ready = "curl -fs -o /dev/null --max-time 5 http://127.0.0.1:4312/api/state"
        for c in ("systemctl --user daemon-reload", "systemctl --user enable --now ticket-pilot-tick.timer",
                  "systemctl --user restart ticket-pilot-dashboard.service"):
            self.assertIn(c, calls)
        self.assertEqual(calls[-3:], [ready, "sleep 1", ready])
        self.assertIn("dashboard up (systemd --user)", out)
        self.assertIn("sudo loginctl enable-linger", out)  # linger off: a hint, never run
        self.assertFalse(any(c.startswith("launchctl ") for c in calls))

    def test_lan_off_and_linger_on(self):
        tmp, env = self.prepare(lan=False)
        unit = self.home / ".config/systemd/user/ticket-pilot-dashboard.service"
        out = self.run_sh("systemd/install.sh", env).stdout
        dash = unit.read_text()
        self.assertIn('Environment="TP_HOST=127.0.0.1"\n', dash)
        self.assertNotIn("linger", out)
        self.run_sh("systemd/install.sh", env)
        self.assertEqual(dash, unit.read_text())
        calls = (tmp / "calls").read_text().splitlines()
        self.assertEqual(calls.count("systemctl --user restart ticket-pilot-dashboard.service"), 2)

    def test_relative_log_dir_is_refused(self):
        _, env = self.prepare(lan=False)
        self.run_sh("systemd/install.sh", {**env, "TP_LOG_DIR": "logs"}, status=1)
        self.assertFalse((self.home / ".config").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "the launchd/install.sh handover happens on Linux")
    def test_launchd_install_hands_over_on_linux(self):
        tmp, env = self.prepare(lan=False)
        self.run_sh("launchd/install.sh", env)  # what tp update, tp setup and 로컬망 공유 run
        self.assertTrue((self.home / ".config/systemd/user/ticket-pilot-dashboard.service").is_file())
        self.assertNotIn("launchctl", (tmp / "calls").read_text())


class LaunchdTest(InstallScript):
    def test_plists_match_the_templates_and_rerun_kicks_both_agents(self):
        tmp, env = self.prepare(lan=True, port=4312, darwin=True)
        out = self.run_sh("launchd/install.sh", env).stdout
        py = re.search(r"launchd agents use (\S+)", out).group(1)
        subs = {"__TP_DIR__": str(self.checkout), "__TP_PYTHON__": py, "__TP_LOG_DIR__": str(self.logs),
                "__TP_HOME__": str(self.home), "__TP_PORT__": "4312", "__TP_HOST__": "0.0.0.0"}
        self.assertIn(f"dashboard up (gui/{os.getuid()})", out)
        self.run_sh("launchd/install.sh", env)
        calls = (tmp / "calls").read_text()
        for label in ("com.ticket-pilot.dashboard", "com.ticket-pilot.tick"):
            want = (ROOT / "launchd" / f"{label}.plist").read_text()
            for k, v in subs.items():
                want = want.replace(k, v)
            plist = self.home / "Library/LaunchAgents" / f"{label}.plist"
            self.assertEqual(plist.read_text(), want)
            self.assertEqual(calls.count(f"launchctl bootstrap gui/{os.getuid()} {plist}\n"), 1)
            self.assertIn(f"launchctl kickstart -k gui/{os.getuid()}/{label}\n", calls)
            self.assertFalse(plist.with_suffix(".plist.new").exists())
        self.assertEqual(calls.count("curl -fs -o /dev/null --max-time 5 http://127.0.0.1:4312/api/state\n"), 2)
        self.assertFalse((self.home / ".config").exists())
        self.assertNotIn("systemctl", calls)


class SharedInstallTest(InstallScript):
    def test_settings_error_stops_install(self):
        for darwin, script in ((True, "launchd/install.sh"), (False, "systemd/install.sh")):
            with self.subTest(script=script):
                _, env = self.prepare(lan=False, darwin=darwin)
                Path(env["TP_DB"]).write_text("invalid database")
                p = self.run_sh(script, env, status=1)
                self.assertIn("could not read the dashboard_port setting from data/tp.db", p.stderr)
                self.assertFalse(self.home.exists())
                self.assertFalse(self.logs.exists())

    def test_readiness_timeout_preserves_exit_status_and_log_hint(self):
        for darwin, script in ((True, "launchd/install.sh"), (False, "systemd/install.sh")):
            with self.subTest(script=script):
                tmp, env = self.prepare(lan=False, darwin=darwin)
                p = self.run_sh(script, {**env, "CURL_FAILURES": "30"}, status=1)
                hint = f"{self.logs}/dashboard.log"
                if not darwin:
                    hint += " (journalctl --user -u ticket-pilot-dashboard)"
                self.assertEqual(p.stderr, f"dashboard did not answer http://127.0.0.1:3032/api/state; see {hint}\n")
                self.assertNotIn("dashboard up", p.stdout)
                calls = (tmp / "calls").read_text().splitlines()
                self.assertEqual(calls.count("curl -fs -o /dev/null --max-time 5 http://127.0.0.1:3032/api/state"), 30)
                self.assertEqual(calls.count("sleep 1"), 30)


if __name__ == "__main__":
    unittest.main()
