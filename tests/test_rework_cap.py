"""Rework cap: after review.max_rework_rounds fix rounds a 'changes' verdict goes to the owner as BLOCKED, and their reply
relaunches the agent and restarts the count. Run: python3 -m unittest discover tests"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import core, pipeline as P, review


class ReworkCapTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO tickets(gid,key,name,has_tag,assigned,repo_override) VALUES ('A','A','t',1,1,'r')")
        rid = self.con.execute("""INSERT INTO runs(ticket_gid,repo,worktree_path,ws_status,state)
                                  VALUES ('A','r','/wt','in-review','active')""").lastrowid
        self.con.execute("INSERT INTO prs(url,run_id,ticket_gid,repo,number,state,head_sha) VALUES ('u',?,'A','r',1,'OPEN','s0')",
                         (rid,))
        self.p = [mock.patch.object(P, "db", lambda: self.con), mock.patch.object(P, "github_token", return_value="tok"),
                  mock.patch.object(P.R, "launch", return_value="1 /tmp/tp-test-agent.log"),
                  mock.patch.object(P.R, "running", return_value=False),
                  mock.patch.object(P, "max_rework_rounds", return_value=3),
                  mock.patch.object(P.N.CH, "_layout", return_value=[]),
                  mock.patch.object(P, "settings", return_value={"max_active_runs": 3, "agent": "codex"})]
        for x in self.p:
            x.start()
            self.addCleanup(x.stop)
        self.launch = P.R.launch

    def stage(self):
        P.restage(self.con)
        return self.con.execute("SELECT stage FROM tickets WHERE gid='A'").fetchone()["stage"]

    def review_changes(self, n):
        """Agent pushed head s<n> and marked it in-review; the AI review asks for changes."""
        self.con.execute("UPDATE prs SET head_sha=?", (f"s{n}",))
        self.con.execute("UPDATE runs SET ws_status='in-review', ws_comment=NULL")
        self.con.execute("INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict,summary) VALUES ('u','A',?,'changes','x')",
                         (f"s{n}",))

    def launched(self):
        return self.launch.call_args_list

    def ws_comment(self):
        return self.con.execute("SELECT ws_comment FROM runs WHERE ticket_gid='A'").fetchone()["ws_comment"] or ""

    def test_cap_then_reply_restarts(self):
        for n in range(3):
            self.review_changes(n)
            self.assertEqual(self.stage(), "rework")
            P.dispatch(log=lambda *a: None)
            self.assertEqual(self.stage(), "in_progress")
        self.assertEqual(len(self.launched()), 3)

        self.review_changes(3)
        self.assertEqual(self.stage(), "rework")
        P.dispatch(log=lambda *a: None)
        self.assertEqual(len(self.launched()), 3)  # no fourth agent
        self.assertEqual(self.stage(), "blocked")
        self.assertTrue(self.ws_comment().startswith("BLOCKED"))
        unread = "SELECT count(*) c FROM notifications WHERE kind='blocked' AND read_at IS NULL"
        self.assertEqual(self.con.execute(unread).fetchone()["c"], 1)  # TP-138

        P.request_reply("A", "keep going, simpler approach")
        self.assertEqual(self.con.execute(unread).fetchone()["c"], 0)
        P.dispatch(log=lambda *a: None)  # unblock: one agent with the owner's answer, no extra rework agent
        self.assertEqual(len(self.launched()), 4)
        P.dispatch(log=lambda *a: None)
        self.assertEqual(len(self.launched()), 4)
        self.assertEqual(self.stage(), "in_progress")

        self.review_changes(4)
        self.assertEqual(self.stage(), "rework")
        P.dispatch(log=lambda *a: None)  # count restarted: normal rework
        self.assertEqual(len(self.launched()), 5)
        self.assertEqual(self.stage(), "in_progress")

    def test_no_github_token_no_agent_but_escalation_runs(self):
        """TP-112: without tp's GitHub token no agent starts (one error event), the cap still goes to the owner."""
        errors = lambda: self.con.execute("SELECT count(*) c FROM events WHERE kind='error'").fetchone()["c"]  # noqa: E731
        with mock.patch.object(P, "github_token", return_value=None):
            self.review_changes(0)
            P.dispatch(log=lambda *a: None)
            P.dispatch(log=lambda *a: None)
            self.assertEqual(len(self.launched()), 0)
            self.assertEqual(self.stage(), "rework")
            self.assertEqual(errors(), 1)
            with mock.patch.object(P, "max_rework_rounds", return_value=1):
                self.con.execute("UPDATE reviews SET rework_at='2000-01-01'")  # one round already sent
                P.dispatch(log=lambda *a: None)
            self.assertEqual(self.stage(), "blocked")
        self.assertEqual(len(self.launched()), 0)

    def test_review_queue_round(self):
        self.review_changes(0)
        self.con.execute("UPDATE prs SET head_sha='s1'")
        q = P.review_queue(self.con)
        self.assertEqual(q[0]["round"], 2)
        self.assertEqual(q[0]["previous_review"]["head_sha"], "s0")

    def test_saving_an_old_head_keeps_the_current_head_queued(self):
        self.con.execute("UPDATE prs SET head_sha=?", ("b" * 40,))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(review, "db", return_value=self.con), \
                mock.patch.object(review, "run") as gh, mock.patch("builtins.print"):
            body = Path(tmp) / "review.md"
            body.write_text("old review")
            P.review_save("u", "a" * 40, "approve", str(body), "old", post=False)
            gh.assert_not_called()
            [item] = P.review_queue(self.con)
            self.assertEqual(item["head_sha"], "b" * 40)
            self.assertEqual(item["previous_review"]["head_sha"], "a" * 40)
            self.assertEqual(self.stage(), "pr_open")

            P.review_save("u", "b" * 40, "approve", str(body), "current", post=False)
            self.assertEqual(P.review_queue(self.con), [])
            self.assertEqual(self.stage(), "reviewed")

    def test_dispatch_retries_a_conflict_once_per_head(self):
        self.con.execute("UPDATE prs SET base='staging', mergeable='CONFLICTING'")
        self.con.execute("UPDATE tickets SET approve_requested_at='old', approve_error='old error'")
        self.assertEqual(self.stage(), "conflict")
        self.assertEqual(P.dispatch(log=lambda *_: None), ["A"])
        p = self.con.execute("SELECT conflict_sha, conflict_at FROM prs").fetchone()
        self.assertEqual(p["conflict_sha"], "s0")
        self.assertTrue(p["conflict_at"])
        self.assertIn("staging", self.launch.call_args.args[1])
        self.assertEqual(tuple(self.con.execute("SELECT approve_requested_at, approve_error FROM tickets").fetchone()),
                         (None, None))

        self.con.execute("UPDATE runs SET ws_status='in-review'")
        self.assertEqual(self.stage(), "pr_open")
        self.assertEqual(P.dispatch(log=lambda *_: None), [])
        self.assertEqual(len(self.launched()), 1)

        self.con.execute("UPDATE prs SET head_sha='s1'")
        self.assertEqual(self.stage(), "conflict")
        self.assertEqual(P.dispatch(log=lambda *_: None), ["A"])
        self.assertEqual(len(self.launched()), 2)

    def test_answer_without_commit_is_rereviewed(self):
        self.review_changes(0)
        self.assertEqual(self.stage(), "rework")
        P.dispatch(log=lambda *a: None)
        self.assertEqual(P.review_queue(self.con), [])  # agent working: ws_status in-progress
        self.con.execute("UPDATE runs SET ws_status='in-review'")  # answered in the PR, same head
        self.assertEqual(self.stage(), "agent_done")
        q = P.review_queue(self.con)
        self.assertEqual((q[0]["head_sha"], q[0]["round"]), ("s0", 2))
        self.con.execute("INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict) VALUES ('u','A','s0','approve')")
        self.assertEqual(P.review_queue(self.con), [])
        self.assertEqual(self.stage(), "reviewed")


if __name__ == "__main__":
    unittest.main()
