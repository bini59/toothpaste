"""TP-165 tp mode: the Docker environment check and the host <-> Docker switch. docker is a fake (FakeDocker keeps the
"volume": its tp.db and secrets); launchd / systemd, the 401 wait and the tick lock are mocks; the host secrets use the
file store. Run: python3 -m unittest discover tests"""
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core, mode as M, server as S

TOKEN = "ghp_SECRETvalue123"


def done(cmd, out=""):
    return subprocess.CompletedProcess(cmd, 0, out, "")


class FakeDocker:
    """`docker compose ...` as tp mode calls it, against a folder that stands in for the tp-data volume."""

    def __init__(self, vol: Path):
        self.vol, self.calls, self.secrets, self.running, self.busy, self.fail = vol, [], {}, False, [], set()

    def __call__(self, cmd, check=True, quiet=True, input=None, timeout=0, cwd=None):
        self.calls.append((cmd, input))
        sub = cmd[2] if cmd[:2] == ["docker", "compose"] else None
        if sub in self.fail:
            self.fail.discard(sub)
            raise RuntimeError(f"docker compose {sub} 실패 (fake)")
        if sub == "ps":
            return done(cmd, "abc\n" if self.running else "")
        if sub == "exec":
            return done(cmd, "\n".join(self.busy))
        if sub in ("up", "stop", "down"):
            self.running = sub == "up"
        if sub == "run":
            script = cmd[-1]
            if "--mount" in cmd:  # the real CLI: unknown flag (compose run only takes -v)
                raise RuntimeError("docker compose run 실패 (1): unknown flag: --mount")
            mnt = next((cmd[i + 1] for i, x in enumerate(cmd) if x == "-v"), "")
            src = mnt.split(":")[0]
            if "/import" in mnt:  # the DB into the volume
                name = re.search(r'/import/([^"]+)"', script).group(1)
                (self.vol / "tp.db").write_bytes(Path(src, name).read_bytes())
            elif "/export" in mnt:  # the volume's DB out
                (Path(src) / "tp.db").write_bytes((self.vol / "tp.db").read_bytes())
            elif script == M.SECRETS_IN:
                self.secrets = json.loads(input)
                return done(cmd, "ok\n")
            elif script == M.SECRETS_OUT:
                return done(cmd, json.dumps(self.secrets) + "\n")
        return done(cmd)

    def subs(self):
        return [c[2] for c, _ in self.calls if c[:2] == ["docker", "compose"]]


class ModeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = self.t = Path(os.path.realpath(tmp.name))
        self.home, self.dock, vol = t / "home", t / "docker", t / "vol"
        for d in (self.home / ".claude", self.dock, vol, t / "app" / "data", t / "repoA"):
            d.mkdir(parents=True)
        self.db = t / "app" / "data" / "tp.db"
        self.docker = FakeDocker(vol)
        self.addCleanup(mock.patch.stopall)
        for p in (mock.patch.object(core, "DB_PATH", self.db), mock.patch.object(core, "_CON", None),
                  mock.patch.dict(os.environ, {"HOME": str(self.home), "TP_MODE": "", "TP_LOG_DIR": str(t / "logs")}),
                  mock.patch.object(core, "KEYCHAIN", False),
                  mock.patch.object(core, "SECRETS_FILE", self.home / ".config" / "ticket-pilot" / "secrets.json"),
                  mock.patch.object(M, "DOCKER_DIR", self.dock), mock.patch.object(M, "OVERRIDE", self.dock / "compose.override.yaml"),
                  mock.patch.object(M, "ENV_FILE", self.dock / ".env"), mock.patch.object(M, "MAC", False),
                  mock.patch.object(M, "_sh", self.docker), mock.patch.object(M, "_wait"),
                  mock.patch.object(M, "_github_login", return_value="octo"),
                  mock.patch.object(M, "_services_installed", return_value=True),
                  mock.patch.object(M.migrate, "tick_lock", return_value=None)):
            p.start()
        self.stop = mock.patch.object(M, "_stop_services").start()
        self.install = mock.patch.object(M, "_install").start()
        self.check = mock.patch.object(M, "check", return_value=[{"id": "docker", "title": "docker", "ok": True}]).start()
        self.addCleanup(lambda: core._CON and core._CON.close())  # runs first
        core.config_cache_clear()
        self.addCleanup(core.config_cache_clear)
        con = core.db()
        core.write_config(con, {"settings": {"worktree_root": str(t / "wts"), "clone_root": "~/repos", "dashboard_port": 4100},
                                "project_map": {"repos": {"a": {"path": str(t / "repoA"), "github": "o/a"},
                                                          "sub": {"path": str(t / "repoA" / "x"), "github": "o/b"}},
                                                "projects": []}})
        core.load_map(con)
        con.commit()
        core.secret_set(core.GITHUB_KEYCHAIN, "github", TOKEN)

    def run_mode(self, *a, **kw) -> str:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                M.switch(*a, **kw)
            except SystemExit as e:
                raise SystemExit(out.getvalue() + str(e.code)) from None
        return out.getvalue()

    def to_docker(self) -> str:
        return self.run_mode("docker", apply=True)

    def test_preview_changes_nothing(self):
        out = self.run_mode("docker")
        self.assertIn("미리보기", out)
        self.assertIn(str(self.t / "repoA"), out)
        self.assertIn("관리자 (TP_ADMINS): octo", out)
        self.assertNotIn(TOKEN, out)
        self.assertFalse(M.state_path().exists())
        self.assertEqual(list(self.dock.iterdir()), [])
        self.assertEqual(self.docker.subs(), [])
        self.stop.assert_not_called()
        self.assertTrue(os.access(self.db, os.W_OK))
        self.assertEqual(M.current(), "linux")

    def test_running_agent_or_failed_check_refuses(self):
        with mock.patch.object(M.migrate, "busy", return_value=["티켓 에이전트 T-1 (/w)"]):
            with self.assertRaises(SystemExit) as e:
                self.run_mode("docker", apply=True)
        self.assertIn("실행 중: 티켓 에이전트 T-1", str(e.exception))
        self.check.return_value = [{"id": "daemon", "title": "Docker 실행 중", "ok": False, "detail": "꺼짐", "fix": "켜세요"}]
        with self.assertRaises(SystemExit) as e:
            self.run_mode("docker", apply=True)
        self.assertIn("Docker 실행 중: 꺼짐 → 켜세요", str(e.exception))
        self.assertFalse(M.state_path().exists())
        self.assertEqual(self.docker.subs(), [])
        self.stop.assert_not_called()

    def test_no_admins_refuses(self):
        with mock.patch.object(M, "_github_login", return_value=None), self.assertRaises(SystemExit) as e:
            self.run_mode("docker", apply=True)
        self.assertIn("--admins", str(e.exception))
        with self.assertRaises(SystemExit):
            self.run_mode("docker", admins="bad name;rm")

    def test_to_docker_mounts_tokens_and_readonly_db(self):
        out = self.to_docker()
        self.assertEqual(self.docker.subs(), ["build", "run", "run", "up"])
        self.stop.assert_called_once()
        # the override: every folder at the same path (repoA/x is under repoA), host uid / gid / home
        y = M.OVERRIDE.read_text()
        for p in (self.t / "repoA", self.t / "wts", self.home / "repos", self.home / ".claude"):
            self.assertIn(f'source: "{p}"\n        target: "{p}"', y, p)
        self.assertNotIn("repoA/x", y)
        self.assertNotIn(".codex", y)  # missing on the host: not mounted (docker would create it as root)
        self.assertIn(f'TP_UID: "{os.getuid()}"', y)
        self.assertIn(f'HOME: "{self.home}"', y)
        self.assertTrue((self.home / "repos").is_dir())  # clone_root made before mounting
        self.assertEqual(M.ENV_FILE.read_text(), "TP_ADMINS=octo\nTP_PORT=4100\n")
        # the DB went into the volume; the host one is read-only now
        self.assertEqual(sqlite3.connect(self.docker.vol / "tp.db").execute(
            "SELECT path FROM repos WHERE name='a'").fetchone()[0], str(self.t / "repoA"))
        self.assertFalse(os.stat(self.db).st_mode & 0o222)
        # the token: over stdin only, never in argv, output, the state file or the log
        self.assertEqual(self.docker.secrets, {core.GITHUB_KEYCHAIN: {"github": TOKEN}})
        self.assertFalse(any(TOKEN in " ".join(c) for c, _ in self.docker.calls))
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN, M.state_path().read_text())
        self.assertIn("http://127.0.0.1:4100", out)
        self.assertEqual(M.current(), "docker")
        self.assertIn("이미 Docker", self.run_mode("docker", apply=True))

    def test_failure_then_resume(self):
        self.docker.fail.add("up")
        with self.assertRaises(SystemExit) as e:
            self.to_docker()
        msg = str(e.exception)
        self.assertIn("끝난 단계: stop, backup, files, build, db, secrets", msg)
        self.assertIn("bin/tp mode docker --apply", msg)
        self.assertIn("docker compose down", msg)
        self.assertIn("launchd/install.sh", msg)  # the services were stopped: how to start them again
        self.assertTrue(os.access(self.db, os.W_OK))  # not read-only before the container runs
        # the other direction is refused while this one is unfinished
        with self.assertRaises(SystemExit) as e2:
            self.run_mode("host", apply=True)
        self.assertIn("먼저 끝내세요", str(e2.exception))
        out = self.to_docker()
        self.assertIn("이어서 실행", out)
        self.assertIn("이미지 빌드 (docker compose build): 이미 끝남", out)
        # the failed up is followed by a stop: a container that came up must not write the volume's copy
        self.assertEqual(self.docker.subs(), ["build", "run", "run", "up", "stop", "up"])
        self.assertEqual(M.current(), "docker")

    def test_second_run_refused_while_one_runs(self):
        lock = M.state_path().with_name("mode.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "w") as f:
            M.fcntl.flock(f, M.fcntl.LOCK_EX)
            with self.assertRaises(SystemExit) as e:
                self.to_docker()
        self.assertIn("다른 tp mode가 실행 중", str(e.exception))
        self.assertEqual(self.docker.subs(), [])

    def test_back_to_host(self):
        self.to_docker()
        # the container worked on: a new row in its DB, a new Asana token in its secrets file
        vdb = sqlite3.connect(self.docker.vol / "tp.db")
        vdb.execute("INSERT INTO events(ts,kind,message) VALUES('t','x','from docker')")
        vdb.commit()
        vdb.close()
        self.docker.secrets["ticket-pilot-asana"] = {"asana": "1/asana-tok"}
        self.docker.busy = ["티켓 에이전트 T-9"]
        with self.assertRaises(SystemExit) as e:
            self.run_mode("host", apply=True)
        self.assertIn("실행 중 (컨테이너): 티켓 에이전트 T-9", str(e.exception))
        self.docker.busy = []
        self.docker.calls.clear()
        out = self.run_mode("host", apply=True)
        self.assertEqual(self.docker.subs(), ["ps", "exec", "stop", "run", "run", "down"])
        self.install.assert_called_once()
        self.assertTrue(os.access(self.db, os.W_OK))
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE message='from docker'").fetchone()[0], 1)
        con.close()
        self.assertEqual(core.secret_get("ticket-pilot-asana", "asana"), "1/asana-tok")
        self.assertNotIn("asana-tok", out)
        self.assertEqual(M.current(), "linux")
        self.assertTrue(list((self.db.parent / "backups").glob("tp-*-host.db")))

    def test_update_and_setup_refused_after_switch(self):
        self.to_docker()  # deploy.yml -> tp update -> install.sh would start a second, host tick
        from ticket_pilot import cli
        ran = mock.Mock()
        with mock.patch.dict(os.environ, {"TP_MODE": ""}), self.assertRaises(SystemExit) as e:
            cli._not_docker(ran)(None)
        self.assertIn("docker compose up -d --build", str(e.exception))
        ran.assert_not_called()

    def test_back_to_host_skips_services_that_were_not_there(self):
        with mock.patch.object(M, "_services_installed", return_value=False):
            self.to_docker()
        self.stop.assert_not_called()
        self.run_mode("host", apply=True)
        self.install.assert_not_called()


class CheckTest(unittest.TestCase):
    def check(self, which="/usr/bin/docker", info=0, compose="2.29.1", free=100, arch="x86_64", mac=False, shared=None,
              paths=()):
        def sh(cmd, **kw):
            if cmd[1] == "info":
                return subprocess.CompletedProcess(cmd, info, "27.0" if not info else "", "Cannot connect" if info else "")
            return subprocess.CompletedProcess(cmd, 0 if compose else 1, compose or "", "")
        with mock.patch.object(M.shutil, "which", return_value=which), mock.patch.object(M, "_sh", sh), \
                mock.patch.object(M.shutil, "disk_usage", return_value=mock.Mock(free=free * 2**30)), \
                mock.patch.object(M.platform, "machine", return_value=arch), mock.patch.object(M, "MAC", mac), \
                mock.patch.object(M, "_mac_shared", return_value=shared or M.MAC_SHARED):
            return {c["id"]: c for c in M.check(paths)}

    def test_all_pass(self):
        r = self.check()
        self.assertTrue(all(c["ok"] for c in r.values()), r)
        self.assertNotIn("sharing", r)  # Linux has no Docker Desktop file sharing

    def test_each_failure_says_why_and_how(self):
        r = self.check(which=None)
        self.assertEqual([k for k, c in r.items() if not c["ok"]], ["docker", "daemon", "compose"])
        self.assertIn("docs.docker.com/engine/install", r["docker"]["fix"])
        self.assertIn("확인하지 못했어요", r["daemon"]["detail"])
        r = self.check(info=1)
        self.assertFalse(r["daemon"]["ok"])
        self.assertIn("Cannot connect", r["daemon"]["detail"])
        self.assertIn("systemctl start docker", r["daemon"]["fix"])
        self.assertFalse(self.check(compose="1.29.2")["compose"]["ok"])
        self.assertIn("compose/install", self.check(compose="")["compose"]["fix"])
        r = self.check(free=1)
        self.assertFalse(r["disk"]["ok"])
        self.assertIn("1.0GB 남음", r["disk"]["detail"])
        self.assertFalse(self.check(arch="riscv64")["arch"]["ok"])
        self.assertTrue(self.check(arch="aarch64")["arch"]["ok"])
        r = self.check(mac=True, shared=["/home"], paths=["/home/u/repo", "/Volumes/ext/repo"])
        self.assertFalse(r["sharing"]["ok"])
        self.assertIn("/Volumes/ext/repo", r["sharing"]["fix"])
        self.assertNotIn("/home/u/repo", r["sharing"]["detail"])
        self.assertIn("Docker Desktop을 실행", self.check(mac=True, info=1)["daemon"]["fix"])


class PiecesTest(unittest.TestCase):
    def test_env_text_keeps_other_lines(self):
        old = "# c\nTP_ADMINS=\nTP_ORG=acme\n"
        self.assertEqual(M.env_text(old, {"TP_ADMINS": "a,b", "TP_PORT": 3032}),
                         "# c\nTP_ADMINS=a,b\nTP_ORG=acme\nTP_PORT=3032\n")

    def test_override_quotes_paths(self):
        y = M.override_yaml(["/home/u/my repo: x"], 501, 20, "/home/u", "/home/u/.gitconfig")
        self.assertIn('source: "/home/u/my repo: x"', y)
        self.assertIn("read_only: true", y)

    def test_inside_docker_refused(self):
        with mock.patch.dict(os.environ, {"TP_MODE": "docker"}), self.assertRaises(SystemExit) as e:
            M.main("host", apply=True)
        self.assertIn("호스트의 checkout", str(e.exception))
        with mock.patch.dict(os.environ, {"TP_MODE": "docker"}), self.assertRaises(SystemExit):
            S.start_mode("host")

    def test_dashboard_argv(self):
        with mock.patch.dict(os.environ, {"TP_MODE": ""}):
            self.assertEqual(S._mode_argv("docker", "a,b")[-4:], ["mode", "docker", "--admins", "a,b"])
            for bad in (("x", None), ("docker", "a b"), ("docker", "--apply")):
                with self.assertRaises(SystemExit):
                    S._mode_argv(*bad)


if __name__ == "__main__":
    unittest.main()
