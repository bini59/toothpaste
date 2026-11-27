import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECK = Path(__file__).resolve().parents[1] / "scripts" / "check_architecture_map.py"
MAP = "docs/architecture.md"
CODE = "ticket_pilot/example.py"
BASE = "milestone/example"


class ArchitectureMapTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.repo = Path(work.name)
        self.git("init", "--quiet", "--initial-branch", BASE)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.filemode", "true")
        self.write(MAP, "# Code map\n")
        self.write(CODE, "value = 0\n")
        self.commit("common ancestor")
        self.git("checkout", "--quiet", "-b", "pr")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True)

    def write(self, path, contents):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)

    def check(self, *refs):
        return subprocess.run([sys.executable, str(CHECK), *(refs or (BASE,))],
                              cwd=self.repo, capture_output=True, text=True)

    def assert_needs_map(self, *paths):
        result = self.check(BASE, "pr")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(MAP, result.stderr)
        for path in paths:
            self.assertIn(repr(path), result.stderr)

    def test_code_only_lists_changed_paths(self):
        paths = (
            CODE, "web/src/new view.tsx", "tests/test_example.py", "bin/tp",
            "scripts/build.sh", "build.sh", "launchd/install.sh", "systemd/install.sh",
            "docker/Dockerfile", "web/package-lock.json", ".github/workflows/build.yml",
            "web/src/view\nname.ts",
        )
        for path in paths:
            self.write(path, "changed\n")
        self.commit("code changes")
        self.assert_needs_map(*paths)

    def test_code_and_modified_map_pass(self):
        self.write(CODE, "value = 1\n")
        self.write(MAP, "# Updated code map\n")
        self.commit("code and map")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_extensionless_executable_is_code(self):
        path = "bin/tp-build"
        self.write(path, "#!/bin/sh\n")
        (self.repo / path).chmod(0o755)
        self.commit("new extensionless executable")
        self.assert_needs_map(path)

    def test_map_permission_change_cannot_cover_code(self):
        self.write(CODE, "value = 1\n")
        (self.repo / MAP).chmod(0o755)
        self.commit("code and map mode")
        self.assert_needs_map(CODE)

    def test_replacing_map_with_symlink_cannot_cover_code(self):
        map_file = self.repo / MAP
        map_file.unlink()
        map_file.symlink_to("other.md")
        self.write(CODE, "value = 1\n")
        self.commit("code and map symlink")
        self.assert_needs_map(CODE)

    def test_replacing_map_symlink_with_content_can_cover_code(self):
        self.git("checkout", "--quiet", BASE)
        map_file = self.repo / MAP
        map_file.unlink()
        map_file.symlink_to("other.md")
        self.commit("map symlink on base")
        self.git("checkout", "--quiet", "-B", "pr")
        self.git("rm", "--quiet", MAP)
        self.write(MAP, "# Restored code map\n")
        self.write(CODE, "value = 1\n")
        self.commit("code and restored map")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_code_and_added_map_pass(self):
        self.git("checkout", "--quiet", BASE)
        self.git("rm", "--quiet", MAP)
        self.commit("base without a map")
        self.git("checkout", "--quiet", "-B", "pr")
        self.write(CODE, "value = 1\n")
        self.write(MAP, "# New code map\n")
        self.commit("code and map")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_docs_only_pass_even_if_map_is_deleted(self):
        self.write("docs/notes.md", "Notes\n")
        self.write("web/src/README.md", "Notes\n")
        self.commit("docs")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git("rm", "--quiet", MAP)
        self.commit("delete map")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deleting_map_cannot_cover_a_code_deletion(self):
        self.git("rm", "--quiet", CODE, MAP)
        self.commit("delete code and map")
        self.assert_needs_map(CODE)

    def test_moving_code_checks_both_paths(self):
        renamed = "ticket_pilot/renamed.py"
        self.git("mv", CODE, renamed)
        self.commit("move code within the code paths")
        self.assert_needs_map(CODE, renamed)

    def test_moving_code_out_of_scope_checks_old_path(self):
        self.git("mv", CODE, "docs/example.md")
        self.commit("move code out of scope")
        self.assert_needs_map(CODE)

    def test_target_branch_only_changes_are_ignored(self):
        self.git("checkout", "--quiet", BASE)
        self.write(CODE, "value = 2\n")
        self.write(MAP, "# Map updated on target branch\n")
        self.commit("target-only changes")
        self.git("checkout", "--quiet", "pr")
        self.write("docs/notes.md", "PR notes\n")
        self.commit("docs on PR branch")
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

        other_code = "ticket_pilot/other.py"
        self.write(other_code, "value = 3\n")
        self.commit("code on PR branch")
        self.assert_needs_map(other_code)
        self.assertNotIn(CODE, self.check().stderr)

    def test_missing_ref_fails(self):
        result = self.check("missing-target")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("common history", result.stderr)
