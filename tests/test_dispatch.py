"""Dispatch selects eligible tickets and records a run after the agent starts."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import briefs, cleanup, core, dispatch as D, milestone, pipeline as P, review, runner
from ticket_pilot.storage import connection


class DispatchFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(core, "DB_PATH", self.root / "tp.db"))
        self.enterContext(mock.patch.object(connection, "_main_connections", {}))
        self.addCleanup(core.config_cache_clear)
        self.con = core.db()
        self.addCleanup(core.close_db, self.con)
        self.con.execute("INSERT INTO repos(name, path, github) VALUES ('app', ?, 'owner/app')", (str(self.root),))
        settings = {"max_active_runs": 1, "worktree_root": str(self.root / "worktrees"), "deploy": {"repos": {
            "app": {"train": True, "modes": ["staging"], "staging": {"branch": "dev"}, "prod": {"branch": "main"}},
        }}}
        core.write_config(self.con, {"settings": settings, "project_map": {}})
        (self.root / "prompts").symlink_to(core.ROOT / "prompts")
        self.enterContext(mock.patch.object(D, "ROOT", self.root))
        self.enterContext(mock.patch.object(review, "ROOT", self.root))
        self.enterContext(mock.patch.object(D, "github_token", return_value="fixture"))
        self.enterContext(mock.patch.object(D, "asana_comments", return_value=[]))
        self.enterContext(mock.patch.object(D, "gh_json", return_value={"comments": []}))
        self.enterContext(mock.patch.object(P, "prepare_worktree", return_value=(None, "")))
        self.launch = self.enterContext(mock.patch.object(runner, "launch", return_value="fixture"))

    def ticket(self, key, *, gid=None, deps=(), parent=None, due=None):
        deps = [{"gid": d, "name": d, "completed": False} for d in deps]
        self.con.execute("""
            INSERT INTO tickets(gid,key,name,repo_override,assigned,has_tag,open_deps,deps_json,parent_gid,in_milestone,due_on)
            VALUES(?,?,?,'app',1,1,?,?,?,?,?)
        """, (gid or key, key, key, len(deps), json.dumps(deps), parent, bool(parent), due))
        P.restage(self.con)
        self.con.commit()

    def run_dispatch(self, **kwargs):
        return P.dispatch(log=lambda *_: None, **kwargs)


class DispatchTest(DispatchFixture):
    def setUp(self):
        super().setUp()
        self.existing = []
        self.enterContext(mock.patch.object(runner, "worktrees", side_effect=lambda _: self.existing.copy()))
        self.enterContext(mock.patch.object(runner, "local_branches", return_value=[]))
        self.create = self.enterContext(mock.patch.object(runner, "create", side_effect=self.make_worktree))
        self.enterContext(mock.patch.object(runner, "mark_checkout", side_effect=lambda path: self.checkout_at(path)["checkout_id"]))
        self.enterContext(mock.patch.object(runner, "owns_checkout", side_effect=self.owns_checkout))

    def checkout_at(self, path):
        return next((wt for wt in self.existing if wt["path"] == path), {})

    def owns_checkout(self, path, branch, checkout_id):
        wt = self.checkout_at(path)
        return bool(checkout_id and wt.get("checkout_id") == checkout_id and wt["branch"] == branch)

    def make_worktree(self, repo, path, name, base, *, before_add=None):
        if before_add:
            before_add()
        worktree = {"repo": repo, "path": str(self.root / name), "branch": f"tp/{name}", "checkout_id": name}
        self.existing.append(worktree)
        return worktree

    def test_slots_dependencies_and_dry_run(self):
        self.ticket("A", due="2026-01-01")
        self.ticket("B", deps=("A",))
        self.ticket("C")
        core.meta_set(self.con, "github_missing_logged", True)
        self.con.commit()
        changes = self.con.total_changes
        lines = []
        self.assertEqual(P.dispatch(dry_run=True, log=lines.append), [])
        self.assertEqual([line.splitlines()[0] for line in lines if line.startswith("---")], ["--- would create app/a"])
        self.assertEqual(self.con.total_changes, changes)
        self.create.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse((self.root / "data").exists())

        self.assertEqual(self.run_dispatch(only="B"), [])
        self.assertEqual(self.run_dispatch(), ["A"])
        self.assertEqual(self.run_dispatch(), [])
        self.launch.assert_called_once()
        self.assertEqual(P.find_ticket(self.con, "B")["stage"], "waiting_dep")
        self.assertEqual(P.find_ticket(self.con, "C")["stage"], "queued")

    def test_create_failure_leaves_a_retryable_ticket(self):
        self.ticket("A")
        self.create.side_effect = RuntimeError("fetch failed")
        self.assertEqual(self.run_dispatch(), [])
        self.assertIsNone(P.latest_run(self.con, "A"))
        self.assertEqual(P.find_ticket(self.con, "A")["stage"], "queued")
        self.launch.assert_not_called()
        self.create.side_effect = self.make_worktree
        self.assertEqual(self.run_dispatch(), ["A"])
        self.launch.assert_called_once()

    def test_start_failure_reuses_the_milestone_worktree_once(self):
        parent = "gh:owner/other#7"
        self.ticket("APP-9", gid="gh:owner/app#9", parent=parent)
        base = milestone.branch(parent)
        with mock.patch.object(milestone, "ensure", return_value=base) as ensure:
            self.launch.side_effect = [OSError("start failed"), "fixture"]
            self.assertEqual(self.run_dispatch(), [])
            self.assertIsNone(P.latest_run(self.con, "gh:owner/app#9"))
            self.assertEqual(P.find_ticket(self.con, "APP-9")["stage"], "queued")
            self.assertEqual(self.run_dispatch(), ["APP-9"])
            self.assertEqual(self.run_dispatch(), [])
        ensure.assert_called_once()
        self.create.assert_called_once_with("app", str(self.root), "app-9", base, before_add=mock.ANY)
        self.assertEqual(self.launch.call_count, 2)
        self.assertIn(f"--base {base}", self.launch.call_args.args[1])
        self.assertEqual(self.launch.call_args.args[0], self.existing[0]["path"])
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT origin, terminal FROM runs")], [("dispatch", "fixture")])
        self.assertIsNone(core.meta_get(self.con, f"dispatch_pending:{self.existing[0]['path']}"))

    def test_start_failure_recovers_when_the_staging_base_changes(self):
        self.ticket("APP-9", gid="gh:owner/app#9")
        self.launch.side_effect = [OSError("start failed"), OSError("start failed again"), "fixture"]
        self.assertEqual(self.run_dispatch(), [])
        core.write_config(self.con, {"settings": {"max_active_runs": 1, "deploy": {"repos": {
            "app": {"train": True, "modes": ["staging"], "staging": {"branch": "next"}, "prod": {"branch": "main"}},
        }}}, "project_map": {}})

        self.assertEqual(self.run_dispatch(), [])
        self.assertEqual(self.run_dispatch(), ["APP-9"])
        self.assertEqual(self.run_dispatch(), [])
        self.assertEqual([c.args[3] for c in self.create.call_args_list], ["dev", "next"])
        old, new = self.existing
        self.assertNotEqual(old["path"], new["path"])
        self.assertEqual([c.args[0] for c in self.launch.call_args_list], [old["path"], new["path"], new["path"]])
        self.assertIn("--base next", self.launch.call_args.args[1])
        self.assertEqual(P.latest_run(self.con, "gh:owner/app#9")["worktree_path"], new["path"])
        self.assertIsNone(core.meta_get(self.con, f"dispatch_pending:{new['path']}"))

    def test_start_failure_recovers_when_the_milestone_changes(self):
        gid = "gh:owner/app#9"
        self.ticket("APP-9", gid=gid, parent="gh:owner/other#7")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        with mock.patch.object(milestone, "ensure"):
            self.assertEqual(self.run_dispatch(), [])
            self.con.execute("UPDATE tickets SET parent_gid='gh:owner/other#8' WHERE gid=?", (gid,))
            self.con.commit()
            self.assertEqual(self.run_dispatch(), ["APP-9"])
        self.assertEqual([c.args[3] for c in self.create.call_args_list],
                         [milestone.branch("gh:owner/other#7"), milestone.branch("gh:owner/other#8")])
        self.assertNotEqual(*[c.args[0] for c in self.launch.call_args_list])

    def test_start_failure_recovers_when_the_checkout_branch_changes(self):
        self.ticket("A")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(), [])
        self.existing[0]["branch"] = "manual-branch"
        self.assertEqual(self.run_dispatch(), ["A"])
        self.assertEqual(self.create.call_count, 2)
        self.assertNotEqual(*[c.args[0] for c in self.launch.call_args_list])

    def test_ticket_closed_during_fetch_does_not_start(self):
        self.ticket("APP-9", gid="gh:owner/app#9")

        def fetch(*args, **kwargs):
            self.con.execute("UPDATE tickets SET completed=1 WHERE gid='gh:owner/app#9'")
            self.con.commit()
            return self.make_worktree(*args, **kwargs)

        self.create.side_effect = fetch
        self.assertEqual(self.run_dispatch(), [])
        self.assertEqual(self.existing, [])
        self.launch.assert_not_called()
        self.assertEqual(P.find_ticket(self.con, "APP-9")["stage"], "done")

    def test_dispatch_continues_when_optional_comments_are_unavailable(self):
        self.ticket("A")
        with mock.patch.object(D, "asana_comments", side_effect=TimeoutError("offline")):
            self.assertEqual(self.run_dispatch(), ["A"])
        self.assertIn("(no comments)", self.launch.call_args.args[1])

    def test_registered_run_holds_its_slot_if_the_pass_stops_early(self):
        self.ticket("A", due="2026-01-01")
        self.ticket("B")
        restage = P.restage

        def interrupt(con, *, ticket_gid=None):
            if ticket_gid is None:
                raise RuntimeError("pass interrupted")
            restage(con, ticket_gid=ticket_gid)

        with mock.patch.object(P, "restage", side_effect=interrupt), self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.run_dispatch()
        self.assertEqual(P.find_ticket(self.con, "A")["stage"], "in_progress")
        self.assertEqual(self.run_dispatch(), [])
        self.launch.assert_called_once()

    def test_existing_worktree_is_adopted_without_launching(self):
        self.ticket("A")
        self.make_worktree("app", str(self.root), "a", "dev")
        self.assertEqual(self.run_dispatch(), ["A"])
        self.assertEqual(self.run_dispatch(), [])
        self.create.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual(P.latest_run(self.con, "A")["origin"], "adopt")

    def test_worktree_with_the_same_slug_cannot_be_adopted_twice(self):
        self.ticket("A-B")
        self.ticket("A B")
        self.assertEqual(self.run_dispatch(only="A-B"), ["A-B"])
        self.con.execute("UPDATE runs SET ws_status='in-review'")
        P.restage(self.con)
        self.con.commit()
        self.assertEqual(self.run_dispatch(only="A B"), [])
        self.assertIsNone(P.latest_run(self.con, "A B"))
        self.create.assert_called_once()
        self.launch.assert_called_once()

    def test_pending_worktree_is_reserved_for_its_original_ticket(self):
        self.ticket("A-B")
        self.ticket("A B")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(only="A-B"), [])
        self.assertEqual(self.run_dispatch(only="A B"), [])
        self.assertIsNone(P.latest_run(self.con, "A B"))
        self.assertEqual(self.run_dispatch(only="A-B"), ["A-B"])
        self.create.assert_called_once()
        self.assertEqual(self.launch.call_count, 2)

    def assert_pending_worktree_allows_collision(self, owner_update):
        self.ticket("A-B")
        self.ticket("A B")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(only="A-B"), [])
        original_path = self.existing[0]["path"]
        reservation_key = f"dispatch_pending:{original_path}"
        reservation = core.meta_get(self.con, reservation_key)
        self.assertEqual(reservation["ticket_gid"], "A-B")

        self.con.execute(owner_update, ("A-B",))
        self.con.commit()  # the stored stage can still be queued when the pass starts
        self.assertEqual(self.run_dispatch(only="A B"), ["A B"])

        run = P.latest_run(self.con, "A B")
        self.assertEqual(run["worktree_name"], "a-b--retry-1")
        self.assertEqual(self.launch.call_args.args[0], run["worktree_path"])
        self.assertNotEqual(run["worktree_path"], original_path)
        self.assertEqual(core.meta_get(self.con, reservation_key), reservation)
        self.assertIsNone(P.latest_run(self.con, "A-B"))

    def test_pending_worktree_does_not_block_after_ticket_completion(self):
        self.assert_pending_worktree_allows_collision("UPDATE tickets SET completed=1 WHERE gid=?")

    def test_pending_worktree_does_not_block_after_ticket_leaves_tracking(self):
        self.assert_pending_worktree_allows_collision("UPDATE tickets SET assigned=0 WHERE gid=?")

    def test_pending_worktree_does_not_block_after_ticket_key_changes_slug(self):
        self.assert_pending_worktree_allows_collision("UPDATE tickets SET key='C' WHERE gid=?")

    def test_replaced_pending_checkout_is_neither_retried_nor_removed(self):
        self.ticket("A")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(), [])
        self.existing[0] = self.existing[0] | {"checkout_id": "manual"}
        P.set_skip("A", True)
        with mock.patch.object(cleanup, "_ps_entries", return_value={}), \
                mock.patch.object(cleanup, "docker_prune"), mock.patch.object(cleanup, "remove") as remove:
            self.assertEqual(cleanup.cleanup_scan(log=lambda *_: None)["removed"], 0)
            remove.assert_not_called()

        P.set_skip("A", False)
        self.assertEqual(self.run_dispatch(), ["A"])
        self.assertNotEqual(self.launch.call_args.args[0], self.existing[0]["path"])

    def test_legacy_reservation_cannot_authorize_retry_or_removal(self):
        self.ticket("A")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(), [])
        path = self.existing[0]["path"]
        core.meta_set(self.con, f"dispatch_pending:{path}", {"ticket_gid": "A", "repo": "app", "branch": "tp/a", "base": "dev"})
        P.set_skip("A", True)
        with mock.patch.object(cleanup, "_ps_entries", return_value={}), \
                mock.patch.object(cleanup, "docker_prune"), mock.patch.object(cleanup, "remove") as remove:
            self.assertEqual(cleanup.cleanup_scan(log=lambda *_: None)["removed"], 0)
            remove.assert_not_called()

        P.set_skip("A", False)
        self.assertEqual(self.run_dispatch(), ["A"])
        self.assertNotEqual(self.launch.call_args.args[0], path)

    def test_cleanup_releases_a_skipped_reservation_after_the_server_stops(self):
        self.ticket("A-B")
        self.ticket("A B")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(only="A-B"), [])
        path = self.existing[0]["path"]
        P.set_skip("A-B", True)
        with mock.patch.object(runner, "root", return_value=self.root), \
                mock.patch.object(cleanup, "_ps_entries", return_value={}), \
                mock.patch.object(cleanup, "_listening_in", return_value=["test server"]) as listeners, \
                mock.patch.object(cleanup, "docker_prune"), \
                mock.patch.object(cleanup, "remove", return_value="removed") as remove:
            scan = lambda: cleanup.cleanup_scan(log=lambda *_: None)
            self.assertEqual(scan()["kept"], 1)
            self.assertIsNotNone(core.meta_get(self.con, f"dispatch_pending:{path}"))
            remove.assert_not_called()

            listeners.return_value = []
            self.assertEqual(scan()["removed"], 1)
            remove.assert_called_once_with(self.con, path, str(self.root), "tp/a-b", force=True, checkout_id="a-b")
            self.existing.clear()  # cleanup removed the checkout
            self.assertEqual(scan()["removed"], 0)

        self.assertIsNone(core.meta_get(self.con, f"dispatch_pending:{path}"))
        self.assertIsNone(P.latest_run(self.con, "A-B"))
        self.assertEqual(self.run_dispatch(only="A B"), ["A B"])
        self.assertEqual(self.launch.call_args.args[0], path)

    def test_skipped_reservation_with_a_different_checkout_branch_is_left_alone(self):
        self.ticket("A-B")
        self.ticket("A B")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(only="A-B"), [])
        self.existing[0]["branch"] = "manual-branch"
        P.set_skip("A-B", True)
        with mock.patch.object(cleanup, "_ps_entries", return_value={}), \
                mock.patch.object(cleanup, "docker_prune"), mock.patch.object(cleanup, "remove") as remove:
            self.assertEqual(cleanup.cleanup_scan(log=lambda *_: None)["removed"], 0)
            remove.assert_not_called()
        self.assertEqual(self.run_dispatch(only="A B"), ["A B"])
        self.assertNotEqual(*[c.args[0] for c in self.launch.call_args_list])

    def test_cleanup_rechecks_the_checkout_before_forcing_removal(self):
        self.ticket("A")
        self.launch.side_effect = OSError("start failed")
        self.assertEqual(self.run_dispatch(), [])
        P.set_skip("A", True)

        def replace_checkout(*_):
            self.existing[0] = self.existing[0] | {"checkout_id": "manual"}

        with mock.patch.object(runner, "root", return_value=self.root), \
                mock.patch.object(cleanup, "_ps_entries", return_value={}), \
                mock.patch.object(cleanup, "_listening_in", return_value=[]), \
                mock.patch.object(cleanup, "docker_prune"), \
                mock.patch.object(cleanup.worktree_dev, "stop", side_effect=replace_checkout), \
                mock.patch.object(runner, "remove") as remove:
            self.assertEqual(cleanup.cleanup_scan(log=lambda *_: None)["removed"], 0)
            remove.assert_not_called()

    def test_work_brief_followups_use_the_ticket_id_when_slugs_collide(self):
        keys = ("A-B", "A B")
        for key in keys:
            self.ticket(key)
        self.launch.side_effect = [OSError("start failed"), "first", "second"]
        self.assertEqual(self.run_dispatch(only="A-B"), [])
        self.existing[0]["branch"] = "manual-branch"
        for number, key in enumerate(keys, 1):
            self.assertEqual(self.run_dispatch(only=key), [key])
            rid = P.latest_run(self.con, key)["id"]
            self.con.execute("UPDATE runs SET ws_status='in-review' WHERE id=?", (rid,))
            self.con.execute("""INSERT INTO prs(url,run_id,ticket_gid,repo,number,state,head_sha)
                              VALUES (?,?,?,'app',?,'OPEN','head')""", (f"https://example.com/pr/{number}", rid, key, number))
            P.restage(self.con)
            self.con.commit()

        queue = {item["ticket"]: Path(item["brief_file"]) for item in review.review_queue(self.con)}
        self.assertNotEqual(queue["A-B"], queue["A B"])
        for key in keys:
            file = queue[key]
            self.assertTrue(file.read_text().startswith(f"# {key}: "))
            t = dict(P.find_ticket(self.con, key)) | {"reply": "continue"}
            run = P.latest_run(self.con, key)
            pr = P.run_pr(self.con, run["id"])
            self.assertIn(str(file), review.build_rework_brief(t, run, pr, {"source": None, "summary": "", "body": ""}))
            lines = []
            review.unblock(self.con, t, dry_run=True, log=lines.append)
            self.assertIn(str(file), lines[0])

    def test_legacy_brief_is_used_only_for_the_matching_key_and_url(self):
        url = "https://example.com/ticket/1"
        legacy = self.root / "data" / "briefs" / "a-b.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(f"# A-B: first\n\n## Ticket\n- Link: {url}\n")
        self.assertEqual(briefs.find_brief(self.root, "first", "A-B", url), legacy)
        self.assertIsNone(briefs.find_brief(self.root, "second", "A B", url))
        self.assertIsNone(briefs.find_brief(self.root, "second", "A-B", "https://example.com/ticket/2"))


class DispatchGitTest(DispatchFixture):
    def test_dispatch_uses_a_free_branch_after_partial_cleanup(self):
        origin, repo = self.root / "origin", self.root / "repo"
        origin.mkdir()
        repo.mkdir()

        def git(*args, cwd=repo):
            return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True).stdout.strip()

        def commit(message, cwd):
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", message, "--allow-empty", cwd=cwd)

        git("init", "-qb", "dev", cwd=origin)
        commit("base", origin)
        git("clone", "-q", str(origin), ".")
        self.con.execute("UPDATE repos SET path=? WHERE name='app'", (str(repo),))
        config = core.read_config(self.con)
        config["settings"]["branch_prefix"] = "work/"
        core.write_config(self.con, config)

        self.ticket("A-B")
        self.launch.side_effect = [OSError("start failed"), "fixture"]
        self.assertEqual(self.run_dispatch(only="A-B"), [])
        path = self.launch.call_args.args[0]
        commit("preserved local work", path)
        leftover_head = git("rev-parse", "refs/heads/work/a-b")
        reserved = f"dispatch_pending:{path}"
        self.assertIsNotNone(core.meta_get(self.con, reserved))
        P.set_skip("A-B", True)
        with mock.patch.object(cleanup, "_ps_entries", return_value={}), \
                mock.patch.object(cleanup, "_listening_in", return_value=[]), \
                mock.patch.object(cleanup, "docker_prune"):
            with mock.patch.object(cleanup, "_drop_branch", side_effect=OSError("branch deletion failed")):
                cleanup.cleanup_scan(log=lambda *_: None)
            self.assertFalse(Path(path).exists())
            self.assertIsNotNone(core.meta_get(self.con, reserved))
            cleanup.cleanup_scan(log=lambda *_: None)
        self.assertIsNone(core.meta_get(self.con, reserved))

        git("branch", "work/a-b--retry-1")
        self.ticket("A B")
        self.assertEqual(self.run_dispatch(only="A B"), ["A B"])
        run = P.latest_run(self.con, "A B")
        self.assertEqual(run["worktree_name"], "a-b--retry-2")
        self.assertEqual(run["branch"], "work/a-b--retry-2")
        self.assertEqual(git("symbolic-ref", "--short", "HEAD", cwd=run["worktree_path"]), run["branch"])
        self.assertEqual(git("rev-parse", "refs/heads/work/a-b--retry-2"), git("rev-parse", "origin/dev"))
        self.assertEqual(git("rev-parse", "refs/heads/work/a-b"), leftover_head)
        self.assertEqual(git("rev-parse", "refs/heads/work/a-b--retry-1"), git("rev-parse", "origin/dev"))


class CheckoutIdentityTest(unittest.TestCase):
    def test_git_registration_reuse_does_not_inherit_the_checkout_id(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = root / "repo"
        checkout = str(root / "worktree")
        repo.mkdir()

        def git(*args, cwd=repo):
            return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-q")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "base", "--allow-empty")
        git("worktree", "add", "-qb", "tp/a", checkout)
        git_dir = git("rev-parse", "--absolute-git-dir", cwd=checkout)
        checkout_id = runner.mark_checkout(checkout)
        self.assertTrue(runner.owns_checkout(checkout, "tp/a", checkout_id))
        with mock.patch.object(cleanup.worktree_dev, "stop"):
            cleanup.remove(None, checkout, str(repo), "tp/a", checkout_id=checkout_id)

        git("worktree", "add", "-qb", "tp/a", checkout)
        self.assertEqual(git("rev-parse", "--absolute-git-dir", cwd=checkout), git_dir)
        self.assertFalse(runner.owns_checkout(checkout, "tp/a", checkout_id))
