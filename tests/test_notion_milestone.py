"""Notion milestone settings, dispatch and verified completion against the local HTTP fixture."""
import json
from unittest import mock

from test_notion import DOCS_DB, HOME_DB, TEAM_DB, NotionDashboard, uid
from ticket_pilot import cli, core, deploy, milestone, notion, notion_stages, notion_tickets, pipeline, release, server


def relation(*ids):
    return {"type": "relation", "relation": [{"id": i} for i in ids]}


class NotionMilestones(NotionDashboard):
    def setUp(self):
        super().setUp()
        notion.save_app("app-id", "app-secret")
        self.cfg = {"connection_id": self.connect("team"), "data_source_id": TEAM_DB, "people_property": "people-id",
                    "tag_property": "tag-id", "status_property": "status-id", "status_option": "todo-id",
                    "milestone_data_source_id": DOCS_DB, "milestone_property": "ms-rel", "stage_statuses": {"done": "done-id"}}
        self.fixture.props["출시"] = {"id": "ms-rel", "type": "relation", "relation": {"data_source_id": DOCS_DB}}
        for row in self.fixture.pages[TEAM_DB]:
            row["properties"]["출시"] = relation()
        self.parent = self.fixture.pages[DOCS_DB][1]
        self.parent["properties"]["라벨"]["multi_select"] = []
        self.parent_gid = "notion:" + self.parent["id"]
        self.child, self.other = self.fixture.pages[TEAM_DB][0], self.fixture.pages[TEAM_DB][3]
        self.child["properties"]["출시"] = relation(self.parent["id"])
        self.gid = "notion:" + self.child["id"]
        self.save(self.cfg)
        self.add_repo("app")

    def add_repo(self, name):
        self.post("/api/config", {"scope": f"repo.{name}", "value": {
            "path": str(self.root), "github": f"example/{name}", "deploy": {
                "modes": ["staging"], "staging": {"branch": "staging"}, "prod": {"branch": "main"}}}})

    def ticket(self, gid):
        return self.con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()

    def test_save_checks_both_fields_and_the_target_then_sync_rechecks(self):
        for change in ({"milestone_property": ""}, {"milestone_data_source_id": ""}, {"milestone_property": "title"},
                       {"milestone_data_source_id": HOME_DB}, {"milestone_data_source_id": TEAM_DB}):
            with self.subTest(change=change):
                value = {"source": "notion", "name": "example", "notion": {**self.cfg, **change}}
                status, result, _ = self.call("/api/config", {"scope": "project.example", "value": value})
                self.assertEqual(status, 400)
                self.assertIn("마일스톤", result["error"])
        cli.main(["sync"])
        self.fixture.props["출시"]["relation"]["data_source_id"] = HOME_DB
        self.assertIn("마일스톤 관계 열", pipeline.sync()["notion_errors"]["example"])
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))

    def test_sync_replaces_and_clears_parent_and_targets_its_branch(self):
        cli.main(["sync"])
        def projection():
            t = self.ticket(self.gid)
            return (t["parent_gid"], t["parent_name"], t["parent_url"], t["in_milestone"])
        self.assertEqual(projection(), (self.parent_gid, "가이드 초안", self.parent["url"], 1))
        self.assertIsNone(self.ticket(self.parent_gid))
        self.assertIn(f"--base milestone/{self.parent['id']}", "\n".join(self.dispatch_log()))
        with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            milestone.ensure(self.con, self.ticket(self.gid), "app")
        self.con.commit()
        self.parent["properties"]["제목"]["title"] = [{"plain_text": "가이드 최종본"}]
        cli.main(["sync"])
        self.assertEqual(projection()[1], "가이드 최종본")
        self.assertEqual(self.ticket(milestone.ms_gid(self.parent_gid, "app"))["name"], "가이드 최종본 · app 마일스톤")
        other = self.fixture.pages[DOCS_DB][0]
        self.child["properties"]["출시"] = relation(other["id"])
        cli.main(["sync"])
        self.assertEqual(projection(), ("notion:" + other["id"], "가이드 발행", other["url"], 1))
        self.child["properties"]["출시"] = relation()
        cli.main(["sync"])
        self.assertEqual(projection(), (None, None, None, 0))
        self.assertEqual(self.ticket(self.gid)["stage"], "queued")

    def test_schema_validation_preserves_concurrent_settings_save(self):
        read = notion.Client.properties

        def save_during_read(client, source):
            self.post("/api/config", {"scope": "global", "value": {"max_active_runs": 1}})
            return read(client, source)

        with mock.patch.object(notion.Client, "properties", save_during_read):
            status, result, _ = self.call("/api/config", {"scope": "project.second", "value": {
                "source": "notion", "name": "second", "notion": self.cfg}})
        self.assertEqual(status, 400)
        self.assertIn("다시 저장", result["error"])
        self.assertEqual(core.settings()["max_active_runs"], 1)
        self.assertIsNone(notion_tickets.project_config(self.con, "second"))

    def test_multiple_links_hold_only_that_ticket_and_recover(self):
        cli.main(["sync"])
        self.child["properties"]["출시"]["has_more"] = True
        self.fixture.relation_items[f"/pages/{self.child['id']}/properties/ms-rel"] = [self.parent["id"], uid(301)]
        tickets = self.preview(self.cfg)
        self.assertEqual({t["title"]: t["auto_start"] for t in tickets}, {"계정 연결": False, "팀 보드": True})
        for _ in range(2):
            cli.main(["sync"])
        self.assertEqual(self.ticket(self.gid)["stage"], "milestone_error")
        errors = self.con.execute("SELECT message FROM events WHERE kind='error' AND ticket_gid=?", (self.gid,)).fetchall()
        self.assertEqual(len(errors), 1)
        self.assertIn("마일스톤이 2개", errors[0][0])
        starts = [s for s in self.dispatch_log() if "would create" in s]
        self.assertEqual(len(starts), 1)
        self.assertIn("팀 보드", starts[0])
        self.child["properties"]["출시"] = relation(self.parent["id"])
        cli.main(["sync"])
        self.assertEqual(self.ticket(self.gid)["stage"], "queued")
        self.assertIsNone(self.ticket(self.gid)["milestone_error"])

    def test_sync_during_dispatch_does_not_start_on_the_old_branch(self):
        cli.main(["sync"])
        def unlink():
            self.child["properties"]["출시"] = relation()
            pipeline.sync()
        self.fixture.on_markdown = unlink
        lines = self.dispatch_log()
        self.assertTrue(any("대기열" in s for s in lines))
        self.assertFalse(any("계정 연결" in s for s in lines if "would create" in s))

    def merged_pr(self, gid, repo, base):
        rid = self.con.execute("INSERT INTO runs(ticket_gid,repo,state,ws_status) VALUES (?,?,'active','in-review')",
                               (gid, repo)).lastrowid
        url = f"https://github.com/example/{repo}/pull/{rid}"
        self.con.execute("""INSERT INTO prs(url,number,run_id,ticket_gid,repo,state,base,merged_at)
                            VALUES (?,?,?,?,?,'MERGED',?,'2026-11-01')""", (url, rid, rid, gid, repo, base))
        return url

    def test_filtered_run_keeps_its_slot_and_rechecks_ambiguous_links(self):
        self.post("/api/config", {"scope": "global", "value": {"max_active_runs": 1}})
        cli.main(["sync"])
        self.con.execute("INSERT INTO runs(ticket_gid,repo,state,ws_status) VALUES (?,'app','active','in-progress')", (self.gid,))
        self.con.commit()
        self.child["properties"]["상태"]["status"] = {"name": "진행 중", "id": "doing-id"}
        self.child["properties"]["출시"] = relation(self.parent["id"], uid(301))
        cli.main(["sync"])
        t = self.ticket(self.gid)
        self.assertEqual((t["assigned"], t["stage"], t["parent_gid"]), (0, "in_progress", self.parent_gid))
        self.assertIn("마일스톤이 2개", t["milestone_error"])
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        self.assertEqual(release.approval_target(self.con, t)[1], t["milestone_error"])
        self.con.execute("UPDATE tickets SET auto_approved_sha='old-head' WHERE gid=?", (self.gid,))
        self.con.commit()
        self.child["properties"]["출시"] = relation(uid(301))
        cli.main(["sync"])
        t = self.ticket(self.gid)
        self.assertEqual((t["parent_gid"], t["milestone_error"], t["auto_approved_sha"]), ("notion:" + uid(301), None, None))

    def test_unlinked_open_pr_retargets_only_when_its_old_base_is_contained(self):
        cli.main(["sync"])
        gid = milestone.ms_gid(self.parent_gid, "app")
        with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            milestone.ensure(self.con, self.ticket(self.gid), "app")
        rid = self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES (?,'app','active')", (self.gid,)).lastrowid
        self.con.commit()
        self.child["properties"]["출시"] = relation()
        cli.main(["sync"])
        r = self.con.execute("SELECT r.*, 'example/app' github FROM runs r WHERE id=?", (rid,)).fetchone()
        old = milestone.branch(self.parent_gid)
        pr = {"number": 7, "url": "u7", "state": "OPEN", "isDraft": False, "headRefOid": "h", "title": "t",
              "createdAt": "2026-10-01", "updatedAt": "x", "baseRefName": old, "mergedAt": None,
              "mergeCommit": None, "mergeable": "MERGEABLE"}
        for ahead, target in ((1, old), (0, "staging")):
            with mock.patch.object(pipeline, "gh_json", return_value=[pr.copy()]), mock.patch.object(pipeline, "run") as patch, \
                    mock.patch.object(milestone, "_gh", return_value=mock.Mock(stdout="a" * 40)), \
                    mock.patch.object(milestone, "_ahead", return_value=ahead):
                pipeline.refresh_prs(self.con, r, "tp/example")
            if ahead:
                patch.assert_not_called()
            else:
                self.assertIn("base=staging", patch.call_args.args[0])
            self.assertEqual(pipeline.run_pr(self.con, rid)["base"], target)
            milestone.reopen(self.con)
            self.assertEqual(self.ticket(gid)["completed"], int(not ahead))

        self.con.commit()
        self.assertFalse(self.fixture.writes)
        self.child["properties"]["출시"] = relation(self.parent["id"])
        cli.main(["sync"])
        self.assertEqual(milestone.reopen(self.con), 1)
        self.assertFalse(self.ticket(gid)["completed"])

    def test_ambiguous_reviewed_ticket_cannot_deploy_directly(self):
        cli.main(["sync"])
        url = self.merged_pr(self.gid, "app", milestone.branch(self.parent_gid))
        self.con.execute("UPDATE prs SET state='OPEN',merged_at=NULL,head_sha='head' WHERE url=?", (url,))
        self.con.execute("INSERT INTO reviews(pr_url,head_sha,verdict) VALUES (?,'head','approve')", (url,))
        self.con.commit()
        with mock.patch.object(deploy, "repo_rules", return_value={**core.repo_rules("app"), "modes": ["staging", "direct"]}):
            self.assertIsNone(deploy.deploy_target(self.con, "direct", self.gid)[1])
            self.child["properties"]["출시"] = relation(self.parent["id"], uid(301))
            cli.main(["sync"])
            t = self.ticket(self.gid)
            self.assertEqual(t["stage"], "reviewed")
            self.assertEqual(deploy.deploy_target(self.con, "direct", self.gid)[1], t["milestone_error"])
            request = {"status": "requested", "repo": "app", "mode": "direct", "ticket_gid": self.gid}
            with self.assertRaisesRegex(RuntimeError, "마일스톤이 2개"), mock.patch.object(deploy, "_prepare") as prepare:
                deploy._step(self.con, request, print)
            prepare.assert_not_called()

    def test_merged_child_ships_with_its_branch_after_relation_is_cleared(self):
        cli.main(["sync"])
        gid = milestone.ms_gid(self.parent_gid, "app")
        with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            branch = milestone.ensure(self.con, self.ticket(self.gid), "app")
        self.merged_pr(self.gid, "app", branch)
        self.con.commit()
        self.child["properties"]["출시"] = relation()
        cli.main(["sync"])
        self.assertIsNone(self.ticket(self.gid)["parent_gid"])
        self.assertTrue(milestone.ready(self.con, self.parent_gid))
        self.assertEqual([t["gid"] for t in milestone.subtasks(self.con, self.parent_gid)], [self.gid])
        self.assertEqual(len(milestone.merged_subtasks(self.con, self.ticket(gid))), 1)
        displayed = next(t for t in server._state(self.con)["tickets"] if t["gid"] == self.gid)
        self.assertEqual(displayed["parent_gid"], self.parent_gid)
        self.ship(gid, "app", self.merged_pr(gid, "app", "staging"))
        self.assertEqual(self.ticket(self.gid)["completed"], 1)
        self.assertEqual(self.parent["properties"]["상태"]["status"]["name"], "완료")

    def test_milestone_briefs_are_distinct_for_similar_repos(self):
        cli.main(["sync"])
        names = []
        for repo in ("shop-api", "shop-accounts", "30_Api", "31_Api"):
            self.add_repo(repo)
            with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
                milestone.ensure(self.con, self.ticket(self.gid), repo)
            key = self.ticket(milestone.ms_gid(self.parent_gid, repo))["key"]
            names.append(pipeline.slug(key))
            self.con.commit()
        self.assertEqual(len(set(names)), len(names))

    def ship(self, gid, repo, url):
        rid = self.con.execute("INSERT INTO releases(repo,status,prs_json) VALUES (?,'deploying',?)",
                               (repo, json.dumps([{"url": url, "ticket_gid": gid}]))).lastrowid
        release.finalize(self.con, self.con.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone())
        notion_stages.flush(self.con)

    def test_only_last_live_repo_completes_parent_in_its_own_database(self):
        # The two DBs have independent property and option IDs.
        self.fixture.props_by_source[DOCS_DB] = {
            "진척": {"type": "status", "id": "progress", "status": {"options": [{"id": "shipped", "name": "완료"}]}}}
        self.parent["properties"]["진척"] = self.parent["properties"].pop("상태")
        self.add_repo("web")
        self.other["properties"]["출시"] = relation(self.parent["id"])
        cli.main(["sync"])
        tickets = {}
        for page, repo in ((self.child, "app"), (self.other, "web")):
            child = self.ticket("notion:" + page["id"])
            gid = milestone.ms_gid(self.parent_gid, repo)
            with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
                branch = milestone.ensure(self.con, child, repo)
            self.assertEqual(milestone.parse(gid), (self.parent_gid, repo))
            self.assertEqual(branch, "milestone/" + self.parent["id"])
            self.assertEqual(self.ticket(gid)["key"], notion_tickets.ticket_key(self.parent["id"]) + "-" + repo)
            self.assertEqual(self.ticket(gid)["url"], self.parent["url"])
            self.merged_pr(child["gid"], repo, branch)
            tickets[repo] = gid
            page["properties"]["상태"]["status"] = {"name": "진행 중", "id": "doing-id"}
        self.con.commit()
        cli.main(["sync"])  # stage changes filtered both tasks out before their milestone PRs opened
        self.assertFalse(self.ticket(self.gid)["assigned"])
        self.assertTrue(milestone.ready(self.con, self.parent_gid))
        self.post("/api/milestone-done", {"parent": self.parent_gid})
        self.assertTrue(all(self.ticket(g)["ms_done_at"] for g in tickets.values()))
        urls = {repo: self.merged_pr(gid, repo, "staging") for repo, gid in tickets.items()}
        self.ship(tickets["app"], "app", urls["app"])
        self.assertEqual(self.fixture.writes[0][1], "/pages/" + self.child["id"])
        self.assertEqual(self.parent["properties"]["진척"]["status"]["name"], "할 일")
        self.assertIsNone(self.con.execute("SELECT 1 FROM notion_status_queue WHERE ticket_gid=?", (self.parent_gid,)).fetchone())

        path = "/pages/" + self.parent["id"]
        self.fixture.deny_writes.add(("PATCH", path))
        self.ship(tickets["web"], "web", urls["web"])
        retry = self.con.execute("SELECT * FROM notion_status_queue WHERE ticket_gid=?", (self.parent_gid,)).fetchone()
        self.assertIn("콘텐츠 수정", retry["last_error"])
        self.assertEqual(retry["data_source_id"], DOCS_DB)
        self.fixture.deny_writes.clear()
        with mock.patch.object(notion_stages, "time", return_value=retry["next_attempt_at"]):
            notion_stages.flush(self.con)
        self.assertEqual(self.parent["properties"]["진척"]["status"], {"name": "완료", "id": "shipped"})
        self.assertEqual(self.other["properties"]["상태"]["status"]["id"], "done-id")
        self.assertIsNone(self.ticket(self.parent_gid))
        self.assertEqual(sum(w[1] == path for w in self.fixture.writes), 1)
