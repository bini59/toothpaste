"""Milestone dev servers (devserver.py): command/port detection, the .env check, and start() with a port in use
(refused, kill, other port) against real processes. Run: python3 -m unittest discover tests"""
import json
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core
from ticket_pilot import devserver as DS

GID = "ms:P1:30_Api"


def listen_proc(port: int) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if DS.port_open(port):
            return p
        time.sleep(0.05)
    raise RuntimeError("listener did not start")


def free() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.extra = mock.patch("ticket_pilot.pipeline.repo_extra", lambda repo: self.conf)
        self.extra.start()
        self.conf = {}

    def tearDown(self):
        self.extra.stop()

    def pkg(self, scripts, lock="pnpm-lock.yaml"):
        (self.d / "package.json").write_text(json.dumps({"scripts": scripts}))
        (self.d / lock).write_text("")

    def test_next_gets_port_flag_and_default_3000(self):
        self.pkg({"dev": "next dev --turbopack"})
        d = DS.detect("Site", self.d)
        self.assertEqual((d["cmd"], d["port"], d["fixed"]), ("pnpm run dev --port {port}", 3000, False))

    def test_vite_strict_port_npm_separator(self):
        self.pkg({"dev": "remix vite:dev"}, lock="package-lock.json")
        d = DS.detect("Web", self.d)
        self.assertEqual((d["cmd"], d["port"]), ("npm run dev -- --port {port} --strictPort", 5173))

    def test_nest_uses_env_port(self):
        self.pkg({"start": "nest start", "start:dev": "nest start --watch"}, lock="yarn.lock")
        (self.d / ".env").write_text("NODE_ENV=local\nPORT=4100\nDB_PORT=5432\n")
        d = DS.detect("Api", self.d)
        self.assertEqual((d["cmd"], d["port"]), ("yarn run start:dev", 4100))

    def test_hard_coded_port_is_fixed(self):
        self.pkg({"dev": "next dev -p 3005"})
        d = DS.detect("X", self.d)
        self.assertEqual((d["cmd"], d["port"], d["fixed"]), ("pnpm run dev", 3005, True))

    def test_setting_wins(self):
        self.pkg({"dev": "next dev"})
        self.conf = {"dev": {"cmd": "make serve P={port}", "port": 8100}}
        d = DS.detect("X", self.d)
        self.assertEqual((d["cmd"], d["port"]), ("make serve P={port}", 8100))

    def test_configured_package_runner_uses_the_named_script_for_ticket_port_detection(self):
        self.pkg({"dev": "next dev", "ui": "vite --port 6200 & wait"})
        for cmd in ("npm run ui", "pnpm run-script ui", "yarn ui"):
            with self.subTest(cmd=cmd):
                self.conf = {"dev": {"cmd": cmd}}
                result = DS.detect("Site", self.d, ticket=True)
                self.assertTrue(result["cmd"].endswith(" " + cmd))
                self.assertEqual(result["port"], 6200)
                with self.assertRaises(DS.DevError):
                    DS.detect("Site", self.d)  # milestone preflight still requires its configured port

    def test_ticket_detects_and_replaces_only_port_option_words(self):
        for open_arg in ('"/docs --port 5173 --strictPort"', "'/docs --port 5173 --strictPort'",
                         r"/docs\ --port\ 5173\ --strictPort"):
            for port_arg in ('--port 6100', '--port="6100"', "'--port=6100'", "-p '6100'"):
                with self.subTest(open_arg=open_arg, port_arg=port_arg):
                    self.conf = {"dev": {"cmd": f"vite --open {open_arg} {port_arg}"}}
                    result = DS.detect("Site", self.d, ticket=True)
                    self.assertEqual(result["port"], 6100)
                    argv = shlex.split(result["cmd"].format(port=6000))
                    self.assertEqual(argv[:4], ["vite", "--strictPort", "--open", "/docs --port 5173 --strictPort"])
                    self.assertIn(argv[4:], [["--port", "6000"], ["--port=6000"], ["-p", "6000"]])

    def test_ticket_ignores_port_text_in_non_options(self):
        for suffix in ('--open "/docs --port 6100"', '> "--port=6100"', '# --port 6100', '-- --port 6100'):
            with self.subTest(suffix=suffix):
                command = f"vite {suffix}"
                self.conf = {"dev": {"cmd": command}}
                result = DS.detect("Site", self.d, ticket=True)
                self.assertEqual(result["port"], 5173)
                self.assertEqual(result["cmd"], f"vite --port {{port}} --strictPort {suffix}")

    def test_env_check_reports_missing_and_empty_keys_without_values(self):
        main = Path(tempfile.mkdtemp())
        (main / ".env").write_text("A=1\nB=\n")
        (self.d / ".env.example").write_text("A=\nB=\nC=x\n")
        r = DS.env_check("X", str(main), self.d)
        self.assertEqual(r["copied"], [".env"])
        self.assertEqual(r["missing"], {".env.example": ["B", "C"]})
        self.assertNotIn("1", " ".join(r["warnings"]))


class StartTest(unittest.TestCase):
    def setUp(self):
        self.dbf = tempfile.mktemp(suffix=".db")
        con = sqlite3.connect(self.dbf)
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        core._migrate(con)
        con.commit()
        con.close()
        self.wt = Path(tempfile.mkdtemp())
        (self.wt / "node_modules").mkdir()
        self.port = free()
        self.ps = []

        def db():
            c = sqlite3.connect(self.dbf)
            c.row_factory = sqlite3.Row
            return c
        self.patches = [mock.patch.object(DS, "db", db),
                        mock.patch.object(DS, "LOGS", Path(tempfile.mkdtemp())),
                        mock.patch.object(DS, "worktree", lambda con, gid: (self.wt, "30_Api", "/x", [])),
                        mock.patch.object(DS, "env_check", lambda *a: {}),
                        mock.patch.object(DS, "detect", lambda repo, wt: {
                            "cmd": f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1", "port": self.port,
                            "how": "test", "fixed": False})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        DS.stop(GID)
        for p in self.ps:
            p.kill()
            p.wait()
        for p in self.patches:
            p.stop()

    def wait_listen(self, port):
        for _ in range(100):
            if DS.port_open(port):
                return True
            time.sleep(0.05)
        return False

    def test_busy_port_refused_then_other_port(self):
        self.ps.append(listen_proc(self.port))
        with self.assertRaises(DS.PortBusy) as e:
            DS.start(GID)
        info = e.exception.info
        self.assertEqual(info["port"], self.port)
        self.assertEqual([b["pid"] for b in info["busy"]], [self.ps[0].pid])
        self.assertNotEqual(info["free"], self.port)
        s = DS.start(GID, port=info["free"])
        self.assertEqual(s["port"], info["free"])
        self.assertTrue(self.wait_listen(info["free"]))
        self.assertIsNone(self.ps[0].poll())  # the other process was left alone

    def test_milestone_respects_an_unbound_ticket_port(self):
        with mock.patch("ticket_pilot.worktree_dev.reserved_ports", return_value={self.port: "/ticket"}):
            with self.assertRaises(DS.PortBusy) as err:
                DS.start(GID)
            self.assertNotEqual(err.exception.info["free"], self.port)
            check = DS.check(GID)
            self.assertFalse(check["ours"])  # keep the dashboard's milestone-only classification
            self.assertEqual(check["busy"][0]["cwd"], "/ticket")

    def test_kill_then_start_on_same_port(self):
        other = listen_proc(self.port)
        self.ps.append(other)
        s = DS.start(GID, kill=True)
        other.wait(5)
        self.assertIsNotNone(other.returncode)
        self.assertTrue(self.wait_listen(self.port))
        self.assertTrue(DS.status_one(s)["alive"])
        with self.assertRaises(DS.DevError):
            DS.start(GID)  # already running
        self.assertTrue(DS.stop(GID))
        for _ in range(50):
            if not DS.port_open(self.port):
                break
            time.sleep(0.1)
        self.assertFalse(DS.port_open(self.port))


if __name__ == "__main__":
    unittest.main()

