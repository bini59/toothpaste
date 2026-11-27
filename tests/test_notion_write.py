"""The CLI and host use the same project account and page ID against a local Notion HTTP server."""
import io
import json
import os
import threading
from contextlib import contextmanager
from unittest import mock

from test_notion import DOCS_DB, MEMBER, OWNER, TEAM_DB, NotionDashboard, uid
from ticket_pilot import ticket_sync
from ticket_pilot import dispatch
from ticket_pilot import chat, cli, core, migrate, notion, notion_stages, notion_tickets, pipeline, precheck, release, track


class NotionWrites(NotionDashboard):
    def setUp(self):
        super().setUp()
        notion.save_app("app-id", "app-secret")
        self.cfg = {"connection_id": self.connect("team"), "data_source_id": TEAM_DB, "people_property": "people-id",
                    "tag_property": "tag-id", "tag_option": "tp-id", "status_property": "status-id",
                    "status_option": "todo-id"}
        self.save(self.cfg)
        self.page = self.fixture.pages[TEAM_DB][0]
        self.page_id = self.page["id"]
        self.gid = "notion:" + self.page_id
        self.key = notion_tickets.ticket_key(self.page_id)
        self.path = "/pages/" + self.page_id

    def cmd(self, *args):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main(["notion", *args])
            return json.loads(out.getvalue())

    def test_create_and_edit_before_import(self):
        result = self.cmd("create", "--project", "example", "--title", "로그인 오류", "--body", "# 재현",
                          "--tag", "버그", "--assignee", "me", "--due", "2027-01-15")
        row = self.fixture.pages[TEAM_DB][-1]
        key = result["key"]
        self.assertEqual(result, {"key": notion_tickets.ticket_key(row["id"]), "url": row["url"]})
        self.assertEqual(self.fixture.markdown[row["id"]][1]["markdown"], "# 재현")
        self.assertEqual(row["properties"]["개발 담당"]["people"], [{"id": OWNER}])
        self.assertEqual(row["properties"]["기한"]["date"], {"start": "2027-01-15"})
        self.assertEqual(self.con.execute("SELECT assigned FROM tickets WHERE key=?", (key,)).fetchone()[0], 0)

        self.cmd("update", key, "--title", "모바일 로그인", "--body", "새 본문", "--assignee", "서준", "--due", "none")
        self.assertEqual(row["properties"]["제목"]["title"][0]["plain_text"], "모바일 로그인")
        self.assertEqual(self.fixture.markdown[row["id"]][1]["markdown"], "새 본문")
        self.assertEqual(row["properties"]["개발 담당"]["people"], [{"id": MEMBER}])
        self.assertIsNone(row["properties"]["기한"]["date"])
        self.cmd("tag", key, "긴급")
        self.cmd("tag", key, "긴급")
        self.assertEqual([v["name"] for v in row["properties"]["라벨"]["multi_select"]], ["버그", "긴급"])
        self.cmd("tag", key, "긴급", "--remove")
        self.cmd("tag", key, "긴급", "--remove")
        self.assertEqual([v["name"] for v in row["properties"]["라벨"]["multi_select"]], ["버그"])
        self.assertEqual([o["name"] for o in self.cmd("status", key)["options"]], ["할 일", "진행 중", "완료"])
        self.cmd("status", key, "진행 중")
        self.assertEqual(self.fixture.writes[-1][2], {"properties": {"status-id": {"status": {"id": "doing-id"}}}})
        self.assertEqual(row["properties"]["개발 담당"]["people"], [{"id": MEMBER}])
        self.cmd("comment", key, "수정 PR https://github.com/example/app/pull/42")
        comment = self.fixture.writes[-1]
        self.assertEqual(comment[:2], ("POST", "/comments"))
        self.assertEqual(comment[2]["parent"]["page_id"], row["id"])
        self.assertIn("/pull/42", comment[2]["rich_text"][0]["text"]["content"])

    def test_select_tag_and_status(self):
        for name, kind in (("라벨", "multi_select"), ("상태", "status")):
            prop = self.fixture.props[name]
            prop["type"], prop["select"] = "select", prop.pop(kind)
            for page in self.fixture.pages[TEAM_DB]:
                old = page["properties"][name][kind]
                page["properties"][name] = {"type": "select", "select": old[0] if kind == "multi_select" else old}
        pipeline.sync()
        self.cmd("tag", self.key, "버그", "--remove")
        self.assertEqual(self.fixture.writes, [])
        self.cmd("tag", self.key, "버그")
        self.assertEqual(self.page["properties"]["라벨"]["select"]["name"], "버그")
        self.cmd("tag", self.key, "버그", "--remove")
        self.assertIsNone(self.page["properties"]["라벨"]["select"])
        self.cmd("status", self.key, "doing-id")
        self.assertEqual(self.page["properties"]["상태"]["select"]["name"], "진행 중")
        with self.assertRaisesRegex(SystemExit, "현재 선택지"):
            self.cmd("status", self.key, "없는 값")

    def test_reject_wrong_project_or_moved_page(self):
        pipeline.sync()
        self.save({**self.cfg, "data_source_id": DOCS_DB}, "docs")
        with self.assertRaisesRegex(SystemExit, "데이터베이스에 없어요"):
            self.cmd("comment", self.key, "메모", "--project", "docs")
        self.fixture.pages[DOCS_DB].append(self.fixture.pages[TEAM_DB].pop(0))
        with self.assertRaisesRegex(SystemExit, "데이터베이스에 없어요"):
            self.cmd("status", self.key, "완료")
        self.assertEqual(self.fixture.writes, [])

    def test_shared_db_creator_can_edit_after_another_project_imports_it(self):
        self.save(self.cfg, "second")
        created = self.cmd("create", "--project", "second", "--title", "새 티켓", "--assignee", "me", "--tag", "tp")
        pipeline.sync()
        self.assertEqual(pipeline.find_ticket(self.con, created["key"])["project_gid"], "notion:example")
        self.cmd("comment", "--project", "second", created["key"], "후속 메모")
        self.assertEqual(self.fixture.writes[-1][2]["parent"]["page_id"], self.fixture.pages[TEAM_DB][-1]["id"])

    def test_edit_during_listing_holds_only_the_edited_candidate(self):
        pipeline.sync()
        self.fixture.after_listing = lambda: self.cmd("status", self.key, "완료")
        pipeline.sync()
        starts = [s for s in self.dispatch_log() if "would create" in s]
        self.assertEqual(len(starts), 1)
        self.assertIn("팀 보드", starts[0])
        pipeline.sync()
        self.assertEqual(pipeline.find_ticket(self.con, self.key)["stage"], "closed")
        self.cmd("status", self.key, "할 일")
        pipeline.sync()
        self.assertEqual(len([s for s in self.dispatch_log() if "would create" in s]), 2)

    def test_lost_patch_and_idempotent_retry_during_listing(self):
        pipeline.sync()
        def interrupted_patch():
            self.fixture.truncate_path, self.fixture.truncate_method = self.path, "PATCH"
            with self.assertRaisesRegex(SystemExit, "변경 내용을 확인"):
                self.cmd("status", self.key, "완료")
            self.fixture.truncate_path = None
        self.fixture.after_listing = interrupted_patch
        pipeline.sync()
        self.assertFalse(any(self.key in s for s in self.dispatch_log() if "would create" in s))
        self.cmd("status", self.key, "완료")
        self.assertEqual(pipeline.find_ticket(self.con, self.key)["section"], "완료")
        self.assertEqual(len(self.fixture.writes), 1)
        self.assertFalse(any(self.key in s for s in self.dispatch_log() if "would create" in s))

    def test_permissions_and_partial_body_failure(self):
        self.fixture.deny_writes = {("POST", "/pages"), ("PATCH", self.path), ("POST", "/comments"), ("GET", "/users")}
        self.assertNotIn("notion_errors", pipeline.sync())
        for args, capability in ((["create", "--project", "example", "--title", "새 티켓"], "콘텐츠 추가"),
                                 (["status", self.key, "진행 중"], "콘텐츠 수정"), (["comment", self.key, "메모"], "댓글 추가"),
                                 (["update", self.key, "--assignee", "서준"], "사용자 정보 읽기")):
            with self.subTest(capability=capability), self.assertRaisesRegex(SystemExit, capability + ".*전역 설정.*다시 승인"):
                self.cmd(*args)
        self.fixture.deny_writes = {("PATCH", self.path + "/markdown")}
        with self.assertRaisesRegex(SystemExit, "속성은 저장.*본문.*콘텐츠 수정"):
            self.cmd("update", self.key, "--title", "수정한 제목", "--body", "다시 쓸 본문")
        self.assertEqual(self.page["properties"]["제목"]["title"][0]["plain_text"], "수정한 제목")

    def test_interrupted_post_and_local_failure_after_create(self):
        pipeline.sync()
        for path, args in (("/pages", ["create", "--project", "example", "--title", "오류"]),
                           ("/comments", ["comment", self.key, "메모"])):
            self.fixture.truncate_path = path
            with self.assertRaisesRegex(SystemExit, "다시 요청하기 전에.*확인"):
                self.cmd(*args)
        self.assertEqual([p for _, p, _ in self.fixture.writes], ["/pages", "/comments"])
        self.fixture.truncate_path = None
        self.con.execute("""CREATE TRIGGER reject_ticket BEFORE INSERT ON tickets
                            BEGIN SELECT RAISE(FAIL,'fixture'); END""")
        with self.assertRaisesRegex(SystemExit, r"NT-\w+ https://www.notion.so/.*로컬 DB"):
            self.cmd("create", "--project", "example", "--title", "로컬 기록 실패")

    @contextmanager
    def fake_agent(self):
        (self.root / "prompts").symlink_to(core.ROOT / "prompts", target_is_directory=True)
        with mock.patch.object(dispatch, "ROOT", self.root), mock.patch.object(dispatch, "github_token", return_value="fixture"), \
                mock.patch.object(pipeline.R, "worktrees", return_value=[]), \
                mock.patch.object(pipeline.R, "local_branches", return_value=[]), \
                mock.patch.object(pipeline.R, "create", return_value={
                    "path": str(self.root / "worktree"), "branch": "tp/example"}), \
                mock.patch.object(pipeline.R, "mark_checkout", return_value="fixture"), \
                mock.patch.object(pipeline, "prepare_worktree", return_value=(None, None)), \
                mock.patch.object(pipeline.R, "launch", return_value="fixture") as launch:
            yield launch

    def start_ticket(self):
        pipeline.sync()
        with self.fake_agent() as launch:
            self.assertEqual(pipeline.dispatch(only=self.key), [self.key])
            self.assertIn("ticket create --project example", launch.call_args.args[1])
        return pipeline.latest_run(self.con, self.gid)["id"]

    def merged_pr(self, run_id):
        url = "https://github.com/example/app/pull/42"
        self.con.execute("""INSERT INTO prs(url,number,run_id,ticket_gid,repo,github,state,base,merge_sha,head_sha)
                            VALUES(?,42,?,?,'app','example/app','MERGED','main','deployed','head')""",
                         (url, run_id, self.gid))
        pipeline.restage(self.con)
        self.con.commit()
        return url

    def test_dispatch_filtered_sync_and_verified_release(self):
        self.save({**self.cfg, "stage_statuses": {"in_progress": "doing-id", "done": "done-id"}})
        self.save({**self.cfg, "status_option": "doing-id"}, "second")
        run_id = self.start_ticket()
        self.assertEqual(self.fixture.writes, [])
        notion_stages.flush(self.con)
        self.assertEqual(self.page["properties"]["상태"]["status"]["name"], "진행 중")
        for _ in range(2):
            self.assertEqual(pipeline.sync()["new_tagged"], [])
            self.assertEqual(pipeline.dispatch(only=self.key, dry_run=True), [])
        row = pipeline.find_ticket(self.con, self.key)
        self.assertEqual((row["project_gid"], row["stage"], row["assigned"]), ("notion:example", "in_progress", 0))
        self.assertEqual(row["section"], "진행 중")
        self.cmd("update", self.key, "--title", "채팅에서 바꾼 제목", "--due", "2027-02-01")
        pipeline.sync()
        row = pipeline.find_ticket(self.con, self.key)
        self.assertEqual((row["name"], row["due_on"], row["assigned"]), ("채팅에서 바꾼 제목", "2027-02-01", 0))
        url = self.merged_pr(run_id)
        self.assertEqual(self.page["properties"]["상태"]["status"]["name"], "진행 중")
        rid = self.con.execute("INSERT INTO releases(repo,status,prs_json) VALUES('app','deploying',?)",
                               (json.dumps([{"key": self.key, "url": url, "ticket_gid": self.gid}]),)).lastrowid
        rel = self.con.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()
        release.finalize(self.con, rel)
        self.assertEqual(pipeline.find_ticket(self.con, self.key)["stage"], "done")
        self.assertEqual(self.page["properties"]["상태"]["status"]["name"], "진행 중")
        notion_stages.flush(self.con)
        self.assertEqual(self.page["properties"]["상태"]["status"]["name"], "완료")
        pipeline.sync()
        self.assertEqual(pipeline.find_ticket(self.con, self.key)["stage"], "done")
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertEqual([(p, b["properties"]["status-id"]["status"]["id"]) for _, p, b in self.fixture.writes
                          if "status-id" in b["properties"]],
                         [(self.path, "doing-id"), (self.path, "done-id")])

    def test_verified_external_merge_retries_failed_write(self):
        self.save({**self.cfg, "stage_statuses": {"done": "done-id"}})
        run_id = self.start_ticket()
        self.assertEqual(self.fixture.writes, [])
        self.merged_pr(run_id)
        self.fixture.deny_writes = {("PATCH", self.path)}
        with mock.patch.object(track, "track_config", return_value={"app": {"release": True}}), \
                mock.patch.object(track, "deployed_sha", return_value=("deployed", "fixture")):
            self.assertEqual(track.track_deploys(), 1)
        self.assertEqual(pipeline.find_ticket(self.con, self.key)["completed"], 1)
        notion_stages.flush(self.con)
        pending = self.con.execute("SELECT last_error, next_attempt_at FROM notion_status_queue").fetchone()
        self.assertIn("콘텐츠 수정", pending["last_error"])
        # Repeated identical errors still postpone delivery and only produce one failure event.
        with mock.patch.object(notion_stages, "time", return_value=pending["next_attempt_at"]):
            notion_stages.flush(self.con)
        due = self.con.execute("SELECT next_attempt_at FROM notion_status_queue").fetchone()[0]
        self.assertEqual(due, pending["next_attempt_at"] + notion_stages.RETRY_SECONDS)
        errors = self.con.execute("SELECT count(*) FROM events WHERE message LIKE 'Notion 상태 반영 대기%'").fetchone()[0]
        self.assertEqual(errors, 1)
        self.fixture.deny_writes.clear()
        pipeline.sync()
        with mock.patch.object(notion_stages, "time", return_value=due - 1) as clock:
            with mock.patch("subprocess.Popen") as spawn:
                notion_stages.start_worker(self.con)
                spawn.assert_not_called()
            notion_stages.flush(self.con)
            self.assertEqual(self.fixture.writes, [])
            clock.return_value = due
            notion_stages.flush(self.con)
        self.assertEqual(self.page["properties"]["상태"]["status"]["name"], "완료")
        self.assertEqual(len(self.fixture.writes), 1)

    def test_dropped_run_remains_skipped_after_leaving_filter(self):
        self.save({**self.cfg, "stage_statuses": {"in_progress": "doing-id"}})
        self.start_ticket()
        notion_stages.flush(self.con)
        pipeline.sync()
        self.assertFalse(pipeline.find_ticket(self.con, self.key)["assigned"])
        pipeline.drop(self.key)
        pipeline.sync()
        self.assertEqual(pipeline.find_ticket(self.con, self.key)["stage"], "skipped")

    def test_verified_stage_supersedes_a_backed_off_start(self):
        self.save({**self.cfg, "stage_statuses": {"in_progress": "doing-id", "done": "done-id"}})
        run_id = self.start_ticket()
        self.fixture.deny_writes = {("PATCH", self.path)}
        notion_stages.flush(self.con)
        self.fixture.deny_writes.clear()
        pending = self.con.execute("SELECT last_error FROM notion_status_queue").fetchone()
        self.assertIn("콘텐츠 수정", pending["last_error"])
        url = self.merged_pr(run_id)
        release.complete_asana(self.con, self.gid, pr_url=url)
        pipeline.restage(self.con)
        self.con.commit()
        notion_stages.flush(self.con)
        expected = {"properties": {"status-id": {"status": {"id": "done-id"}}}}
        self.assertEqual(self.fixture.writes, [("PATCH", self.path, expected)])
        self.assertEqual(self.con.execute("SELECT count(*) FROM notion_status_queue").fetchone()[0], 0)

    def test_stage_disabled_while_retry_is_reading_the_schema(self):
        self.save({**self.cfg, "stage_statuses": {"in_progress": "doing-id"}})
        self.fixture.deny_writes = {("PATCH", self.path)}
        self.start_ticket()
        notion_stages.flush(self.con)
        retry_at = self.con.execute("SELECT next_attempt_at FROM notion_status_queue").fetchone()[0]
        self.fixture.deny_writes.clear()
        read_schema = notion.Client.properties
        def disable(client, source):
            schema = read_schema(client, source)
            self.save(self.cfg)
            return schema
        with mock.patch.object(notion_stages, "time", return_value=retry_at), \
                mock.patch.object(notion.Client, "properties", disable):
            notion_stages.flush(self.con)
            notion_stages.flush(self.con)
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.con.execute("SELECT count(*) FROM notion_status_queue").fetchone()[0], 0)

    def test_stalled_status_worker_allows_github_dispatch(self):
        self.save({**self.cfg, "status_option": "done-id", "stage_statuses": {"in_progress": "doing-id"}})
        backlog = notion_stages.BATCH_SIZE + 2
        for n in range(backlog):
            notion_stages.enqueue(self.con, {"gid": "notion:" + uid(600 + n), "project_gid": "notion:example"}, "in_progress")
        self.con.commit()

        reading, resume = threading.Event(), threading.Event()
        reads = []
        respond = self.fixture.respond
        def slow_page(method, path, headers, body):
            if method == "GET" and path.startswith("/pages/"):
                reads.append(path)
                reading.set()
                if not resume.wait(10):
                    resume.set()  # bound the test if a host call waits for this request
                return 504, {}
            return respond(method, path, headers, body)

        # Route the actual worker process to the fixture server and its temporary credentials on both host platforms.
        (self.root / "sitecustomize.py").write_text(
            f"from ticket_pilot import core, notion\ncore.KEYCHAIN = False\nnotion.API = {notion.API!r}\n")
        env = {"PYTHONPATH": os.pathsep.join(map(str, (self.root, core.ROOT))), "TP_SECRETS_FILE": str(core.SECRETS_FILE)}
        issue = {"number": 42, "title": "다른 티켓", "url": "https://github.com/example/app/issues/42"}
        with mock.patch.object(self.fixture, "respond", slow_page), mock.patch.dict(os.environ, env):
            worker = notion_stages.start_worker(self.con)
            try:
                self.assertTrue(reading.wait(10))
                self.assertIsNone(notion_stages.start_worker(self.con))
                self.assertIn("Notion 상태 반영", migrate.busy(self.con))
                with self.fake_agent() as launch, mock.patch.object(precheck, "_release_steps"), \
                        mock.patch.object(ticket_sync, "issue_repos", return_value={"app": ("GH", None)}), \
                        mock.patch.object(ticket_sync, "_open_issues", return_value=[issue]), \
                        mock.patch.object(dispatch, "gh_json", return_value={"comments": []}), \
                        self.assertRaises(SystemExit) as exit_status:
                    cli.main(["precheck-dispatch"])
                self.assertEqual(exit_status.exception.code, 1)  # deterministic dispatch finished, no AI triage
                launch.assert_called_once()
                self.assertEqual(pipeline.find_ticket(self.con, "GH-42")["stage"], "in_progress")
                self.assertFalse(resume.is_set())
                self.assertEqual(len(reads), 1)
            finally:
                resume.set()
                worker.wait(timeout=10)
            self.assertEqual(worker.returncode, 0)
            self.assertFalse(notion_stages.worker_running())
            self.assertEqual(len(reads), notion_stages.BATCH_SIZE)
            notion_stages.flush(self.con)  # only the untried pages are due
            self.assertEqual(len(set(reads)), backlog)
            self.assertEqual(len(reads), backlog)

    def test_read_only_and_project_chat_instructions(self):
        run_id = self.start_ticket()
        self.merged_pr(run_id)
        release.complete_asana(self.con, self.gid)
        self.con.commit()
        pipeline.sync()
        self.assertEqual(self.fixture.writes, [])
        prompt = chat.prompt(chat._project("example"), "버그 티켓을 만들어 줘")
        self.assertIn("ticket create --project example", prompt)
        self.assertIn("notion comment --project example", prompt)
