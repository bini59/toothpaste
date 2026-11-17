"""A dead pending train row (past day, or today's train stopped) must not block dashboard deploys of the same repo;
waiting train rows can be cancelled. Run: python3 -m unittest discover tests"""
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import core, deploy as D, release as R


class BusyReleaseTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.p = [mock.patch.object(D, "db", lambda: self.con), mock.patch.object(D, "_today", lambda: "2026-10-23"),
                  mock.patch.object(R, "_today", lambda: "2026-10-23"), mock.patch.object(D, "restage")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def rel(self, day, repo, status, mode=None):
        return self.con.execute("INSERT INTO releases(day,repo,status,mode) VALUES (?,?,?,?)",
                                (day, repo, status, mode)).lastrowid

    def status(self, rid):
        return self.con.execute("SELECT status FROM releases WHERE id=?", (rid,)).fetchone()["status"]

    def test_past_day_pending_does_not_block_and_expires(self):
        old = self.rel("2026-10-22", "Site", "pending")
        dep = self.rel("2026-10-23", "Site", "requested", "direct")
        self.assertIsNone(D.busy_release(self.con, "Site", dep))
        self.assertEqual(R.expire_stale_train(self.con), 1)
        self.assertEqual(self.status(old), "cancelled")

    def test_stopped_day_pending_does_not_block(self):
        self.rel("2026-10-23", "Api", "aborted")
        self.rel("2026-10-23", "Site", "pending")
        dep = self.rel("2026-10-23", "Site", "requested", "direct")
        self.assertIsNone(D.busy_release(self.con, "Site", dep))

    def test_live_today_pending_blocks(self):
        train = self.rel("2026-10-23", "Site", "pending")
        dep = self.rel("2026-10-23", "Site", "requested", "direct")
        self.assertEqual(D.busy_release(self.con, "Site", dep)["id"], train)

    def test_cancel_pending_train_row(self):
        train = self.rel("2026-10-23", "Site", "pending")
        D.cancel_deploy(train)
        self.assertEqual(self.status(train), "cancelled")
        running = self.rel("2026-10-23", "Site", "deploying")
        with self.assertRaises(SystemExit):
            D.cancel_deploy(running)


if __name__ == "__main__":
    unittest.main()
