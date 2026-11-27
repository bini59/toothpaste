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

    def add_repo(self, name, **deploy):
        self.post("/api/config", {"scope": f"repo.{name}", "value": {
            "path": str(self.root), "github": f"example/{name}", "deploy": {
                "modes": ["staging", "direct", "promote"], "staging": {"branch": "staging"},
                "prod": {"branch": "main"}, **deploy}}})

    def ticket(self, gid):
        return self.con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()

    def save_project(self):
        self.post("/api/config", {"scope": "project.example", "value": {"source": "notion", "name": "example", "notion": self.cfg}})

    def use_task_db(self, kind="select"):
        task = {"id": "task-id", "name": "작업"}
        marker = {"id": "milestone-id", "name": "마일스톤"}
        self.fixture.props["유형"] = {"id": "kind-id", "type": kind, kind: {"options": [task, marker]}}
        self.fixture.props["출시"]["relation"]["data_source_id"] = TEAM_DB
        self.parent = self.other  # also matches the assignee, work tag and status filters
        self.parent_gid = "notion:" + self.parent["id"]
        for row in self.fixture.pages[TEAM_DB]:
            row["properties"]["유형"] = {"type": kind, kind: [] if kind == "multi_select" else None}
        self.parent["properties"]["유형"][kind] = [task, marker] if kind == "multi_select" else marker
        self.child["properties"]["출시"] = relation(self.parent["id"])
        self.cfg.update(milestone_data_source_id=TEAM_DB, milestone_kind_property="kind-id",
                        milestone_kind_option="milestone-id")
        self.save_project()

    def test_task_db_select_uses_ids_for_listing_dispatch_and_verified_status(self):
        self.use_task_db()
        self.assertEqual(notion_tickets.project_config(self.con, "example"), self.cfg)
        self.fixture.props["작업 유형"] = self.fixture.props.pop("유형")
        self.fixture.props["작업 유형"]["select"]["options"][1]["name"] = "출시 단위"
        for row in self.fixture.pages[TEAM_DB]:
            row["properties"]["작업 유형"] = row["properties"].pop("유형")
        self.assertEqual([t["gid"] for t in self.preview(self.cfg)], [self.gid])
        cli.main(["sync"])
        self.assertEqual(self.stages(), {"계정 연결": "queued"})
        self.assertIsNone(self.ticket(self.parent_gid))
        self.assertEqual(self.ticket(self.gid)["parent_gid"], self.parent_gid)
        starts = [s for s in self.dispatch_log() if "would create" in s]
        self.assertEqual(len(starts), 1)
        self.assertIn(f"--base milestone/{self.parent['id']}", starts[0])

        self.parent["properties"]["개발 담당"]["people"] = []  # the relation target can be outside the query
        cli.main(["sync"])
        with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            base = milestone.ensure(self.con, self.ticket(self.gid), "app")
        self.merged_pr(self.gid, "app", base)
        gid = milestone.ms_gid(self.parent_gid, "app")
        self.ship(gid, "app", self.merged_pr(gid, "app", "staging"))
        self.assertEqual(self.parent["properties"]["상태"]["status"]["id"], "done-id")
        self.assertIsNone(self.ticket(self.parent_gid))

    def test_task_db_multi_select_holds_only_the_wrong_link_until_tagged(self):
        self.use_task_db("multi_select")
        values = self.parent["properties"]["유형"]["multi_select"]
        marker = values.pop()
        preview = {t["gid"]: t["stage"] for t in self.preview(self.cfg)}
        self.assertEqual(preview, {self.gid: "milestone_error", self.parent_gid: "queued"})
        cli.main(["sync"])
        self.assertIn("마일스톤 태그", self.ticket(self.gid)["milestone_error"])
        starts = [s.splitlines()[0] for s in self.dispatch_log() if "would create" in s]
        self.assertEqual(starts, ["--- would create app/" + self.ticket(self.parent_gid)["key"].lower()])
        values.append(marker)
        cli.main(["sync"])
        self.assertEqual(self.stages(), {"계정 연결": "queued"})
        self.assertEqual(self.ticket(self.gid)["parent_gid"], self.parent_gid)
        self.assertNotIn(self.parent_gid, {t["gid"] for t in server._state(self.con)["tickets"]})

    def test_task_db_requires_valid_kind_binding_at_save_and_after_schema_changes(self):
        self.use_task_db()
        for change in ({"milestone_kind_property": ""}, {"milestone_kind_option": ""}, {"milestone_kind_property": "status-id"},
                       {"milestone_kind_option": "마일스톤"}, {"milestone_data_source_id": DOCS_DB},
                       {"status_property": "kind-id", "status_option": "", "stage_statuses": {"done": "task-id"}},
                       {"status_property": "kind-id", "status_option": "", "stage_statuses": {"in_progress": "milestone-id"}}):
            with self.subTest(change=change):
                value = {"source": "notion", "name": "example", "notion": {**self.cfg, **change}}
                status, result, _ = self.call("/api/config", {"scope": "project.example", "value": value})
                self.assertEqual(status, 400)
                self.assertIn("마일스톤", result["error"])
        cli.main(["sync"])
        col = self.fixture.props.pop("유형")
        for replacement in (None, {**col, "id": "new-column"}, {**col, "select": {"options": [{"id": "new-id", "name": "마일스톤"}]}}):
            with self.subTest(replacement=replacement):
                if replacement:
                    self.fixture.props["유형"] = replacement
                self.assertIn("다시 골라 주세요", pipeline.sync()["notion_errors"]["example"])
                status, result, _ = self.call("preview", {"project_id": "example", "repos": ["app"], "notion": self.cfg})
                self.assertEqual(status, 400)
                self.assertIn("다시 골라 주세요", result["error"])
                self.assertFalse(any("would create" in s for s in self.dispatch_log()))
                self.assertIsNone(self.ticket(self.parent_gid))
        self.fixture.props["유형"] = col
        cli.main(["sync"])
        self.assertTrue(any("would create" in s for s in self.dispatch_log()))

    def test_task_retagged_during_a_run_leaves_the_list_and_blocks_on_the_board(self):
        self.use_task_db()
        self.cfg["stage_statuses"]["blocked"] = "doing-id"
        self.save_project()
        cli.main(["sync"])
        fetch = notion_tickets.fetch
        def start_during_sync(*args):
            listings = fetch(*args)
            self.con.execute("INSERT INTO runs(ticket_gid,repo,state,ws_status) VALUES (?,'app','active','in-progress')", (self.gid,))
            self.con.commit()
            return listings
        self.child["properties"]["유형"]["select"] = self.parent["properties"]["유형"]["select"]
        with mock.patch.object(notion_tickets, "fetch", start_during_sync):
            cli.main(["sync"])
        self.assertEqual(self.preview(self.cfg), [])
        card = next(t for t in server._state(self.con)["tickets"] if t["gid"] == self.gid)
        self.assertEqual((card["assigned"], card["stage"], card["parent_gid"]), (0, "blocked", self.parent_gid))
        self.assertTrue(card["milestone_kind_blocked"])
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        notion_stages.flush(self.con)
        self.assertEqual(self.fixture.writes, [])

        self.child["properties"]["유형"]["select"] = None
        cli.main(["sync"])
        t = self.ticket(self.gid)
        self.assertEqual((t["assigned"], t["stage"], t["milestone_error"]), (1, "in_progress", None))

    def test_changing_parent_tag_while_reading_brief_holds_child(self):
        self.use_task_db()
        self.cfg.update(milestone_kind_property="tag-id", milestone_kind_option="docs-id")
        self.save_project()
        cli.main(["sync"])
        key = self.ticket(self.parent_gid)["key"]
        cli.main(["notion", "tag", key, "문서"])
        cli.main(["sync"])
        self.assertEqual(self.stages(), {"계정 연결": "queued"})
        self.fixture.on_markdown = lambda: cli.main(["notion", "tag", key, "문서", "--remove"])
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        cli.main(["sync"])
        self.assertEqual(self.ticket(self.gid)["stage"], "milestone_error")

    def test_filtered_run_retries_wait_for_parent_edits_and_a_valid_schema(self):
        self.use_task_db()
        self.cfg.update(milestone_kind_property="tag-id", milestone_kind_option="docs-id")
        self.save_project()
        cli.main(["sync"])
        parent = self.ticket(self.parent_gid)["key"]
        cli.main(["notion", "tag", parent, "문서"])
        self.con.execute("""INSERT INTO runs(ticket_gid,repo,state,worktree_path,ws_comment)
                            VALUES (?,'app','active',?,'BLOCKED: 확인')""", (self.gid, str(self.root)))
        self.con.commit()
        self.child["properties"]["상태"]["status"] = {"id": "doing-id", "name": "진행 중"}
        cli.main(["sync"])
        pipeline.request_reply(self.gid, "계속")
        self.assertFalse(self.ticket(self.gid)["assigned"])
        def would_resume():
            return any("would relaunch" in s for s in self.dispatch_log())
        self.assertTrue(would_resume())
        cli.main(["notion", "tag", parent, "문서", "--remove"])
        self.assertFalse(would_resume())
        cli.main(["notion", "tag", parent, "문서"])
        options = self.fixture.props["라벨"]["multi_select"]["options"]
        marker = next(o for o in options if o["id"] == "docs-id")
        options.remove(marker)
        self.assertIn("다시 골라 주세요", pipeline.sync()["notion_errors"]["example"])
        self.assertFalse(would_resume())
        options.append(marker)
        cli.main(["sync"])
        self.assertTrue(would_resume())

    def test_retag_during_worktree_fetch_does_not_create_a_worktree_or_run(self):
        self.use_task_db()
        self.child["properties"]["출시"] = relation()
        cli.main(["sync"])
        def git(path, *args, **kwargs):
            if "fetch" in args:
                self.child["properties"]["유형"]["select"] = self.parent["properties"]["유형"]["select"]
                pipeline.sync()
            self.assertNotIn("add", args)
            return mock.Mock(returncode=0, stdout="")
        (self.root / "prompts").symlink_to(core.ROOT / "prompts", target_is_directory=True)
        with mock.patch.object(pipeline, "ROOT", self.root), mock.patch.object(pipeline, "github_token", return_value="fixture"), \
                mock.patch.object(pipeline.R, "worktrees", return_value=[]), \
                mock.patch.object(pipeline.R, "root", return_value=self.root), \
                mock.patch.object(pipeline.R, "_git", side_effect=git), mock.patch.object(pipeline.R, "launch") as launch:
            self.assertEqual(pipeline.dispatch(only=self.gid), [])
        launch.assert_not_called()
        self.assertIsNone(pipeline.latest_run(self.con, self.gid))
        self.assertFalse(self.ticket(self.gid)["assigned"])

    def test_switch_to_task_db_during_worktree_discovery_prevents_adoption(self):
        cli.main(["sync"])
        gid = "notion:" + self.other["id"]
        path = self.root / pipeline.slug(self.ticket(gid)["key"])
        def discover(_repos):
            self.use_task_db()
            cli.main(["sync"])
            return [{"repo": "app", "path": str(path)}]
        with mock.patch.object(pipeline, "github_token", return_value="fixture"), \
                mock.patch.object(pipeline.R, "worktrees", side_effect=discover), mock.patch.object(pipeline.R, "launch") as launch:
            self.assertEqual(pipeline.dispatch(only=gid), [])
        launch.assert_not_called()
        self.assertIsNone(pipeline.latest_run(self.con, gid))
        self.assertFalse(self.ticket(gid)["assigned"])

    def test_milestone_status_retry_preserves_resumed_tasks_status(self):
        self.use_task_db()
        self.cfg["stage_statuses"]["in_progress"] = "doing-id"
        self.save_project()
        prop = self.parent["properties"]["유형"]
        marker, prop["select"] = prop["select"], None
        cli.main(["sync"])
        self.con.execute("INSERT INTO runs(ticket_gid,repo,state,ws_status) VALUES (?,'app','active','in-progress')", (self.parent_gid,))
        self.con.commit()
        prop["select"] = marker
        cli.main(["sync"])
        with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            branch = milestone.ensure(self.con, self.ticket(self.gid), "app")
        self.merged_pr(self.gid, "app", branch)
        gid = milestone.ms_gid(self.parent_gid, "app")
        self.fixture.deny_writes.add(("PATCH", "/pages/" + self.parent["id"]))
        self.ship(gid, "app", self.merged_pr(gid, "app", "staging"))
        retry = self.con.execute("SELECT next_attempt_at FROM notion_status_queue WHERE stage='milestone_done'").fetchone()
        self.assertIsNotNone(retry)
        prop["select"] = None
        cli.main(["sync"])
        self.assertEqual(self.ticket(self.parent_gid)["stage"], "in_progress")
        self.fixture.deny_writes.clear()
        with mock.patch.object(notion_stages, "time", return_value=retry[0]):
            notion_stages.flush(self.con)
        self.assertEqual(self.parent["properties"]["상태"]["status"]["id"], "doing-id")
        self.assertIsNone(self.con.execute("SELECT 1 FROM notion_status_queue").fetchone())

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

    def test_direct_only_holds_the_subtask_before_dispatch_and_recovers(self):
        cli.main(["sync"])
        self.add_repo("app", modes=["direct"], staging=None)  # the saved queue still says "queued"
        with mock.patch.object(pipeline, "github_token", return_value="fixture"), \
                mock.patch.object(pipeline.R, "worktrees") as worktrees:
            self.assertEqual(pipeline.dispatch(only=self.gid), [])
        worktrees.assert_not_called()
        self.assertIsNone(pipeline.latest_run(self.con, self.gid))
        task = next(t for t in server._state(self.con)["tickets"] if t["gid"] == self.gid)
        self.assertEqual((task["parent_gid"], task["in_milestone"], task["stage"]), (self.parent_gid, 1, "needs_staging"))
        self.assertIn("app → 배포", task["staging_config_error"])
        self.assertIn("staging 브랜치 사용", task["staging_config_error"])
        self.assertIn("배포 방식", task["staging_config_error"])
        self.post("/api/config", {"scope": "global", "value": {"max_active_runs": 1}})
        cli.main(["sync"])
        starts = [s for s in self.dispatch_log() if "would create" in s]
        self.assertEqual(len(starts), 1)
        self.assertIn("팀 보드", starts[0])

        self.add_repo("app", staging={"branch": "dev"})
        cli.main(["sync"])
        self.assertEqual(self.ticket(self.gid)["stage"], "queued")
        lines = []
        pipeline.dispatch(only=self.gid, dry_run=True, log=lines.append)
        self.assertIn(f"--base {milestone.branch(self.parent_gid)} ", "\n".join(lines))

    def test_preview_and_assignment_check_the_selected_repo(self):
        self.add_repo("web", modes=["direct"], staging=None)
        for repos, stage in (([], "unmapped"), (["app", "web"], "needs_repo"), (["web"], "needs_staging"), (["app"], "queued")):
            with self.subTest(repos=repos):
                task = next(t for t in self.preview(self.cfg, repos) if t["gid"] == self.gid)
                self.assertEqual((task["stage"], task["auto_start"]), (stage, stage == "queued"))
        self.post("/api/config", {"scope": "project.example", "value": {"name": "example", "source": "notion",
                  "notion": self.cfg, "repos": [{"repo": name, "role": "app"} for name in ("app", "web")]}})
        cli.main(["sync"])
        self.assertEqual(self.ticket(self.gid)["stage"], "needs_repo")
        pipeline.assign(self.gid, "web")
        self.assertEqual(self.ticket(self.gid)["stage"], "needs_staging")
        task = next(t for t in self.preview(self.cfg, ["app"]) if t["gid"] == self.gid)
        self.assertFalse(task["auto_start"])  # the explicit override still selects web
        pipeline.assign(self.gid, "app")
        self.assertEqual(self.ticket(self.gid)["stage"], "queued")

    def test_staging_disabled_while_reading_the_brief_holds_the_subtask(self):
        cli.main(["sync"])
        self.fixture.on_markdown = lambda: self.add_repo("app", modes=["direct"], staging=None)
        with mock.patch.object(pipeline, "github_token", return_value="fixture"), \
                mock.patch.object(pipeline.R, "worktrees") as worktrees:
            self.assertEqual(pipeline.dispatch(only=self.gid), [])
        worktrees.assert_not_called()
        self.assertEqual(self.ticket(self.gid)["stage"], "needs_staging")

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

    def reviewed_pr(self, gid, base):
        url = self.merged_pr(gid, "app", base)
        self.con.execute("UPDATE prs SET state='OPEN',merged_at=NULL,head_sha='head' WHERE url=?", (url,))
        self.con.execute("INSERT INTO reviews(pr_url,head_sha,verdict) VALUES (?,'head','approve')", (url,))
        pipeline.restage(self.con)
        self.con.commit()
        return url

    def assert_direct_blocked(self, gid, url, reason):
        status, result, _ = self.call("/api/deploy", {"mode": "direct", "ticket": gid})
        self.assertEqual(status, 400)
        self.assertIn(reason, result["error"])
        pr = next(p for p in server._state(self.con)["prs"] if p["url"] == url)
        self.assertEqual(pr["direct_deploy_error"], result["error"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM releases").fetchone()[0], 0)

    def assert_last_deploy_failed(self, reason):
        row = self.con.execute("SELECT status,reason FROM releases ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn(reason, row["reason"])

    def check_unlink_without_staging_mode(self, target, **rule):
        cli.main(["sync"])
        old = milestone.branch(self.parent_gid)
        url = self.reviewed_pr(self.gid, old)
        self.add_repo("app", modes=["direct"], **rule)
        self.child["properties"]["출시"] = relation()
        cli.main(["sync"])
        run = dict(pipeline.latest_run(self.con, self.gid), github="example/app")
        pr = {"number": 1, "url": url, "state": "OPEN", "isDraft": True, "headRefOid": "head", "title": "task",
              "createdAt": core.now(), "updatedAt": core.now(), "baseRefName": old, "mergeable": "CONFLICTING"}
        fork = "a" * 40
        for ahead in (1, 0):
            reads = [[pr.copy()]]
            if not rule.get("pr_into_staging"):
                reads.append({"defaultBranchRef": {"name": target}})
            with (
                mock.patch.object(pipeline, "gh_json", side_effect=reads),
                mock.patch.object(pipeline, "run") as retarget,
                mock.patch.object(milestone, "_gh", side_effect=[mock.Mock(stdout=fork), mock.Mock(stdout=str(ahead))]) as compare,
            ):
                pipeline.refresh_prs(self.con, run, "tp/example")
            self.assertEqual(compare.call_args_list, [
                mock.call(["api", f"repos/example/app/compare/{old}...head", "--jq", ".merge_base_commit.sha"]),
                mock.call(["api", f"repos/example/app/compare/{target}...{fork}", "--jq", ".ahead_by"]),
            ])
            self.con.commit()
            stored = pipeline.run_pr(self.con, run["id"])
            if ahead:
                retarget.assert_not_called()
                self.assertEqual(stored["base"], old)
                self.assert_direct_blocked(self.gid, url, old)
                self.assertIsNone(pipeline.resume_staging_error(self.con, self.ticket(self.gid)))
            else:
                self.assertIn(f"base={target}", retarget.call_args.args[0])
                self.assertEqual((stored["base"], stored["mergeable"]), (target, None))
        self.post("/api/deploy", {"mode": "direct", "ticket": self.gid})

    def test_unlink_returns_to_dev_with_staging_mode_disabled(self):
        self.check_unlink_without_staging_mode("dev", pr_into_staging=True, staging={"branch": "dev"})

    def test_unlink_returns_to_github_default_without_a_configured_base(self):
        self.check_unlink_without_staging_mode("trunk", staging=None)

    def test_reviewed_main_pr_explains_missing_staging_until_settings_are_fixed(self):
        cli.main(["sync"])
        url = self.reviewed_pr(self.gid, "main")
        for rule in (None, {"modes": ["direct"], "staging": None}, {"modes": ["direct"], "pr_into_staging": True},
                     {"staging": {"branch": "main"}, "train": True, "preview_label": "preview"},
                     {"staging": {"branch": "main"}, "via": "next"}):
            with self.subTest(rule=rule):
                if rule is None:
                    self.post("/api/config", {"scope": "repo.app", "value": {"path": str(self.root), "github": "example/app"}})
                else:
                    self.add_repo("app", **rule)
                pr = next(p for p in server._state(self.con)["prs"] if p["url"] == url)
                reason = pr["staging_deploy_error"]
                self.assertIn("app → 배포", reason)
                self.assertEqual(pr["direct_deploy_error"], reason)
                self.assertEqual(release.approval_target(self.con, self.ticket(self.gid))[1], reason)
                for mode in ("direct", "staging", "preview"):
                    status, result, _ = self.call("/api/deploy", {"mode": mode, "ticket": self.gid})
                    self.assertEqual((status, result["error"]), (400, reason))
                self.assertIsNone(self.con.execute("SELECT id FROM releases").fetchone())

        self.add_repo("app")
        run = pipeline.latest_run(self.con, self.gid)
        pr = {"number": 1, "url": url, "state": "OPEN", "isDraft": True, "headRefOid": "head", "title": "task",
              "createdAt": core.now(), "updatedAt": core.now(), "baseRefName": "main", "mergeable": "MERGEABLE"}
        with mock.patch.object(pipeline, "gh_json", return_value=[pr]), mock.patch.object(pipeline, "run") as retarget, \
                mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            pipeline.refresh_prs(self.con, {**dict(run), "github": "example/app"}, "tp/example")
        base = milestone.branch(self.parent_gid)
        self.assertIn(f"base={base}", retarget.call_args.args[0])
        self.assertEqual(release.approval_target(self.con, self.ticket(self.gid))[0]["base"], base)

    def test_relaunch_checks_the_existing_worktrees_repo(self):
        cli.main(["sync"])
        self.reviewed_pr(self.gid, "main")
        self.add_repo("app", modes=["direct"], staging=None)
        self.add_repo("web")
        pipeline.assign(self.gid, "web")  # the existing worktree and PR still belong to app
        for stage, handler in (("rework", "rework"), ("conflict", "resolve_conflict"), ("blocked", "unblock")):
            with self.subTest(stage=stage):
                self.con.execute("UPDATE tickets SET stage=?, reply='계속' WHERE gid=?", (stage, self.gid))
                self.con.commit()
                lines = []
                with mock.patch.object(pipeline, handler) as resume:
                    pipeline.dispatch(only=self.gid, dry_run=True, log=lines.append)
                resume.assert_not_called()
                self.assertTrue(any("app → 배포" in s for s in lines))

    def test_direct_request_checks_both_relation_and_original_pr_base(self):
        cli.main(["sync"])
        base = milestone.branch(self.parent_gid)
        url = self.reviewed_pr(self.gid, base)
        self.assertEqual(release.approval_target(self.con, self.ticket(self.gid))[0]["base"], base)
        self.assert_direct_blocked(self.gid, url, "staging")
        self.child["properties"]["출시"] = relation(self.parent["id"], uid(301))
        cli.main(["sync"])
        self.assertEqual(self.ticket(self.gid)["stage"], "reviewed")
        self.assert_direct_blocked(self.gid, url, "마일스톤이 2개")
        self.child["properties"]["출시"] = relation()
        cli.main(["sync"])
        self.assert_direct_blocked(self.gid, url, base)
        self.con.execute("UPDATE prs SET base='staging' WHERE url=?", (url,))
        self.con.commit()
        self.post("/api/deploy", {"mode": "direct", "ticket": self.gid})

    def test_queued_direct_deploy_rechecks_new_milestone_links_before_preparing(self):
        cli.main(["sync"])
        gid = "notion:" + self.other["id"]
        self.reviewed_pr(gid, "staging")
        for ids, reason in [([self.parent["id"]], "staging"), ([self.parent["id"], uid(301)], "마일스톤이 2개")]:
            with self.subTest(ids=ids):
                self.post("/api/deploy", {"mode": "direct", "ticket": gid})
                self.other["properties"]["출시"] = relation(*ids)
                cli.main(["sync"])
                with mock.patch.object(deploy, "_prepare") as prepare:
                    deploy.deploy_tick()
                prepare.assert_not_called()
                self.assert_last_deploy_failed(reason)
                self.other["properties"]["출시"] = relation()
                cli.main(["sync"])

    def test_direct_deploy_checks_live_pr_base_before_cherry_pick(self):
        cli.main(["sync"])
        gid = "notion:" + self.other["id"]
        self.reviewed_pr(gid, "staging")
        self.post("/api/deploy", {"mode": "direct", "ticket": gid})
        live = {"state": "OPEN", "baseRefName": milestone.branch(self.parent_gid), "headRefOid": "head"}
        with mock.patch.object(deploy, "gh_json", return_value=live), mock.patch.object(deploy, "_release_branch") as branch:
            deploy.deploy_tick()
        branch.assert_not_called()
        self.assert_last_deploy_failed(live["baseRefName"])

    def test_milestone_link_added_while_marking_draft_ready_stops_the_merge(self):
        cli.main(["sync"])
        gid = "notion:" + self.other["id"]
        url = self.reviewed_pr(gid, "main")
        self.post("/api/deploy", {"mode": "direct", "ticket": gid})
        live = {"state": "OPEN", "baseRefName": "main", "headRefOid": "head", "number": 1,
                "isDraft": True, "mergeable": "MERGEABLE", "statusCheckRollup": []}

        def link_while_ready(args):
            self.assertEqual(args, ["pr", "ready", url])
            self.other["properties"]["출시"] = relation(self.parent["id"])
            cli.main(["sync"])

        with mock.patch.object(deploy, "gh_json", return_value=live), \
                mock.patch.object(deploy, "_gh", side_effect=link_while_ready) as gh:
            deploy.deploy_tick()
        gh.assert_called_once_with(["pr", "ready", url])
        self.assert_last_deploy_failed("마일스톤 PR")

    def test_direct_deploy_rechecks_the_ticket_while_its_release_pr_waits_for_checks(self):
        cli.main(["sync"])
        gid = "notion:" + self.other["id"]
        self.reviewed_pr(gid, "staging")
        self.post("/api/deploy", {"mode": "direct", "ticket": gid})
        source = {"state": "OPEN", "baseRefName": "staging", "headRefOid": "head"}
        prod = {"state": "OPEN", "baseRefName": "main", "number": 42, "isDraft": False, "statusCheckRollup": []}
        views = [source, {**prod, "mergeable": "UNKNOWN"}, {**prod, "mergeable": "MERGEABLE"}]
        with mock.patch.object(deploy, "gh_json", side_effect=views), mock.patch.object(deploy, "POLL_S", -1), \
                mock.patch.object(deploy, "_release_branch", return_value=("release/test", None, None)), \
                mock.patch.object(deploy, "_open_pr", return_value="https://github.com/example/app/pull/42"), \
                mock.patch.object(deploy, "_gh") as merge:
            deploy.deploy_tick()
            self.assertEqual(self.con.execute("SELECT status FROM releases").fetchone()[0], "running")
            self.other["properties"]["출시"] = relation(self.parent["id"])
            cli.main(["sync"])
            deploy.deploy_tick()
        merge.assert_not_called()
        self.assert_last_deploy_failed("마일스톤 PR")

    def test_milestone_pr_goes_through_staging_before_promotion(self):
        cli.main(["sync"])
        gid = milestone.ms_gid(self.parent_gid, "app")
        with mock.patch.object(milestone, "_gh", return_value=mock.Mock(returncode=0)):
            base = milestone.ensure(self.con, self.ticket(self.gid), "app")
        self.merged_pr(self.gid, "app", base)
        pipeline.restage(self.con)
        self.con.commit()
        self.post("/api/milestone-done", {"parent": self.parent_gid})
        url = self.reviewed_pr(gid, "staging")
        self.assert_direct_blocked(gid, url, "staging")
        self.assertIsNone(release.approval_target(self.con, self.ticket(gid))[1])
        self.con.execute("UPDATE prs SET state='MERGED',merged_at=? WHERE url=?", (core.now(), url))
        self.con.commit()
        self.post("/api/deploy", {"mode": "promote", "ticket": gid, "pr_url": url})

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
