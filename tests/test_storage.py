"""File-backed initialization through the shared core.db() entry point."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from ticket_pilot import core
from ticket_pilot.storage import connection, migrations


class StorageTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "db" / "tp.db"
        self.config = root / "config"
        self.config.mkdir()
        self.enterContext(mock.patch.object(core, "DB_PATH", self.path))
        self.enterContext(mock.patch.object(core, "CONFIG", self.config))
        self.enterContext(mock.patch.object(connection, "_main_connections", {}))
        core.config_cache_clear()
        self.addCleanup(core.config_cache_clear)

    def open_db(self):
        con = core.db()
        self.addCleanup(core.close_db, con)
        return con

    def test_new_db_and_connection_ownership(self):
        main = self.open_db()
        self.assertIs(core.db(), main)
        self.assertEqual(main.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(main.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertIsInstance(main.execute("SELECT 1").fetchone(), sqlite3.Row)
        self.assertFalse(main.in_transaction)
        self.assertEqual(core.settings()["asana_tag"], "tp")
        self.assertTrue(core.meta_get(main, "roots_pinned"))
        self.assertTrue(core.meta_get(main, "tags_pinned"))
        self.assertIsNone(core.meta_get(main, "config_imported"))
        core.meta_set(main, "example", {"retained": True})
        main.commit()

        def request():
            with closing(core.db()) as first, closing(core.db()) as second:
                self.assertIsNot(first, main)
                self.assertIsNot(first, second)
                self.assertEqual(core.meta_get(second, "example"), {"retained": True})

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(request).result()
        core.close_db(main)
        reopened = self.open_db()
        self.assertIsNot(reopened, main)
        self.assertEqual(core.meta_get(reopened, "example"), {"retained": True})

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_fork_opens_fresh_connection(self):
        parent = self.open_db()
        pid = os.fork()
        if pid == 0:
            result = 1
            try:
                child = core.db()
                if child is not parent and core.meta_get(child, "tags_pinned"):
                    core.close_db(child)
                    result = 0
            finally:
                os._exit(result)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIs(core.db(), parent)
        self.assertTrue(core.meta_get(parent, "tags_pinned"))

    def seed_legacy(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as con:
            con.executescript("""
                CREATE TABLE config_projects (asana_gid TEXT PRIMARY KEY, pos INTEGER, name TEXT, extra TEXT);
                CREATE TABLE config_project_repos (asana_gid TEXT, repo TEXT, pos INTEGER, role TEXT,
                                                   is_default INTEGER, PRIMARY KEY (asana_gid, repo));
            """)
            con.executescript(core.SCHEMA)
            con.executescript("""
                INSERT INTO config_projects VALUES ('11',0,'Web','{}');
                INSERT INTO config_project_repos VALUES ('11','shop',0,'main',1);
                INSERT INTO tickets(gid,stage,asana_modified_at) VALUES ('old','done','2026-10-01');
                INSERT INTO runs(ticket_gid,repo,state) VALUES ('old','shop','finished');
                INSERT INTO chat_conversations(id,repo,title) VALUES ('c','shop','기존 채팅');
                INSERT INTO chat_messages(conversation_id,role,text) VALUES ('c','assistant','reply'), ('c','assistant',NULL);
                INSERT INTO events(ts,kind,message) VALUES ('2026-10-01','old','retained');
            """)
        (self.config / "settings.json").write_text(json.dumps({"max_active_runs": 2, "asana_tag": "custom"}))
        (self.config / "project_map.json").write_text(json.dumps({
            "projects": [{"asana_gid": "11", "name": "Web", "repos": [{"repo": "shop", "role": "main", "default": True}]}],
            "repos": {"shop": {"path": str(self.config)}},
        }))

    def test_two_initializers_migrate_legacy_db_once(self):
        self.seed_legacy()
        start = threading.Barrier(2, timeout=5)

        def request():
            start.wait()
            with closing(core.db()) as con:
                self.assertFalse(con.in_transaction)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request) for _ in range(2)]
            for future in futures:
                future.result()

        (self.config / "settings.json").write_text('{"max_active_runs":9}')
        con = self.open_db()
        settings = core.settings()
        self.assertEqual(settings["max_active_runs"], 2)
        self.assertEqual(settings["worktree_root"], "~/ticket-pilot/worktrees")
        self.assertEqual(settings["asana_tag"], "custom")
        self.assertEqual(settings["milestone_tag"], "orca-milestone")
        self.assertEqual(core.project_map()["projects"][0]["id"], "web")
        self.assertEqual(tuple(con.execute("SELECT stage,done_at FROM tickets").fetchone()), ("done", "2026-10-01"))
        self.assertEqual(tuple(con.execute("SELECT ticket_gid,repo,state FROM runs").fetchone()), ("old", "shop", "finished"))
        self.assertEqual(tuple(con.execute("SELECT title,project_id,read_id FROM chat_conversations").fetchone()),
                         ("기존 채팅", "web", 1))
        events = [tuple(row) for row in con.execute("SELECT kind,message FROM events ORDER BY id")]
        self.assertEqual([kind for kind, _ in events], ["old", "config", "config", "config", "config"])
        self.assertEqual(events[0], ("old", "retained"))
        for (_, message), marker in zip(events[1:], ("TP-91", "settings.json", "TP-121", "TP-184")):
            self.assertIn(marker, message)
        self.assertEqual([row[0] for row in con.execute("SELECT k FROM meta ORDER BY k")],
                         ["config_imported", "roots_pinned", "tags_pinned"])
        self.assertEqual(len(list((self.path.parent / "backups").glob("config-*.json"))), 1)

    def test_failed_backfill_rolls_back_added_columns_and_retries(self):
        self.seed_legacy()
        with mock.patch.object(migrations, "_backfill", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                core.db()
        with closing(sqlite3.connect(self.path)) as con:
            self.assertNotIn("done_at", {row[1] for row in con.execute("PRAGMA table_info(tickets)")})
            self.assertEqual(con.execute("SELECT name FROM config_projects WHERE id='web'").fetchone(), ("Web",))
            self.assertEqual(con.execute("SELECT count(*) FROM meta").fetchone(), (0,))
        con = self.open_db()
        self.assertEqual(con.execute("SELECT done_at FROM tickets WHERE gid='old'").fetchone()[0], "2026-10-01")
        self.assertTrue(core.meta_get(con, "config_imported"))
