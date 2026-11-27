import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_pilot import core, metrics, metrics_usage, update

MONDAY = "2026-11-16T00:00:00Z"


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.addCleanup(self.con.close)

    def pr(self, gid, prod, *, base="main", created=None, merged=None, url=None):
        self.con.execute("""INSERT INTO prs(url,ticket_gid,repo,number,base,created_at,merged_at,prod_at)
                            VALUES(?,?,'repo',1,?,?,?,?)""", (url or gid, gid, base, created, merged, prod))

    def event(self, gid, kind, ts, message=None):
        return self.con.execute("INSERT INTO events(ticket_gid,kind,ts,message) VALUES(?,?,?,?)",
                                (gid, kind, ts, message)).lastrowid

    def test_production_cohort_and_interventions(self):
        before = "2026-11-16T03:30:00Z"
        prod = "2026-11-17T02:00:00+00:00"
        for gid in ("clean", "future", "legacy", "inbox", "timeout", *metrics.INTERVENTIONS):
            self.pr(gid, prod)
        self.pr("clean", "2026-11-19T00:00:00Z", url="second-pr")
        for kind in metrics.INTERVENTIONS:
            self.event(kind, kind, before)
        self.event("clean", "rework", before)
        self.event("clean", "reply", before, "clean: BLOCKED answer cleared")
        self.event("future", "review_override", "2026-11-18T00:00:00+00:00")
        self.con.execute("""INSERT INTO runs(ticket_gid,ws_status,ws_comment,updated_at)
                            VALUES('legacy','todo','Blocked: please check',?),
                                  ('timeout','todo','BLOCKED: agent exited (code 1) without marking the PR ready; log x',?)""",
                         (before, before))
        self.con.executemany("""INSERT INTO notifications(ts,kind,ticket_gid,body,read_at)
                             VALUES(?,'blocked',?,?,?)""",
                             [(before, "inbox", "old BLOCKED report", prod),
                              (before, "timeout", "agent exited without marking the PR ready; log x", prod)])
        data = metrics.report(self.con, at=metrics_usage.timestamp("2026-11-22T00:00:00Z"))
        self.assertEqual(data["weeks"][-1]["start"], metrics.iso(metrics_usage.timestamp(MONDAY)))
        self.assertEqual(data["period"]["values"]["intervention_free"], {"value": 3 / 11, "n": 11})

    def test_weekly_diagnostics_and_marker_windows(self):
        mark = metrics_usage.timestamp(MONDAY)
        day = 86400
        self.con.execute("INSERT INTO metric_markers(at,label,kind) VALUES(?,'baseline','manual')", (mark,))
        self.pr("staging", MONDAY, base="preprod", created="2026-11-15T00:00:00Z", merged="2026-11-15T12:00:00Z")
        self.pr("milestone", "2026-11-16T08:00:00Z", base="milestone/7", created=MONDAY, merged="2026-11-16T04:00:00Z")
        self.pr("main", "2026-11-16T18:00:00Z", created=MONDAY, merged="2026-11-16T14:00:00Z")
        self.pr("main2", "2026-11-16T20:00:00Z", created=MONDAY, merged="2026-11-16T16:00:00Z")
        self.pr("left-edge", metrics.iso(mark - 14 * day))
        self.pr("right-edge", metrics.iso(mark + 14 * day))
        self.pr("outside", metrics.iso(mark - 14 * day - 1))
        self.con.executemany("""INSERT INTO reviews(pr_url,ticket_gid,verdict,source,manual,created_at,rework_at)
                               VALUES(?,?,?,?,?,?,?)""", [
            ("main", "main", "changes", "check", 0, MONDAY, MONDAY),
            ("main", "main", "approve", None, 0, "2026-11-16T02:00:00Z", None),
            ("main2", "main2", "changes", None, 1, MONDAY, None),
            ("main2", "main2", "changes", None, 0, "2026-11-16T01:00:00Z", MONDAY),
            ("main2", "main2", "approve", None, 0, "2026-11-16T05:00:00Z", None),
        ])
        self.event("main2", "rework_cap", MONDAY, "main2: PR #1 still needs changes after 3 rounds")
        self.event("main", "error", MONDAY, "repeat")
        self.event("main2", "error", "2026-11-16T04:00:00Z", "repeat")
        self.event("main2", "error", "2026-11-17T04:00:00Z", "another")
        self.event("main2", "conflict_rework", MONDAY)
        exit_event = self.event("main", "agent_exit", MONDAY)
        self.event("main2", "agent_timeout", MONDAY)
        self.con.execute("""INSERT INTO metric_agent_runs(log,kind,ticket_gid,started_at,finished_at,has_ws_set,exit_event,
                            tool,tokens,tool_calls,help_calls,dev_calls,wait_calls)
                            VALUES('work.log','work','main',?,?,0,?,'claude',130,4,1,2,0),
                                  ('after.log','work','main',?,?,1,NULL,'codex',900,8,0,1,1)""",
                         (mark, mark + 1, exit_event, mark + day, mark + day))
        self.con.execute("INSERT INTO metric_agent_runs(log,kind,tool) VALUES('review.log','review','codex')")
        self.con.execute("INSERT INTO metric_review_tokens VALUES('review.log','main',?,70)", (mark,))
        rules = {"repo": {"staging": {"branch": "preprod"}}}
        report = metrics.report(self.con, mark_id=1, at=mark + 20 * day, rules=rules)
        before, after = report["comparison"]["before"], report["comparison"]["after"]
        self.assertEqual(before["start"], metrics.iso(mark - 14 * day))
        self.assertEqual(before["end"], after["start"])
        self.assertEqual(after["end"], metrics.iso(mark + 14 * day))
        self.assertEqual(before["values"]["intervention_free"]["n"], 1)
        self.assertEqual(after["values"]["intervention_free"]["n"], 4)
        values = after["values"]
        for key, value, n in (
            ("first_review_approval", 0.5, 2), ("rework_rounds", 0.5, 2),
            ("pr_to_merge_main", 15, 2), ("pr_to_merge_milestone", 4, 1),
            ("merge_to_prod_staging", 12, 1), ("merge_to_prod_main", 4, 2),
            ("tools_per_run", 6, 2), ("help_calls", 1, 2), ("dev_calls", 3, 2), ("wait_calls", 1, 2),
            ("errors", 2, 3), ("missing_ws_set", 2, 2), ("conflict_rework", 1, 1),
            ("tokens_per_ticket", 200, 1),
        ):
            self.assertEqual(values[key], {"value": value, "n": n}, key)
        self.assertEqual(after["token_sources"], ["claude", "codex"])
        first_week = next(week for week in report["weeks"] if week["start"] == metrics.iso(mark))
        prev_week = next(week for week in report["weeks"] if week["end"] == metrics.iso(mark))
        self.assertEqual(first_week["values"]["first_review_approval"], {"value": 0.5, "n": 2})
        self.assertEqual(first_week["values"]["errors"], {"value": 2, "n": 3})
        self.assertEqual(prev_week["values"]["pr_to_merge_staging"], {"value": 12, "n": 1})
        partial = metrics.report(self.con, mark_id=1, at=mark + day)["comparison"]
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["before"]["start"], metrics.iso(mark - day))
        self.assertEqual(partial["after"]["end"], metrics.iso(mark + day))
        self.assertIsNone(metrics.report(self.con, mark_id=1, at=mark)["comparison"]["after"]["values"]["errors"]["value"])
        with self.assertRaises(ValueError):
            metrics.report(self.con, days=0)

    def test_version_markers_wait_for_success_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(update, "current", return_value="v1.2.3") as current:
            root = Path(tmp)
            (root / "data").mkdir()
            pending = root / "data/update.pending"
            pending.touch()
            update.record_installed(self.con, root)
            pending.unlink()
            update.record_installed(self.con, root)
            update.record_installed(self.con, root)
            metrics.create_marker(self.con, "  프롬프트 수정  ")
            current.return_value = "v1.2.4-2-gabc"
            update.record_installed(self.con, root)
            current.return_value = "v1.2.4"
            update.record_installed(self.con, root)
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT label,kind FROM metric_markers ORDER BY id")],
                         [("v1.2.3", "version"), ("프롬프트 수정", "manual"), ("v1.2.4", "version")])
        with self.assertRaises(SystemExit):
            metrics.create_marker(self.con, "a\nb")

    def test_transcript_backfill_and_review_shares_are_collected_once(self):
        session = "a1234567-1234-abcd-1234-a123456789ab"
        start = metrics_usage.timestamp("2020-01-06T12:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "ticket-1"
            logs = root / "agents"
            logs.mkdir()

            def log_record(tag, agent, cwd, directory, *, has_session=True):
                log = logs / f"{tag}-20200106-120000-000000.log"
                log.write_text(f"session id: {session}\n")
                Path(f"{log}.json").write_text(json.dumps({
                    "agent": agent, "cwd": str(cwd), "started": start,
                    "session": session if has_session else None, "transcript_home": str(directory),
                }))
                exit_file = Path(f"{log}.exit")
                exit_file.touch()
                os.utime(exit_file, (start + 60, start + 60))
                return log

            claude_home = root / "claude"
            work = log_record("ticket-1-rework", "claude", wt, claude_home)
            project = claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt.resolve()))
            project.mkdir(parents=True)
            claude = project / f"{session}.jsonl"
            call = {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "bin/tp screenshot --help; sleep 3"}}
            assistant = {"type": "assistant", "timestamp": metrics.iso(start + 10),
                         "message": {"id": "msg", "content": [call], "usage": {
                             "input_tokens": 10, "cache_creation_input_tokens": 30,
                             "cache_read_input_tokens": 40, "output_tokens": 5,
                         }}}
            claude.write_text("\n".join(map(json.dumps, [
                assistant, assistant | {"isSidechain": True},
                assistant | {"message": assistant["message"] | {"usage": {"output_tokens": 8}}},
                assistant | {"message": {"content": 3}},
            ])))

            codex_home = root / "codex"
            review = log_record("auto-review", "codex", root, codex_home, has_session=False)
            folder = codex_home / "sessions/2020/01/06"
            folder.mkdir(parents=True)
            codex = folder / f"rollout-2020-01-06T12-00-00-{session}.jsonl"

            def token(at, input_tokens, output_tokens):
                return {"type": "event_msg", "timestamp": metrics.iso(start + at), "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": input_tokens, "cached_input_tokens": 21, "output_tokens": output_tokens,
                    }},
                }}

            codex.write_text("\n".join(map(json.dumps, [
                token(5, 24, 6), token(6, 24, 6),
                {"type": "response_item", "timestamp": metrics.iso(start + 25), "payload": {"type": {}}},
                {"type": "response_item", "timestamp": metrics.iso(start + 15), "payload": {
                    "type": "custom_tool_call", "call_id": "shell", "name": "exec",
                    "input": 'await tools.exec_command({cmd: "npm run dev; tp guide; until curl; do sleep 1; done"})',
                }},
                token(21, 34, 6), token(22, 80, 20),
            ])))
            self.con.execute("INSERT INTO tickets(gid,key) VALUES('one','TICKET-1')")
            self.con.execute("""INSERT INTO runs(ticket_gid,worktree_name,worktree_path)
                                VALUES('one','ticket-1',?)""", (str(wt),))
            self.con.executemany("INSERT INTO metric_review_starts VALUES(?,?,?)", [
                (str(review), start + 2, "one"), (str(review), start + 20, "two"),
            ])
            with patch.object(metrics_usage.runner, "LOGS", logs):
                self.assertEqual(metrics_usage.collect(self.con), 2)
                claude.unlink()
                codex.unlink()
                self.assertEqual(metrics_usage.collect(self.con), 0)
            rows = self.con.execute("""SELECT log,tool,tokens,tool_calls,help_calls,dev_calls,wait_calls
                                       FROM metric_agent_runs ORDER BY kind DESC""").fetchall()
            self.assertEqual([tuple(r) for r in rows], [
                (str(work), "claude", 88, 1, 1, 0, 1), (str(review), "codex", 100, 1, 1, 1, 1),
            ])
            shares = self.con.execute("SELECT ticket_gid,tokens FROM metric_review_tokens ORDER BY ticket_gid").fetchall()
            self.assertEqual([tuple(r) for r in shares], [("one", 40), ("two", 60)])
        self.assertIsNone(metrics_usage.DEV.search("which vite; npx vite build"))

    def test_infer_review_window_if_one_start_is_missing(self):
        start = metrics_usage.timestamp(MONDAY)
        self.con.execute("INSERT INTO metric_review_starts VALUES('review.log',?,'one')", (start + 4,))
        self.con.executemany("INSERT INTO reviews(ticket_gid,created_at,agent_log) VALUES(?,?,'review.log')", [
            ("one", metrics.iso(start + 9)), ("two", metrics.iso(start + 50)),
        ])
        bounds, owners = metrics_usage._review_windows(self.con, "review.log", start, start + 60)
        self.assertEqual(list(zip(bounds, owners)), [(start, "one"), (start + 10, "two")])


if __name__ == "__main__":
    unittest.main()
