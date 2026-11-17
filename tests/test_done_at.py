"""TP-81: tickets.done_at (when the stage became done/closed), its backfill and clearing on reopen.
Run: python3 -m unittest discover tests"""
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import core
from ticket_pilot import pipeline as P


class DoneAtTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)

    def tearDown(self):
        self.con.close()

    def add(self, gid, stage, completed=0, done_at=None, modified=None):
        self.con.execute("""INSERT INTO tickets(gid,key,name,has_tag,assigned,deps_json,open_deps,stage,completed,
                            done_at,asana_modified_at) VALUES (?,?,?,1,1,'[]',0,?,?,?,?)""",
                         (gid, gid, gid, stage, completed, done_at, modified))

    def done_at(self, gid):
        return self.con.execute("SELECT done_at FROM tickets WHERE gid=?", (gid,)).fetchone()[0]

    def test_set_on_done_kept_then_cleared_on_reopen(self):
        self.add("t1", "in_progress", completed=1)
        with mock.patch.object(P, "now", lambda: "2026-11-01T00:00:00+00:00"):
            P.restage(self.con)
        self.assertEqual(self.done_at("t1"), "2026-11-01T00:00:00+00:00")
        with mock.patch.object(P, "now", lambda: "2026-11-05T00:00:00+00:00"):
            P.restage(self.con)  # still done: keeps the first time
        self.assertEqual(self.done_at("t1"), "2026-11-01T00:00:00+00:00")
        self.con.execute("UPDATE tickets SET completed=0 WHERE gid='t1'")
        with mock.patch.object(P, "compute_stage", lambda con, t: "in_progress"):
            P.restage(self.con)
        self.assertIsNone(self.done_at("t1"))

    def test_backfill(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        core._migrate(con)
        con.execute("ALTER TABLE tickets DROP COLUMN done_at")  # a DB from before TP-81
        rows = [("prod", "done", "2026-10-01T00:00:00Z"), ("merged", "done", "2026-10-02T00:00:00Z"),
                ("asana", "done", "2026-10-03T00:00:00Z"), ("bare", "closed", None), ("open", "pr_open", None)]
        for gid, stage, mod in rows:
            con.execute("INSERT INTO tickets(gid,stage,asana_modified_at) VALUES (?,?,?)", (gid, stage, mod))
        con.execute("INSERT INTO prs(url,ticket_gid,number,merged_at,prod_at) VALUES ('a','prod',1,'2026-09-01','2026-09-02')")
        con.execute("INSERT INTO prs(url,ticket_gid,number,merged_at) VALUES ('b','merged',2,'2026-09-05')")
        with mock.patch.object(core, "now", lambda: "2026-11-06T00:00:00+00:00"):
            core._migrate(con)
        got = {r["gid"]: r["done_at"] for r in con.execute("SELECT gid, done_at FROM tickets")}
        self.assertEqual(got, {"prod": "2026-09-02", "merged": "2026-09-05", "asana": "2026-10-03T00:00:00Z",
                               "bare": "2026-11-06T00:00:00+00:00", "open": None})
        con.close()


if __name__ == "__main__":
    unittest.main()
