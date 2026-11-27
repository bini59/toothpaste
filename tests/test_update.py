"""tp update and version bumps (TP-140). Run: python3 -m unittest discover tests"""
import functools
import http.client
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ticket_pilot import core, server as S, update as U


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout.strip()


class BumpTest(unittest.TestCase):
    def test_bump(self):
        self.assertEqual(U.bump("v0.4.0", []), "v0.4.1")
        self.assertEqual(U.bump("v0.4.0", ["minor"]), "v0.5.0")
        self.assertEqual(U.bump("v0.4.0", ["major", "minor"]), "v1.0.0")
        self.assertEqual(U.bump(None, ["major"]), "v0.4.0")
        self.assertEqual(U.latest(["v0.9.0", "v0.10.0", "v1.0.0-rc1", "x"]), "v0.10.0")

    def test_release_skips_tagged_commit(self):
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            out = {"matching-refs": '[[{"ref": "refs/tags/v0.4.0", "object": {"sha": "aaa"}}]]',
                   "pulls": '[{"number": 7, "labels": [{"name": "minor"}]}]'}
            hit = next((v for key, v in out.items() if any(key in c for c in cmd)), "")
            return subprocess.CompletedProcess(cmd, 0, hit, "")

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "web" / "dist").mkdir(parents=True)
        (root / "web" / "dist" / "index.html").write_text("built")
        uploaded = []

        def run2(cmd, **k):
            if cmd[:3] == ["git", "-C", str(root)]:  # rev-parse HEAD
                return subprocess.CompletedProcess(cmd, 0, "bbb\n", "")
            if cmd[:3] == ["gh", "release", "upload"]:
                with tarfile.open(cmd[4]) as t:
                    uploaded.append(t.extractfile("./index.html").read())
            return run(cmd, **k)

        with mock.patch.object(U.subprocess, "run", side_effect=run2):
            self.assertIsNone(U.release("aaa", "o/r", root=root))
            self.assertEqual(U.release("bbb", "o/r", root=root), "v0.5.0")
            with self.assertRaisesRegex(U.UpdateError, "릴리즈 안 만듦"):  # web/dist built from another commit
                U.release("ccc", "o/r", root=root)
        self.assertFalse([c for c in calls if "ccc" in c])  # no tag without the asset
        self.assertIn(["gh", "release", "create", "v0.5.0", "--repo", "o/r", "--target", "bbb", "--generate-notes"], calls)
        self.assertEqual(uploaded, [b"built"])

    def test_release_given_version(self):
        """release.yml workflow_dispatch (TP-186): the given version instead of the label bump"""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "web" / "dist").mkdir(parents=True)
        (root / "web" / "dist" / "index.html").write_text("built")
        calls = []

        def run(cmd, **k):
            calls.append(cmd)
            out = "bbb\n" if cmd[0] == "git" else "[[]]" if "matching-refs" in " ".join(cmd) else "[]"
            return subprocess.CompletedProcess(cmd, 0, out, "")

        with mock.patch.object(U.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(U.UpdateError, "vX.Y.Z"):
                U.release("bbb", "o/r", root=root, version="0.5")
            self.assertEqual(U.release("bbb", "o/r", root=root, version="v0.5.0"), "v0.5.0")
        self.assertIn(["gh", "release", "create", "v0.5.0", "--repo", "o/r", "--target", "bbb", "--generate-notes"], calls)


class AuthEnvTest(unittest.TestCase):
    """TP-182: git/gh run with tp's GitHub token, not the owner's login (Docker mode has none)"""
    def git_env(self, tok, extra=None):
        seen = []
        with mock.patch.object(core, "github_token", return_value=tok), \
                mock.patch.dict(os.environ, {"GH_TOKEN": "owner", **(extra or {})}), \
                mock.patch.object(U.subprocess, "run", side_effect=lambda cmd, **k: seen.append(k["env"])
                                  or subprocess.CompletedProcess(cmd, 0, "", "")):
            if not extra:
                os.environ.pop("GITHUB_ACTIONS", None)
            U._git("/x", "fetch", "-q", "--tags", "origin")
        return seen[0]

    def test_token(self):
        env = self.git_env("tp-tok")
        self.assertEqual(env["GH_TOKEN"], "tp-tok")
        self.assertLessEqual(core.GIT_CREDENTIAL_ENV.items(), env.items())

    def test_no_token_no_owner_login(self):
        env = self.git_env(None)
        self.assertNotIn("GH_TOKEN", env)
        self.assertLessEqual(core.GIT_NO_CREDENTIAL_ENV.items(), env.items())

    def test_deploy_job_auth_kept(self):
        self.assertIsNone(self.git_env("tp-tok", {"GITHUB_ACTIONS": "true"}))  # inherits deploy.yml's extraHeader


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin, self.dev, self.co = self.tmp / "origin.git", self.tmp / "dev", self.tmp / "co"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", "-q", str(self.origin), str(self.dev))
        for c in (("user.email", "t@t"), ("user.name", "t")):
            git(self.dev, "config", *c)
        self.commit("README", "a")
        self.commit("web/src.ts", "1")
        git(self.dev, "push", "-q", "origin", "main")
        git(self.tmp, "clone", "-q", str(self.origin), str(self.co))
        (self.co / "web" / "dist").mkdir(parents=True)
        (self.co / "web" / "dist" / "index.html").write_text("x")
        (self.co / "web" / "node_modules").mkdir()
        self.calls = self.tmp / "calls"
        self.npm, self.install = self.fake("npm"), self.fake("install")
        self.gh = self.fake("gh", "exit 1")  # no release asset; gh_with_asset adds one
        self.asset = self.tmp / "asset.tar.gz"

    def fake(self, name, then=""):
        p = self.tmp / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> {self.calls}\n{then}\n')
        p.chmod(0o755)
        return p

    def commit(self, path, text):
        f = self.dev / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        git(self.dev, "add", path)
        git(self.dev, "commit", "-q", "-m", path)

    def tag(self, t):
        git(self.dev, "tag", t)
        git(self.dev, "push", "-q", "origin", "main", t)

    def run_update(self, to=None, build=False):
        self.calls.unlink(missing_ok=True)
        out = U.update(to, root=self.co, npm=str(self.npm), install=[str(self.install)], log=lambda m: None,
                       gh=str(self.gh), build=build)
        return out, self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_fast_forward_to_latest_tag_then_up_to_date(self):
        self.tag("v0.4.0")
        self.commit("ticket_pilot/x.py", "py")
        self.tag("v0.4.1")
        self.commit("web/src.ts", "2")  # on main after the latest tag: not taken
        git(self.dev, "push", "-q", "origin", "main")
        out, calls = self.run_update()
        self.assertEqual(out[1], "v0.4.1")
        self.assertEqual(git(self.co, "rev-parse", "HEAD"), git(self.dev, "rev-parse", "v0.4.1^{commit}"))
        self.assertEqual(calls, ["install "])  # web/ unchanged: no npm ci, no build
        self.assertFalse((self.co / "data" / "update.pending").exists())
        out, calls = self.run_update()
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_web_change_builds(self):
        self.commit("web/package-lock.json", "{}")
        self.tag("v0.4.0")
        _, calls = self.run_update()
        self.assertTrue(calls[0].startswith("gh release download v0.4.0 --pattern web-dist.tar.gz --dir "))
        self.assertEqual(calls[1:], ["npm ci --no-audit --no-fund", "npm run build", "install "])  # no asset: build

    def gh_with_asset(self, files):
        with tarfile.open(self.asset, "w:gz") as t:
            for name, text in files.items():
                f = self.tmp / "src" / name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)
                t.add(f, arcname="./" + name)
        self.gh = self.fake("gh", f'cp {self.asset} "$7/web-dist.tar.gz"')

    def test_download_replaces_dist_without_npm(self):
        self.commit("web/package-lock.json", "{}")
        self.tag("v0.4.0")
        (self.co / "web" / "dist" / "stale.js").write_text("old")
        self.gh_with_asset({"index.html": "new", "assets/a.js": "js"})
        _, calls = self.run_update()
        self.assertEqual([c.split(" --dir ")[0] for c in calls],
                         ["gh release download v0.4.0 --pattern web-dist.tar.gz", "install "])
        dist = self.co / "web" / "dist"
        self.assertEqual((dist / "index.html").read_text(), "new")
        self.assertEqual((dist / "assets" / "a.js").read_text(), "js")
        self.assertFalse((dist / "stale.js").exists())
        self.assertEqual([p.name for p in (self.co / "data").iterdir() if p.name.startswith("web-dist-")], [])

    def test_build_flag_and_sha_target_skip_download(self):
        self.commit("web/src.ts", "2")
        self.tag("v0.4.0")
        self.gh_with_asset({"index.html": "new"})
        _, calls = self.run_update(build=True)
        self.assertEqual(calls, ["npm run build", "install "])
        self.commit("web/src.ts", "3")
        git(self.dev, "push", "-q", "origin", "main")
        _, calls = self.run_update(to=git(self.dev, "rev-parse", "HEAD"))
        self.assertEqual(calls, ["npm run build", "install "])

    def test_broken_asset_keeps_old_dist(self):
        self.commit("web/src.ts", "2")
        self.tag("v0.4.0")
        self.asset.write_text("not a tarball")
        self.gh = self.fake("gh", f'cp {self.asset} "$7/web-dist.tar.gz"')
        with self.assertRaisesRegex(U.UpdateError, "압축 해제 실패"):
            self.run_update()
        self.assertEqual((self.co / "web" / "dist" / "index.html").read_text(), "x")
        self.gh_with_asset({"other.html": "no index"})
        with self.assertRaisesRegex(U.UpdateError, "index.html이 없어요"):
            self.run_update()
        self.assertEqual((self.co / "web" / "dist" / "index.html").read_text(), "x")
        self.assertNotIn("npm", self.calls.read_text())

    def test_failed_build_finishes_on_rerun(self):
        self.commit("web/src.ts", "2")
        self.tag("v0.4.0")
        self.npm.write_text("#!/bin/sh\nexit 1\n")
        with self.assertRaises(U.UpdateError):
            self.run_update()
        self.fake("npm")
        _, calls = self.run_update()  # already at the tag, but the last run stopped after the fast-forward
        self.assertEqual(calls[1:], ["npm run build", "install "])  # calls[0]: gh (no asset)

    def test_resume_after_manual_pull(self):
        self.commit("web/src.ts", "2")
        self.tag("v0.4.0")
        self.npm.write_text("#!/bin/sh\nexit 1\n")
        with self.assertRaises(U.UpdateError):
            self.run_update()
        self.fake("npm")
        self.commit("README", "b")
        self.tag("v0.4.1")
        self.commit("web/src.ts", "3")  # untagged, on main past v0.4.1
        git(self.dev, "push", "-q", "origin", "main")
        git(self.co, "pull", "-q", "--ff-only")  # someone pulled by hand past the stopped run's target
        self.gh_with_asset({"index.html": "v0.4.1"})
        _, calls = self.run_update()
        self.assertEqual(calls, ["npm run build", "install "])  # builds HEAD's web/, not v0.4.1's asset

    def test_npm_from_nvm(self):
        # launchd PATH (dashboard button) has no nvm node: npm is found in nvm's default version
        nvm = self.tmp / "nvm"
        (nvm / "alias").mkdir(parents=True)
        (nvm / "alias" / "default").write_text("22\n")
        bin_ = nvm / "versions" / "node" / "v22.1.0" / "bin"
        bin_.mkdir(parents=True)
        self.npm.rename(bin_ / "npm")
        self.commit("web/src.ts", "2")
        self.tag("v0.4.0")
        self.calls.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {"NVM_DIR": str(nvm), "PATH": "/usr/bin:/bin"}):
            U.update(root=self.co, install=[str(self.install)], log=lambda m: None, gh=str(self.gh))
        self.assertEqual(self.calls.read_text().splitlines()[1:], ["npm run build", "install "])

    def test_behind_head_is_up_to_date(self):
        self.tag("v0.4.0")
        self.run_update()
        self.commit("web/src.ts", "2")
        git(self.dev, "push", "-q", "origin", "main")
        git(self.co, "pull", "-q", "--ff-only")  # HEAD ahead of the latest tag (release job lagging)
        out, calls = self.run_update()
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_refuses(self):
        self.tag("v0.4.0")
        (self.co / "README").write_text("changed")
        with self.assertRaisesRegex(U.UpdateError, "수정 사항"):
            self.run_update()
        git(self.co, "checkout", "-q", "README")
        git(self.co, "checkout", "-q", "-b", "other")
        with self.assertRaisesRegex(U.UpdateError, "main 브랜치"):
            self.run_update()
        self.assertFalse(self.calls.exists())

    def test_check_latest(self):
        self.commit("ticket_pilot/x.py", "py")
        self.tag("v0.4.0")
        meta = {}
        with mock.patch.object(U, "meta_get", lambda con, k: meta.get(k)), \
                mock.patch.object(U, "meta_set", lambda con, k, v: meta.__setitem__(k, v)):
            self.assertEqual(U.check_latest(None, root=self.co)["latest"], "v0.4.0")
            self.assertTrue(U.newer(self.co, "v0.4.0"))
            self.tag("v0.4.1")
            self.assertEqual(U.check_latest(None, root=self.co)["latest"], "v0.4.0")  # within 6 hours: no fetch
            self.assertEqual(U.check_latest(None, root=self.co, force=True)["latest"], "v0.4.1")

    def test_check_min_setting(self):
        with mock.patch.object(U, "settings", return_value={"update": {"check_min": 10}}):
            self.assertEqual(U.check_every_s(), 600)

    def test_auto_update(self):
        """update.auto (TP-186): a new release starts tp update once; blocked skips, alerts once, retries next tick"""
        self.tag("v0.4.0")
        git(self.co, "fetch", "-q", "--tags", "origin")
        self.commit("ticket_pilot/x.py", "py")
        self.tag("v0.4.1")
        meta, alerts, starts = {}, [], []
        chk = {"latest": "v0.4.1"}
        git(self.co, "fetch", "-q", "--tags", "origin")
        conf = {"update": {"auto": False}}

        def blocked():
            raise SystemExit("checkout이 main 브랜치가 아니에요")

        with mock.patch.object(U, "meta_get", lambda con, k: meta.get(k)), \
                mock.patch.object(U, "meta_set", lambda con, k, v: meta.__setitem__(k, v)), \
                mock.patch.object(U, "settings", lambda: conf), \
                mock.patch("ticket_pilot.notifications.add", lambda *a: alerts.append(a[2])):
            self.assertIsNone(U.auto_update(None, chk, lambda: starts.append(1), root=self.co))  # off
            conf["update"]["auto"] = True
            self.assertIsNone(U.auto_update(None, {"latest": "v0.4.0"}, lambda: starts.append(1), root=self.co))  # in HEAD
            self.assertEqual(U.auto_update(None, chk, blocked, root=self.co), "blocked")
            self.assertEqual(U.auto_update(None, chk, blocked, root=self.co), "blocked")
            self.assertEqual(alerts, ["자동 업데이트 건너뜀: v0.4.1"])  # once per tag
            self.assertEqual(U.auto_update(None, chk, lambda: starts.append(1), root=self.co), "started")
            self.assertIsNone(U.auto_update(None, chk, lambda: starts.append(1), root=self.co))  # not again (failed run)
            with mock.patch.object(U, "now", return_value="2099-01-01T00:00:00+00:00"):
                U.auto_update(None, chk, lambda: starts.append(1), root=self.co)
                U.auto_update(None, chk, lambda: starts.append(1), root=self.co)
            self.assertEqual(alerts[1:], ["자동 업데이트 실패: v0.4.1"])  # once, still not retried
        self.assertEqual(starts, [1])

    def test_check_now_endpoint(self):
        """POST /api/update/check (TP-153): fetches within the 6 hours, stamps the check, reports a failed fetch"""
        mock.patch.object(core, "DB_PATH", self.tmp / "tp.db").start()
        mock.patch.object(core, "_CON", None).start()
        mock.patch.object(U, "check_latest", functools.partial(U.check_latest, root=self.co)).start()
        self.addCleanup(mock.patch.stopall)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def post(origin=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=30)
            self.addCleanup(c.close)
            c.request("POST", "/api/update/check", "{}", {"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
            r = c.getresponse()
            return r.status, json.loads(r.read())

        self.tag("v0.4.0")
        con = core.db()
        U.check_latest(con, root=self.co)  # the tick's check: v0.4.0
        con.commit()
        first = core.meta_get(con, "update_check")["at"]
        self.tag("v0.4.1")
        with mock.patch.object(U, "now", return_value="2099-01-01T00:00:00+00:00"):
            code, got = post("http://127.0.0.1:3032")
        self.assertEqual(code, 200)
        self.assertEqual((got["latest"], got["checked_at"]), ("v0.4.1", "2099-01-01T00:00:00+00:00"))
        self.assertTrue({"current", "blocked", "notes"} <= got.keys())
        self.assertNotEqual(first, got["checked_at"])

        self.assertEqual(post("https://evil.example")[0], 403)

        git(self.co, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        code, got = post()
        self.assertEqual(code, 502)
        self.assertIn("태그를 가져오지 못했어요", got["error"])
        with mock.patch.object(U, "github_token", return_value=None), mock.patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            self.assertIn("GitHub가 연결되지 않았어요", post()[1]["error"])  # the update screen shows why
        self.assertEqual(core.meta_get(con, "update_check")["at"], "2099-01-01T00:00:00+00:00")  # kept


if __name__ == "__main__":
    unittest.main()
