import json
from subprocess import CompletedProcess, TimeoutExpired
import unittest
from unittest.mock import Mock, patch

from ticket_pilot import deploy, release
from ticket_pilot.release_checks import AwaitingTests, merge_deployment_pr, require_main_protection, verify


REPO = "bini59/toothpaste"
BASE, HEAD, TESTED, MERGED, TREE = (digit * 40 for digit in "abcde")
ADVANCED = "f" * 40
PR_URL = f"https://github.com/{REPO}/pull/54"
PASSED = {"status": "completed", "conclusion": "success"}
JOBS = ["oss-check", "unit (ubuntu-latest)", "unit (macos-latest)", "docker", "mode", "deployment-tests-passed"]


class MainProtectionTest(unittest.TestCase):
    def test_requires_the_strict_actions_check_without_bypass(self):
        rule = {
            "isAdminEnforced": True, "requiresStatusChecks": True, "requiresStrictStatusChecks": True,
            "bypassPullRequestAllowances": {"totalCount": 0},
            "requiredStatusChecks": [{"context": "deployment-tests-passed", "app": {"slug": "github-actions"}}],
        }
        for configured in (rule, None, dict(rule, isAdminEnforced=False), dict(rule, requiresStrictStatusChecks=False),
                           dict(rule, requiredStatusChecks=[]), dict(rule, bypassPullRequestAllowances={"totalCount": 1}),
                           dict(rule, requiredStatusChecks=[{"context": "deployment-tests-passed", "app": None}])):
            data = {"data": {"repository": {"ref": {"branchProtectionRule": configured}}}}
            with self.subTest(rule=configured), patch("ticket_pilot.core.run", return_value=Mock(stdout=json.dumps(data))) as run:
                if configured is rule:
                    require_main_protection()
                else:
                    with self.assertRaisesRegex(AwaitingTests, "main branch protection"):
                        require_main_protection()
                self.assertEqual(run.call_args.args[0][:3], ["gh", "api", "graphql"])
                self.assertIn('ref(qualifiedName: "refs/heads/main")', run.call_args.args[0][4])


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 54, "merged": True, "merged_at": "2024-09-01T12:00:00Z", "merge_commit_sha": MERGED,
            "head": {"ref": "staging", "sha": HEAD, "repo": {"full_name": REPO}},
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}},
        }
        self.event = {"action": "closed", "pull_request": self.pr}
        self.run = {
            **PASSED, "id": 701, "run_number": 42, "event": "pull_request", "head_branch": "staging",
            "head_sha": HEAD, "head_repository": {"full_name": REPO}, "pull_requests": [],
            "display_title": f"test PR #54 at {TESTED}", "updated_at": "2024-09-01T11:59:00Z",
        }
        self.runs = [self.run]
        self.jobs = [dict(PASSED, name=name, run_attempt=1) for name in JOBS]
        self.tested = {"parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}}
        self.merged = {"tree": {"sha": TREE}}
        self.comparison = {"status": "ahead"}
        self.responses = {
            "pulls/54": dict(self.pr, state="open", merged=False, merge_commit_sha=TESTED, draft=False, mergeable=True),
            f"actions/workflows/test.yml/runs?event=pull_request&branch=staging&head_sha={HEAD}&per_page=100":
                [{"workflow_runs": self.runs}],
            "actions/runs/701/jobs?filter=all&per_page=100": [{"jobs": self.jobs}],
            f"git/commits/{TESTED}": self.tested,
            f"git/commits/{MERGED}": self.merged,
            f"compare/{MERGED}...main?per_page=1": self.comparison,
        }
        api = patch("ticket_pilot.release_checks.gh_api", self.mock_api)
        api.start()
        self.addCleanup(api.stop)
        self.host_env = {"GH_TOKEN": "test-fixture"}
        auth = patch("ticket_pilot.core.gh_env", return_value=self.host_env)
        auth.start()
        self.addCleanup(auth.stop)
        run = patch("ticket_pilot.core.run", side_effect=AssertionError("Unexpected GitHub mutation"))
        run.start()
        self.addCleanup(run.stop)

    def mock_api(self, endpoint, *, paginate=False, env=None):
        if env is not None or "/pulls/" in endpoint:
            self.assertIs(env, self.host_env)
        self.assertTrue(endpoint.startswith(f"repos/{REPO}/"))
        response = self.responses[endpoint.removeprefix(f"repos/{REPO}/")]
        self.assertEqual(paginate, isinstance(response, list))
        return response


class VerifyReleaseTest(ReleaseFixture):
    def test_publishes_actual_main_commit_after_success(self):
        self.assertEqual(verify(self.event, REPO), MERGED)

    def test_rejects_fork_named_staging(self):
        self.pr["head"]["repo"]["full_name"] = "fork/toothpaste"
        with self.assertRaisesRegex(ValueError, "same-repository"):
            verify(self.event, REPO)

    def test_rejects_unmerged_pr_and_direct_push(self):
        self.pr["merged"] = False
        for event in (self.event, {"ref": "refs/heads/main", "after": MERGED}):
            with self.subTest(event=event), self.assertRaisesRegex(ValueError, "merged pull request"):
                verify(event, REPO)

    def test_rejects_missing_or_unrelated_test_results(self):
        for runs in (
            [],
            [dict(self.run, head_sha=BASE)],
            [dict(self.run, head_repository={"full_name": "fork/toothpaste"})],
            [dict(self.run, event="push")],
            [dict(self.run, display_title=f"test PR #53 at {TESTED}")],
        ):
            self.runs[:] = runs
            with self.subTest(runs=runs), self.assertRaisesRegex(ValueError, "no test run"):
                verify(self.event, REPO)

    def test_does_not_fall_back_to_older_success(self):
        self.runs.append(dict(self.run, id=702, run_number=43, conclusion="failure"))
        with self.assertRaisesRegex(ValueError, "702 did not succeed"):
            verify(self.event, REPO)

    def test_ignores_skipped_metadata_edit_after_success(self):
        self.runs.append(dict(self.run, id=702, run_number=43, display_title="PR metadata edited"))
        self.assertEqual(verify(self.event, REPO), MERGED)

    def test_rejects_pending_or_late_test_results(self):
        for result, reason in (
            ({"status": "in_progress"}, "still pending"),
            ({"updated_at": "2024-09-01T12:00:01Z"}, "before the merge"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, reason):
                self.runs[:] = [dict(self.run, **result)]
                verify(self.event, REPO)

    def test_rejects_missing_and_skipped_jobs(self):
        self.jobs.pop()
        with self.assertRaisesRegex(ValueError, "required job"):
            verify(self.event, REPO)
        self.jobs.append(dict(PASSED, conclusion="skipped", name="deployment-tests-passed", run_attempt=1))
        with self.assertRaisesRegex(ValueError, "required job"):
            verify(self.event, REPO)
        self.jobs[-1] = dict(self.jobs[-1], conclusion="success")
        self.jobs.remove(next(job for job in self.jobs if job["name"] == "unit (macos-latest)"))
        with self.assertRaisesRegex(ValueError, "required job"):
            verify(self.event, REPO)

    def test_uses_latest_attempt_of_each_job(self):
        previous = self.jobs.pop()
        self.jobs.extend([
            dict(previous, run_attempt=2, conclusion="success"),
            dict(previous, conclusion="failure"),
        ])
        self.assertEqual(verify(self.event, REPO), MERGED)

    def test_rejects_stale_tested_tree(self):
        self.merged["tree"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "merged contents"):
            verify(self.event, REPO)

    def test_rejects_commit_outside_main(self):
        self.comparison["status"] = "diverged"
        with self.assertRaisesRegex(ValueError, "not in main"):
            verify(self.event, REPO)


class HostMergeTest(ReleaseFixture):
    def setUp(self):
        super().setUp()
        self.open_pr = self.responses["pulls/54"]
        protection = patch("ticket_pilot.release_checks.require_main_protection", autospec=True)
        self.require_protection = protection.start()
        self.addCleanup(protection.stop)

    def github_merge(self, command, *, timeout, check):
        self.assertEqual(command, ["gh", "api", "--method", "PUT", f"repos/{REPO}/pulls/54/merge",
                                   "-f", f"sha={HEAD}", "-f", "merge_method=merge"])
        self.assertFalse(check)
        self.responses["pulls/54"] = dict(self.pr, state="closed")
        return CompletedProcess(command, 0, json.dumps({"merged": True, "sha": MERGED}), "")

    def test_host_merges_the_tested_head_and_returns_the_actual_merge_sha(self):
        with patch("ticket_pilot.core.run", side_effect=self.github_merge) as mutation:
            self.assertEqual(merge_deployment_pr(REPO, 54, base="main", method="merge"), MERGED)
        self.require_protection.assert_called_once()
        mutation.assert_called_once()
        self.assertEqual(verify(self.event, REPO), MERGED)

    def test_host_waits_for_protection_before_attempting_merge(self):
        self.require_protection.side_effect = AwaitingTests("Waiting for main branch protection")
        with patch("ticket_pilot.core.run") as mutation, self.assertRaisesRegex(AwaitingTests, "main branch protection"):
            merge_deployment_pr(REPO, 54, base="main", method="merge")
        mutation.assert_not_called()

    def test_host_fails_closed_if_github_refuses_the_merge_with_both_tips_current(self):
        result = CompletedProcess([], 1, "", "gh: update rejected by repository rules")
        with patch("ticket_pilot.core.run", return_value=result) as mutation:
            with self.assertRaisesRegex(RuntimeError, "update rejected by repository rules"):
                merge_deployment_pr(REPO, 54, base="main", method="merge")
        mutation.assert_called_once()
        self.assertIs(self.responses["pulls/54"], self.open_pr)

    def test_host_uses_normal_merge_for_other_prs(self):
        self.responses["pulls/54"]["head"] = {"ref": "staging", "repo": {"full_name": "fork/toothpaste"}}
        with patch("ticket_pilot.core.run") as mutation:
            self.assertIsNone(merge_deployment_pr(REPO, 54, base="main", method="merge"))
            self.assertIsNone(merge_deployment_pr("other/repo", 54, base="main", method="merge"))
        mutation.assert_not_called()

    def test_host_waits_for_new_or_missing_test_run(self):
        self.runs.clear()
        with self.assertRaises(AwaitingTests):
            merge_deployment_pr(REPO, 54, base="main", method="merge")
        self.runs.append(dict(self.run, status="queued", conclusion=None))
        with self.assertRaises(AwaitingTests):
            merge_deployment_pr(REPO, 54, base="main", method="merge")

    def test_host_waits_when_main_changes_the_candidate(self):
        self.responses["pulls/54"]["merge_commit_sha"] = MERGED
        with self.assertRaisesRegex(AwaitingTests, "current merge commit"):
            merge_deployment_pr(REPO, 54, base="main", method="merge")

    def test_host_waits_if_pr_candidate_has_stale_parents(self):
        self.responses["pulls/54"]["base"] = dict(self.pr["base"], sha=ADVANCED)
        with self.assertRaisesRegex(AwaitingTests, "current base and head"):
            merge_deployment_pr(REPO, 54, base="main", method="merge")

    def dashboard_merge(self, **pr_fields):
        info = {"state": "OPEN", "baseRefName": "main", "number": 54, "mergeable": "MERGEABLE",
                "isDraft": False, "statusCheckRollup": [], **pr_fields}
        rel = {"id": 1, "github": REPO, "repo": "toothpaste", "reason": None, "ticket_gid": None}
        state = {"pr": PR_URL, "into": "main", "method": "merge", "since": "2099-01-01T00:00:00+00:00"}
        save = Mock()
        with patch.multiple(
            deploy, gh_json=Mock(return_value=info), _ready_for_merge=Mock(), _save=save, POLL_S=-1,
            deploy_timeout_min=Mock(return_value=20), _gh=Mock(), event=Mock(), restage=Mock(), _row=Mock(),
            verify_deploy=Mock(return_value="deploying"),
        ):
            result = deploy._merge(None, rel, {"prod": {"branch": "main"}}, state, None)
            deploy._gh.assert_not_called()
        return result, save

    def train_merge(self):
        con = Mock()
        con.execute.return_value.fetchone.return_value = None
        ticket_url = f"https://github.com/{REPO}/pull/53"
        pr = {"number": 53, "key": "TP-1", "title": "Ticket", "url": ticket_url, "ticket_gid": None}
        rel = {"id": 1, "repo": "toothpaste", "github": REPO, "day": "2024-09-01", "mode": None,
               "prs_json": json.dumps([pr])}
        rules = {"staging": {"branch": "staging"}, "prod": {"branch": "main"}}
        github = Mock(side_effect=[
            {"sha": HEAD}, {"commits": [{"sha": HEAD}]}, {"commits": [{"oid": HEAD}]},
            [{"url": PR_URL}],
        ])
        finish = Mock()
        with patch.multiple(release, gh_json=github, repo_rules=Mock(return_value=rules), _gh=Mock(), event=Mock(),
                            _finish=finish):
            result = release._start(con, rel, Mock())
            github.assert_any_call(["api", f"repos/{REPO}/compare/main...{HEAD}"])
            release._gh.assert_not_called()
        return result, con, finish

    def test_dashboard_waits_if_github_has_not_listed_test_jobs(self):
        self.runs.clear()
        self.assertEqual(self.dashboard_merge()[0], "waiting for checks")

    def test_train_waits_if_checked_head_advanced_past_approved_head(self):
        self.responses["pulls/54"]["head"] = dict(self.pr["head"], sha=ADVANCED)
        with patch("ticket_pilot.core.run") as mutation:
            self.assertEqual(self.train_merge()[0], "waiting for staging recheck")
        mutation.assert_not_called()

    def test_host_paths_stop_if_pr_closes_or_is_retargeted_after_tests(self):
        original_pr = self.responses["pulls/54"]
        for changes, message in (
            ({"state": "closed"}, "no longer open"),
            ({"base": dict(original_pr["base"], ref="other")}, "no longer the same-repository"),
        ):
            def change_pr_after_tests(endpoint, **kwargs):
                response = self.mock_api(endpoint, **kwargs)
                if endpoint.endswith(f"/git/commits/{TESTED}"):
                    self.responses["pulls/54"] = dict(original_pr, **changes)
                return response

            with self.subTest(changes=changes), patch("ticket_pilot.release_checks.gh_api", side_effect=change_pr_after_tests):
                self.responses["pulls/54"] = original_pr
                with self.assertRaisesRegex(ValueError, message):
                    self.dashboard_merge()
                self.responses["pulls/54"] = original_pr
                result, con, finish = self.train_merge()
                self.assertEqual(result, "failed")
                self.assertIn(message, finish.call_args.args[-1])
                self.assertFalse(any("status='deploying'" in call.args[0] for call in con.execute.call_args_list))

    def test_dashboard_rejects_pr_already_merged_into_another_branch(self):
        with self.assertRaisesRegex(RuntimeError, "base가 other"):
            self.dashboard_merge(state="MERGED", baseRefName="other", mergeCommit={"oid": MERGED})

    def test_host_only_recovers_a_timeout_for_a_confirmed_pr_merge(self):
        for confirmed in (False, True):
            def lose_response(command, **kwargs):
                self.responses["pulls/54"] = (dict(self.pr, state="closed") if confirmed
                                               else dict(self.open_pr, state="closed"))
                raise TimeoutExpired(command, kwargs["timeout"])

            with self.subTest(confirmed=confirmed), patch("ticket_pilot.core.run", side_effect=lose_response):
                self.responses["pulls/54"] = self.open_pr
                if confirmed:
                    self.assertEqual(merge_deployment_pr(REPO, 54, base="main", method="merge"), MERGED)
                else:
                    with self.assertRaisesRegex(ValueError, "not confirmed merged"):
                        merge_deployment_pr(REPO, 54, base="main", method="merge")

    def test_host_rejects_api_success_for_pr_retargeted_after_validation(self):
        def retarget(*args, **kwargs):
            result = self.github_merge(*args, **kwargs)
            self.responses["pulls/54"]["base"] = dict(self.pr["base"], ref="other")
            return result

        with patch("ticket_pilot.core.run", side_effect=retarget):
            with self.assertRaisesRegex(ValueError, "no longer the same-repository"):
                merge_deployment_pr(REPO, 54, base="main", method="merge")

    def test_host_rejects_api_success_for_an_unexpected_merge_tree(self):
        self.merged["tree"]["sha"] = ADVANCED
        with patch("ticket_pilot.core.run", side_effect=self.github_merge):
            with self.assertRaisesRegex(ValueError, "did not test the merged contents"):
                self.dashboard_merge()
            self.responses["pulls/54"] = self.open_pr
            result, con, finish = self.train_merge()
        self.assertEqual(result, "failed")
        self.assertIn("did not test the merged contents", finish.call_args.args[-1])
        self.assertFalse(any("status='deploying'" in call.args[0] for call in con.execute.call_args_list))

    def test_host_paths_wait_if_main_advances_after_validation(self):
        def advance_main(command, **kwargs):
            self.responses["pulls/54"] = dict(self.open_pr, base=dict(self.pr["base"], sha=ADVANCED),
                                              merge_commit_sha=MERGED)
            return CompletedProcess(command, 1, "", "gh: Base branch was modified (HTTP 405)")

        for merge, expected in ((self.dashboard_merge, "waiting for checks"),
                                (self.train_merge, "waiting for deployment PR tests")):
            with self.subTest(merge=merge.__name__):
                self.responses["pulls/54"] = self.open_pr
                with patch("ticket_pilot.core.run", side_effect=advance_main) as mutation:
                    self.assertEqual(merge()[0], expected)
                mutation.assert_called_once()

    def test_train_waits_if_staging_advances_after_validation(self):
        def advance_staging(command, **kwargs):
            self.assertIn(f"sha={HEAD}", command)
            self.responses["pulls/54"] = dict(self.open_pr, head=dict(self.pr["head"], sha=ADVANCED))
            return CompletedProcess(command, 1, "", "gh: Head branch was modified (HTTP 409)")

        with patch("ticket_pilot.core.run", side_effect=advance_staging) as mutation:
            self.assertEqual(self.train_merge()[0], "waiting for staging recheck")
        mutation.assert_called_once()

    def test_host_paths_record_the_exact_landed_commit(self):
        with patch("ticket_pilot.core.run", side_effect=self.github_merge):
            result, save = self.dashboard_merge()
            self.assertEqual(result, "deploying")
            self.assertEqual(save.call_args.kwargs["merge_sha"], MERGED)
            self.responses["pulls/54"] = self.open_pr
            result, con, _ = self.train_merge()
            self.assertEqual(result, "deploying")
            self.assertEqual(con.execute.call_args.args[1][:2], (PR_URL, MERGED))
