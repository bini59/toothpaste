"""Tick policy, CLI exit codes, and the persisted automation state."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ticket_pilot import automation, cli, core, precheck, tick


AT = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
IDS = [row[0] for row in automation.AUTOMATIONS]


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.runner = mock.Mock(spec=["agent_state", "started_at", "stop"])
        self.runner.agent_state.return_value = (True, None)
        self.runner.started_at.return_value = AT.timestamp()
        self.precheck = mock.Mock(return_value=1)
        self.launch = mock.Mock(return_value="new-handle")
        self.on_timeout = mock.Mock()

    def advance(self, snapshot, history=(), *, only=(), force=False):
        return automation.advance(snapshot, history, only=only, force=force, clock=lambda: AT, runner=self.runner,
                                  precheck=self.precheck, launch=self.launch, on_timeout=self.on_timeout)

    def test_due_at_interval_minus_thirty_seconds_and_explicit_selection(self):
        last = (AT - timedelta(seconds=270)).isoformat()
        snapshot = {"automations": [{"id": aid, "lastRunAt": last} for aid in IDS]}
        snapshot["automations"][1]["lastRunAt"] = (AT - timedelta(seconds=269)).isoformat()
        self.advance(snapshot)
        self.assertEqual(self.precheck.call_args_list, [mock.call("dispatch"), mock.call("cleanup")])
        self.assertEqual(snapshot["automations"][0]["lastRunAt"], last)
        self.precheck.reset_mock()

        self.advance(snapshot, force=True)
        self.assertEqual(self.precheck.call_args_list, [mock.call(aid) for aid in IDS])
        self.precheck.reset_mock()
        self.advance(snapshot, only=["diff", "review"], force=True)
        self.assertEqual(self.precheck.call_args_list, [mock.call("review"), mock.call("diff")])

    def test_running_step_blocks_relaunch_and_finished_steps_update_history_even_when_excluded(self):
        snapshot = {"items": [{"automationId": aid, "usageSummary": {"handle": aid, "knownRuns": 1}}
                              for aid in IDS]}
        history = [{"handle": aid, "status": "running"} for aid in IDS]
        self.runner.agent_state.side_effect = [(True, None), (True, None), (False, 0), (False, 7)]
        self.runner.started_at.side_effect = [AT.timestamp() - 3600, None, None, None]

        result, history = self.advance(snapshot, history, only=["dispatch", "review"], force=True)

        self.runner.stop.assert_not_called()
        self.precheck.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual([row["status"] for row in history], ["running", "running", "completed", "failed (7)"])
        self.assertEqual([row["usageSummary"] for row in result["items"]], [
            {"handle": "dispatch", "knownRuns": 1}, {"handle": "review", "knownRuns": 1},
            {"knownRuns": 1, "lastRunStatus": "completed"}, {"knownRuns": 1, "lastRunStatus": "failed (7)"},
        ])

    def test_timed_out_step_stops_before_the_next_precheck_and_relaunch(self):
        calls = mock.Mock()
        calls.attach_mock(self.runner.stop, "stop")
        calls.attach_mock(self.on_timeout, "on_timeout")
        calls.attach_mock(self.precheck, "precheck")
        calls.attach_mock(self.launch, "launch")
        self.runner.started_at.return_value = AT.timestamp() - 3601
        self.precheck.return_value = 0
        snapshot = {"items": [{"automationId": "review", "usageSummary": {"handle": "old-handle", "knownRuns": 3}}]}
        history = [{"handle": str(i), "status": "completed"} for i in range(45)]
        history.append({"handle": "old-handle", "status": "running"})

        result, history = self.advance(snapshot, history, only=["review"])

        self.assertEqual(calls.mock_calls, [mock.call.stop("old-handle"), mock.call.on_timeout("review"),
                                           mock.call.precheck("review"), mock.call.launch("review", "automation_review.md")])
        self.assertEqual(result["items"][1]["usageSummary"],
                         {"handle": "new-handle", "knownRuns": 4, "lastRunStatus": "running"})
        self.assertEqual(len(history), 40)
        self.assertEqual(history[0]["handle"], "7")
        self.assertEqual([(row["handle"], row["status"]) for row in history[-2:]],
                         [("old-handle", "failed (None)"), ("new-handle", "running")])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(core, "DB_PATH", self.data / "tp.db"))
        self.enterContext(mock.patch.object(core, "_CON", None))
        self.enterContext(mock.patch.object(tick, "DATA", self.data))
        self.enterContext(mock.patch.dict(os.environ, {"TP_MODE": ""}))

    def db(self):
        con = core.db()
        self.addCleanup(con.close)
        return con

    def precheck_exit(self, aid):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as exit_status:
            cli.main([f"precheck-{aid}"])
        return exit_status.exception.code, out.getvalue(), err.getvalue()

    def test_precheck_exit_codes_and_stdout(self):
        for aid, name, output in (("dispatch", "dispatch", "triage needed: {}\n"),
                                  ("review", "review", "review queue: {}\n"),
                                  ("diff", "diff", ""),
                                  ("cleanup", "cleanup_worktrees", "cleanup queue: {}\n")):
            for count in (0, 1):
                with self.subTest(aid=aid, count=count), mock.patch.object(precheck, name, return_value=count):
                    self.assertEqual(self.precheck_exit(aid), (1 - count, output.format(count), ""))

    def test_precheck_error_exit_preserves_traceback_and_event_name(self):
        con = self.db()
        with mock.patch.object(precheck, "dispatch", side_effect=RuntimeError("sync failed")):
            code, out, err = self.precheck_exit("dispatch")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("RuntimeError: sync failed", err)
        self.assertEqual([tuple(row) for row in con.execute("SELECT kind, message FROM events")],
                         [("error", "cmd_precheck_dispatch: sync failed")])

    def test_dispatch_and_review_keep_deterministic_step_order(self):
        con = self.db()
        calls = mock.Mock()
        for module, name in ((precheck.pipeline, "sync"), (precheck.pipeline, "scan"),
                             (precheck.milestone, "tick"), (precheck.release, "process_approvals"),
                             (precheck.deploy, "deploy_tick"), (precheck.release, "release_tick"),
                             (precheck.pipeline, "dispatch"), (precheck.pipeline, "triage_list"),
                             (precheck.track, "track_deploys"), (precheck.checks, "tick"),
                             (precheck.pipeline, "review_queue")):
            label = f"{module.__name__.split('.')[-1]}_{name}"
            patched = self.enterContext(mock.patch.object(module, name, return_value=[]))
            calls.attach_mock(patched, label)
        calls.milestone_tick.side_effect = RuntimeError("milestone failed")
        calls.track_track_deploys.side_effect = RuntimeError("tracking failed")
        steps = [mock.call.milestone_tick(), mock.call.release_process_approvals(),
                 mock.call.deploy_deploy_tick(), mock.call.release_release_tick()]

        code, _, err = self.precheck_exit("dispatch")
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: milestone failed", err)
        self.assertEqual(calls.mock_calls, [mock.call.pipeline_sync(), *steps, mock.call.pipeline_dispatch(),
                                           mock.call.pipeline_triage_list(mark=False)])
        calls.reset_mock()
        code, _, err = self.precheck_exit("review")
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: tracking failed", err)
        self.assertEqual(calls.mock_calls, [mock.call.pipeline_scan(), *steps, mock.call.track_track_deploys(),
                                           mock.call.checks_tick(), mock.call.pipeline_review_queue()])
        self.assertEqual([r[0] for r in con.execute("SELECT message FROM events ORDER BY id")],
                         ["milestones: milestone failed", "milestones: milestone failed", "track: tracking failed"])

    def test_tick_writes_snapshot_history_and_logs_by_precheck_exit_code(self):
        con = self.db()
        clock = self.enterContext(mock.patch.object(tick, "datetime"))
        clock.now.return_value = AT
        self.enterContext(mock.patch.object(tick.chat, "compact_idle", side_effect=lambda con: core.meta_set(con, "probe", 1)))
        after = self.enterContext(mock.patch.object(tick, "_after_automations"))
        launch = self.enterContext(mock.patch.object(tick.runner, "launch", return_value=f"123 {self.data / 'review.log'}"))
        codes = dict(zip(IDS, [1, 0, 2, -15]))

        def child(argv, **options):
            self.assertFalse(con.in_transaction)
            aid = argv[-1].removeprefix("precheck-")
            return subprocess.CompletedProcess(argv, codes[aid], stdout="a" * 310 + "\n", stderr=aid + ":" + "e" * 2000)

        with mock.patch.object(tick.subprocess, "run", side_effect=child) as run, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(["tick", "--force"])

        self.assertEqual(out.getvalue().splitlines(), [f"{aid}: precheck exit {codes[aid]} " + "a" * 300 for aid in IDS])
        self.assertEqual(err.getvalue(), ("e" * 2000 + "\n") * 2)
        self.assertEqual(run.call_count, 4)
        for call, aid in zip(run.call_args_list, IDS):
            self.assertEqual(call.args[0], [sys.executable, "-m", "ticket_pilot.cli", f"precheck-{aid}"])
            self.assertEqual(call.kwargs["timeout"], 1800)
            self.assertEqual(call.kwargs["cwd"], core.ROOT)
            self.assertTrue(call.kwargs["capture_output"])
            self.assertTrue(call.kwargs["text"])
            self.assertEqual(call.kwargs["env"]["PYTHONPATH"], str(core.ROOT))
        launch.assert_called_once_with(str(core.ROOT), (core.ROOT / "prompts/automation_review.md").read_text(),
                                       "auto-review", role="review")
        handle = launch.return_value
        after.assert_called_once_with(con)
        self.assertEqual(con.execute("SELECT handle, kind FROM metric_agent_runs").fetchone()[:], (handle, "review"))
        snapshot = core.meta_get(con, "automations")
        self.assertEqual(snapshot, {
            "automations": [{"id": aid, "name": name, "rrule": f"{every}분마다", "enabled": True,
                             "lastRunAt": AT.isoformat(), "nextRunAt": (AT + timedelta(minutes=every)).isoformat()}
                            for aid, name, _, every in automation.AUTOMATIONS],
            "items": [{"automationId": aid, "usageSummary": summary} for aid, summary in zip(IDS, [
                {"knownRuns": 0, "lastRunStatus": "skipped_precheck"},
                {"knownRuns": 1, "handle": handle, "lastRunStatus": "running"},
                {"knownRuns": 0, "lastRunStatus": "precheck failed (2)"},
                {"knownRuns": 0, "lastRunStatus": "precheck failed (-15)"},
            ])],
            "captured_at": AT.isoformat(),
        })
        self.assertEqual(core.meta_get(con, "automation_runs"), [
            {"automationId": "review", "name": "ticket-pilot: PR review", "startedAt": AT.isoformat(),
             "status": "running", "handle": handle},
        ])

    def test_duplicate_tick_and_docker_maintenance_skip_the_database(self):
        with mock.patch.object(tick, "db") as db, contextlib.redirect_stdout(io.StringIO()) as out:
            with (self.data / "tick.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                cli.main(["tick"])
            self.assertEqual(out.getvalue(), "tick: another tick is running\n")
            status = self.data / "state" / "status.json"
            status.parent.mkdir()
            status.write_text(json.dumps({"state": "switching", "heartbeat": AT.timestamp()}))
            with mock.patch.dict(os.environ, {"TP_MODE": "docker", "TP_DOCKER_UPDATE_DIR": str(self.data)}):
                cli.main(["tick", "--force"])
            self.assertEqual(out.getvalue(), "tick: another tick is running\n")
            db.assert_not_called()

    def test_precheck_subprocess_timeout_leaves_previous_snapshot_and_releases_tick_lock(self):
        con = self.db()
        snapshot = {"captured_at": AT.isoformat(), "automations": [], "items": []}
        history = [{"status": "completed"}]
        core.meta_set(con, "automations", snapshot)
        core.meta_set(con, "automation_runs", history)
        con.commit()
        error = subprocess.TimeoutExpired("precheck-diff", 1800)
        with mock.patch.object(tick.chat, "compact_idle"), mock.patch.object(tick.subprocess, "run", side_effect=error), \
                mock.patch.object(tick.runner, "launch") as launch, mock.patch.object(tick, "_after_automations") as after:
            with self.assertRaises(subprocess.TimeoutExpired):
                cli.main(["tick", "--only", "diff"])
            launch.assert_not_called()
            after.assert_not_called()
        self.assertEqual(core.meta_get(con, "automations"), snapshot)
        self.assertEqual(core.meta_get(con, "automation_runs"), history)
        with (self.data / "tick.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
