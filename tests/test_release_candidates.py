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
        self.con.execute("""INSERT INTO tickets(gid,key,name,stage,assigned,completed) VALUES (?,?,?,?,1,0)
                            ON CONFLICT(gid) DO NOTHING""",
                         (gid, gid, gid, stage))
        self.con.execute("""INSERT INTO prs(url,ticket_gid,repo,github,number,state,base,merged_at,head_sha,merge_sha,release_id,prod_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (url, gid, repo, f"o/{repo}", number, state, base, f"2026-11-{number:02d}",
                          f"head{number}", f"merge{number}", release_id, prod_at))
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

    def test_state_includes_every_staging_pr_and_its_ticket(self):
        first = self.pr(1)
        second = self.pr(9, gid="T-1")
        self.pr(10, gid="T-1", state="OPEN")
        self.con.execute("UPDATE tickets SET stage='done', completed=1, assigned=0 WHERE gid='T-1'")
        state = server._state(self.con)
        self.assertEqual(state["staged_pr_urls"], {"api": [first, second]})
        self.assertEqual([t["gid"] for t in state["tickets"]], ["T-1"])

    def test_promote_records_selected_older_pr_when_latest_run_has_an_open_pr(self):
        first = self.pr(1, stage="reviewed")
        second = self.pr(9, gid="T-1")
        latest = self.pr(10, gid="T-1", state="OPEN")
        for url in (first, second, latest):
            run_id = self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES ('T-1','api','active')").lastrowid
            self.con.execute("UPDATE prs SET run_id=? WHERE url=?", (run_id, url))
        self.assertEqual([p["url"] for p in deploy.staged_prs(self.con, "api")], [first, second])
        with mock.patch.object(deploy, "db", return_value=self.con):
            with self.assertRaisesRegex(SystemExit, "머지된 PR이 아니에요"):
                deploy.request_deploy("promote", ticket="T-1")
            rel_id = deploy.request_deploy("promote", ticket="T-1", pr_url=first)
            with self.assertRaisesRegex(SystemExit, "이미 배포 중"):
                deploy.request_deploy("promote", ticket="T-1", pr_url=second)
        row = self.con.execute("SELECT * FROM releases WHERE id=?", (rel_id,)).fetchone()
        self.assertEqual((row["repo"], row["status"], row["mode"], row["ticket_gid"]), ("api", "requested", "promote", "T-1"))
        [pr] = json.loads(row["prs_json"])
        self.assertEqual((pr["url"], pr["head_sha"], pr["merge_sha"]), (first, "head1", "merge1"))

    def test_promote_rejects_prs_outside_the_tickets_staging_candidates(self):
        staged = self.pr(1)
        other_ticket = self.pr(2)
        open_pr = self.pr(3, gid="T-1", state="OPEN")
        other_base = self.pr(4, gid="T-1", base="main")
        released = self.pr(5, gid="T-1", release_id=1)
        live = self.pr(6, gid="T-1", prod_at="2026-11-08")
        for url, repo, message in (
            (other_ticket, None, "지정한 PR을 찾을 수 없어요"),
            (staged, "store", "지정한 PR이 이 레포에 없어요"),
            (open_pr, None, "머지된 PR이 아니에요"),
            (other_base, None, "머지된 PR이 아니에요"),
            (released, None, "이미 프로덕션에 반영됐어요"),
            (live, None, "이미 프로덕션에 반영됐어요"),
        ):
            with self.subTest(url=url, repo=repo), mock.patch.object(deploy, "db", return_value=self.con):
                with self.assertRaisesRegex(SystemExit, message):
                    deploy.request_deploy("promote", ticket="T-1", repo=repo, pr_url=url)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 0)

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
        target, error = deploy.deploy_target(self.con, "promote", ticket="ms:P1:api", pr_url=milestone)
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
