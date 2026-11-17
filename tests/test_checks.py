"""tp check (TP-47): tp's own checks gate the review queue and the dashboard approval; a failed head goes back to the
agent through the rework flow; repos without checks keep the old flow. Run: python3 -m unittest discover tests"""
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import checks as K, core, pipeline as P, release as R

CFG = {"r": {"ci": False, "checks": [{"name": "lint", "cmd": "run lint"}, {"name": "test", "cmd": "run test"}]},
       "m": {"ci": True, "checks": []}}


class CheckGateTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        for gid, repo in (("A", "r"), ("B", "n")):  # r has checks, n has none
            self.con.execute("INSERT INTO tickets(gid,key,name,has_tag,assigned,repo_override) VALUES (?,?,'t',1,1,?)",
                             (gid, gid, repo))
            rid = self.con.execute("""INSERT INTO runs(ticket_gid,repo,worktree_path,ws_status,state)
                                      VALUES (?,?,'/wt','in-review','active')""", (gid, repo)).lastrowid
            self.con.execute("""INSERT INTO prs(url,run_id,ticket_gid,repo,number,state,head_sha,base,mergeable)
                                VALUES (?,?,?,?,1,'OPEN','s0','staging','MERGEABLE')""", (f"u{gid}", rid, gid, repo))
        self.p = [mock.patch.object(K, "repo_checks", side_effect=lambda repo: CFG.get(repo)),
                  mock.patch.object(K, "db", lambda: self.con), mock.patch.object(P, "db", lambda: self.con),
                  mock.patch.object(P, "github_token", return_value="tok"),
                  mock.patch.object(R, "db", lambda: self.con),
                  mock.patch.object(R, "staging_branch", return_value="staging"),
                  mock.patch.object(K, "LOG_DIR", self.tmp / "logs"),
                  mock.patch.object(K, "conf", return_value={"timeout_min": 0.05, "max_parallel": 2}),
                  mock.patch.object(P.R, "launch", return_value="1 /tmp/tp-test-agent.log"),
                  mock.patch.object(P.R, "running", return_value=False),
                  mock.patch.object(P, "max_rework_rounds", return_value=3),
                  mock.patch.object(P, "settings", return_value={"max_active_runs": 3, "agent": "codex"})]
        for x in self.p:
            x.start()
            self.addCleanup(x.stop)  # also when setUp fails halfway: a leaked patch breaks later test modules
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.launch = P.R.launch

    # ------------------------------------------------------------ helpers
    def checks(self, gid="A", sha="s0", **status):
        self.con.execute("DELETE FROM checks WHERE pr_url=? AND head_sha=?", (f"u{gid}", sha))
        for name, st in status.items():
            log = self.tmp / "logs" / f"{name}.log"
            log.write_text("\n".join(f"line {i}" for i in range(200)) + f"\n{name} broke here\n")
            self.con.execute("""INSERT INTO checks(pr_url,ticket_gid,head_sha,name,cmd,status,exit_code,log_path,created_at)
                                VALUES (?,?,?,?,?,?,1,?,?)""", (f"u{gid}", gid, sha, name, f"run {name}", st, str(log), core.now()))

    def approve(self, gid="A", sha="s0"):
        self.con.execute("INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict) VALUES (?,?,?,'approve')", (f"u{gid}", gid, sha))

    def ticket(self, gid="A"):
        return self.con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()

    def stage(self, gid="A"):
        P.restage(self.con)
        return self.ticket(gid)["stage"]

    def queue(self):
        return {q["ticket"]: q for q in P.review_queue(self.con)}

    def launched(self):
        return self.launch.call_args_list

    def pr(self, gid="A"):
        return self.con.execute("SELECT * FROM prs WHERE url=?", (f"u{gid}",)).fetchone()

    def settle(self, gid="A"):
        return K.settle(self.con, self.pr(gid), log=lambda *a: None)

    # ------------------------------------------------------------ approval gate
    def test_approval_gate(self):
        self.approve()
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "tp 검사 아직 안 끝났어요")  # not run yet
        self.checks(lint="pass", test="running")
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "tp 검사 아직 안 끝났어요")  # running
        self.checks(lint="pass", test="fail")
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "tp 검사 실패: test")
        with self.assertRaises(SystemExit):
            R.request_approval("A")
        self.checks(lint="pass", test="flaky")
        pr, err = R.approval_target(self.con, self.ticket())
        self.assertIsNone(err)
        self.assertEqual(pr["url"], "uA")

    def test_blocked_review_approved_with_reason_goes_to_staging(self):
        """TP-99: a regular ticket whose head the AI blocked gets a staging merge request with the owner's reason."""
        self.con.execute("INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict) VALUES ('uB','B','s0','blocked')")
        self.assertEqual(self.stage("B"), "reviewed")
        with mock.patch.object(R, "_gh") as gh:
            R.request_approval("B", override_review="검증 결과를 PR에 남김")
        self.assertEqual(gh.call_args[0][0][:3], ["pr", "comment", "uB"])
        self.assertEqual(self.stage("B"), "approving")
        self.assertEqual(R.approval_target(self.con, self.ticket("B"))[0]["base"], "staging")

    def test_ignore_failed_checks_then_ai_review_then_approve(self):
        """Real order: the head fails, settle() sends it back (check review), the owner ignores the checks with a reason
        before the agent was relaunched, the head goes to the AI review, the AI approves, the owner approves."""
        self.checks(lint="pass", test="fail")
        self.assertEqual(self.settle(), "fail")
        self.assertEqual(self.stage(), "rework")
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "최신 커밋에 AI '괜찮음' 판정이 없어요")
        R.override_checks("A", "  base에서도 깨진 테스트, 이 PR과 무관  ")
        t = self.ticket()
        self.assertEqual((t["check_override"], t["check_override_sha"]), ("base에서도 깨진 테스트, 이 PR과 무관", "s0"))
        self.assertIn("tp 검사 실패: test", self.con.execute("SELECT message FROM events WHERE kind='check_override'")
                      .fetchone()["message"])
        # the unsent failure is gone: no rework, the head goes to the AI review with the owner's reason
        self.assertEqual(self.con.execute("SELECT count(*) c FROM reviews WHERE source='check'").fetchone()["c"], 0)
        self.assertEqual(self.stage(), "pr_open")
        P.dispatch(log=lambda *a: None)
        self.assertEqual(self.launched(), [])
        q = self.queue()["A"]
        self.assertEqual(q["check_override"], "base에서도 깨진 테스트, 이 PR과 무관")
        self.assertEqual([c["status"] for c in q["checks"]], ["pass", "fail"])
        # later ticks leave the overridden head alone (no new check review, no BLOCKED)
        self.assertEqual(K.tick(log=lambda *a: None, spawn=mock.Mock())["states"]["A"], "override")
        self.assertEqual(self.con.execute("SELECT count(*) c FROM reviews WHERE source='check'").fetchone()["c"], 0)
        # merging still needs the AI approve
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "최신 커밋에 AI '괜찮음' 판정이 없어요")
        self.approve()
        self.assertEqual(self.stage(), "reviewed")
        R.request_approval("A")
        self.assertTrue(self.ticket()["approve_requested_at"])
        self.assertIsNone(R.approval_target(self.con, self.ticket())[1])  # the host merge honours it too
        # a new head needs its own checks (or a new override); cancel clears it
        self.con.execute("UPDATE prs SET head_sha='s1' WHERE url='uA'")
        self.approve(sha="s1")
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "tp 검사 아직 안 끝났어요")
        R.request_approval("A", cancel=True)
        self.assertIsNone(self.ticket()["check_override"])

    def test_override_at_approval_after_settle(self):
        """An AI-approved head whose checks then fail (settle adds a check review): approve with a reason."""
        self.approve()
        self.checks(lint="pass", test="fail")
        self.settle()
        self.assertEqual(self.stage(), "rework")
        self.assertEqual(R.approval_target(self.con, self.ticket())[1], "tp 검사 실패: test")
        with self.assertRaises(SystemExit):
            R.request_approval("A")
        R.request_approval("A", override="CI 서버 문제, 로컬에서 통과 확인")
        self.assertEqual(self.stage(), "approving")
        self.assertIsNone(R.approval_target(self.con, self.ticket())[1])

    def test_ignore_needs_a_failing_head(self):
        self.checks(lint="pass", test="pass")
        with self.assertRaises(SystemExit):
            R.override_checks("A", "왜")
        self.checks(lint="pass", test="fail")
        with self.assertRaises(SystemExit):
            R.override_checks("A", "  ")
        self.assertIsNone(self.ticket()["check_override"])

    def test_override_only_covers_the_check_gate(self):
        self.checks(lint="pass", test="fail")  # and no AI approve
        with self.assertRaises(SystemExit) as e:
            R.request_approval("A", override="급해요")
        self.assertIn("AI", str(e.exception))
        self.assertIsNone(self.ticket()["check_override"])

    # ------------------------------------------------------------ review queue + rework
    def test_queue_waits_for_checks_and_carries_them(self):
        self.assertNotIn("A", self.queue())  # not run
        self.checks(lint="pass", test="running")
        self.assertNotIn("A", self.queue())
        self.checks(lint="pass", test="flaky")
        q = self.queue()["A"]
        self.assertEqual([(c["name"], c["status"]) for c in q["checks"]], [("lint", "pass"), ("test", "flaky")])
        self.assertTrue(q["checks"][0]["log_path"].endswith("lint.log"))

    def test_failed_head_goes_back_to_agent(self):
        self.checks(lint="pass", test="fail")
        self.assertNotIn("A", self.queue())
        self.assertEqual(K.settle(self.con, self.con.execute("SELECT * FROM prs WHERE url='uA'").fetchone(),
                                  log=lambda *a: None), "fail")
        self.assertEqual(self.stage(), "rework")
        self.assertEqual(self.con.execute("SELECT count(*) c FROM events WHERE kind='check_fail'").fetchone()["c"], 1)
        P.dispatch(log=lambda *a: None)
        self.assertEqual(len(self.launched()), 1)
        brief = self.launched()[0].args[1]
        self.assertIn("tp check", brief)
        self.assertIn("run test", brief)                   # command
        self.assertIn("test broke here", brief)            # log tail
        self.assertNotIn("line 100\n", brief)              # ~80 lines only
        self.assertEqual(self.stage(), "in_progress")
        self.assertNotIn("A", self.queue())                # agent working

        # agent answers without a new commit: the head still fails, so the owner decides instead of a review
        self.con.execute("UPDATE runs SET ws_status='in-review' WHERE ticket_gid='A'")
        self.assertNotIn("A", self.queue())
        K.tick(log=lambda *a: None, spawn=mock.Mock())
        self.assertTrue(self.con.execute("SELECT ws_comment FROM runs WHERE ticket_gid='A'").fetchone()["ws_comment"]
                        .startswith("BLOCKED"))
        self.assertEqual(self.stage(), "blocked")

    def test_fixed_head_gets_a_first_ai_review_and_counts_toward_the_cap(self):
        self.checks(lint="pass", test="fail")
        K.settle(self.con, self.con.execute("SELECT * FROM prs WHERE url='uA'").fetchone(), log=lambda *a: None)
        P.dispatch(log=lambda *a: None)
        pr = self.con.execute("SELECT * FROM prs WHERE url='uA'").fetchone()
        self.assertEqual(P.rework_rounds(self.con, self.ticket(), pr), 1)  # within review.max_rework_rounds
        self.con.execute("UPDATE prs SET head_sha='s1' WHERE url='uA'")
        self.con.execute("UPDATE runs SET ws_status='in-review' WHERE ticket_gid='A'")
        self.checks(sha="s1", lint="pass", test="pass")
        q = self.queue()["A"]
        self.assertEqual((q["round"], q["previous_review"]), (1, None))  # the check failure was not a code review

    def test_tick_starts_each_head_once(self):
        spawn = mock.Mock()
        K.tick(log=lambda *a: None, spawn=spawn)
        spawn.assert_called_once_with("uA", "s0")  # B's repo has no checks
        pr = self.con.execute("SELECT * FROM prs WHERE url='uA'").fetchone()
        logs = {c["name"]: self.tmp / c["name"] for c in CFG["r"]["checks"]}
        self.assertTrue(K._claim(self.con, pr, CFG["r"], logs))
        self.assertFalse(K._claim(self.con, pr, CFG["r"], logs))  # a second run of the same head is refused
        K.tick(log=lambda *a: None, spawn=spawn)
        self.assertEqual(spawn.call_count, 1)

    def test_dead_partial_run_starts_over(self):
        self.checks(lint="pass", test="running")
        self.con.execute("UPDATE checks SET created_at='2026-01-01T00:00:00+00:00'")  # the runner died long ago
        spawn = mock.Mock()
        K.tick(log=lambda *a: None, spawn=spawn)
        spawn.assert_called_once_with("uA", "s0")
        pr = self.con.execute("SELECT * FROM prs WHERE url='uA'").fetchone()
        self.assertTrue(K._claim(self.con, pr, CFG["r"], {c["name"]: self.tmp / c["name"] for c in CFG["r"]["checks"]}))
        self.assertEqual({r["status"] for r in K.head_rows(self.con, "uA", "s0")}, {"running"})

    def claim(self):
        return K._claim(self.con, self.pr(), CFG["r"], {c["name"]: self.tmp / c["name"] for c in CFG["r"]["checks"]})

    def test_error_is_retried_once(self):
        self.checks(lint="pass", test="error")
        self.assertEqual(K.check_state(self.con, self.pr())["state"], "pending")  # waits for the retry
        self.assertNotIn("A", self.queue())
        spawn = mock.Mock()
        K.tick(log=lambda *a: None, spawn=spawn)
        spawn.assert_called_once_with("uA", "s0")
        self.assertTrue(self.claim())
        self.con.execute("UPDATE checks SET status='error' WHERE name='test'")
        self.con.execute("UPDATE checks SET status='pass' WHERE name='lint'")
        self.assertEqual(K.check_state(self.con, self.pr())["state"], "error")  # second error: no more retries
        self.assertFalse(self.claim())
        self.assertIn("A", self.queue())

    def test_changed_config_reruns_the_head(self):
        self.checks(lint="pass", test="pass")
        self.assertFalse(self.claim())
        with mock.patch.dict(CFG, {"r": {"ci": False, "checks": CFG["r"]["checks"] + [{"name": "build", "cmd": "z"}]}}):
            self.assertEqual(K.check_state(self.con, self.pr())["state"], "pending")
            self.assertTrue(K._claim(self.con, self.pr(), CFG["r"], {n: self.tmp / n for n in ("lint", "test", "build")}))
            self.assertEqual({r["name"] for r in K.head_rows(self.con, "uA", "s0")}, {"lint", "test", "build"})

    def test_rerun_that_passes_withdraws_the_unsent_failure(self):
        self.checks(lint="pass", test="fail")
        self.settle()
        self.checks(lint="pass", test="pass")
        self.assertEqual(self.settle(), "pass")
        self.assertEqual(self.con.execute("SELECT count(*) c FROM reviews").fetchone()["c"], 0)
        self.assertIn("A", self.queue())

    def test_ci_repo_reads_github_checks(self):
        self.con.execute("UPDATE runs SET repo='m' WHERE ticket_gid='A'")
        self.con.execute("UPDATE prs SET repo='m', github='o/m' WHERE url='uA'")
        out = [mock.Mock(stdout='[{"name":"test","bucket":"pending","link":"https://gh/1","workflow":"CI"}]', stderr="", returncode=8),
               mock.Mock(stdout='[{"name":"test","bucket":"fail","link":"https://gh/1","workflow":"CI"}]', stderr="", returncode=1)]
        with mock.patch.object(K, "run", side_effect=out):
            spawn = mock.Mock()
            K.tick(log=lambda *a: None, spawn=spawn)
            self.assertEqual([(r["name"], r["status"]) for r in K.head_rows(self.con, "uA", "s0")], [("test", "ci_pending")])
            self.assertNotIn("A", self.queue())
            K.tick(log=lambda *a: None, spawn=spawn)
        spawn.assert_not_called()  # CI repos never run locally
        self.assertEqual(K.head_rows(self.con, "uA", "s0")[0]["log_path"], "https://gh/1")
        self.assertEqual(self.stage(), "rework")  # a red CI goes back to the agent like a local failure

    def test_ci_rerun_on_github_is_picked_up(self):
        self.con.execute("UPDATE prs SET repo='m', github='o/m' WHERE url='uA'")
        cancelled = mock.Mock(stdout='[{"name":"test","bucket":"cancel","link":"https://gh/1","workflow":"CI"}]', stderr="")
        passed = mock.Mock(stdout='[{"name":"test","bucket":"pass","link":"https://gh/2","workflow":"CI"}]', stderr="")
        with mock.patch.object(K, "run", side_effect=[cancelled, passed]) as gh:
            K.sync_ci(self.con, self.pr())
            K.sync_ci(self.con, self.pr())  # read moments ago: not again yet
            self.assertEqual(gh.call_count, 1)
            self.con.execute("UPDATE checks SET finished_at='2026-01-01T00:00:00+00:00'")
            K.sync_ci(self.con, self.pr())
        self.assertEqual([r["status"] for r in K.head_rows(self.con, "uA", "s0")], ["pass"])

    def test_ci_repo_subtask_into_milestone_is_not_gated(self):
        self.con.execute("UPDATE prs SET repo='m', github='o/m', base='milestone/123' WHERE url='uA'")
        self.approve()
        with mock.patch.object(K, "run") as gh:
            K.tick(log=lambda *a: None, spawn=mock.Mock())
        gh.assert_not_called()  # GitHub CI is not read for milestone bases
        self.assertEqual(K.check_state(self.con, self.pr())["state"], "none")
        self.assertIsNone(K.gate_error(self.con, self.pr()))

    def test_round_cap_question_names_the_checks(self):
        self.checks(lint="pass", test="fail")
        self.settle()
        self.con.execute("UPDATE reviews SET rework_at='2026-10-29T00:00:00+00:00'")
        with mock.patch.object(P, "rework_rounds", return_value=3):
            self.assertTrue(P.escalate_rework(self.con, self.ticket(), 3, log=lambda *a: None))
        q = self.con.execute("SELECT ws_comment FROM runs WHERE ticket_gid='A'").fetchone()["ws_comment"]
        self.assertIn("tp 검사 실패", q)
        self.assertNotIn("AI 리뷰", q)

    # ------------------------------------------------------------ repos without checks: old flow
    def test_repo_without_checks_is_not_gated(self):
        self.assertEqual(self.queue()["B"]["checks"], [])
        self.assertEqual(R.approval_target(self.con, self.ticket("B"))[1], "최신 커밋에 AI '괜찮음' 판정이 없어요")
        self.approve("B")
        self.assertIsNone(R.approval_target(self.con, self.ticket("B"))[1])
        self.assertEqual(K.tick(log=lambda *a: None, spawn=mock.Mock())["states"].get("B"), None)


class RunChecksTest(unittest.TestCase):
    """run_checks on a real git commit: clean archive copy, rerun once, flaky / fail / timeout, nothing written back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("hello\n")
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        self.sha = git("rev-parse", "HEAD").stdout.decode().strip()
        (self.tmp / "logs").mkdir()
        self.p = [mock.patch.object(K, "LOG_DIR", self.tmp / "logs"),
                  mock.patch.object(K, "conf", return_value={"timeout_min": 0.03, "max_parallel": 2})]
        for x in self.p:
            x.start()
            self.addCleanup(x.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_statuses(self):
        marker = self.tmp / "ran-once"
        cfg = {"checks": [
            {"name": "ok", "cmd": "grep -q hello a.txt && touch made-in-copy"},
            {"name": "flaky", "cmd": f"if [ -f {marker} ]; then exit 0; else touch {marker}; exit 1; fi"},
            {"name": "broken", "cmd": "echo boom; exit 3"},
            {"name": "slow", "cmd": "sleep 5"},
        ]}
        logs = {c["name"]: self.tmp / "logs" / f"{c['name']}.log" for c in cfg["checks"]}
        got = {}
        K.run_checks([str(self.repo)], self.sha, cfg, logs, lambda n, st, rc, s: got.__setitem__(n, (st, rc)))
        self.assertEqual(got, {"ok": ("pass", 0), "flaky": ("flaky", 0), "broken": ("fail", 3), "slow": ("timeout", None)})
        self.assertIn("running it once more", logs["broken"].read_text())
        self.assertEqual(logs["broken"].read_text().count("\nboom\n"), 2)  # ran twice
        self.assertFalse((self.repo / "made-in-copy").exists())  # ran in a temp copy, not the checkout
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(status.stdout, "")

    def test_unknown_commit_is_an_error_not_a_failure(self):
        cfg = {"checks": [{"name": "ok", "cmd": "true"}]}
        logs = {"ok": self.tmp / "logs" / "ok.log"}
        got = {}
        K.run_checks([str(self.repo)], "f" * 40, cfg, logs, lambda n, st, rc, s: got.__setitem__(n, st))
        self.assertEqual(got, {"ok": "error"})


if __name__ == "__main__":
    unittest.main()
