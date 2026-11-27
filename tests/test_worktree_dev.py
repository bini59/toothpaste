"""Ticket worktree dev servers, including their cleanup ownership boundary."""
import json
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from ticket_pilot import cleanup, core, devserver, worktree_dev as dev


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NodePathTest(unittest.TestCase):
    def test_nvmrc_selects_the_newest_matching_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, NVM_DIR=tmp), \
                mock.patch.object(dev.R, "agent_path", return_value="/usr/bin"):
            wt = Path(tmp)
            self.assertEqual(dev.node_path(wt), "/usr/bin")
            for ver in ("v2.9.0", "v20.1.9", "v20.1.10", "v20.11.1", "v20.11.12", "v22.1.0"):
                node = wt / "versions" / "node" / ver / "bin" / "node"
                node.parent.mkdir(parents=True)
                node.touch(mode=0o755)
            alias = wt / "alias" / "lts" / "*"
            alias.parent.mkdir(parents=True)
            alias.write_text("lts/example\n")
            (alias.parent / "example").write_text("20.1\n")
            for want, got in [("2", "v2.9.0"), ("v20", "v20.11.12"), ("20.1", "v20.1.10"),
                              ("v20.1.9", "v20.1.9"), ("lts/*", "v20.1.10")]:
                with self.subTest(want=want):
                    (wt / ".nvmrc").write_text(f"# project version\n{want} # local\n")
                    self.assertEqual(dev.node_path(wt), f"{wt}/versions/node/{got}/bin:/usr/bin")
            (wt / ".nvmrc").write_text("24")
            system_bin = wt / "system" / "bin"
            system_bin.mkdir(parents=True)
            node = system_bin / "node"
            node.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo v24.22.3; else echo Krypton; fi\n')
            node.chmod(0o755)
            with mock.patch.object(dev.R, "agent_path", return_value=str(system_bin)), \
                    mock.patch.dict(os.environ, NVM_DIR=str(wt / "empty-nvm")):
                for want in ("24", "lts/*", "lts/krypton"):
                    (wt / ".nvmrc").write_text(want)
                    self.assertEqual(dev.node_path(wt), str(system_bin))
                (wt / ".nvmrc").write_text("lts/other")
                with self.assertRaisesRegex(devserver.DevError, "Node lts/other"):
                    dev.node_path(wt)
                (wt / ".nvmrc").write_text("21")
                with self.assertRaisesRegex(devserver.DevError, "Node 21"):
                    dev.node_path(wt)


class WorktreeDevTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.wt = self.root / "ticket with spaces"
        self.wt.mkdir()
        (self.wt / "index.html").write_text("ticket preview")
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        self.con.execute("INSERT INTO runs(repo, worktree_path) VALUES('site', ?)", (str(self.wt),))
        self.con.commit()
        self.port = free_port()
        self.cmd = f"exec {shlex.quote(sys.executable)} -m http.server {{port}} --bind 127.0.0.1"
        patch = mock.patch.object(devserver, "LOGS", self.root / "logs")
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("ticket_pilot.pipeline.repo_extra", side_effect=self.repo_settings)
        self.detect = patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.stop)

    def repo_settings(self, repo):
        self.assertEqual(repo, "site")  # the ticket directory's basename is not the configured repo
        return {"dev": {"cmd": self.cmd, "port": self.port}}

    def stop(self):
        dev.stop(self.con, self.wt)

    def test_wait_then_reuse_from_canonical_path(self):
        self.cmd = f"sleep 0.4; {self.cmd}"
        started = dev.start(self.con, self.wt)
        self.assertFalse(started["reused"])
        with urlopen(started["url"], timeout=2) as response:
            self.assertEqual(response.read(), b"ticket preview")
        alias = self.root / "alias"
        alias.symlink_to(self.wt, target_is_directory=True)
        reused = dev.start(self.con, alias, free_port())
        self.assertEqual(reused, {**started, "reused": True})
        self.detect.assert_called_once()
        self.assertEqual(dev.status(self.con, alias)["status"], "running")
        self.stop()
        self.assertFalse(devserver.port_open(started["port"]))
        self.assertEqual(dev.status(self.con, self.wt), {"status": "stopped"})
        self.assertTrue(Path(started["log"]).is_file())

    def test_stale_pid_and_foreign_port_are_never_adopted_or_stopped(self):
        # Keep the unrelated group leader alive until this test closes its stdin, even on a slow runner.
        with socket.socket() as foreign, \
                subprocess.Popen(["cat"], stdin=subprocess.PIPE, start_new_session=True) as proc:
            foreign.bind(("127.0.0.1", self.port))
            foreign.listen(10)
            stale = {"pid": proc.pid, "port": self.port, "log": str(self.root / "old.log")}
            core.meta_set(self.con, dev.KEY, {str(self.wt): stale})
            self.assertEqual(dev.status(self.con, self.wt)["status"], "stopped")
            Path(stale["log"]).touch()  # a retained log alone is no proof of ownership either
            result = dev.start(self.con, self.wt)
            self.assertNotEqual(result["port"], self.port)
            self.assertFalse(result["reused"])
            self.stop()
            self.assertTrue(devserver.port_open(self.port))
            self.assertIsNone(proc.poll())

    def test_exit_and_timeout_report_log_and_release_the_registration(self):
        idle = f"exec {shlex.quote(sys.executable)} -c 'import signal; signal.pause()'"
        for command, message in [("exit 7", "종료"), (idle, "포트가 열리지")]:
            with self.subTest(command=command), mock.patch.object(dev, "READY_TIMEOUT", 0.4):
                self.cmd = command
                with self.assertRaisesRegex(devserver.DevError, message) as err:
                    dev.start(self.con, self.wt)
                log = Path(str(err.exception).split("로그: ")[1])
                self.assertIn(command, log.read_text())
                self.assertIsNone(dev.record(self.con, self.wt))

    def test_explicit_port_for_script_without_a_default(self):
        (self.wt / "package.json").write_text(json.dumps({"scripts": {"dev": "node index.js"}}))
        (self.wt / "package-lock.json").touch()
        with mock.patch("ticket_pilot.pipeline.repo_extra", return_value={}):
            self.assertEqual(devserver.detect("site", self.wt, port_hint=self.port)["port"], self.port)

    def test_configured_vite_can_take_the_selected_port(self):
        for invocation in ("cd web && vite", "vite build && vite preview"):
            with self.subTest(invocation=invocation):
                self.cmd = invocation + " --host 127.0.0.1"
                detected = devserver.detect("site", self.wt)
                self.assertEqual(detected["cmd"], self.cmd)  # milestone command
                self.assertEqual(detected["port_cmd"], invocation + " --port {port} --strictPort --host 127.0.0.1")

    def test_background_listener_with_redirected_logs_stops_with_the_group(self):
        self.cmd = self.cmd.removeprefix("exec ") + " >app.log 2>&1 &"
        server = dev.start(self.con, self.wt)
        self.assertEqual(dev.start(self.con, self.wt)["pid"], server["pid"])
        self.stop()
        self.assertFalse(devserver.port_open(server["port"]))

    def test_stop_rechecks_the_group_when_signalling_is_denied(self):
        log = self.root / "server.log"
        log.touch()
        with subprocess.Popen(["/bin/sh", "-c", "read -r line", str(log)], stdin=subprocess.PIPE,
                              start_new_session=True) as proc:
            rec = {"pid": proc.pid, "port": self.port, "log": str(log)}
            core.meta_set(self.con, dev.KEY, {str(self.wt): rec})
            with mock.patch.object(os, "killpg", side_effect=PermissionError), self.assertRaises(PermissionError):
                self.stop()
            self.assertEqual(dev.record(self.con, self.wt), rec)

            # Darwin can return EPERM if the group exits just before killpg().
            killpg = os.killpg

            def exit_before_signal(group, sig):
                if sig == 0:
                    return killpg(group, 0)
                proc.stdin.close()
                proc.wait(timeout=5)
                raise PermissionError

            with mock.patch.object(os, "killpg", side_effect=exit_before_signal):
                self.stop()
            self.assertIsNone(dev.record(self.con, self.wt))

    def test_stop_kills_a_child_that_ignores_term_and_closes_the_log(self):
        (self.wt / "serve.py").write_text(
            "import os, signal, socket, time\n"
            "os.closerange(3, 1024)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', int(os.environ['PORT'])))\n"
            "s.listen()\n"
            "while os.path.exists(__file__): time.sleep(0.1)\n")
        self.cmd = f"{shlex.quote(sys.executable)} serve.py >app.log 2>&1 &"
        server = dev.start(self.con, self.wt)
        self.stop()
        self.assertFalse(devserver.port_open(server["port"]))

    def test_listening_child_outlives_leader_and_can_still_be_stopped(self):
        self.cmd += " >app.log 2>&1"
        started = dev.start(self.con, self.wt)
        os.kill(started["pid"], signal.SIGKILL)
        os.waitpid(started["pid"], 0)
        self.assertTrue(dev.start(self.con, self.wt)["reused"])
        self.stop()
        self.assertFalse(devserver.port_open(started["port"]))

    def test_port_taken_before_bind_retries_after_child_exit(self):
        launch = devserver._launch
        with socket.socket() as foreign:
            def race(wt, root, port, cmd, *args, **kwargs):
                if port == self.port:
                    foreign.bind(("127.0.0.1", port))
                    foreign.listen(10)
                    pid = launch(wt, root, port, "exit 1", *args, **kwargs)
                    os.waitpid(pid, 0)  # first readiness poll sees the bind failure after the child has exited
                    return pid
                return launch(wt, root, port, cmd, *args, **kwargs)

            with mock.patch.object(devserver, "_launch", side_effect=race):
                started = dev.start(self.con, self.wt)
            self.assertNotEqual(started["port"], self.port)
            self.assertEqual(started["status"], "running")

    def test_removing_worktree_rejects_start(self):
        self.con.execute("INSERT INTO cleanups(path, status) VALUES(?, 'removing')", (str(self.wt),))
        with self.assertRaisesRegex(devserver.DevError, "정리 중"):
            dev.start(self.con, self.wt)
        self.detect.assert_not_called()

    def test_fixed_port_command_moves_and_leaves_the_other_server_running(self):
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", self.port))
            foreign.listen()
            server = self.wt / "serve.py"
            server.write_text("import argparse, http.server\n"
                              "p = argparse.ArgumentParser()\np.add_argument('--port', type=int)\n"
                              "http.server.HTTPServer(('127.0.0.1', p.parse_args().port), "
                              "http.server.SimpleHTTPRequestHandler).serve_forever()\n")
            self.cmd = f"exec {shlex.quote(sys.executable)} serve.py --port={self.port}"
            started = dev.start(self.con, self.wt)
            self.assertNotEqual(started["port"], self.port)
            self.assertIn(f"--port={started['port']}", Path(started["log"]).read_text())
            self.assertTrue(devserver.port_open(self.port))

    def test_cleanup_ignores_only_the_managed_group_and_stops_it_on_removal(self):
        managed = dev.start(self.con, self.wt)
        with mock.patch.object(cleanup.R, "root", return_value=self.root):
            runs = [{"ticket_gid": "t1", "key": "T-1", "repo": "site", "repo_path": str(self.root),
                     "branch": "tp/t-1", "github": "org/site"}]
            stats = {"checked": 0, "removed": 0}
            cleanup._clean_skipped(self.con, str(self.wt), runs, {}, {}, True, stats, lambda s: None)
            self.assertEqual(stats["removed"], 1)
        self.assertTrue(devserver.port_open(managed["port"]))
        # A second listener in this worktree must still keep it, even if it uses the same executable.
        with subprocess.Popen([sys.executable, "-u", "-c",
                               "import socket, sys; s=socket.socket(); s.bind(('127.0.0.1', 0)); "
                               "s.listen(); print(s.getsockname()[1]); sys.stdin.read()"], cwd=self.wt,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as foreign:
            self.assertTrue(foreign.stdout.readline().strip().isdigit())
            self.assertIn(str(foreign.pid), cleanup._busy(self.con, str(self.wt), {}, {}))
            foreign.stdin.close()
        with mock.patch.object(cleanup.R, "remove") as remove:
            remove.side_effect = lambda *a, **kw: self.assertFalse(devserver.port_open(managed["port"]))
            cleanup.remove(self.con, str(self.wt), str(self.root), "")
            remove.assert_called_once()
        self.assertIsNone(dev.record(self.con, self.wt))
