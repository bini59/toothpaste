"""TP-122 tp migrate: a fake install in a temp folder (checkout with data/tp.db, one more repo, two worktrees, Claude
session folders) is moved; git links, DB paths, settings and sessions follow. Preview changes nothing, a running agent
refuses, a failure resumes. launchd is only ever touched through mocks. Run: python3 -m unittest discover tests"""
import contextlib
import errno
import hashlib
import io
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core, migrate as M


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


def munge(p):
    return "".join(c if c.isalnum() else "-" for c in str(p))


@unittest.skipUnless(sys.platform == "darwin", "tp migrate moves a macOS (launchd) install")
class MigrateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = home = Path(os.path.realpath(tmp.name)) / "home"
        self.old, self.repo, self.wts, self.to = home / "old" / "checkout", home / "repoA", home / "wts", home / "new"
        for r in (self.old, self.repo):
            r.mkdir(parents=True)
            git(r, "init", "-q", "-b", "main")
            (r / "f.txt").write_text("x")
            (r / ".gitignore").write_text("data/\n")
            git(r, "add", "-A")
            git(r, "commit", "-qm", "init")
        self.wt_a, self.wt_tp = self.wts / "repoA" / "t-1", self.wts / "tp" / "t-2"
        git(self.repo, "worktree", "add", "-q", "-b", "tp/t-1", self.wt_a)
        git(self.old, "worktree", "add", "-q", "-b", "tp/t-2", self.wt_tp)
        self.sessions = home / ".claude" / "projects"
        for p in (self.old, self.wt_a):
            (self.sessions / munge(p)).mkdir(parents=True)
            (self.sessions / munge(p) / "s.jsonl").write_text("{}")
        self.patched = False
        self.use(self.old)
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(lambda: core._CON and core._CON.close())  # runs first
        con = core.db()
        core.write_config(con, {"settings": {"worktree_root": "~/wts", "clone_root": "~/clones"}, "project_map": {
            "repos": {"tp": {"path": "~/old/checkout", "github": "o/tp"}, "repoA": {"path": str(self.repo), "github": "o/a"}},
            "projects": []}})
        core.load_map(con)
        log = self.old / "data" / "agents" / "t-1.log"
        con.execute("INSERT INTO runs(ticket_gid,repo,worktree_path,terminal,state) VALUES('1','repoA',?,?,'finished')",
                    (str(self.wt_a), f"999999 {log}"))
        con.execute("INSERT INTO checks(pr_url,head_sha,name,log_path) VALUES('u','s','lint',?)",
                    (str(self.old / "data" / "checks" / "a.log"),))
        con.execute("INSERT INTO cleanups(path,status) VALUES(?,'waiting')", (str(self.wt_tp),))
        con.execute("INSERT INTO config_check_runs(repo,log_path) VALUES('tp',?)", (str(self.old / "data" / "c.log"),))
        core.meta_set(con, "dev_servers", {"1": {"pid": 999999, "port": 1, "path": str(self.wt_a), "log": str(log)}})
        core.event(con, "x", f"sibling {self.old}-2 and {self.home}/wts.bak stay")
        con.commit()

    def use(self, root: Path):
        """Run as the tp of the checkout at root (what bin/tp there would see)."""
        if self.patched and core._CON is not None:  # ours, never the connection another test left in core
            core._CON.close()
        self.patched = True
        mock.patch.stopall()
        for p in (mock.patch.object(core, "ROOT", root), mock.patch.object(core, "DB_PATH", root / "data" / "tp.db"),
                  mock.patch.object(core, "_CON", None), mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                  mock.patch.object(M, "_launchd_stop"), mock.patch.object(M, "_install")):
            p.start()
        core.config_cache_clear()

    def run_migrate(self, apply=True, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            M.migrate("~/new", apply=apply, **kw)
        return out.getvalue()

    def tree(self):
        return sorted((str(p), hashlib.md5(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else "")
                      for p in self.home.rglob("*"))

    def dump(self) -> str:
        con = sqlite3.connect(self.to / "app" / "data" / "tp.db")
        try:
            return "\n".join(con.iterdump())
        finally:
            con.close()

    def test_preview_changes_nothing(self):
        before = self.tree()
        out = self.run_migrate(apply=False)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.to.exists())
        self.assertIn(f"{self.old} → {self.to}/app", out)
        self.assertIn(f"{self.wts} → {self.to}/worktrees", out)
        self.assertIn("runs.worktree_path: 1", out)
        M._launchd_stop.assert_not_called()

    def test_apply(self):
        out = self.run_migrate()
        app, wts = self.to / "app", self.to / "worktrees"
        self.assertFalse(self.old.exists() or self.wts.exists())
        # git: both repos list the new paths, and git works inside the worktrees (the checkout's own moved on both ends)
        self.assertIn(f"worktree {wts}/repoA/t-1\n", git(self.repo, "worktree", "list", "--porcelain"))
        self.assertIn(f"worktree {wts}/tp/t-2\n", git(app, "worktree", "list", "--porcelain"))
        for w in (wts / "repoA" / "t-1", wts / "tp" / "t-2"):
            self.assertEqual(git(w, "status", "--porcelain"), "")
        # DB: no old path in any spelling, but look-alike siblings are left alone
        dump = self.dump()
        sibling = f"sibling {self.old}-2 and {self.home}/wts.bak stay"
        self.assertIn(sibling, dump)
        for old in (str(self.old), str(self.wts), "~/wts", "~/old/checkout"):
            self.assertNotIn(old, dump.replace(sibling, ""))
        self.use(app)
        st, con = core.settings(), core.db()
        self.assertEqual((st["worktree_root"], st["clone_root"]), ("~/new/worktrees", "~/new/repos"))
        self.assertEqual({r["name"]: r["path"] for r in con.execute("SELECT name,path FROM repos")},
                         {"tp": str(app), "repoA": str(self.repo)})
        self.assertEqual(core.project_map()["repos"]["tp"]["path"], "~/new/app")
        r = con.execute("SELECT worktree_path, terminal FROM runs").fetchone()
        self.assertEqual(tuple(r), (f"{wts}/repoA/t-1", f"999999 {app}/data/agents/t-1.log"))
        self.assertEqual(core.meta_get(con, "dev_servers")["1"]["path"], f"{wts}/repoA/t-1")
        self.assertEqual(con.execute("SELECT path FROM cleanups").fetchone()[0], f"{wts}/tp/t-2")
        self.assertTrue(list((app / "data" / "backups").glob("tp-*.db")))
        # Claude sessions are copied to the new names, originals kept
        for p in (app, wts / "repoA" / "t-1"):
            self.assertTrue((self.sessions / munge(p) / "s.jsonl").is_file())
        self.assertTrue((self.sessions / munge(self.old)).is_dir())
        self.assertTrue((self.to / "repos").is_dir())
        self.assertIn("TP_DIR", out)
        M._launchd_stop.assert_not_called()  # no plist for this checkout
        # again, from the new checkout: nothing left to do
        before = self.dump()
        out = self.run_migrate()
        self.assertIn("이미 끝남", out)
        self.assertEqual(self.dump(), before)

    def test_refuses_running_agent_and_existing_target(self):
        p = subprocess.Popen(["sleep", "60"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        con = core.db()
        con.execute("INSERT INTO runs(ticket_gid,repo,worktree_path,terminal,state) VALUES('2','repoA',?,?,'active')",
                    (str(self.wt_a), f"{p.pid} {self.home}/none.log"))
        con.execute("INSERT INTO checks(pr_url,head_sha,name,status,created_at) VALUES('u2','s','lint','running',?)",
                    (core.now(),))
        con.commit()
        (self.to / "app").mkdir(parents=True)
        before = self.tree()
        with self.assertRaises(SystemExit) as e:
            self.run_migrate()
        self.assertIn("티켓 에이전트", str(e.exception))
        self.assertIn("tp check (PR 1개)", str(e.exception))
        self.assertIn(f"대상에 이미 있어요: {self.to}/app", str(e.exception))
        self.assertEqual(self.tree(), before)

    def test_launchd_of_this_checkout_is_stopped_and_reinstalled(self):
        la = self.home / "Library" / "LaunchAgents"
        la.mkdir(parents=True)
        (la / f"{M.LABELS[0]}.plist").write_bytes(plistlib.dumps(
            {"WorkingDirectory": str(self.old), "StandardOutPath": str(self.home / "oldlogs" / "tick.log")}))
        with mock.patch.dict(os.environ):
            os.environ.pop("TP_LOG_DIR", None)
            out = self.run_migrate()
        M._launchd_stop.assert_called_once()
        M._install.assert_called_once_with(f"{self.to}/app", f"{self.to}/logs")
        self.assertIn(f"예전 로그 폴더 정리: {self.home}/oldlogs", out)
        self.assertIn("launchctl bootout", M._undo(json.loads((self.to / M.STATE).read_text())))

    def test_failure_tells_how_to_undo_and_resumes(self):
        with mock.patch.object(M, "_repair", side_effect=RuntimeError("boom")), self.assertRaises(SystemExit) as e:
            self.run_migrate()
        msg = str(e.exception)
        self.assertIn("boom", msg)
        self.assertIn(f"mv {self.to}/app {self.old}", msg)
        self.assertIn(f"rm {self.to}/{M.STATE}", msg)
        self.assertIn(f"git -C {self.repo} worktree repair {self.wt_a}", msg)  # repair may have run for some repos
        self.assertIn(f"{self.to}/app/bin/tp migrate --to {self.to} --apply", msg)
        self.assertFalse(self.old.exists())  # the empty-DB trap: nothing recreated the old data/
        self.assertEqual(json.loads((self.to / M.STATE).read_text())["done"], ["backup"])
        self.use(self.to / "app")
        out = self.run_migrate()
        self.assertIn("DB 백업: 이미 끝남", out)
        self.assertNotIn(str(self.wts) + "/", self.dump())
        late = mock.Mock(side_effect=lambda moves, apply: [] if not apply else exec('raise RuntimeError("late")'))
        with mock.patch.object(M, "_sessions", late), self.assertRaises(SystemExit) as e:
            (self.to / M.STATE).write_text((self.to / M.STATE).read_text().replace('"sessions"', '"x"'))
            self.run_migrate()
        self.assertRegex(str(e.exception), r"1\. cp \S+/new/app/data/backups/tp-\S+\.db \S+/new/app/data/tp\.db")
        self.assertEqual(git(self.to / "worktrees" / "tp" / "t-2", "status", "--porcelain"), "")

    def test_other_disk_asks_again(self):
        before = self.tree()
        with mock.patch.object(M, "_dev", lambda p: str(p) == str(self.wts)), self.assertRaises(SystemExit):
            self.run_migrate(confirm=lambda q: "n")
        self.assertEqual(self.tree(), before)
        real = os.rename

        def rename(a, b):  # the worktree root cannot be renamed across disks: copy, then delete
            if str(a) == str(self.wts):
                raise OSError(errno.EXDEV, "cross-device")
            real(a, b)
        with mock.patch.object(M, "_dev", lambda p: str(p) == str(self.wts)), mock.patch.object(M.os, "rename", rename):
            self.assertIn("다른 디스크", self.run_migrate(confirm=lambda q: "y"))
        self.assertFalse(self.wts.exists())
        self.assertEqual(git(self.to / "worktrees" / "tp" / "t-2", "status", "--porcelain"), "")
        self.assertTrue((self.to / "worktrees" / "repoA" / "t-1" / "f.txt").is_file())


class LinuxTest(unittest.TestCase):
    def test_does_nothing_on_linux(self):
        out = io.StringIO()
        with mock.patch.object(M, "sys", mock.Mock(platform="linux")), mock.patch.object(M, "_plan") as plan, \
                mock.patch.object(M.core, "db") as db, contextlib.redirect_stdout(out):
            M.migrate("~/new", apply=True)
        plan.assert_not_called()
        db.assert_not_called()
        self.assertIn("macOS 예전 설치", out.getvalue())


class FixerTest(unittest.TestCase):
    def test_whole_path_prefix_only(self):
        fix = M._fixer({"/a/wt": "/n/worktrees", "~/wt": "~/n/worktrees"})
        self.assertEqual(fix('123 /a/wt/r/t.log "/a/wt" ~/wt.'), '123 /n/worktrees/r/t.log "/n/worktrees" ~/n/worktrees.')
        for keep in ("/a/wt-2/x", "/a/wt_old", "/a/wt.bak", "/x/a/wt/y", "~/wtx"):
            self.assertEqual(fix(keep), keep)


if __name__ == "__main__":
    unittest.main()
