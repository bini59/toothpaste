"""GitHub issue tickets (TP-92): repos of a `source: github` project sync open issues labeled `tp`; legacy
github_issues repos sync every open issue. Run: python3 -m unittest discover tests"""
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import core, milestone, pipeline as P


def issue(n, *labels):
    return {"number": n, "title": f"i{n}", "body": "b", "url": f"u{n}", "updatedAt": "t",
            "labels": [{"name": x} for x in labels]}


class SyncIssuesTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO repos(name,github) VALUES ('tp','o/tp'), ('old','o/old')")
        self.open = {"o/tp": [], "o/old": []}
        self.st = {"github_issues": {"tp": "TP", "old": "OLD"}}
        self.pm = {"projects": [{"id": "x", "source": "github", "repos": [{"repo": "tp"}]},
                                {"id": "y", "source": "asana", "asana_gid": "1", "repos": [{"repo": "old"}]}]}
        for p in (mock.patch.object(P, "settings", lambda: self.st), mock.patch.object(P, "project_map", lambda: self.pm),
                  mock.patch.object(P, "_open_issues", self.open.__getitem__)):
            p.start()
            self.addCleanup(p.stop)

    def sync(self):
        new = []
        P.sync_issues(self.con, "now", new)
        return new

    def row(self, gid):
        r = self.con.execute("SELECT key,completed,assigned FROM tickets WHERE gid=?", (gid,)).fetchone()
        return r and (r["key"], r["completed"], r["assigned"])

    def test_label_filter_and_lifecycle(self):
        self.open["o/tp"] = [issue(1, "TP"), issue(2, "bug")]
        self.assertEqual(self.sync(), ["TP-1"])
        self.assertEqual(self.row("gh:o/tp#1"), ("TP-1", 0, 1))
        self.assertIsNone(self.row("gh:o/tp#2"))  # no tp label: not a ticket
        self.open["o/tp"] = [issue(1, "bug")]  # label removed, issue still open
        self.sync()
        self.assertEqual(self.row("gh:o/tp#1"), ("TP-1", 0, 0))
        self.open["o/tp"] = [issue(1, "tp")]  # label back
        self.assertEqual(self.sync(), ["TP-1"])
        self.assertEqual(self.row("gh:o/tp#1"), ("TP-1", 0, 1))
        self.open["o/tp"] = []  # closed
        self.sync()
        self.assertEqual(self.row("gh:o/tp#1"), ("TP-1", 1, 1))

    def test_legacy_repo_takes_every_open_issue(self):
        self.open["o/old"] = [issue(5)]
        self.assertEqual(self.sync(), ["OLD-5"])
        self.open["o/old"] = []
        self.sync()
        self.assertEqual(self.row("gh:o/old#5"), ("OLD-5", 1, 1))

    def test_default_prefix(self):
        self.st["github_issues"] = {}
        self.open["o/tp"] = [issue(3, "tp")]
        self.assertEqual(self.sync(), ["TP-3"])  # from the repo name
        self.assertIsNone(self.row("gh:o/old#5"))  # asana-source repo not in github_issues: not scanned

    def test_repo_no_longer_a_source(self):
        self.open["o/tp"] = [issue(1, "tp")]
        self.sync()
        self.pm["projects"][0]["source"] = "none"
        self.st["github_issues"] = {}
        self.sync()
        self.assertEqual(self.row("gh:o/tp#1"), ("TP-1", 0, 0))

    def test_failed_read_keeps_tickets(self):
        self.open["o/tp"] = [issue(1, "tp")]
        self.sync()
        with mock.patch.object(P, "_open_issues", side_effect=RuntimeError("down")):
            self.sync()
        self.assertEqual(self.row("gh:o/tp#1"), ("TP-1", 0, 1))

    def test_cross_repo_parent_relabel_and_unlink(self):
        parent = {**issue(12, "tp-milestone"), "repository": {"nameWithOwner": "O/old"}}
        self.open["o/tp"] = [{**issue(1, "tp"), "parent": parent}]
        self.open["o/old"] = [parent]
        self.assertEqual(self.sync(), ["TP-1"])
        def relation():
            return tuple(self.con.execute("SELECT parent_gid,parent_name,parent_url,in_milestone FROM tickets WHERE key='TP-1'").fetchone())
        self.assertEqual(relation(), ("gh:o/old#12", "i12", "u12", 1))
        parent["labels"] = []
        self.sync()
        self.assertEqual(relation(), ("gh:o/old#12", "i12", "u12", 0))
        self.open["o/tp"][0]["parent"] = None
        self.sync()
        self.assertEqual(relation(), (None, None, None, 0))

    def test_github_branch_is_a_valid_ref_for_dotted_repo_names(self):
        import subprocess
        ref = milestone.branch("gh:my-org/app..v2#12")
        self.assertEqual(subprocess.run(["git", "check-ref-format", "refs/heads/" + ref]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
