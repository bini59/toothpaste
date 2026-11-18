"""The train, dashboard, and production tracking select PRs independently of ticket stage."""
import json
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import core, deploy, release, server, track


class ReleaseCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        core.write_config(self.con, {"settings": {"deploy": {"repos": {
            "api": {"train": True, "modes": ["staging", "promote", "all"],
                    "staging": {"branch": "dev"}, "prod": {"branch": "main"}},
            "store": {"staging": {"branch": "staging"}, "prod": {"branch": "main"}},
            "site": {"prod": {"branch": "main"}},
        }}}})
        self.addCleanup(self.con.close)
        self.addCleanup(core.config_cache_clear)
        self.enterContext(mock.patch.object(core, "_config_con", return_value=self.con))

    def pr(self, number, *, repo="api", base="dev", state="MERGED", stage="staged",
           gid=None, release_id=None, prod_at=None):
        gid = gid or f"T-{number}"
        url = f"https://github.com/o/{repo}/pull/{number}"
        self.con.execute("INSERT INTO tickets(gid,key,name,stage,assigned,completed) VALUES (?,?,?,?,1,0)",
                         (gid, gid, gid, stage))
        self.con.execute("""INSERT INTO prs(url,ticket_gid,repo,github,number,state,base,merged_at,merge_sha,release_id,prod_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (url, gid, repo, f"o/{repo}", number, state, base, f"2026-11-{number:02d}",
                          f"merge{number}", release_id, prod_at))
        return url

    def test_same_train_and_deploy_candidates_as_staged_tickets(self):
        milestone = self.pr(2, gid="ms:P1:api")
        regular = self.pr(1)
        manual = self.pr(3, repo="store", base="staging")
        self.pr(4, state="OPEN", stage="reviewed")
        self.pr(5, base="main", stage="merged")
        self.pr(6, base="milestone/P1", stage="milestone")
        self.pr(7, release_id=1, stage="done")
        self.pr(8, prod_at="2026-11-09", stage="done")
        legacy = {}
        for repo in ("api", "store", "site"):
            legacy[repo] = [row[0] for row in self.con.execute(
                """SELECT p.url FROM prs p JOIN tickets t ON t.gid=p.ticket_gid
                   WHERE t.stage='staged' AND p.repo=? ORDER BY p.merged_at""", (repo,))]
            self.assertEqual([p["url"] for p in deploy.staged_prs(self.con, repo)], legacy[repo])
        self.assertEqual(release.staged_pr_urls(self.con), {"api": legacy["api"]})
        self.assertEqual(legacy, {"api": [regular, milestone], "store": [manual], "site": []})

    def test_candidates_use_pr_fields_even_when_ticket_stage_disagrees(self):
        regular = self.pr(1, stage="done")
        milestone = self.pr(2, gid="ms:P1:api", stage="collecting")
        self.pr(3, state="OPEN")
        self.pr(4, base="staging")
        self.pr(5, release_id=10)
        self.pr(6, prod_at="2026-11-07")
        self.pr(7, repo="site", base="main")
        manual = self.pr(8, repo="store", base="staging", stage="merged")
        self.assertEqual(release.staged_pr_urls(self.con), {"api": [regular, milestone]})
        self.assertEqual([p["url"] for p in deploy.staged_prs(self.con, "api")], [regular, milestone])
        self.assertEqual(server._state(self.con)["staged_pr_urls"], {"api": [regular, milestone], "store": [manual]})

    def test_train_skips_repo_when_only_milestones_awaiting_another_round_remain(self):
        milestone = self.pr(1, gid="ms:P1:api")
        self.con.execute("UPDATE tickets SET parent_gid='P1' WHERE gid='ms:P1:api'")
        self.con.execute("""INSERT INTO tickets(gid,parent_gid,has_tag,assigned,completed,stage)
                            VALUES ('child','P1',1,1,0,'queued')""")
        run = self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES ('ms:P1:api','api','active')").lastrowid
        self.con.execute("UPDATE prs SET run_id=? WHERE url=?", (run, milestone))
        regular = self.pr(2, repo="store", base="staging")
        self.assertEqual(release.staged_pr_urls(self.con), {"api": [milestone]})
        state = server._state(self.con)
        self.assertTrue(next(p for p in state["prs"] if p["url"] == milestone)["awaiting_milestone_round"])
        target, error = deploy.deploy_target(self.con, "promote", ticket="ms:P1:api")
        self.assertIsNone(target)
        self.assertIn("다음 마일스톤 PR", error)
        with mock.patch.object(release, "db", return_value=self.con), \
                mock.patch.object(release, "train_repos", return_value=["api", "store"]), \
                mock.patch.object(release, "_start", return_value="started"):
            self.assertEqual(release.release_tick(force=True), "started")
        row, = self.con.execute("SELECT repo, prs_json FROM releases").fetchall()
        self.assertEqual((row["repo"], [p["url"] for p in json.loads(row["prs_json"])]), ("store", [regular]))

    def test_track_checks_merged_prs_outside_merged_or_staged_ticket_stages(self):
        waiting = self.pr(1, base="main", stage="collecting")
        self.pr(2, state="OPEN")
        self.pr(3, release_id=10)
        self.pr(4, prod_at="2026-11-07")
        active = self.pr(5)
        self.con.execute("INSERT INTO releases(repo, status, prs_json) VALUES ('api','deploying',?)",
                         (json.dumps([{"url": active}]),))
        self.pr(6, base="milestone/P1")
        with mock.patch.object(track, "db", return_value=self.con), \
                mock.patch.object(track, "track_config", return_value={"api": {"workflow": "deploy.yml"}}), \
                mock.patch.object(track, "deployed_sha", return_value=("live", "job")), \
                mock.patch.object(track, "_ancestor", return_value=True) as ancestor, \
                mock.patch.object(release, "complete_asana") as complete:
            self.assertEqual(track.track_deploys(log=lambda _: None), 1)
        ancestor.assert_called_once_with("o/api", "merge1", "live")
        complete.assert_called_once_with(self.con, "T-1", pr_url=waiting)
        self.assertTrue(self.con.execute("SELECT prod_at FROM prs WHERE url=?", (waiting,)).fetchone()[0])

    def test_verified_older_pr_preserves_newer_ticket_work(self):
        url = self.pr(1)
        self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES ('T-1','api','active')")
        with mock.patch.object(release, "asana") as asana:
            self.assertIsNone(release.complete_asana(self.con, "T-1", pr_url=url))
        asana.assert_not_called()
        self.assertFalse(self.con.execute("SELECT completed FROM tickets WHERE gid='T-1'").fetchone()[0])
