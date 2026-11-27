import json
import unittest
from unittest.mock import Mock, patch

from ticket_pilot import deploy, release
from ticket_pilot.release_checks import AwaitingTests, tested_deployment_head, verify


REPO = "bini59/toothpaste"
BASE, HEAD, TESTED, MERGED, TREE = (digit * 40 for digit in "abcde")
PASSED = {"status": "completed", "conclusion": "success"}
JOBS = ["oss-check", "unit (ubuntu-latest)", "unit (macos-latest)", "docker", "mode"]


class VerifyReleaseTest(unittest.TestCase):
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
            "pulls/54": dict(self.pr, state="open", merge_commit_sha=TESTED),
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

    def mock_api(self, endpoint, *, paginate=False, env=None):
        if env is not None or "/pulls/" in endpoint:
            self.assertIs(env, self.host_env)
        self.assertTrue(endpoint.startswith(f"repos/{REPO}/"))
        response = self.responses[endpoint.removeprefix(f"repos/{REPO}/")]
        self.assertEqual(paginate, isinstance(response, list))
        return response

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
        self.jobs.append({"status": "completed", "conclusion": "skipped", "name": "mode", "run_attempt": 1})
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

    def test_host_pins_merge_to_checked_head(self):
        self.assertEqual(tested_deployment_head(REPO, 54), HEAD)
        self.assertIsNone(tested_deployment_head("other/repo", 54))

    def test_host_waits_for_new_or_missing_test_run(self):
        self.runs.clear()
        with self.assertRaises(AwaitingTests):
            tested_deployment_head(REPO, 54)
        self.runs.append(dict(self.run, status="queued", conclusion=None))
        with self.assertRaises(AwaitingTests):
            tested_deployment_head(REPO, 54)

    def test_host_waits_when_main_changes_the_candidate(self):
        self.responses["pulls/54"]["merge_commit_sha"] = MERGED
        with self.assertRaisesRegex(AwaitingTests, "current merge commit"):
            tested_deployment_head(REPO, 54)


class HostMergeTest(unittest.TestCase):
    def test_dashboard_waits_if_github_has_not_listed_test_jobs(self):
        info = {"state": "OPEN", "baseRefName": "main", "number": 54, "mergeable": "MERGEABLE",
                "isDraft": False, "statusCheckRollup": []}
        rel = {"id": 1, "github": REPO}
        state = {"pr": f"https://github.com/{REPO}/pull/54", "into": "main", "method": "merge",
                 "since": "2099-01-01T00:00:00+00:00"}
        with patch.multiple(
            deploy, gh_json=Mock(return_value=info), _ready_for_merge=Mock(), _save=Mock(), POLL_S=-1,
            deploy_timeout_min=Mock(return_value=20), _gh=Mock(),
            tested_deployment_head=Mock(side_effect=AwaitingTests("No test run yet")),
        ):
            self.assertEqual(deploy._merge(None, rel, {}, state, None), "waiting for checks")
            deploy._gh.assert_not_called()

    def test_train_waits_if_checked_head_advanced_past_approved_head(self):
        con = Mock()
        con.execute.return_value.fetchone.return_value = None
        ticket_url = f"https://github.com/{REPO}/pull/53"
        pr = {"number": 53, "key": "TP-1", "title": "Ticket", "url": ticket_url, "ticket_gid": None}
        rel = {"id": 1, "repo": "toothpaste", "github": REPO, "day": "2024-09-01", "mode": None,
               "prs_json": json.dumps([pr])}
        rules = {"staging": {"branch": "staging"}, "prod": {"branch": "main"}}
        github = Mock(side_effect=[
            {"sha": HEAD}, {"commits": [{"sha": HEAD}]}, {"commits": [{"oid": HEAD}]},
            [{"url": f"https://github.com/{REPO}/pull/54"}],
        ])
        with patch.multiple(release, gh_json=github, repo_rules=Mock(return_value=rules), _gh=Mock(),
                            tested_deployment_head=Mock(return_value=TESTED)):
            self.assertEqual(release._start(con, rel, Mock()), "waiting for staging recheck")
            github.assert_any_call(["api", f"repos/{REPO}/compare/main...{HEAD}"])
            release._gh.assert_not_called()
