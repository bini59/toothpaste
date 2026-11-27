"""PR scan contracts with isolated worktree, GitHub and agent fixtures."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ticket_pilot import core, pipeline, pr_tracking


class PullRequestTrackingTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = str(Path(tmp).resolve())
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO repos(name,path,github) VALUES ('app',?,'example/app')", (self.path,))
        self.con.execute("INSERT INTO tickets(gid,key,name,has_tag,assigned) VALUES ('A','APP-1','task',1,1)")
        self.handle = f"999999 {self.path}/agent.log"
        self.rid = self.con.execute(
            """INSERT INTO runs(ticket_gid,repo,worktree_path,worktree_name,ws_status,state,terminal)
               VALUES ('A','app',?,'app-1','in-progress','active',?)""", (self.path, self.handle)).lastrowid
        self.pr = {"number": 7, "url": "https://github.com/example/app/pull/7", "state": "OPEN", "isDraft": True,
                   "title": "task", "headRefOid": "a" * 40, "createdAt": "2026-10-01", "updatedAt": "2026-10-01",
                   "baseRefName": "main", "mergeable": "CONFLICTING"}
        self.enterContext(mock.patch.object(pr_tracking, "db", return_value=self.con))
        self.enterContext(mock.patch.object(pr_tracking.M, "base_for", return_value=None))
        self.enterContext(mock.patch.object(pr_tracking.N.CH, "_layout", return_value=[]))
        self.enterContext(mock.patch.object(pr_tracking.R, "worktrees", return_value=[
            {"path": self.path, "branch": "tp/app-1"}]))

    def scan(self):
        self.assertEqual(pipeline.scan(log=lambda *_: None), {"active_runs": 1, "prs": 1})

    def count_events(self, kind):
        return self.con.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]

    def test_scan_uses_new_pr_for_exit_and_deduplicates_repeated_conflicts(self):
        with mock.patch.object(pr_tracking, "gh_json", side_effect=lambda _: [self.pr.copy()]) as gh, \
                mock.patch.object(pr_tracking, "run", return_value=SimpleNamespace(stdout="")) as git, \
                mock.patch.object(pr_tracking.R, "agent_state", return_value=(False, 0)) as agent:
            calls = mock.Mock()
            calls.attach_mock(gh, "gh")
            calls.attach_mock(git, "git")
            calls.attach_mock(agent, "agent")
            self.scan()
            self.assertEqual([c[0] for c in calls.mock_calls], ["gh", "git", "agent"])
            self.assertEqual(gh.call_args.args[0][:8],
                             ["pr", "list", "--repo", "example/app", "--head", "tp/app-1", "--state", "all"])
            r = pr_tracking.latest_run(self.con, "A")
            self.assertEqual((r["ws_status"], r["ws_comment"]), ("in-review", "PR #7 ready (agent exited)"))
            self.assertEqual(self.con.execute("SELECT stage FROM tickets").fetchone()[0], "conflict")

            self.scan()
            self.assertEqual(self.con.execute("SELECT count(*) FROM prs").fetchone()[0], 1)
            self.assertEqual(self.count_events("conflict"), 1)
            self.assertEqual(self.count_events("agent_exit"), 1)
            self.pr["mergeable"] = "UNKNOWN"
            self.scan()
            self.assertEqual(pr_tracking.run_pr(self.con, self.rid)["mergeable"], "CONFLICTING")
            self.pr["headRefOid"] = "b" * 40
            self.scan()
            self.assertIsNone(pr_tracking.run_pr(self.con, self.rid)["mergeable"])

    def test_direct_push_violation_is_recorded_once_across_scans(self):
        self.pr["mergeable"] = "MERGEABLE"
        heads = "deadbeef\trefs/heads/milestone/parent\n"
        commit = "deadbeef" + "a" * 32 + "\x1f[APP-1] direct push\n"
        with mock.patch.object(pr_tracking, "gh_json", return_value=[self.pr]), \
                mock.patch.object(pr_tracking, "run", side_effect=[
                    SimpleNamespace(stdout=out) for out in (heads, "", commit) * 2]), \
                mock.patch.object(pr_tracking.R, "running", return_value=True), \
                mock.patch.object(pr_tracking.R, "stop") as stop:
            self.scan()
            r = pr_tracking.latest_run(self.con, "A")
            self.assertEqual(r["ws_status"], "todo")
            self.assertTrue(r["ws_comment"].startswith(pr_tracking.VIOLATION))
            self.assertIn("milestone/parent", r["ws_comment"])
            self.assertEqual(self.con.execute("SELECT stage FROM tickets").fetchone()[0], "blocked")
            self.scan()
        stop.assert_called_once_with(self.handle)
        self.assertEqual(self.count_events("violation"), 1)

    def test_timeout_stops_the_agent_and_blocks_once(self):
        with mock.patch.object(pr_tracking, "settings", return_value={"agent_max_min": 1}), \
                mock.patch.object(pr_tracking.R, "agent_state", return_value=(True, None)), \
                mock.patch.object(pr_tracking.R, "started_at", return_value=1), \
                mock.patch.object(pr_tracking.time, "time", return_value=62), \
                mock.patch.object(pr_tracking.R, "last_message", return_value="still working"), \
                mock.patch.object(pr_tracking.R, "stop") as stop:
            pr_tracking.agent_exits(self.con)
            pr_tracking.agent_exits(self.con)

        stop.assert_called_once_with(self.handle)
        r = pr_tracking.latest_run(self.con, "A")
        self.assertEqual(r["ws_status"], "todo")
        self.assertIn("BLOCKED: still working", r["ws_comment"])
        self.assertIn("agent stopped after 1 min", r["ws_comment"])
        self.assertEqual(self.count_events("agent_timeout"), 1)
