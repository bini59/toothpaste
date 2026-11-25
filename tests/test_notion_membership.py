"""Shared task DB membership through the dashboard, CLI and sync against a paginated Notion fixture."""
import io
import json
from unittest import mock

from test_notion import DOCS_DB, HOME_DB, MEMBER, OWNER, TEAM_DB, NotionDashboard, page, uid
from ticket_pilot import cli, core, notion, notion_tickets, pipeline


def relation(*ids):
    return {"type": "relation", "relation": [{"id": i} for i in ids]}


class NotionMembershipTest(NotionDashboard):
    def setUp(self):
        super().setUp()
        notion.save_app("app-id", "app-secret")
        self.cfg = {"connection_id": self.connect("team"), "data_source_id": TEAM_DB, "people_property": "people-id",
                    "tag_property": "tag-id", "tag_option": "tp-id", "status_property": "status-id",
                    "status_option": "todo-id"}
        for name, prop, kind, spec in (("상위 항목", "parent", "relation", {"data_source_id": TEAM_DB}),
                                       ("프로젝트", "proj%3Ax", "relation", {"data_source_id": DOCS_DB}),
                                       ("업무 코드", "code", "rich_text", {}), ("수식", "calc", "formula", {}),
                                       ("티켓 번호", "number", "unique_id", {})):
            self.fixture.props[name] = {"id": prop, "type": kind, kind: spec}
        self.external = {"project_data_source_id": DOCS_DB, "project_page_id": uid(301), "project_property": "proj%3Ax"}
        self.text_rule = {"contains_property": "code", "contains_text": "TP-"}
        self.parent = self.fixture.pages[TEAM_DB][0]
        self.parent["properties"].update({
            "제목": {"type": "title", "title": [{"plain_text": "TP-17 로그인 오류"}]},
            "업무 코드": {"type": "rich_text", "rich_text": [{"plain_text": "모바일 / T"}, {"plain_text": "P-17"}]},
        })

    def create(self, *args):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main(["notion", "create", "--project", "toothpaste", *args])
            return json.loads(out.getvalue())

    def assert_imports(self, cfg, titles):
        before = set(t["title"] for t in self.preview(cfg, pid="toothpaste"))
        self.assertEqual(before, set(titles))
        self.save(cfg, "toothpaste")
        self.assertNotIn("notion_errors", pipeline.sync(log=lambda *_: None))
        rows = self.con.execute("SELECT name FROM tickets WHERE assigned=1 AND project_gid='notion:toothpaste'")
        self.assertEqual({r["name"] for r in rows}, before)

    def test_subtask_inherits_code_and_keeps_its_own_import_filters(self):
        cfg = {**self.cfg, **self.text_rule, "parent_property": "parent"}
        self.save(cfg, "toothpaste")
        result = self.create("--parent", notion_tickets.ticket_key(uid(101)), "--title", "재현 테스트",
                             "--assignee", "me", "--tag", "tp")
        child = self.fixture.pages[TEAM_DB][-1]
        self.assertEqual(result, {"key": notion_tickets.ticket_key(child["id"]), "url": child["url"]})
        payload = self.fixture.writes[-1][2]
        self.assertEqual(payload["parent"], {"type": "data_source_id", "data_source_id": TEAM_DB})
        self.assertEqual(payload["properties"]["parent"], {"relation": [{"id": uid(101)}]})
        self.assertNotIn("proj%3Ax", payload["properties"])
        for n, title, who, tags, status in [(901, "다른 담당자", MEMBER, ["tp"], "할 일"),
                                            (902, "다른 태그", OWNER, ["문서"], "할 일"),
                                            (903, "다른 상태", OWNER, ["tp"], "완료"),
                                            (904, "재현 테스트의 하위 작업", OWNER, ["tp"], "할 일")]:
            row = page(n, title, [who], tags, status)
            row["properties"]["상위 항목"] = relation(child["id"])
            self.fixture.pages[TEAM_DB].append(row)
        children = ["재현 테스트", "재현 테스트의 하위 작업"]
        self.assert_imports(cfg, ["TP-17 로그인 오류", *children])
        self.parent["properties"]["개발 담당"]["people"] = [{"id": MEMBER}]
        self.assert_imports(cfg, children)

    def test_literal_contains_on_each_readable_property(self):
        props = self.parent["properties"]
        props["티켓 번호"] = {"type": "unique_id", "unique_id": {"prefix": "TP", "number": 17}}
        props["수식"] = {"type": "formula", "formula": {"type": "string", "string": "팀 / toothpaste"}}
        other = self.fixture.pages[TEAM_DB][3]["properties"]
        other["수식"] = {"type": "formula", "formula": {"type": "number", "number": 17}}
        other["티켓 번호"] = {"type": "unique_id", "unique_id": {"prefix": None, "number": 0}}
        for prop, needle, titles in (("code", "TP-", ["TP-17 로그인 오류"]), ("title", "로그인", ["TP-17 로그인 오류"]),
                                     ("calc", "toothpaste", ["TP-17 로그인 오류"]), ("calc", "17", []),
                                     ("number", "TP-17", ["TP-17 로그인 오류"]), ("number", "0", ["팀 보드"])):
            with self.subTest(prop=prop, text=needle):
                self.assert_imports({**self.cfg, "contains_property": prop, "contains_text": needle}, titles)

    def test_project_picker_and_create_link_the_stored_row_after_rename(self):
        cfg = {**self.cfg, **self.external, "parent_property": "parent"}
        self.fixture.pages[DOCS_DB][0]["properties"]["제목"]["title"] = [{"plain_text": "새 프로젝트명"}]
        self.fixture.pages[DOCS_DB][1]["in_trash"] = True
        self.assertEqual(self.post("rows", {**cfg, "data_source_id": DOCS_DB}), [{"id": uid(301), "name": "새 프로젝트명"}])
        self.assertEqual(len(self.post("rows", cfg)), len(self.fixture.pages[TEAM_DB]))
        self.assertEqual(self.call("rows", {**cfg, "data_source_id": HOME_DB})[0], 400)
        self.assertEqual(self.call("rows", {**cfg, "data_source_id": "wrong"})[0], 400)
        self.fixture.props["소속"] = self.fixture.props.pop("프로젝트")
        self.save(cfg, "toothpaste")
        self.assertEqual(core.project_map()["projects"][0]["notion"], cfg)
        parent = self.create("--title", "일반 작업", "--assignee", "me", "--tag", "tp")
        self.create("--title", "하위 작업", "--parent", parent["key"], "--assignee", "me", "--tag", "tp")
        for row in self.fixture.pages[TEAM_DB][-2:]:
            self.assertEqual(row["properties"]["소속"], relation(uid(301)))
        self.assert_imports(cfg, ["일반 작업", "하위 작업"])

    def test_direct_then_ancestor_then_text_across_filtered_projects(self):
        cfg = {**self.cfg, **self.text_rule, "parent_property": "parent"}
        self.save(cfg, "toothpaste")  # the earlier project matches the child's string
        other = {**self.cfg, **self.external}  # only the first project defines the task DB's parent column
        self.save(other, "other")
        child = self.fixture.pages[TEAM_DB][3]
        child["properties"]["업무 코드"] = self.parent["properties"]["업무 코드"]
        child["properties"]["상위 항목"] = relation(uid(101))
        self.parent["properties"]["프로젝트"] = relation(uid(301))
        self.assert_imports(cfg, [])
        self.assertEqual({t["title"] for t in self.preview(other, pid="other")}, {"TP-17 로그인 오류", "팀 보드"})
        self.assertEqual(set(self.stages()), {"TP-17 로그인 오류", "팀 보드"})
        # Even if the explicit owner's assignee filter excludes these tickets, its peer cannot take them by text.
        self.save({**other, "assignee": MEMBER, "status_option": "done-id"}, "other")
        self.assert_imports(cfg, [])
        self.assertEqual(self.stages(), {})
        # A direct link on the child overrides the conflicting project on its parent.
        cfg.update(self.external, project_page_id=uid(302))
        child["properties"]["프로젝트"] = relation(uid(302))
        self.assert_imports(cfg, ["팀 보드"])

    def test_parent_edits_hold_existing_and_newly_discovered_descendants(self):
        cfg = {**self.cfg, "contains_property": "title", "contains_text": "TP-", "parent_property": "parent"}
        child = self.fixture.pages[TEAM_DB][3]
        self.save(cfg, "toothpaste")
        pipeline.sync(log=lambda *_: None)
        child["properties"]["상위 항목"] = relation(uid(101))
        key = notion_tickets.ticket_key(uid(101))

        def rename(title):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                cli.main(["notion", "update", key, "--title", title])

        self.fixture.after_listing = lambda: rename("로그인 오류")
        pipeline.sync(log=lambda *_: None)
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        rename("TP-17 로그인 오류")
        self.assert_imports(cfg, ["TP-17 로그인 오류", "팀 보드"])
        rename("로그인 오류")
        self.assertFalse(any("would create" in s for s in self.dispatch_log()))
        self.assert_imports(cfg, [])

    def test_stale_peer_text_does_not_block_direct_owner(self):
        other = {**self.cfg, **self.text_rule}
        cfg = {**self.cfg, **self.external}
        self.save(other, "other")
        self.save(cfg, "toothpaste")
        pipeline.sync(log=lambda *_: None)
        self.assertEqual(pipeline.find_ticket(self.con, notion_tickets.ticket_key(uid(101)))["project_gid"], "notion:other")
        self.parent["properties"]["프로젝트"] = relation(uid(301))
        del self.fixture.props["업무 코드"]
        tickets = self.preview(cfg, pid="toothpaste")
        self.assertEqual([(r["title"], r["stage"]) for r in tickets], [("TP-17 로그인 오류", "queued")])
        errors = pipeline.sync(log=lambda *_: None)["notion_errors"]
        self.assertEqual(set(errors), {"other"})
        self.assertEqual(set(self.stages()), {"TP-17 로그인 오류"})
        self.assertEqual(pipeline.find_ticket(self.con, notion_tickets.ticket_key(uid(101)))["project_gid"], "notion:toothpaste")

    def test_same_source_project_row_and_parent_cycles(self):
        cfg = {**self.cfg, "project_data_source_id": TEAM_DB, "project_page_id": uid(103), "parent_property": "parent"}
        self.parent["properties"]["상위 항목"] = relation(uid(103))  # the project row is outside the filter
        child = self.fixture.pages[TEAM_DB][3]
        child["properties"]["상위 항목"] = relation(uid(101))
        self.assert_imports(cfg, ["TP-17 로그인 오류", "팀 보드"])
        self.parent["properties"]["상위 항목"] = relation(uid(104))
        self.assert_imports(cfg, [])
        self.assert_imports({**cfg, **self.text_rule}, ["TP-17 로그인 오류", "팀 보드"])

    def test_paginated_relations_and_invalid_settings(self):
        cfg = {**self.cfg, **self.external}
        self.parent["properties"]["프로젝트"] = {**relation(uid(302)), "has_more": True}
        self.fixture.relation_items[f"/pages/{uid(101)}/properties/proj%3Ax"] = [uid(302), uid(301)]
        self.assert_imports(cfg, ["TP-17 로그인 오류"])
        for invalid in ({"contains_property": "code"}, {"contains_text": "abc"}, {"project_page_id": uid(301)},
                        {**self.external, "parent_property": "proj%3Ax"}):
            project = {"name": "x", "source": "notion", "repos": [], "notion": {**self.cfg, **invalid}}
            self.assertEqual(self.call("/api/config", {"scope": "project.invalid", "value": project})[0], 400)
        self.fixture.props["프로젝트"]["relation"]["data_source_id"] = HOME_DB
        self.assertEqual(self.call("preview", {"project_id": "toothpaste", "repos": [], "notion": cfg})[0], 400)
        self.assertIn("toothpaste", pipeline.sync(log=lambda *_: None)["notion_errors"])

    def test_reject_bad_parent_and_moved_project_row_before_creating(self):
        self.save(self.cfg, "toothpaste")
        with self.assertRaisesRegex(SystemExit, "상위 항목"):
            self.create("--title", "하위", "--parent", notion_tickets.ticket_key(uid(101)))
        self.save({**self.cfg, **self.external, "parent_property": "parent"}, "toothpaste")
        for key, error in (("NT-invalid", "찾을 수 없어요"), (notion_tickets.ticket_key(uid(301)), "데이터베이스에 없어요"),
                           (notion_tickets.ticket_key(uid(999)), "404")):
            with self.assertRaisesRegex(SystemExit, error):
                self.create("--title", "하위", "--parent", key)
        self.fixture.pages[TEAM_DB].append(self.fixture.pages[DOCS_DB].pop(0))
        with self.assertRaisesRegex(SystemExit, "데이터베이스에 없어요"):
            self.create("--title", "일반")
        self.assertEqual(self.fixture.writes, [])

    def test_chat_title_lookup_includes_unimported_parents_and_disambiguates(self):
        cfg = {**self.cfg, **self.text_rule, "parent_property": "parent",
               "project_data_source_id": DOCS_DB, "project_page_id": uid(301)}
        self.parent["properties"]["개발 담당"]["people"] = [{"id": MEMBER}]
        self.assert_imports(cfg, [])
        self.create("--parent", "로그인 오류", "--title", "재현 테스트", "--assignee", "me", "--tag", "tp")
        self.assert_imports(cfg, ["재현 테스트"])
        self.fixture.pages[TEAM_DB].append(page(700, "TP-18 로그인 오류", [], [], "할 일"))
        with self.assertRaisesRegex(SystemExit, "부모 작업 후보가 여러 개.*TP-17.*NT-.*TP-18.*NT-"):
            self.create("--parent", "로그인 오류", "--title", "중복")
        self.assertEqual(len(self.fixture.writes), 1)
