"""그만두기: a mid-flight ticket (PR open) can be dropped; its run leaves the review queue and its agent is stopped.
Run: python3 -m unittest discover tests"""
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import core, pipeline as P


class DropTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO tickets(gid,key,has_tag,assigned,repo_override) VALUES ('A','A',1,1,'r')")
        rid = self.con.execute("""INSERT INTO runs(ticket_gid,repo,worktree_path,ws_status,state,terminal)
                                  VALUES ('A','r','/wt','in-review','active','1 /tmp/tp-test-agent.log')""").lastrowid
        self.con.execute("INSERT INTO prs(url,run_id,ticket_gid,repo,number,state,head_sha) VALUES ('u',?,'A','r',1,'OPEN','s')", (rid,))
        P.restage(self.con)
        self.p = [mock.patch.object(P, "db", lambda: self.con), mock.patch.object(P.R, "stop")]
        for x in self.p:
            x.start()
            self.addCleanup(x.stop)
        self.stop = P.R.stop

    def stage(self):
        return self.con.execute("SELECT stage FROM tickets WHERE gid='A'").fetchone()["stage"]

    def test_drop_pr_open(self):
        self.assertEqual(self.stage(), "pr_open")
        self.assertEqual(len(P.review_queue(self.con)), 1)
        P.drop("A")
        self.assertEqual(self.stage(), "skipped")
        self.assertEqual(P.review_queue(self.con), [])
        self.stop.assert_called_once_with("1 /tmp/tp-test-agent.log")

    def test_drop_rejects_merged(self):
        self.con.execute("UPDATE prs SET state='MERGED'")
        P.restage(self.con)
        with self.assertRaises(SystemExit):
            P.drop("A")
