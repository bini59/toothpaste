"""TP-103: chat unread replies (migration default, counts, read marks, 수신함). Run: python3 -m unittest discover tests"""
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import chat as CH, core


class UnreadTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.pm = {"repos": {"r": {"path": "/tmp"}, "s": {"path": "/tmp"}},
                   "projects": [{"id": "p", "name": "P", "source": "none", "repos": [{"repo": "r", "default": True}]},
                                {"id": "q", "name": "Q", "source": "none", "repos": [{"repo": "s", "default": True}]}]}
        for p in (mock.patch.object(CH, "db", lambda: self.con),
                  mock.patch.object(CH, "project_map", lambda: self.pm),
                  mock.patch.object(CH.R, "describe", lambda a: "claude"),
                  mock.patch.object(CH.R, "agent_state", lambda h: (True, None))):
            p.start()
            self.addCleanup(p.stop)
        for cid, pid, repo in (("ca", "p", "r"), ("cb", "p", "r"), ("cc", "q", "s")):
            self.con.execute("INSERT INTO chat_conversations(id,project_id,repo,title,updated_at) VALUES(?,?,?,?,?)",
                             (cid, pid, repo, cid.upper(), "2026-11-10T00:00:00"))

    def msg(self, cid, role="assistant", text="hi", failed=0, at="2026-11-10T01:00:00"):
        self.con.execute("INSERT INTO chat_messages(conversation_id,role,text,ts,failed,finished_at) VALUES(?,?,?,?,?,?)",
                         (cid, role, text, at, failed, at))
        return self.con.execute("SELECT last_insert_rowid()").fetchone()[0]

    def unread(self):
        return ({p["id"]: p["unread"] for p in CH.projects()},
                {c["id"]: c["unread"] for c in CH.conversations("p") + CH.conversations("q")})

    def test_counts_finished_and_failed_replies_not_user_or_running(self):
        self.msg("ca", "user")
        self.msg("ca")
        self.msg("ca", failed=1, text="(중단했어요)")
        self.msg("cc")
        self.con.execute("INSERT INTO chat_messages(conversation_id,role,ts) VALUES('cb','assistant','x')")  # running
        self.assertEqual(self.unread(), ({"p": 2, "q": 1}, {"ca": 2, "cb": 0, "cc": 1}))

    def test_read_marks_up_to_the_seen_reply(self):
        a1 = self.msg("ca")
        a2 = self.msg("ca")
        self.msg("cc")
        CH.read("ca", a1)
        self.assertEqual(self.unread()[1]["ca"], 1)  # a reply after the one seen stays unread
        CH.read("ca", a2)
        CH.read("ca", a1)  # an older mark never moves read_id back
        self.assertEqual(self.unread(), ({"p": 0, "q": 1}, {"ca": 0, "cb": 0, "cc": 1}))
        for bad in (None, "3", -1, True):
            with self.assertRaises(SystemExit):
                CH.read("ca", bad)
        with self.assertRaises(SystemExit):
            CH.read("nope", a2)

    def test_inbox_and_read_all(self):
        running = self.con.execute("INSERT INTO chat_messages(conversation_id,role,ts) VALUES('cb','assistant','x')").lastrowid
        self.msg("ca", text="old", at="2026-11-10T01:00:00")
        self.msg("cc", text="newest", at="2026-11-10T03:00:00", failed=1)
        items = CH.inbox()
        self.assertEqual([(i["id"], i["project"], i["unread"], i["preview"]) for i in items],
                         [("cc", "Q", 1, "newest"), ("ca", "P", 1, "old")])
        self.assertTrue(items[0]["failed"])
        later = self.msg("ca", text="after the inbox loaded")
        CH.read_all([{"id": i["id"], "upto": i["last_id"]} for i in items])
        # cb's turn started before the listed replies (lower id) and finishes after 모두 읽음: still unread
        self.con.execute("UPDATE chat_messages SET text='done' WHERE id=?", (running,))
        self.assertEqual({i["id"]: i["last_id"] for i in CH.inbox()}, {"ca": later, "cb": running})
        with self.assertRaises(SystemExit):
            CH.read_all({"id": "ca"})

    def test_migration_starts_existing_conversations_read(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        core._migrate(con)
        # a database from before TP-103: no read_id column yet
        con.execute("ALTER TABLE chat_conversations DROP COLUMN read_id")
        con.execute("INSERT INTO chat_conversations(id,repo,title) VALUES('old','r','t'), ('empty','r','t')")
        con.execute("INSERT INTO chat_messages(conversation_id,role,text) VALUES('old','user','q'), ('old','assistant','a')")
        con.execute("INSERT INTO chat_messages(conversation_id,role,text) VALUES('old','user','q2')")
        con.execute("INSERT INTO chat_messages(conversation_id,role) VALUES('old','assistant')")  # running at deploy
        core._migrate(con)
        rows = {r["id"]: r["read_id"] for r in con.execute("SELECT id, read_id FROM chat_conversations")}
        self.assertEqual(rows, {"old": 3, "empty": 0})  # the running turn's reply will count as unread once done


if __name__ == "__main__":
    unittest.main()
