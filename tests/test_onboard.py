"""TP-87: 프로젝트 추가 wizard backend (onboard.py): origin check, clone target inside clone_root, cached picker lists.
Run: python3 -m unittest discover tests"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core, onboard as OB


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


class PathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_norm_github_ignores_spelling(self):
        for u in ("https://github.com/Alice/Repo.git", "https://github.com/alice/repo", "git@github.com:alice/repo.git",
                  "ssh://git@github.com/alice/repo.git", "alice/repo"):
            self.assertEqual(OB.norm_github(u), "alice/repo", u)

    def test_check_path(self):
        self.assertEqual(OB.check_path(str(self.tmp / "nope"), "o/r"),
                         {"exists": False, "is_git": False, "origin": None, "matches": False})
        self.assertEqual(OB.check_path(str(self.tmp), "o/r")["is_git"], False)
        git("init", "-q", cwd=self.tmp)
        r = OB.check_path(str(self.tmp), "o/r")
        self.assertEqual((r["is_git"], r["origin"], r["matches"]), (True, None, False))
        git("remote", "add", "origin", "git@github.com:O/r.git", cwd=self.tmp)
        self.assertTrue(OB.check_path(str(self.tmp), "o/r")["matches"])
        r = OB.check_path(str(self.tmp), "o/other")
        self.assertEqual((r["origin"], r["matches"]), ("git@github.com:O/r.git", False))

    def test_subfolder_and_relative_path_refused(self):
        git("init", "-q", cwd=self.tmp)
        git("remote", "add", "origin", "https://github.com/o/r", cwd=self.tmp)
        (self.tmp / "web").mkdir()
        self.assertTrue(OB.check_path(str(self.tmp) + "/", "o/r")["matches"])
        self.assertEqual(OB.check_path(str(self.tmp / "web"), "o/r")["matches"], False)
        with self.assertRaises(SystemExit):
            OB.check_path("relative/dir", "o/r")

    def test_bad_github_refused(self):
        for g in ("o", "o/r/x", "o/r; rm -rf /", "", None):
            with self.assertRaises(SystemExit):
                OB.check_path(str(self.tmp), g)


class CloneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "projects"
        self.root.mkdir()
        self.p = mock.patch.object(OB, "clone_root", lambda: Path(os.path.realpath(self.root)))
        self.p.start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, "gh_env", return_value={}).start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_target_outside_clone_root_refused(self):
        self.assertEqual(OB.clone_target("my-repo"), Path(os.path.realpath(self.root)) / "my-repo")
        for n in ("..", ".", "../x", "a/b", "/etc", "", None):
            with self.assertRaises(SystemExit, msg=n):
                OB.clone_target(n)
        (self.root / "link").symlink_to(self.tmp)  # a name that resolves outside
        with self.assertRaises(SystemExit):
            OB.clone_target("link")

    def test_existing_target_not_cloned(self):
        (self.root / "r").mkdir()
        (self.root / "r" / "keep.txt").write_text("x")
        with mock.patch.object(OB.threading, "Thread") as th:
            job = OB.start_clone("o/r", "r")
        th.assert_not_called()
        self.assertEqual(job["status"], "exists")
        self.assertEqual((self.root / "r" / "keep.txt").read_text(), "x")

    def test_clone_runs_gh_with_args_and_reports_stderr(self):
        done = mock.Mock(returncode=1, stderr="repository not found", stdout="")
        with mock.patch.object(OB.subprocess, "run", return_value=done) as run, \
             mock.patch.object(OB.threading, "Thread", lambda target, args, daemon: mock.Mock(start=lambda: target(*args))):
            job = OB.start_clone("o/r", "r")
        self.assertEqual(run.call_args[0][0], ["gh", "repo", "clone", "o/r", str(Path(os.path.realpath(self.root)) / "r")])
        st = OB.clone_status(job["id"])
        self.assertEqual((st["status"], st["error"]), ("error", "repository not found"))
        self.assertIsNone(OB.clone_status(99999))


class ListTest(unittest.TestCase):
    def setUp(self):
        OB._cache.clear()
        pm = {"repos": {"shop": {"github": "https://github.com/O/Shop.git"}}, "projects": [], "hidden_projects": {"2": "Old"}}
        self.p = [mock.patch.object(core, "project_map", lambda: pm), mock.patch.object(core, "gh_env", return_value={}),
                  mock.patch.object(core, "settings", lambda: {"asana_workspace_gid": "1"}),
                  mock.patch.dict(os.environ, {"TP_ASANA_FIXTURE": "unused"}),
                  mock.patch.object(core, "_FIXTURE", {"/projects": {"data": [{"gid": "3", "name": "b"}, {"gid": "2", "name": "old"},
                                                                              {"gid": "1", "name": "A"}]}})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        OB._cache.clear()

    def test_github_cached_and_registered_filtered(self):
        out = mock.Mock(returncode=0, stdout='{"full_name":"o/shop","description":"z","default_branch":"main"}\n'
                                             '{"full_name":"o/new","description":null,"default_branch":"dev"}\n')
        with mock.patch.object(OB.subprocess, "run", return_value=out) as run:
            r1, r2 = OB.github_repos(), OB.github_repos()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(r1, r2)
            self.assertEqual([r["full_name"] for r in r1["repos"]], ["o/new"])
            OB.github_repos(refresh=True)
            self.assertEqual(run.call_count, 2)
            with mock.patch.object(OB, "CACHE_S", 0):
                OB.github_repos()
            self.assertEqual(run.call_count, 3)
        self.assertIn("--paginate", run.call_args[0][0])

    def test_github_error_is_a_message(self):
        with mock.patch.object(OB.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="not logged in", stdout="")):
            r = OB.github_repos()
        self.assertEqual(r["repos"], [])
        self.assertIn("not logged in", r["error"])

    def test_asana_cached_and_hidden_filtered(self):
        with mock.patch.object(core, "asana_all", wraps=core.asana_all) as aa:
            r = OB.asana_projects()
            OB.asana_projects()
            self.assertEqual(aa.call_count, 1)
        self.assertEqual(r, {"projects": [{"gid": "1", "name": "A"}, {"gid": "3", "name": "b"}]})

    def test_asana_error_is_a_message(self):
        with mock.patch.object(core, "asana_all", side_effect=core.AsanaError("Asana PAT not found")):
            r = OB.asana_projects()
        self.assertEqual((r["projects"], r["error"]), ([], "Asana PAT not found"))


if __name__ == "__main__":
    unittest.main()
