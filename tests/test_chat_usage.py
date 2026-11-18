"""Claude transcript and compaction usage, with a temporary dashboard database."""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ticket_pilot import chat, chat_usage, core, runner


class ClaudeUsageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / "claude"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        config_dir = mock.patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config))
        config_dir.start()
        self.addCleanup(config_dir.stop)

    def _session(self, sid):
        project = re.sub(r"[^A-Za-z0-9]", "-", str(self.cwd))
        path = self.config / "projects" / project / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_finalized_turn_sums_window_and_counts_each_api_message_once(self):
        def response(second, ident, usage, **extra):
            return json.dumps({"type": "assistant", "timestamp": f"2026-11-18T12:00:{second:02}Z",
                               "message": {"id": ident, "usage": dict(zip(chat_usage.FIELDS, usage))}, **extra})

        self._session("session-1").write_text("\n".join([
            response(0, "old", (1000, 0, 0, 10)), "{malformed",
            response(10, "one", (20, 90, 40, 1)),
            response(10, "one", (20, 90, 40, 10)),  # a later block has the final output count
            response(11, "two", (30, 170, 5, 15)),
            response(11, "sidechain", (2000, 0, 0, 15), isSidechain=True),
            response(12, "synthetic", (0, 0, 0, 0)),
            response(50, "next", (5000, 0, 0, 10)),
        ]), encoding="utf-8")
        log = self.root / "turn.log"
        log.write_text("Reply", encoding="utf-8")
        start = datetime.fromisoformat("2026-11-18T12:00:01+00:00").timestamp()
        Path(f"{log}.json").write_text(json.dumps({"agent": "claude", "session": "session-1",
                                                 "cwd": str(self.cwd), "started": start}), encoding="utf-8")
        exit_file = Path(f"{log}.exit")
        exit_file.write_text("0\n", encoding="utf-8")
        os.utime(exit_file, (start + 20, start + 20))
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        con.execute("INSERT INTO chat_conversations(id) VALUES('c')")
        con.execute("INSERT INTO chat_messages(conversation_id,role,handle) VALUES('c','assistant',?)", (f"1 {log}",))
        core._migrate(con)
        row = con.execute("SELECT * FROM chat_messages").fetchone()
        self.assertIsNone(row["context_tokens"])
        chat._finalize(con, row)
        self.assertEqual(tuple(con.execute(f"SELECT {', '.join(chat_usage.FIELDS)}, context_tokens FROM chat_messages").fetchone()),
                         (50, 260, 45, 25, 205))
        self.assertEqual(con.execute("SELECT session FROM chat_conversations").fetchone()[0], "session-1")

    def test_compaction_reads_usage_even_on_failure_and_keeps_last_context(self):
        log = self.root / "compact.log"
        log.write_text(json.dumps({"type": "result", "subtype": "error_during_execution", "usage": {},
                                   "modelUsage": {"model": {"inputTokens": 9999}}}), encoding="utf-8")
        handle = f"1 {log}"
        self.assertEqual(runner.compacted_usage(handle), (None,) * 5)
        fields = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")

        def event(usage, sequence):
            attrs = [{"key": key, "value": {"intValue": str(count)}} for key, count in zip(fields, usage)]
            attrs.append({"key": "event.sequence", "value": {"intValue": sequence}})
            return {"body": {"stringValue": "claude_code.api_request"}, "attributes": attrs}

        payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [
            event((30, 10, 0, 3), 9), {"body": {"stringValue": "claude_code.compaction"}},
            event((50, 30, 20, 5), 7), event((50, 30, 20, 5), 7),
        ]}]}]}
        captured = list(chat_usage._api_usage(payload))
        Path(f"{log}.usage").write_text("\n".join(map(json.dumps, captured)) + "\n{partial", encoding="utf-8")
        self.assertEqual(runner.compacted_usage(handle), (80, 40, 20, 8, 40))

    def test_compaction_json_fallback_has_only_turn_totals(self):
        log = self.root / "compact.log"
        log.write_text(json.dumps({"type": "result", "usage": {"input_tokens": 20, "output_tokens": 5}}),
                       encoding="utf-8")
        self.assertEqual(runner.compacted_usage(f"1 {log}"), (20, 0, 0, 5, None))


class CompactionReportTest(unittest.TestCase):
    def test_limits_attempts_by_period_and_attributes_resumed_turns(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        core._migrate(con)
        con.execute("INSERT INTO chat_conversations(id,title) VALUES ('c','Thread'), ('legacy','Old'), ('idle','Idle')")
        recent = "2026-11-18T12:00:00+00:00"
        old = "2026-09-01T12:00:00+00:00"

        def compact(cid, before, cost, *, failed=False, at=recent):
            return con.execute("""INSERT INTO chat_messages(conversation_id,role,text,failed,finished_at,before_context_tokens,
                                  input_tokens,cache_read_input_tokens,cache_creation_input_tokens,output_tokens)
                                  VALUES(?,'compaction','',?,?,?,?,?,?,?)""",
                               (cid, failed, at, before, cost, 0, 0, 0)).lastrowid

        def turn(compaction_id, context, *, cid="c", handle="handle", failed=False):
            con.execute("INSERT INTO chat_messages(conversation_id,role,text) VALUES(?,'user','q')", (cid,))
            con.execute("""INSERT INTO chat_messages(conversation_id,role,text,compaction_id,context_tokens,handle,failed)
                           VALUES(?,'assistant','a',?,?,?,?)""", (cid, compaction_id, context, handle, failed))

        first = compact("c", 200, 30)
        turn(first, None, handle=None, failed=True)
        turn(first, 50)
        failure = compact("c", 50, 20, failed=True)
        turn(first, 100)  # a failed compaction leaves the resumed session linked to its last successful one
        turn(first, None)
        second = compact("c", 100, 50)
        turn(second, 130)
        turn(None, 20)  # a new session without this handoff must not be credited to it
        idle = compact("idle", 100, 15)
        turn(idle, None, cid="idle", handle=None, failed=True)
        legacy = compact("legacy", None, None, at=old)
        with mock.patch.object(chat, "db", return_value=con), mock.patch.object(chat, "now", return_value=recent):
            rows = {row["id"]: row for row in chat.compaction_report(7)}
            self.assertEqual(set(rows), {first, failure, second, idle})
            self.assertIsNone(next(row for row in chat.compaction_report(None) if row["id"] == legacy)["cost_tokens"])
        self.assertEqual((rows[first]["first_context_tokens"], rows[first]["first_savings_tokens"],
                          rows[first]["measured"], rows[first]["turns"], rows[first]["net_savings_tokens"]),
                         (50, 150, 2, 3, 220))
        self.assertEqual((rows[failure]["failed"], rows[failure]["net_savings_tokens"]), (True, -20))
        self.assertEqual((rows[idle]["continued"], rows[idle]["net_savings_tokens"]), (False, -15))
        self.assertEqual(rows[second]["net_savings_tokens"], -80)


if __name__ == "__main__":
    unittest.main()
