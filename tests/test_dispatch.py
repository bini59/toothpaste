"""Dispatch selects eligible tickets and records a run after the agent starts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core, dispatch as D, milestone, pipeline as P, runner


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(core, "DB_PATH", self.root / "tp.db"))
        self.enterContext(mock.patch.object(core, "_CON", None))
        self.addCleanup(core.config_cache_clear)
        self.con = core.db()
        self.addCleanup(self.con.close)
        self.con.execute("INSERT INTO repos(name, path, github) VALUES ('app', ?, 'owner/app')", (str(self.root),))
        core.write_config(self.con, {"settings": {"max_active_runs": 1, "deploy": {"repos": {
            "app": {"train": True, "modes": ["staging"], "staging": {"branch": "dev"}, "prod": {"branch": "main"}},
        }}}, "project_map": {}})
        (self.root / "prompts").symlink_to(core.ROOT / "prompts")
        self.enterContext(mock.patch.object(D, "ROOT", self.root))
        self.enterContext(mock.patch.object(D, "github_token", return_value="fixture"))
        self.enterContext(mock.patch.object(D, "asana_comments", return_value=[]))
        self.enterContext(mock.patch.object(D, "gh_json", return_value={"comments": []}))
        self.enterContext(mock.patch.object(P, "prepare_worktree", return_value=(None, "")))
        self.existing = []
        self.enterContext(mock.patch.object(runner, "worktrees", side_effect=lambda _: self.existing.copy()))
        self.create = self.enterContext(mock.patch.object(runner, "create", side_effect=self.make_worktree))
        self.launch = self.enterContext(mock.patch.object(runner, "launch", return_value="fixture"))

    def make_worktree(self, repo, path, name, base, *, before_add=None):
        if before_add:
            before_add()
        worktree = {"repo": repo, "path": str(self.root / name), "branch": f"tp/{name}"}
        self.existing.append(worktree)
        return worktree

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
