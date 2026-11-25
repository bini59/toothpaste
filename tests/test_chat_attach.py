"""TP-72: chat attachments (validation, storage, agent delivery, file lookup, delete). Run: python3 -m unittest discover tests"""
import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from ticket_pilot import chat as CH, core, runner


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        other = Path(self.tmp.name) / "r2"
        other.mkdir()
        self.pm = {"repos": {"r": {"github": "o/r", "path": self.tmp.name, "agent": {"tool": "claude"}},
                             "r2": {"github": "o/r2", "path": str(other)}, "solo": {"path": "/nonexistent"},
                             "solo2": {"path": self.tmp.name}},
                   "projects": [{"id": "p", "name": "P", "source": "none",
                                 "repos": [{"repo": "r2", "role": "be"}, {"repo": "r", "role": "fe", "default": True}]},
                                {"id": "q", "name": "Q", "source": "github", "repos": [{"repo": "r2", "role": "be"}]}]}
        self.launch = mock.MagicMock(return_value="1 /tmp/tp-test-chat.log")
        for p in (mock.patch.object(CH, "db", lambda: self.con),
                  mock.patch.object(CH, "UPLOADS", Path(self.tmp.name) / "uploads"),
                  mock.patch.object(CH, "project_map", lambda: self.pm),
                  mock.patch.object(CH.R, "resolve", lambda a: {"tool": "claude"}),
                  mock.patch.object(CH.R, "describe", lambda a: "claude"),
                  mock.patch.object(CH.R, "launch", self.launch),
                  mock.patch.object(CH.R, "stop"),
                  mock.patch.object(CH.R, "agent_state", lambda h: (True, None))):
            p.start()
            self.addCleanup(p.stop)
        self.id = "c" + "0" * 16
        self.con.execute("INSERT INTO chat_conversations(id,repo,title) VALUES(?,?,?)", (self.id, "r", "새 대화"))
        self.con.commit()

    def test_projects_are_projects_with_repos(self):
        ps = {p["id"]: p for p in CH.projects()}
        self.assertEqual(list(ps), ["p", "q", core.UNSORTED])
        self.assertEqual(([r["name"] for r in ps["p"]["repos"]], ps["p"]["repo"]), (["r", "r2"], "r"))  # default first
        self.assertEqual([r["name"] for r in ps["q"]["repos"]], ["r2"])  # r2 is in p and q
        self.assertEqual(([r["name"] for r in ps[core.UNSORTED]["repos"]], ps[core.UNSORTED]["name"]), (["solo", "solo2"], "미분류"))
        # the pre-TP-91 conversation (project_id NULL) shows under the first project with its repo
        self.assertEqual((ps["p"]["conversations"], ps["q"]["conversations"]), (1, 0))
        self.assertEqual([c["id"] for c in CH.conversations("p")], [self.id])
        cid = CH.create("q")
        self.assertEqual(dict(self.con.execute("SELECT project_id, repo FROM chat_conversations WHERE id=?", (cid,)).fetchone()),
                         {"project_id": "q", "repo": "r2"})
        self.assertEqual([c["id"] for c in CH.conversations("q")], [cid])
        self.pm["projects"].pop(1)  # q deleted: its conversation goes to the first project with r2
        self.assertEqual({c["id"] for c in CH.conversations("p")}, {self.id, cid})

    def test_send_runs_in_the_default_repo_with_the_others_added(self):
        CH.send(self.id, "hi")
        kw = self.launch.call_args.kwargs
        self.assertEqual((self.launch.call_args.args[0], kw["add_dirs"]), (self.tmp.name, [str(Path(self.tmp.name) / "r2")]))
        prompt = self.launch.call_args.args[1]
        self.assertIn("project P", prompt)
        self.assertIn(f"r2 (be, GitHub o/r2): {Path(self.tmp.name) / 'r2'}", prompt)

    def test_unsorted_chat_runs_in_its_own_repo_only(self):
        cid = CH.create(core.UNSORTED, "solo2")
        CH.send(cid, "hi")
        self.assertEqual((self.launch.call_args.args[0], self.launch.call_args.kwargs["add_dirs"]), (self.tmp.name, []))
        self.assertEqual(self.con.execute("SELECT repo FROM chat_conversations WHERE id=?", (cid,)).fetchone()[0], "solo2")
        with self.assertRaises(SystemExit):
            CH.create("p", "solo")

    def test_send_keeps_a_conversation_in_its_repo(self):
        self.con.execute("UPDATE chat_conversations SET repo='r2', session='s1', tool='claude' WHERE id=?", (self.id,))
        self.con.commit()
        CH.send(self.id, "hi")  # r2 is not p's default, but still in p: same checkout, the session resumes
        self.assertEqual((self.launch.call_args.args[0], self.launch.call_args.kwargs["resume"]),
                         (str(Path(self.tmp.name) / "r2"), "s1"))
        self.assertEqual(self.launch.call_args.kwargs["add_dirs"], [self.tmp.name])

    def test_send_stores_files_and_passes_paths(self):
        CH.send(self.id, "이거 봐줘", [{"name": "image.png", "data": b64(b"\x89PNG")},
                                     {"name": "build log.txt", "data": b64(b"err")}])
        d = (CH.UPLOADS / self.id).resolve()
        files = sorted(p.name for p in d.iterdir())
        self.assertEqual(len(files), 2)
        prompt = self.launch.call_args.args[1]
        png = next(f for f in files if f.endswith("image.png"))
        self.assertIn(f"- {d / png} (이미지)", prompt)
        self.assertIn(str(d / next(f for f in files if f.endswith("build log.txt"))), prompt)
        self.assertEqual(self.launch.call_args.kwargs["images"], [str(d / png)])
        m = CH.messages(self.id)["messages"][0]
        self.assertEqual([a["name"] for a in m["attachments"]], ["image.png", "build log.txt"])
        self.assertTrue(m["attachments"][0]["image"])
        self.assertEqual(m["text"], "이거 봐줘")  # the stored text has no paths
        name = m["attachments"][1]["url"].split("name=", 1)[1]
        self.assertEqual(CH.file_path(self.id, name.replace("%20", " ")).read_bytes(), b"err")
        CH.delete(self.id)
        self.assertFalse(d.exists())

    def test_agent_files_belong_to_the_reply_and_survive_finalization(self):
        shot = Path(self.tmp.name) / "결과 화면.png"
        note = shot.with_suffix(".txt")
        shot.write_bytes(b"png")
        note.write_text("ok")
        turn = CH.send(self.id, "보여 줘", [{"name": "input.txt", "data": b64(b"in")}])
        self.assertEqual(self.launch.call_args.kwargs["chat_turn"], (self.id, turn))
        with mock.patch.dict(os.environ, {"TP_CHAT_ID": self.id, "TP_CHAT_TURN_ID": str(turn)}):
            self.assertEqual(CH.attach([shot, note]), 2)
            self.assertEqual(CH.attach([shot]), 1)
        shot.unlink()
        user, reply = CH.messages(self.id)["messages"]
        self.assertEqual([a["name"] for a in user["attachments"]], ["input.txt"])
        files = reply["attachments"]
        self.assertEqual([(a["name"], a["size"], a["image"]) for a in files],
                         [(shot.name, 3, True), (note.name, 2, False), (shot.name, 3, True)])
        self.assertNotEqual(files[0]["url"], files[2]["url"])
        q = parse_qs(urlsplit(files[0]["url"]).query)
        self.assertEqual(CH.file_path(q["id"][0], q["name"][0]).read_bytes(), b"png")
        with mock.patch.object(CH.R, "agent_state", return_value=(False, 0)), \
             mock.patch.object(CH.R, "last_message", return_value="첨부했어요"):
            self.assertEqual(CH.messages(self.id)["messages"][-1]["text"], "첨부했어요")
        self.assertEqual(CH.messages(self.id)["messages"][-1]["attachments"], files)
        CH.delete(self.id)
        self.assertFalse((CH.UPLOADS / self.id).exists())
        self.assertTrue(note.is_file())

    def test_agent_attach_checks_the_conversation_and_running_turn(self):
        path = Path(self.tmp.name) / "reply.txt"
        path.touch()
        first = CH.send(self.id, "first")
        CH.stop(self.id)
        second = CH.send(self.id, "second")
        other = CH.create("p")
        CH.send(other, "other")
        for ident, turn in (("", ""), (self.id, first), (other, second)):
            with self.subTest(ident=ident, turn=turn), \
                 mock.patch.dict(os.environ, {"TP_CHAT_ID": ident, "TP_CHAT_TURN_ID": str(turn)}), \
                 self.assertRaises(SystemExit):
                CH.attach([path])
        # A descendant can outlive the runner before the next dashboard poll finalizes the row.
        with mock.patch.dict(os.environ, {"TP_CHAT_ID": self.id, "TP_CHAT_TURN_ID": str(second)}), \
             mock.patch.object(CH.R, "agent_state", return_value=(False, 0)), self.assertRaises(SystemExit):
            CH.attach([path])
        self.assertFalse(CH.UPLOADS.exists())

    def test_attachment_only_reply_finishes_successfully(self):
        path = Path(self.tmp.name) / "shot.png"
        path.touch()
        turn = CH.send(self.id, "이미지만 보여 줘")
        with mock.patch.dict(os.environ, {"TP_CHAT_ID": self.id, "TP_CHAT_TURN_ID": str(turn)}):
            CH.attach([path])
        with mock.patch.object(CH.R, "agent_state", return_value=(False, 0)), \
             mock.patch.object(CH.R, "last_message", return_value=""):
            reply = CH.messages(self.id)["messages"][-1]
        self.assertEqual((reply["text"], reply["failed"], reply["attachments"][0]["name"]), ("", False, path.name))

    def test_agent_attach_validates_the_whole_batch_and_cumulative_count(self):
        path = Path(self.tmp.name) / "ok.txt"
        path.write_bytes(b"123")
        big = path.with_name("big.txt")
        big.write_bytes(b"1234")
        turn = CH.send(self.id, "go")
        with mock.patch.dict(os.environ, {"TP_CHAT_ID": self.id, "TP_CHAT_TURN_ID": str(turn)}), \
             mock.patch.object(CH, "MAX_FILES", 2), mock.patch.object(CH, "MAX_FILE", 3):
            for bad, error in ((path.with_name(".env"), "파일 이름"), (big, "너무 커요"), (path.parent, "일반 파일")):
                with self.subTest(path=bad), self.assertRaisesRegex(SystemExit, error):
                    CH.attach([path, bad])
            self.assertFalse((CH.UPLOADS / self.id).exists())
            CH.attach([path])
            with self.assertRaisesRegex(SystemExit, "2개까지"):
                CH.attach([path, path])
            CH.attach([path])
        self.assertEqual(len(CH.messages(self.id)["messages"][-1]["attachments"]), 2)
        self.assertEqual(len(list((CH.UPLOADS / self.id).iterdir())), 2)

    def test_agent_attach_removes_new_files_if_registration_fails(self):
        path = Path(self.tmp.name) / "ok.txt"
        path.touch()
        turn = CH.send(self.id, "go")
        with mock.patch.dict(os.environ, {"TP_CHAT_ID": self.id, "TP_CHAT_TURN_ID": str(turn)}):
            CH.attach([path])
            d = CH.UPLOADS / self.id
            before = list(d.iterdir())
            self.con.execute("""CREATE TRIGGER reject BEFORE UPDATE OF attachments ON chat_messages
                                BEGIN SELECT RAISE(ABORT, 'rejected'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                CH.attach([path, path])
        self.assertEqual(list(d.iterdir()), before)
        self.assertEqual(len(CH.messages(self.id)["messages"][-1]["attachments"]), 1)

    def test_comma_kept_out_of_stored_name(self):
        CH.send(self.id, "", [{"name": "a, b.png", "data": b64(b"x")}])
        self.assertNotIn(",", self.launch.call_args.kwargs["images"][0])
        self.assertEqual(CH.messages(self.id)["messages"][0]["attachments"][0]["name"], "a, b.png")

    def test_files_only_message_titles_by_name(self):
        CH.send(self.id, "", [{"name": "shot.png", "data": b64(b"x")}])
        self.assertEqual(self.con.execute("SELECT title FROM chat_conversations").fetchone()[0], "shot.png")

    def test_rejects_bad_names_and_big_files(self):
        for name in ("../x.txt", "a/b.txt", ".env", "", "a\x00b", "x" * 121):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, "잘못된 파일 이름"):
                CH.send(self.id, "t", [{"name": name, "data": b64(b"x")}])
        with mock.patch.object(CH, "MAX_FILE", 3), self.assertRaisesRegex(SystemExit, "너무 커요"):
            CH.send(self.id, "t", [{"name": "ok.txt", "data": b64(b"x")}, {"name": "big.log", "data": b64(b"1234")}])
        with self.assertRaisesRegex(SystemExit, "읽지 못했어요"):
            CH.send(self.id, "t", [{"name": "a.txt", "data": "not base64!"}])
        self.assertFalse((CH.UPLOADS / self.id).exists())  # nothing written for a rejected message
        self.launch.assert_not_called()

    def test_file_path_stays_inside(self):
        (CH.UPLOADS / self.id).mkdir(parents=True)
        (CH.UPLOADS / self.id / "ok.txt").write_text("x")
        (CH.UPLOADS / "secret.txt").write_text("s")
        self.assertIsNotNone(CH.file_path(self.id, "ok.txt"))
        for ident, name in ((self.id, "../secret.txt"), (self.id, ".."), ("../uploads", "secret.txt"), (self.id, "")):
            self.assertIsNone(CH.file_path(ident, name))
        (CH.UPLOADS / self.id / "outside.txt").symlink_to(CH.UPLOADS / "secret.txt")
        self.assertIsNone(CH.file_path(self.id, "outside.txt"))
        other = CH.create("p")
        (CH.UPLOADS / other).symlink_to(CH.UPLOADS / self.id, target_is_directory=True)
        self.assertIsNone(CH.file_path(other, "ok.txt"))
        with self.assertRaisesRegex(SystemExit, "첨부 폴더"):
            CH.send(other, "", [{"name": "foreign.txt", "data": b64(b"x")}])

    def test_busy_refused_before_upload(self):
        CH.send(self.id, "first")
        with self.assertRaisesRegex(SystemExit, "기다리는 중"):
            CH.send(self.id, "second", [{"name": "a.txt", "data": b64(b"x")}])
        self.assertFalse((CH.UPLOADS / self.id).exists())

    def _claude_history(self, at, context_tokens):
        self.con.execute("UPDATE chat_conversations SET session='old', tool='claude', updated_at=? WHERE id=?",
                         (at, self.id))
        self.con.execute("INSERT INTO chat_messages(conversation_id,role,text,ts) VALUES(?,'user','old question',?)",
                         (self.id, at))
        self.con.execute("""INSERT INTO chat_messages(conversation_id,role,text,ts,context_tokens)
                            VALUES(?,'assistant','old reply',?,?)""", (self.id, at, context_tokens))
        self.con.commit()

    def test_small_and_unknown_context_stay_out_of_idle_candidates(self):
        self._claude_history("2026-11-18T12:00:00+00:00", 120_000)
        with mock.patch.object(CH, "now", return_value="2026-11-18T12:50:00+00:00"), \
             mock.patch.object(CH, "project_map", wraps=CH.project_map) as projects:
            for context in (None, 99_999):
                with self.subTest(context=context):
                    self.con.execute("""INSERT INTO chat_messages(conversation_id,role,text,context_tokens)
                                        VALUES(?,'assistant','latest reply',?)""", (self.id, context))
                    self.con.commit()
                    CH.compact_idle(self.con)
                    CH.compact_idle(self.con)
                    self.launch.assert_not_called()
                    projects.assert_not_called()  # no candidates to look up on either tick

    def test_compaction_at_fifty_minutes_hands_off_to_a_new_session(self):
        self._claude_history("2026-11-18T12:00:00+00:00", 100_000)
        with mock.patch.object(CH, "now", return_value="2026-11-18T12:49:59+00:00"):
            CH.compact_idle(self.con)
        self.launch.assert_not_called()
        with mock.patch.object(CH, "now", return_value="2026-11-18T12:50:00+00:00"):
            CH.compact_idle(self.con)
            CH.compact_idle(self.con)
            self.launch.assert_called_once()
            self.assertTrue(self.launch.call_args.kwargs["compact"])
            self.assertEqual(self.launch.call_args.kwargs["resume"], "old")
            self.assertTrue(self.launch.call_args.args[1].startswith("/compact "))
            self.assertIsNotNone(CH.messages(self.id)["busy"])
            with self.assertRaisesRegex(SystemExit, "압축하는 중"):
                CH.send(self.id, "wait")
        with mock.patch.object(CH.R, "agent_state", return_value=(False, 0)), \
             mock.patch.object(CH.R, "compacted_summary", return_value="private handoff"), \
             mock.patch.object(CH.R, "compacted_usage", return_value=(10, 20, 30, 4, 60)):
            thread = CH.messages(self.id)
        self.assertIsNone(thread["busy"])
        self.assertIsNone(thread["conversation"]["session"])
        self.assertEqual([m["text"] for m in thread["messages"]],
                         ["old question", "old reply", "대화가 압축됐어요"])
        self.assertEqual(thread["messages"][-1]["role"], "compaction")
        self.assertEqual(CH.inbox()[0]["unread"], 1)
        turn = CH.send(self.id, "next question")
        self.assertIsNone(self.launch.call_args.kwargs["resume"])
        prompt = self.launch.call_args.args[1]
        self.assertIn("private handoff", prompt)
        self.assertIn("next question", prompt)
        self.assertNotIn("old reply", prompt)
        compact = self.con.execute("SELECT * FROM chat_messages WHERE role='compaction'").fetchone()
        self.assertEqual((compact["before_context_tokens"], compact["input_tokens"], compact["context_tokens"]),
                         (100_000, 10, 60))
        self.assertEqual(self.con.execute("SELECT compaction_id FROM chat_messages WHERE id=?", (turn,)).fetchone()[0],
                         compact["id"])
        with mock.patch.object(CH.R, "agent_state", return_value=(False, 0)), \
             mock.patch.object(CH.R, "last_message", return_value="answer"), \
             mock.patch.object(CH.R, "session", return_value=("claude", "new")):
            CH.messages(self.id)
        second = CH.send(self.id, "one more")
        self.assertEqual(self.launch.call_args.kwargs["resume"], "new")
        self.assertEqual(self.con.execute("SELECT compaction_id FROM chat_messages WHERE id=?", (second,)).fetchone()[0],
                         compact["id"])

    def test_compaction_without_summary_leaves_existing_session_resumable(self):
        self._claude_history("2026-11-18T12:00:00+00:00", 110_000)
        with mock.patch.object(CH, "now", return_value="2026-11-18T12:50:00+00:00"):
            CH.compact_idle(self.con)
        with mock.patch.object(CH.R, "agent_state", return_value=(False, 0)), \
             mock.patch.object(CH.R, "compacted_summary", return_value=""):
            CH.compact_idle(self.con)
            self.launch.assert_called_once()  # no automatic retry for the same conversation activity
            self.assertEqual([m["role"] for m in CH.messages(self.id)["messages"]], ["user", "assistant"])
        CH.send(self.id, "try again")
        self.assertEqual(self.launch.call_args.kwargs["resume"], "old")

    def test_compaction_report_follows_the_session_established_by_a_successful_retry(self):
        compact = self.con.execute("""INSERT INTO chat_messages(conversation_id,role,text,finished_at,before_context_tokens,
                                      input_tokens,cache_read_input_tokens,cache_creation_input_tokens,output_tokens)
                                      VALUES(?,'compaction','handoff',?,200,100,0,0,0)""", (self.id, CH.now())).lastrowid
        self.con.commit()

        def finish(session, context, *, code=0):
            with mock.patch.object(CH.R, "agent_state", return_value=(False, code)), \
                 mock.patch.object(CH.R, "last_message", return_value="answer"), \
                 mock.patch.object(CH.R, "session", return_value=("claude", session)), \
                 mock.patch.object(CH.R, "claude_turn_usage", return_value=(context, 0, 0, 5, context)):
                return CH.messages(self.id)["conversation"]["session"]

        failed = CH.send(self.id, "first attempt")
        self.assertIsNone(finish("abandoned", 40, code=1))
        self.assertEqual(tuple(self.con.execute("SELECT compaction_id, context_tokens FROM chat_messages WHERE id=?",
                                                (failed,)).fetchone()), (compact, 40))
        report = CH.compaction_report(None)[0]
        self.assertEqual((report["continued"], report["first_context_tokens"], report["turns"],
                          report["net_savings_tokens"]), (False, None, 0, -100))

        CH.send(self.id, "retry")
        self.assertIsNone(self.launch.call_args.kwargs["resume"])
        self.assertIn("Earlier chat summary (background only):\nhandoff", self.launch.call_args.args[1])
        self.assertEqual(finish("adopted", 80), "adopted")
        report = CH.compaction_report(None)[0]
        self.assertEqual((report["continued"], report["first_context_tokens"], report["turns"], report["measured"],
                          report["net_savings_tokens"]), (True, 80, 1, 1, 20))

        CH.send(self.id, "follow up")
        self.assertEqual(self.launch.call_args.kwargs["resume"], "adopted")
        self.assertEqual(finish("adopted", 90, code=1), "adopted")
        report = CH.compaction_report(None)[0]
        self.assertEqual((report["first_context_tokens"], report["turns"], report["measured"],
                          report["net_savings_tokens"]), (80, 2, 2, 130))

    def test_compaction_baseline_includes_failed_resumed_turn_with_usage(self):
        self._claude_history("2026-11-18T12:00:00+00:00", 99_999)
        self.con.execute("""INSERT INTO chat_messages(conversation_id,role,text,failed,handle,context_tokens)
                            VALUES(?,'assistant','stopped',1,'1 /tmp/stop.log',110000)""", (self.id,))
        self.con.execute("""INSERT INTO chat_messages(conversation_id,role,text,failed)
                            VALUES(?,'assistant','failed to launch',1)""", (self.id,))
        self.con.commit()
        with mock.patch.object(CH, "now", return_value="2026-11-18T12:50:00+00:00"), \
             mock.patch.object(CH.R, "session", return_value=("claude", "old")):
            CH.compact_idle(self.con)
        self.assertEqual(self.con.execute("SELECT before_context_tokens FROM chat_messages WHERE role='compaction'").fetchone()[0],
                         110_000)

    def test_failed_turn_only_changes_threshold_for_its_own_session(self):
        self._claude_history("2026-11-18T12:00:00+00:00", 120_000)
        self.con.executemany("""INSERT INTO chat_messages(conversation_id,role,text,failed,claude_session_id,context_tokens)
                                VALUES(?,'assistant','failed',1,?,?)""",
                             [(self.id, "old", 99_999), (self.id, "other", 120_000)])
        self.con.commit()
        with mock.patch.object(CH, "now", return_value="2026-11-18T12:50:00+00:00"):
            CH.compact_idle(self.con)
        self.launch.assert_not_called()


class CodexImageTest(unittest.TestCase):
    def test_image_flag_does_not_swallow_prompt(self):
        a = {"tool": "codex", "model": None, "effort": None, "ultracode": False, "images": ["/u/a.png"]}
        cmd = runner.command(a, Path("/tmp/x.log"))
        self.assertEqual(cmd[-1], "-")
        self.assertIn("--image=/u/a.png", cmd)
        cmd = runner.command({**a, "resume": "sid"}, Path("/tmp/x.log"))
        self.assertEqual(cmd[-2:], ["sid", "-"])
        self.assertIn("--image=/u/a.png", cmd)



class AddDirTest(unittest.TestCase):
    def test_command_adds_dirs(self):
        base = {"model": None, "effort": None, "ultracode": None, "add_dirs": ["/a", "/b"], "session": "s"}
        self.assertIn("--add-dir=/b", runner.command({**base, "tool": "claude"}, Path("/tmp/x.log")))
        self.assertIn("--add-dir=/a", runner.command({**base, "tool": "codex"}, Path("/tmp/x.log")))
        # codex exec resume has no --add-dir
        self.assertNotIn("--add-dir=/a", runner.command({**base, "tool": "codex", "resume": "t"}, Path("/tmp/x.log")))
        self.assertIn("--add-dir=/a", runner.command({**base, "tool": "claude", "resume": "s"}, Path("/tmp/x.log")))

if __name__ == "__main__":
    unittest.main()
