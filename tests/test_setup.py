"""tp setup (TP-144): prechecks, port choice, rerun skips. install.sh, gh, npm and open are fakes; the DB is in memory.
Run: python3 -m unittest discover tests"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import config as G, core, setup as S

from test_config import mem_db, use_db


def fake_run(fail=(), out=None):
    """subprocess.run stand-in: exit 1 for commands whose first two words are in fail, stdout from out."""
    return lambda cmd, **k: subprocess.CompletedProcess(cmd, 1 if tuple(cmd[:2]) in fail else 0, (out or {}).get(cmd[0], ""), "")


class PrecheckTest(unittest.TestCase):
    def test_reports_everything_missing(self):
        miss, warn = S.precheck(which=lambda *a, **k: None, system=lambda: "Windows", version=(3, 9))
        self.assertEqual(len(miss), 5)  # OS, python, git, gh, claude/codex
        self.assertTrue(any("cli.github.com" in m for m in miss))
        self.assertTrue(any("3.11" in m for m in miss))
        ok = lambda cmd, **k: subprocess.CompletedProcess(cmd, 1)  # noqa: E731 gh installed but not logged in
        miss, _ = S.precheck(which=lambda c, **k: None if c == "codex" else "/x", run=ok, system=lambda: "Darwin")
        self.assertEqual(miss, ["gh 로그인: gh auth login"])

    def test_linux(self):
        has = lambda c, **k: "/x"  # noqa: E731
        lingering = fake_run(out={"loginctl": "yes\n"})
        self.assertEqual(S.precheck(which=has, run=lingering, system=lambda: "Linux", version=(3, 12)), ([], []))
        # python.org Python is a macOS-only warning; linger off is a warning, not a blocker
        miss, warn = S.precheck(which=has, run=fake_run(out={"loginctl": "no\n"}), system=lambda: "Linux", version=(3, 12))
        self.assertEqual(miss, [])
        self.assertEqual(len(warn), 1)
        self.assertIn("sudo loginctl enable-linger", warn[0])
        # no systemd user session (WSL, container, su): says why
        miss, _ = S.precheck(which=has, run=fake_run(fail={("systemctl", "--user")}, out={"loginctl": "yes"}),
                             system=lambda: "Linux", version=(3, 12))
        self.assertEqual(len(miss), 1)
        self.assertIn("WSL이나 컨테이너", miss[0])
        miss, _ = S.precheck(which=lambda c, **k: None if c in ("systemctl", "git") else "/x", run=lingering,
                             system=lambda: "Linux", version=(3, 10))
        self.assertEqual(len(miss), 3)  # python 3.10, systemd, git
        self.assertTrue(all("brew" not in m and "xcode" not in m for m in miss))


class BrowserTest(unittest.TestCase):
    def opened(self, mac, env, which=lambda c: f"/usr/bin/{c}", rc=0):
        out = []
        with mock.patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], rc)) as run:
            S.open_browser("http://x/#start", say=out.append, which=which, mac=mac, env=env)
        return (run.call_args.args[0] if run.called else None), out

    def test_open_xdg_open_or_address(self):
        self.assertEqual(self.opened(True, {}), (["/usr/bin/open", "http://x/#start"], []))
        self.assertEqual(self.opened(False, {"DISPLAY": ":0"})[0], ["/usr/bin/xdg-open", "http://x/#start"])
        # a server: no display (never Debian's `open`, which is openvt), or no xdg-open, or it fails -> the address
        for case in [dict(mac=False, env={}), dict(mac=False, env={"DISPLAY": ":0"}, which=lambda c: None),
                     dict(mac=False, env={"WAYLAND_DISPLAY": "w"}, rc=3)]:
            _, out = self.opened(**case)
            self.assertEqual(out, ["브라우저를 열지 못했어요. 이 주소를 여세요: http://x/#start"], case)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.con = mem_db()
        self.p = use_db(self.con) + [mock.patch.object(S, "db", lambda: self.con),
                                     mock.patch.object(G, "backup_dir", lambda: self.tmp / "backups")]
        self.tmp = Path(tempfile.mkdtemp())
        for p in self.p[2:]:
            p.start()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        for p in self.p:
            p.stop()
        core.config_cache_clear()

    def stored(self):
        core.config_cache_clear()
        return ((core.read_config(self.con) or {}).get("settings") or {}).get("dashboard_port")

    def test_port_skips_busy_and_keeps_stored(self):
        self.assertEqual(S.pick_port(free=lambda p: p != 3032, is_installed=lambda: False), (3033, True))
        self.assertEqual(self.stored(), 3033)
        self.assertEqual(S.pick_port(free=lambda p: True, is_installed=lambda: False), (3033, False))

    def test_existing_install_keeps_3032(self):
        # The owner's Mac: installed before the setting, 3032 is busy with its own dashboard
        self.assertEqual(S.pick_port(free=lambda p: False, is_installed=lambda: True), (3032, True))

    def test_missing_prereq_changes_nothing(self):
        with mock.patch.object(S, "web_dist") as wd, mock.patch.object(S.subprocess, "run") as run:
            self.assertEqual(S.setup(root=self.tmp, say=lambda m: None, install=["x"], check=lambda: (["git"], [])), 1)
        wd.assert_not_called()
        run.assert_not_called()
        self.assertIsNone(self.stored())

    def test_second_run_skips_done_steps(self):
        (self.tmp / "web" / "dist").mkdir(parents=True)
        out, calls = [], []
        run = lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)  # noqa: E731
        opener = lambda url, say: calls.append(["open", url])  # noqa: E731
        with mock.patch.object(S.subprocess, "run", side_effect=run), mock.patch.object(S, "_free", lambda p: True):
            # first run: no web/dist -> the release asset; not installed -> install.sh
            with mock.patch.object(S.U, "_git", return_value="v1.2.3"), \
                    mock.patch.object(S.U, "_fetch_dist", side_effect=lambda root, *a: (root / "web" / "dist" / "index.html").write_text("x") or True) as fd, \
                    mock.patch.object(S, "installed", return_value=False):
                self.assertEqual(S.setup(root=self.tmp, say=out.append, install=["install.sh"], check=lambda: ([], []), opener=opener), 0)
            self.assertEqual(fd.call_args[0][1], "v1.2.3")
            self.assertEqual(calls, [["install.sh"], ["open", "http://127.0.0.1:3032/#start"]])
            calls.clear()
            out.clear()
            with mock.patch.object(S.U, "_fetch_dist") as fd, mock.patch.object(S, "installed", return_value=True), \
                    mock.patch.object(S, "up", return_value=True):
                self.assertEqual(S.setup(root=self.tmp, say=out.append, install=["install.sh"], check=lambda: ([], []), opener=opener), 0)
            fd.assert_not_called()
        self.assertEqual(calls, [["open", "http://127.0.0.1:3032/#start"]])
        self.assertIn("화면 파일: 있음, 건너뜀", out)

    def test_fresh_clone_without_data_dir(self):
        # the real _fetch_dist (gh is a fake that fails): needs data/ for its temp dir, then falls back to the build
        calls = []
        with mock.patch.object(S.U, "_git", return_value="v1"), \
                mock.patch.object(S.U.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "nope")), \
                mock.patch.object(S.U, "_run", side_effect=lambda cmd, *a: calls.append(cmd[:2])):
            S.web_dist(self.tmp, say=lambda m: None)
        self.assertEqual(calls, [["npm", "ci"], ["npm", "run"]])

    def test_download_failure_builds(self):
        calls = []
        with mock.patch.object(S.U, "_git", return_value=""), \
                mock.patch.object(S.U, "_run", side_effect=lambda cmd, *a: calls.append(cmd[:2])):
            S.web_dist(self.tmp, say=lambda m: None)
        self.assertEqual(calls, [["npm", "ci"], ["npm", "run"]])
        with mock.patch.object(S.U, "_git", return_value="v1"), mock.patch.object(S.U, "_fetch_dist", return_value=False), \
                mock.patch.object(S.U, "_run", side_effect=lambda cmd, *a: calls.append(cmd[:2])):
            S.web_dist(self.tmp, say=lambda m: None)
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
