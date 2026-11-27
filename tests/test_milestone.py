"""Milestones (milestone.py): subtasks of an Asana parent tagged the milestone tag (default tp-milestone) go into milestone/<parent gid>,
and the milestone ticket ms:<parent>:<repo> ships them to staging as one ticket.
Run: python3 -m unittest discover tests"""
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from ticket_pilot import core, deploy, pr_tracking, review
from ticket_pilot import milestone as M
from ticket_pilot import notifications as N
from ticket_pilot import pipeline as P
from ticket_pilot import release as R

RULES = {"30_Api": {"train": True, "modes": ["staging"], "staging": {"branch": "staging"},
                               "prod": {"branch": "main"}},
         # Shop: staging-mode repo without a feature base (staging -> main -> production via the 배포 page)
         "14_Shop": {"modes": ["staging", "direct", "promote", "all"], "staging": {"branch": "staging"}, "via": "main",
                     "prod": {"branch": "production"}},
         "32_Ticket_pilot": {"modes": ["staging"], "staging": {"branch": "main"}, "prod": {"branch": "main"}}}
API = "30_Api"
MS = "ms:P1:30_Api"


class MilestoneTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO repos(name, github) VALUES (?, 'o/api'), ('32_Ticket_pilot', 'o/tp')", (API,))
        core.write_config(self.con, {"settings": {"deploy": {"repos": RULES}}, "project_map": {}})
        self.p = mock.patch.object(core, "_config_con", lambda: self.con)
        self.p.start()
        self.n = 0

    def tearDown(self):
        self.p.stop()
        core.config_cache_clear()
        self.con.close()

    def ticket(self, gid, parent="P1", in_ms=1, repo=API, tags=("tp",), stage=None):
        self.con.execute("""INSERT INTO tickets(gid,key,name,has_tag,assigned,deps_json,open_deps,repo_override,
                            parent_gid,parent_name,in_milestone,tags,stage,completed)
                            VALUES (?,?,?,?,1,'[]',0,?,?,'#13 PR mail',?,?,?,0)""",
                         (gid, gid, gid, 1 if "tp" in tags else 0, repo, parent, in_ms, json.dumps(list(tags)), stage))

    def pr(self, gid, state, base, head="h1", verdict=None, repo=API, merged_at=None, prod_at=None):
        self.n += 1
        rid = self.con.execute("INSERT INTO runs(ticket_gid,repo,state,ws_status) VALUES (?,?,'active','in-review')",
                               (gid, repo)).lastrowid
        url = f"u{gid}{self.n}"
        self.con.execute("""INSERT INTO prs(url,run_id,ticket_gid,repo,github,number,state,base,head_sha,mergeable,
                            merged_at,prod_at) VALUES (?,?,?,?,'o/api',?,?,?,?,'MERGEABLE',?,?)""",
                         (url, rid, gid, repo, self.n, state, base, head, merged_at, prod_at))
        if verdict:
            self.con.execute("INSERT INTO reviews(pr_url,head_sha,verdict) VALUES (?,?,?)", (url, head, verdict))
        return url

    def row(self, gid):
        return self.con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()

    def stage(self, gid):
        return P.compute_stage(self.con, self.row(gid))

    def restage(self):
        P.restage(self.con)

    # ------------------------------------------------------------ base branch

    def test_subtask_base_is_milestone_branch(self):
        self.ticket("A")
        self.ticket("B", in_ms=0)
        self.assertEqual(M.base_for(self.row("A"), API), "milestone/P1")
        self.assertEqual(M.base_for(self.row("B"), API), "staging")

    def test_github_landed_pr_survives_unlink_and_source_closure(self):
        parent, child = "gh:o/api#12", "gh:o/api#13"
        self.ticket(parent, parent=None, in_ms=0, tags=("tp-milestone",))
        self.ticket(child, parent=parent)
        with mock.patch.object(M, "_gh"):
            base = M.ensure(self.con, self.row(child), API)
        gid = M.ms_gid(parent, API)
        url = self.pr(child, "MERGED", base, merged_at="2026-11-01")
        self.con.execute("UPDATE tickets SET parent_gid=NULL,in_milestone=0,assigned=0,completed=1 WHERE gid=?", (child,))
        self.assertEqual(M.landed_parents(self.con)[child]["parent_gid"], parent)
        self.assertTrue(M.ready(self.con, parent))
        self.assertEqual([r["gid"] for r in M.subtasks(self.con, parent)], [child])
        with mock.patch.object(R, "_gh") as close:
            self.assertIsNone(M.complete(self.con, gid))
        self.assertEqual([c.args[0][2] for c in close.call_args_list], ["12"])
        self.assertIsNotNone(self.con.execute("SELECT prod_at FROM prs WHERE url=?", (url,)).fetchone()[0])

    def test_verified_github_close_retries_child_before_parent(self):
        parent, child = "gh:o/api#12", "gh:o/api#13"
        self.ticket(parent, parent=None, in_ms=0, tags=("tp-milestone",))
        self.ticket(child, parent=parent)
        with mock.patch.object(M, "_gh"):
            base = M.ensure(self.con, self.row(child), API)
        url = self.pr(child, "MERGED", base, merged_at="2026-11-01")
        with mock.patch.object(R, "_gh", side_effect=RuntimeError("offline")) as close:
            self.assertIn("offline", M.complete(self.con, M.ms_gid(parent, API)))
            self.assertEqual([c.args[0][2] for c in close.call_args_list], ["13"])
        self.assertEqual(core.meta_get(self.con, "github_milestone_close_retries"), {parent: {child: url}})
        with mock.patch.object(M, "db", return_value=self.con), mock.patch.object(R, "_gh") as close:
            M.tick(log=lambda *_: None)
            M.tick(log=lambda *_: None)
            self.assertEqual([c.args[0][2] for c in close.call_args_list], ["13", "12"])
        self.assertEqual(core.meta_get(self.con, "github_milestone_close_retries"), {})

    def test_github_deploy_rechecks_relation_after_request(self):
        gid = "gh:o/api#13"
        self.ticket(gid, parent=None, in_ms=0)
        url = self.pr(gid, "OPEN", "staging")
        rel = {"repo": API, "mode": "direct", "ticket_gid": gid, "prs_json": json.dumps([{"url": url}])}
        deploy._check_milestone_deploy(self.con, rel)
        self.con.execute("UPDATE tickets SET parent_gid='gh:o/api#12',in_milestone=1 WHERE gid=?", (gid,))
        with self.assertRaisesRegex(RuntimeError, "마일스톤 PR"):
            deploy._check_milestone_deploy(self.con, rel)

    def test_verified_github_parent_closes_if_undispatched_child_drops_out(self):
        parent, child, added = "gh:o/api#12", "gh:o/api#13", "gh:o/shop#1"
        self.ticket(parent, parent=None, in_ms=0, tags=("tp-milestone",))
        self.ticket(child, parent=parent)
        with mock.patch.object(M, "_gh"):
            base = M.ensure(self.con, self.row(child), API)
        self.pr(child, "MERGED", base, merged_at="2026-11-01")
        self.ticket(added, parent=parent, repo="14_Shop", stage="queued")
        with mock.patch.object(M, "db", return_value=self.con), mock.patch.object(R, "_gh") as close:
            self.assertIsNone(M.complete(self.con, M.ms_gid(parent, API)))
            self.assertFalse(self.row(parent)["completed"])
            self.con.execute("UPDATE tickets SET assigned=0 WHERE gid=?", (added,))
            M.tick(log=lambda *_: None)
            M.tick(log=lambda *_: None)
            self.assertEqual([c.args[0][2] for c in close.call_args_list], ["13", "12"])
        self.assertEqual(core.meta_get(self.con, "github_milestone_close_retries"), {})

    def test_asana_keeps_milestone_base_while_staging_is_disabled(self):
        rules = {API: {**RULES[API], "train": False, "pr_into_staging": True, "modes": ["direct"]}}
        core.write_config(self.con, {"settings": {"deploy": {"repos": rules}}, "project_map": {}})
        self.ticket("100", parent="99")
        self.assertEqual(M.base_for(self.row("100"), API), "milestone/99")
        self.assertEqual(self.stage("100"), "needs_staging")

    def test_external_github_milestone_worktree_names_are_unique(self):
        keys = [M.ms_gid(f"gh:example/{'long-name-' * 7}#{n}", repo)
                for n in (12, 13) for repo in ("frontend_api", "backend_api")]
        self.assertEqual(len({P.slug(key) for key in keys}), len(keys))

    def test_no_milestone_without_feature_base(self):
        self.ticket("A", repo="32_Ticket_pilot")
        self.assertIsNone(M.base_for(self.row("A"), "32_Ticket_pilot"))

    def test_staging_mode_repo_takes_milestones(self):
        self.ticket("A", repo="14_Shop")
        self.ticket("B", repo="14_Shop", in_ms=0)
        self.assertEqual(M.base_for(self.row("A"), "14_Shop"), "milestone/P1")
        self.assertIsNone(M.base_for(self.row("B"), "14_Shop"))  # ordinary Shop tickets: unchanged
        self.assertEqual(M.staging_of("14_Shop"), "staging")
        self.con.execute("INSERT INTO repos(name, github) VALUES ('14_Shop', 'o/shop')")
        self.pr("A", "OPEN", "staging", verdict="approve", repo="14_Shop")
        _, err = R.approval_target(self.con, self.row("A"))
        self.assertIn("milestone/P1", err)

    def test_open_staging_pr_retargeted_to_milestone(self):
        self.ticket("A")
        self.con.execute("INSERT INTO runs(ticket_gid,repo,state,branch,created_at) VALUES ('A',?,'active','tp/a','')",
                         (API,))
        r = self.con.execute("SELECT r.*, rp.github FROM runs r JOIN repos rp ON rp.name=r.repo").fetchone()
        listed = [{"number": 7, "url": "u7", "state": "OPEN", "isDraft": False, "headRefOid": "h", "title": "t",
                   "createdAt": "2026-10-01", "updatedAt": "x", "baseRefName": "staging", "mergedAt": None,
                   "mergeCommit": None, "mergeable": "MERGEABLE"}]
        with mock.patch.object(pr_tracking, "gh_json", return_value=listed), mock.patch.object(pr_tracking, "run") as run, \
                mock.patch.object(M, "ensure") as ensure:
            pr_tracking.refresh_prs(self.con, r, "tp/a")
        ensure.assert_called_once()
        self.assertIn("base=milestone/P1", run.call_args[0][0])
        self.assertEqual(self.con.execute("SELECT base FROM prs WHERE url='u7'").fetchone()["base"], "milestone/P1")

    # ------------------------------------------------------------ stages

    def test_stages(self):
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1")
        self.assertEqual(self.stage("A"), "milestone")
        self.ticket(MS, in_ms=0, tags=())
        self.con.execute("UPDATE tickets SET has_tag=1 WHERE gid=?", (MS,))
        self.assertEqual(self.stage(MS), "collecting")
        self.con.execute("UPDATE tickets SET skip=1 WHERE gid=?", (MS,))
        self.assertEqual(self.stage(MS), "skipped")

    def test_milestone_parent_is_never_dispatched(self):
        self.ticket("P1", parent=None, in_ms=0, tags=("tp", "tp-milestone"))
        self.assertEqual(self.stage("P1"), "open")

    def test_milestone_pr_staged_like_any_ticket(self):
        self.ticket(MS, in_ms=0)
        self.pr(MS, "MERGED", "staging")
        self.assertEqual(self.stage(MS), "staged")

    # ------------------------------------------------------------ auto-merge

    def test_approval_target_wants_milestone_base(self):
        self.ticket("A")
        self.pr("A", "OPEN", "staging", verdict="approve")
        _, err = R.approval_target(self.con, self.row("A"))
        self.assertIn("milestone/P1", err)
        self.ticket("B")
        self.pr("B", "OPEN", "milestone/P1", verdict="approve")
        pr, err = R.approval_target(self.con, self.row("B"))
        self.assertIsNone(err)

    def test_request_merges_once_per_head(self):
        self.ticket("A")
        self.pr("A", "OPEN", "milestone/P1", verdict="approve")
        self.ticket("B")
        self.pr("B", "OPEN", "milestone/P1", verdict="changes")
        self.restage()
        self.assertEqual(M.request_merges(self.con, log=lambda *_: None), 1)
        self.assertIsNotNone(self.row("A")["approve_requested_at"])
        self.assertIsNone(self.row("B")["approve_requested_at"])
        # the merge failed: same head is not requested again
        self.con.execute("UPDATE tickets SET approve_requested_at=NULL, approve_error='boom' WHERE gid='A'")
        self.restage()
        self.assertEqual(M.request_merges(self.con, log=lambda *_: None), 0)

    def test_blocked_subtask_merges_with_owners_reason(self):
        """TP-99: an AI 'blocked' subtask head is approved with a reason and merges into its milestone branch."""
        self.ticket("A")
        url = self.pr("A", "OPEN", "milestone/P1", verdict="blocked")
        self.restage()
        self.assertEqual(M.request_merges(self.con, log=lambda *_: None), 0)  # no auto-merge for blocked
        self.assertEqual(self.stage("A"), "reviewed")
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh") as gh:
            with self.assertRaises(SystemExit):
                R.request_approval("A")  # without a reason it stays refused
            R.request_approval("A", override_review="  실제 LLM으로 검증함  ")
        t = self.row("A")
        self.assertEqual((t["review_override"], t["review_override_sha"]), ("실제 LLM으로 검증함", "h1"))
        self.assertTrue(t["approve_requested_at"])
        self.assertEqual(gh.call_args[0][0][:3], ["pr", "comment", url])
        self.assertIn("실제 LLM으로 검증함", gh.call_args[0][0][4])
        self.assertIn("실제 LLM으로 검증함", self.con.execute(
            "SELECT message FROM events WHERE kind='review_override'").fetchone()["message"])
        self.assertEqual(self.stage("A"), "approving")
        live = {"state": "OPEN", "isDraft": False, "headRefOid": "h1", "baseRefName": "milestone/P1", "mergeable": "MERGEABLE"}
        done = {"state": "MERGED", "mergedAt": "2026-11-09T00:00:00Z", "mergeCommit": {"oid": "m1"},
                "baseRefName": "milestone/P1"}
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh"), \
                mock.patch.object(R, "gh_json", side_effect=[live, done]):
            self.assertEqual(R.process_approvals(log=lambda *_: None), 1)
        self.assertEqual(self.stage("A"), "milestone")

    def test_review_override_is_for_that_head_only(self):
        self.ticket("A")
        url = self.pr("A", "OPEN", "milestone/P1", verdict="changes")
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh"):
            R.request_approval("A", override_review="리뷰 지적은 이 티켓 범위 밖")
        self.assertIsNone(R.approval_target(self.con, self.row("A"))[1])
        self.assertEqual(self.stage("A"), "approving")
        # a new commit: the override no longer applies, and the AI 'blocked' on it needs a new reason
        self.con.execute("UPDATE prs SET head_sha='h2' WHERE url=?", (url,))
        self.con.execute("INSERT INTO reviews(pr_url,head_sha,verdict) VALUES (?, 'h2', 'blocked')", (url,))
        self.assertEqual(R.approval_target(self.con, self.row("A"))[1], R.NO_AI_OK)
        self.assertEqual(self.stage("A"), "reviewed")
        # an approve verdict cannot be "overridden" (nothing to override)
        self.con.execute("INSERT INTO reviews(pr_url,head_sha,verdict) VALUES (?, 'h2', 'approve')", (url,))
        self.con.execute("UPDATE tickets SET approve_requested_at=NULL WHERE gid='A'")
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh") as gh:
            R.request_approval("A", override_review="x")  # plain approval goes through, no override recorded
            gh.assert_not_called()
            R.request_approval("A", cancel=True)
        self.assertIsNone(self.row("A")["review_override"])

    def test_refused_review_override_leaves_no_trace(self):
        """A conflicting head: the approval is refused, so neither the override nor the PR comment happen."""
        self.ticket("A")
        url = self.pr("A", "OPEN", "milestone/P1", verdict="blocked")
        self.con.execute("UPDATE prs SET mergeable='CONFLICTING' WHERE url=?", (url,))
        self.con.commit()  # the refused request is not committed: the rollback below shows what the DB keeps
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh") as gh:
            with self.assertRaises(SystemExit):
                R.request_approval("A", override_review="검증함")
            gh.assert_not_called()
        self.con.rollback()
        self.assertIsNone(self.row("A")["review_override"])

    def test_failed_review_override_comment_refuses_the_approval(self):
        """The reason must be on the PR before it can merge: a failed comment leaves no approval or override."""
        self.ticket("A")
        self.pr("A", "OPEN", "milestone/P1", verdict="blocked")
        self.con.commit()
        with mock.patch.object(R, "db", lambda: self.con), \
                mock.patch.object(R, "_gh", side_effect=RuntimeError("HTTP 401")):
            with self.assertRaises(SystemExit) as cm:
                R.request_approval("A", override_review="검증함")
        self.assertIn("HTTP 401", str(cm.exception))
        t = self.row("A")
        self.assertIsNone(t["approve_requested_at"])
        self.assertIsNone(t["review_override"])
        self.assertIsNone(self.con.execute("SELECT 1 FROM events WHERE kind='review_override'").fetchone())
        self.assertEqual(self.stage("A"), "reviewed")
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh"):
            R.request_approval("A", override_review="검증함")  # retry once GitHub is back
        self.assertTrue(self.row("A")["approve_requested_at"])

    def test_failed_merge_drops_the_review_override(self):
        """Else request_merges would re-request the overridden head on its own after a failed merge."""
        self.ticket("A")
        self.pr("A", "OPEN", "milestone/P1", verdict="blocked")
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "_gh"):
            R.request_approval("A", override_review="검증함")
        with mock.patch.object(R, "db", lambda: self.con), mock.patch.object(R, "gh_json", side_effect=RuntimeError("boom")):
            self.assertEqual(R.process_approvals(log=lambda *_: None), 0)
        t = self.row("A")
        self.assertIsNone(t["review_override"])
        self.assertIn("boom", t["approve_error"])
        self.assertEqual(M.request_merges(self.con, log=lambda *_: None), 0)

    def test_staging_subtask_is_not_auto_merged(self):
        self.ticket("A", in_ms=0)
        self.pr("A", "OPEN", "staging", verdict="approve")
        self.restage()
        self.assertEqual(M.request_merges(self.con, log=lambda *_: None), 0)

    # ------------------------------------------------------------ milestone PR

    def test_ready_needs_every_tagged_subtask(self):
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1")
        self.ticket("B")
        self.pr("B", "OPEN", "milestone/P1")
        self.ticket("C", tags=())  # untagged (manual staging check): ignored
        self.restage()
        self.assertFalse(M.ready(self.con, "P1"))
        self.con.execute("UPDATE prs SET state='MERGED' WHERE ticket_gid='B'")
        self.restage()
        self.assertTrue(M.ready(self.con, "P1"))

    def test_ready_needs_something_in_the_branch(self):
        self.ticket("A")
        self.pr("A", "MERGED", "staging")  # merged to staging before the parent got the tag
        self.restage()
        self.assertFalse(M.ready(self.con, "P1"))

    # ------------------------------------------------------------ production

    # ------------------------------------------------------------ subtasks added later (2026-10-30)

    def test_subtask_added_while_milestone_pr_open(self):
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-10-30T01")
        self.ticket(MS, in_ms=0)
        self.pr(MS, "OPEN", "staging", verdict="approve")
        self.restage()
        self.assertEqual(self.stage(MS), "ms_hold")  # every subtask is in, but the owner has not pressed 작업 완료
        _, err = R.approval_target(self.con, self.row(MS))
        self.assertIn("작업 완료", err)
        self.assertEqual(P.review_queue(self.con), [])
        with mock.patch.object(M, "db", lambda: self.con):
            M.mark_done("P1")
        self.assertIsNone(R.approval_target(self.con, self.row(MS))[1])
        self.assertEqual(self.stage(MS), "reviewed")
        self.ticket("B", stage="queued")  # new subtask: back to 하위작업 진행 중, no staging approval
        self.restage()
        self.assertEqual(self.stage(MS), "collecting")
        _, err = R.approval_target(self.con, self.row(MS))
        self.assertIn("진행 중", err)
        self.assertEqual(P.review_queue(self.con), [])  # no AI review of a PR that is still growing
        # safety net: the 작업 완료 mark is gone, so B landing does not make it approvable again by itself
        self.assertEqual(M.reset_done(self.con, log=lambda *_: None), 1)
        self.assertIsNone(self.row(MS)["ms_done_at"])
        self.pr("B", "MERGED", "milestone/P1", merged_at="2026-10-30T02")
        self.restage()
        self.assertEqual(self.stage(MS), "ms_hold")

    def test_done_button_refused_while_subtasks_run(self):
        self.ticket("A", stage="in_progress")
        self.ticket(MS, in_ms=0)
        with mock.patch.object(M, "db", lambda: self.con):
            with self.assertRaises(SystemExit):
                M.mark_done("P1")
            self.con.execute("UPDATE tickets SET stage='milestone' WHERE gid='A'")
            M.mark_done("P1")
        self.restage()
        self.assertEqual(self.stage(MS), "collecting")  # tick opens the milestone PR now

    def test_next_round_after_the_milestone_went_live(self):
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-10-30T01")
        self.ticket(MS, in_ms=0)
        self.pr(MS, "MERGED", "staging", merged_at="2026-10-30T02", prod_at="2026-10-30T03")
        with mock.patch.object(R, "asana"):
            R.complete_asana(self.con, MS)
        self.restage()
        self.assertEqual(self.stage(MS), "done")
        self.ticket("B")
        self.pr("B", "MERGED", "milestone/P1", merged_at="2026-10-30T04")  # after the round went live
        self.restage()
        self.assertEqual(M.reopen(self.con, log=lambda *_: None), 1)
        self.restage()
        self.assertEqual(self.stage(MS), "ms_hold")  # next round waits for 작업 완료 again
        with mock.patch.object(M, "db", lambda: self.con):
            M.mark_done("P1")
        self.restage()
        self.assertEqual(self.stage(MS), "collecting")
        self.assertTrue(M.ready(self.con, "P1"))  # tick opens the next milestone PR
        self.assertEqual(self.row("B")["completed"], 0)

    def test_next_round_while_the_milestone_is_only_on_staging(self):
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-10-30T01")
        self.ticket(MS, in_ms=0)
        staged = self.pr(MS, "MERGED", "staging", merged_at="2026-10-30T02")  # staged, not live
        self.restage()
        self.assertEqual(self.stage(MS), "staged")
        self.ticket("B")
        self.pr("B", "MERGED", "milestone/P1", merged_at="2026-10-30T04")
        self.restage()
        self.assertEqual(self.stage(MS), "ms_hold")
        self.assertEqual(R.staged_pr_urls(self.con), {API: [staged]})
        self.assertTrue(M.awaiting_next_round(self.con, MS))
        prs_json = json.dumps([{"ticket_gid": MS, "url": staged}])
        self.con.execute("INSERT INTO releases(day,repo,github,status,prs_json) VALUES (?,?,?,'pending',?)",
                         ("2026-10-30", API, "o/api", prs_json))
        rel = self.con.execute("SELECT * FROM releases").fetchone()
        with mock.patch.object(R, "gh_json", return_value={"commits": [{"sha": "held-merge"}]}) as gh:
            self.assertEqual(R._start(self.con, rel, log=lambda _: None), "aborted")
        gh.assert_called_once_with(["api", "repos/o/api/compare/main...staging"])
        with mock.patch.object(M, "db", lambda: self.con):
            M.mark_done("P1")
        self.restage()
        self.assertEqual(self.stage(MS), "collecting")
        self.assertTrue(M.ready(self.con, "P1"))  # tick opens the second milestone PR
        self.pr(MS, "MERGED", "staging", merged_at="2026-10-30T05")
        self.restage()
        self.assertEqual(self.stage(MS), "staged")
        self.assertFalse(M.awaiting_next_round(self.con, MS))

    def test_round_completes_only_its_own_subtasks(self):
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-10-30T01")
        self.ticket("B")
        self.pr("B", "MERGED", "milestone/P1", merged_at="2026-10-30T05")  # merged after the milestone PR
        self.ticket(MS, in_ms=0)
        shipped = self.pr(MS, "MERGED", "staging", merged_at="2026-10-30T02")
        self.pr(MS, "OPEN", "staging")
        done = []
        with mock.patch.object(R, "asana", side_effect=lambda m, path, body=None: done.append(path)):
            R.complete_asana(self.con, MS, pr_url=shipped)
        self.assertEqual(done, ["/tasks/A"])
        self.assertFalse(self.row(MS)["completed"])

    def test_complete_subtasks_then_parent(self):
        self.ticket("P1", parent=None, in_ms=0, tags=("tp-milestone",))
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1")
        self.ticket("B", repo="31_Web", stage="skipped")
        self.ticket(MS, in_ms=0)
        self.ticket("ms:P1:31_Web", in_ms=0, repo="31_Web")
        done = []
        with mock.patch.object(R, "asana", side_effect=lambda m, path, body=None: done.append(path)):
            self.assertIsNone(R.complete_asana(self.con, MS))
            self.assertEqual(done, ["/tasks/A"])  # Web's milestone is not live yet: parent stays open
            self.assertIsNotNone(self.con.execute("SELECT prod_at FROM prs WHERE ticket_gid='A'").fetchone()["prod_at"])
            self.assertIsNone(R.complete_asana(self.con, "ms:P1:31_Web"))
        self.assertEqual(done, ["/tasks/A", "/tasks/P1"])
        self.assertEqual(self.row("P1")["completed"], 1)
        self.assertEqual(self.row("B")["completed"], 0)  # not merged into the milestone: not ours to complete


    # ------------------------------------------------------------ 수신함 alerts (TP-105)

    def _dbs(self):
        for mod in (P, M, N, review):
            p = mock.patch.object(mod, "db", lambda: self.con)
            p.start()
            self.addCleanup(p.stop)

    def alerts(self, kind=None):
        return [a for a in N.unread() if kind in (None, a["kind"])]

    def save_review(self, url, verdict="approve"):
        with tempfile.NamedTemporaryFile("w", suffix=".md") as f, mock.patch("builtins.print"):
            f.write("body")
            f.flush()
            P.review_save(url, "a" * 40, verdict, f.name, "요약", post=False)

    def test_review_alert_only_for_ordinary_tickets(self):
        self._dbs()
        self.ticket("T", parent=None, in_ms=0)
        self.con.execute("UPDATE tickets SET url='https://issue/T' WHERE gid='T'")
        url = self.pr("T", "OPEN", "staging")
        self.ticket("A")  # milestone subtask
        sub = self.pr("A", "OPEN", "milestone/P1")
        self.ticket(MS, in_ms=0)  # milestone PR ticket
        ms = self.pr(MS, "OPEN", "staging")
        self.save_review(url, "changes")
        self.save_review(sub)
        self.save_review(ms)
        [a] = self.alerts()
        self.assertEqual((a["kind"], a["title"], a["body"], a["pr_url"], a["ticket_key"], a["ticket_url"]),
                         ("review", "T PR #1 AI 리뷰: 수정 요청", "요약", url, "T", "https://issue/T"))
        self.assertEqual(N.unread_count(), 1)

    def test_review_alert_opens_board_or_milestone(self):
        self._dbs()
        core.write_config(self.con, {"settings": {"deploy": {"repos": RULES}}, "project_map": {
            "repos": {API: {"github": "o/api"}, "Loose": {"github": "o/loose"}},
            "projects": [{"id": "pul sar", "name": "P", "source": "asana", "asana_gid": "G1", "repos": [{"repo": API}]},
                         {"id": "docs", "name": "D", "source": "asana", "asana_gid": "G2", "repos": []}]}})
        self.con.execute("INSERT INTO project_map(project_gid,project_id) VALUES ('G2','docs')")
        self.ticket("T", parent=None, in_ms=0)
        self.ticket("A")  # milestone subtask
        self.ticket(MS, in_ms=0)  # milestone PR ticket
        urls = {}
        for gid in ("T", "A", MS):
            urls[gid] = self.pr(gid, "OPEN", "staging")
            N.add(self.con, "review", gid, "b", urls[gid], gid)  # what review_save would store (it skips milestone work)
        href = lambda: {a["ticket_gid"]: a["href"] for a in self.alerts()}
        self.assertEqual(href(), {"T": "#projects/pul%20sar/board", "A": "#milestones/P1", MS: "#milestones/P1"})
        self.assertEqual(self.alerts()[-1]["pr_url"], urls["T"])
        self.con.execute("UPDATE tickets SET project_gid='G2' WHERE gid='T'")  # its Asana project's tp project wins
        self.assertEqual(href()["T"], "#projects/docs/board")
        self.con.execute("UPDATE tickets SET project_gid=NULL WHERE gid='T'")
        self.con.execute("UPDATE prs SET repo='Loose' WHERE ticket_gid='T'")  # a repo in no project: 미분류
        self.assertEqual(href()["T"], f"#projects/{core.UNSORTED}/board")
        self.con.execute("UPDATE prs SET repo='Unknown' WHERE ticket_gid='T'")  # no board shows it: the PR
        self.assertEqual(href()["T"], urls["T"])

    def test_ms_ready_once_and_again_after_a_subtask_reopens(self):
        self._dbs()
        self.ticket("A", stage="in_progress")
        self.ticket(MS, in_ms=0)
        self.restage()
        self.assertEqual(M.notify_ready(self.con), 0)  # still in progress
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-11-10T01")
        self.con.execute("UPDATE tickets SET stage=NULL WHERE gid='A'")
        self.restage()
        self.assertEqual([M.notify_ready(self.con) for _ in range(3)], [1, 0, 0])  # repeated ticks: one alert
        [a] = self.alerts("ms_ready")
        self.assertEqual((a["href"], a["ticket_gid"]), ("#milestones/P1", MS))
        self.assertIn("마일스톤 작업 완료 처리 필요", a["title"])
        self.ticket("B", stage="queued")  # a subtask in progress again re-arms it
        self.restage()
        self.assertEqual(M.notify_ready(self.con), 0)
        self.pr("B", "MERGED", "milestone/P1", merged_at="2026-11-10T02")
        self.con.execute("UPDATE tickets SET stage=NULL WHERE gid='B'")
        self.restage()
        self.assertEqual(M.notify_ready(self.con), 1)
        self.assertEqual(len(self.alerts("ms_ready")), 1)  # the first one went read when B reopened it
        M.mark_done("P1")  # pressed from the 마일스톤 page: the alert is done too
        self.assertEqual(self.alerts(), [])

    def test_no_ms_ready_for_a_skipped_milestone(self):
        self._dbs()
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-11-10T01")
        self.ticket(MS, in_ms=0)
        self.con.execute("UPDATE tickets SET skip=1 WHERE gid=?", (MS,))
        self.restage()
        self.assertEqual(M.notify_ready(self.con), 0)

    def test_no_ms_ready_after_done_pressed(self):
        self._dbs()
        self.ticket("A")
        self.pr("A", "MERGED", "milestone/P1", merged_at="2026-11-10T01")
        self.ticket(MS, in_ms=0)
        self.restage()
        M.mark_done("P1")
        self.assertEqual(M.notify_ready(self.con), 0)
        self.assertEqual(self.alerts(), [])

    def test_read_and_read_all_upto(self):
        self._dbs()
        for i in range(3):
            N.add(self.con, "review", f"t{i}", "b", "#x")
        ids = [a["id"] for a in self.alerts()]  # newest first
        N.read(ids[0])
        self.assertEqual(N.unread_count(), 2)
        N.add(self.con, "review", "came in after the 수신함 loaded", "b", "#x")
        N.read_all(ids[1])
        self.assertEqual([a["title"] for a in self.alerts()], ["came in after the 수신함 loaded"])
        for bad in (None, "3", -1, True):
            with self.assertRaises(SystemExit):
                N.read(bad)

if __name__ == "__main__":
    unittest.main()
