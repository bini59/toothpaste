"""TP-162 secret store: the keychain on macOS, ~/.config/ticket-pilot/secrets.json (mode 600) elsewhere. These test
the file store on every OS (core.KEYCHAIN off); the keychain path is covered by test_github / test_asana_connect.
Run: python3 -m unittest discover tests"""
import json
import multiprocessing
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core


def _rotate_with_pending_delete(path, snapshot, deleting, deleted):
    write = core._secrets_write

    def hold(values):
        snapshot.set()  # the rotation has read its snapshot but has not replaced the file
        if not deleting.wait(10):
            raise RuntimeError("delete process did not start")
        deleted.wait(0.3)
        write(values)

    with mock.patch.object(core, "KEYCHAIN", False), mock.patch.object(core, "SECRETS_FILE", path), \
            mock.patch.object(core, "_secrets_write", hold):
        core.secret_set("notion", "account", "rotated")


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.f = self.tmp / ".config" / "ticket-pilot" / "secrets.json"
        for p in (mock.patch.object(core, "KEYCHAIN", False), mock.patch.object(core, "SECRETS_FILE", self.f),
                  mock.patch.object(core.subprocess, "run", side_effect=AssertionError("no `security` off macOS"))):
            p.start()
            self.addCleanup(p.stop)

    def mode(self, p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    def test_set_get_delete(self):
        self.assertIsNone(core.secret_get("ticket-pilot-github", "github"))  # no file yet
        core.secret_set("ticket-pilot-github", "github", "gho_abc123")
        core.secret_set("ticket-pilot-asana", "asana", "2/1/2:ff")
        self.assertEqual(core.secret_get("ticket-pilot-github", "github"), "gho_abc123")
        self.assertEqual(core.secret_get("ticket-pilot-asana"), "2/1/2:ff")  # any account (core.asana_token)
        self.assertIsNone(core.secret_get("ticket-pilot-github", "other"))
        self.assertEqual((self.mode(self.f), self.mode(self.f.parent)), (0o600, 0o700))
        core.secret_set("ticket-pilot-github", "github", "gho_new")  # replaces
        self.assertEqual(core.github_token(), "gho_new")
        core.secret_delete("ticket-pilot-github", "github")
        core.secret_delete("ticket-pilot-github", "github")  # missing is fine
        self.assertIsNone(core.github_token())
        self.assertEqual(json.loads(self.f.read_text()), {"ticket-pilot-asana": {"asana": "2/1/2:ff"}})
        self.assertEqual(self.mode(self.f), 0o600)

    def test_wide_permissions_are_refused(self):
        core.secret_set("s", "a", "tok")
        for mode in (0o644, 0o640, 0o604):
            self.f.chmod(mode)
            with self.assertRaisesRegex(RuntimeError, f"권한이 {mode:o}") as e:
                core.secret_get("s", "a")
            self.assertNotIn("tok", str(e.exception))
            with self.assertRaises(RuntimeError):
                core.secret_set("s", "a", "tok2")
        self.f.chmod(0o600)
        self.assertEqual(core.secret_get("s", "a"), "tok")

    def test_unreadable_store_only_blocks_token_users(self):
        core.secret_set("ticket-pilot-github", "github", "gho_x")
        self.f.chmod(0o644)
        env = core.gh_env({"PATH": "/bin"}, required=False)  # local git still runs, without a token
        self.assertNotIn("GH_TOKEN", env)
        with self.assertRaisesRegex(RuntimeError, "권한이 644"):
            core.gh_env({}, required=True)
        with mock.patch.object(core, "settings", return_value={"asana_keychain_service": "s"}), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(core.AsanaError, "권한이 644"):
                core.asana_token()

    def test_atomic_write_keeps_old_file_on_failure(self):
        core.secret_set("s", "a", "old")
        with mock.patch.object(core.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                core.secret_set("s", "a", "new")
        self.assertEqual(core.secret_get("s", "a"), "old")
        self.assertEqual(sorted(os.listdir(self.f.parent)), ["secrets.json", "secrets.json.lock"])

    def test_rotation_and_delete_from_different_processes(self):
        core.secret_set("dashboard", "account", "old")
        ctx = multiprocessing.get_context("spawn")
        snapshot, deleting, deleted = ctx.Event(), ctx.Event(), ctx.Event()
        worker = ctx.Process(target=_rotate_with_pending_delete, args=(self.f, snapshot, deleting, deleted))
        worker.start()
        try:
            self.assertTrue(snapshot.wait(10))
            deleting.set()
            core.secret_delete("dashboard", "account")
            deleted.set()
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        finally:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        self.assertEqual(json.loads(self.f.read_text()), {"notion": {"account": "rotated"}})

    def test_folder_is_tightened(self):
        self.f.parent.mkdir(parents=True, mode=0o755)
        self.f.parent.chmod(0o755)
        core.secret_set("s", "a", "v")
        self.assertEqual(self.mode(self.f.parent), 0o700)

    def test_bad_values_are_refused(self):
        for bad in ("", "a b", "x\nadd-generic-password", None):
            with self.assertRaisesRegex(RuntimeError, "형식"):
                core.secret_set("s", "a", bad)
        self.assertFalse(self.f.exists())

    def test_asana_token_hint_off_macos(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(core, "settings",
                                                                             return_value={"asana_keychain_service": "svc"}):
            os.environ.pop("ASANA_PAT", None)
            with self.assertRaisesRegex(core.AsanaError, "ASANA_PAT") as e:
                core.asana_token()
            self.assertNotIn("security", str(e.exception))
            core.secret_set("svc", "asana", "2/1/2:ab")
            self.assertEqual(core.asana_token(), "2/1/2:ab")


if __name__ == "__main__":
    unittest.main()
