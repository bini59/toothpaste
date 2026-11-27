"""Verify the GitHub Actions results for a public deployment PR."""

from functools import partial
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode


FULL_TEST_JOBS = {"oss-check", "unit (ubuntu-latest)", "unit (macos-latest)", "docker", "mode"}
SHA_PATTERN = r"[0-9a-f]{40}"
PUBLIC_REPOSITORY = "bini59/toothpaste"


class AwaitingTests(ValueError):
    """GitHub has not reported a result for the current deployment candidate yet."""


class StagingAdvanced(AwaitingTests):
    """The release train needs to recheck the approved staging commits."""


def gh_api(endpoint, *, paginate=False, env=None):
    command = ["gh", "api", endpoint]
    if paginate:
        command += ["--paginate", "--slurp"]
    return json.loads(subprocess.check_output(command, text=True, timeout=30, env=env))


def passed(result):
    return result.get("status") == "completed" and result.get("conclusion") == "success"


def is_deployment_pr(pr, repository):
    head = pr["head"]
    base = pr["base"]
    return (head["ref"] == "staging" and base["ref"] == "main"
            and (head.get("repo") or {}).get("full_name") == repository
            and (base.get("repo") or {}).get("full_name") == repository)


def successful_test(pr, repository, api_get, *, merged_at=None):
    head_sha = pr["head"]["sha"]
    # The Actions API can return an empty pull_requests array even for same-repository PRs.
    # Match the run-name in test.yml to the PR number and recover the tested merge commit.
    name = re.compile(rf"test PR #{pr['number']} at ({SHA_PATTERN})")
    api = f"repos/{repository}"
    query = urlencode({"event": "pull_request", "branch": "staging", "head_sha": head_sha, "per_page": 100})
    pages = api_get(f"{api}/actions/workflows/test.yml/runs?{query}", paginate=True)
    candidates = [
        (run, match[1])
        for page in pages for run in page["workflow_runs"]
        if run["event"] == "pull_request" and run["head_branch"] == "staging" and run["head_sha"] == head_sha
        and (run.get("head_repository") or {}).get("full_name") == repository
        and (match := name.fullmatch(run["display_title"]))
    ]
    if not candidates:
        raise AwaitingTests(f"PR #{pr['number']} has no test run for head {head_sha}.")

    # Check the newest matching run before looking at its conclusion, so an older pass never masks a failure.
    run, tested_sha = max(candidates, key=lambda candidate: candidate[0]["run_number"])
    if run["status"] != "completed":
        raise AwaitingTests(f"Test run {run['id']} is still pending.")
    if not passed(run):
        raise ValueError(f"Test run {run['id']} did not succeed ({run['conclusion']}).")
    if merged_at and run["updated_at"] > merged_at:
        raise ValueError(f"Test run {run['id']} was not confirmed successful before the merge.")

    pages = api_get(f"{api}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", paginate=True)
    latest_jobs = {}
    for page in pages:
        for job in page["jobs"]:
            previous = latest_jobs.get(job["name"])
            if previous is None or job["run_attempt"] > previous["run_attempt"]:
                latest_jobs[job["name"]] = job
    if not all(passed(latest_jobs.get(name, {})) for name in FULL_TEST_JOBS):
        raise ValueError(f"Test run {run['id']} did not pass every required job.")

    return run, tested_sha


def merge_deployment_pr(repository, number, *, base, method, approved_head=None):
    """Land exactly the tested merge commit, or return None for other PRs."""
    if repository != PUBLIC_REPOSITORY or base != "main":
        return None
    # The host uses toothpaste's GitHub connection. Actions uses its job token directly.
    from .core import gh_env, run

    api_get = partial(gh_api, env=gh_env())
    api = f"repos/{repository}"
    pr = api_get(f"{api}/pulls/{number}")
    if pr["base"]["ref"] != base:
        raise ValueError(f"PR #{number} no longer targets {base}.")
    if not is_deployment_pr(pr, repository):
        if approved_head is not None:
            raise ValueError(f"PR #{number} is not the same-repository staging -> main PR.")
        return None
    if pr["state"] != "open":
        raise ValueError(f"PR #{number} is no longer open.")
    if pr["draft"] or pr["mergeable"] is not True:
        raise AwaitingTests(f"PR #{number} is not ready to merge.")
    if method != "merge":
        raise ValueError(f"PR #{number} requires a merge commit to preserve the tested contents.")
    head_sha, base_sha = pr["head"]["sha"], pr["base"]["sha"]
    if approved_head is not None and head_sha != approved_head:
        raise StagingAdvanced("staging advanced; rechecking approved commits")
    _, tested_sha = successful_test(pr, repository, api_get)
    if tested_sha != pr["merge_commit_sha"]:
        raise AwaitingTests(f"PR #{number} is waiting for a test of its current merge commit.")
    tested = api_get(f"{api}/git/commits/{tested_sha}")
    if [parent["sha"] for parent in tested["parents"]] != [base_sha, head_sha]:
        raise AwaitingTests(f"PR #{number} is waiting for a test of its current base and head.")

    current = api_get(f"{api}/pulls/{number}")
    if current["state"] != "open":
        raise ValueError(f"PR #{number} is no longer open.")
    if not is_deployment_pr(current, repository):
        raise ValueError(f"PR #{number} is no longer the same-repository staging -> main PR.")
    if current["head"]["sha"] != head_sha:
        raise StagingAdvanced("staging advanced; rechecking the deployment PR")
    if (current["draft"] or current["mergeable"] is not True
            or current["base"]["sha"] != base_sha or current["merge_commit_sha"] != tested_sha):
        raise AwaitingTests(f"PR #{number} is waiting for a mergeable, tested candidate.")

    # The PR merge API only pins the head. Require both branch tips in a single
    # fast-forward transaction. updateRefs cannot enforce the PR state or target.
    ref_updates = [
        {"name": "refs/heads/staging", "beforeOid": head_sha, "afterOid": head_sha, "force": False},
        {"name": "refs/heads/main", "beforeOid": base_sha, "afterOid": tested_sha, "force": False},
    ]
    request = {
        "query": "mutation($input: UpdateRefsInput!) { updateRefs(input: $input) { clientMutationId } }",
        "variables": {"input": {"repositoryId": pr["base"]["repo"]["node_id"], "refUpdates": ref_updates}},
    }
    try:
        result = run(["gh", "api", "graphql", "--input", "-"], input=json.dumps(request), timeout=30, check=False)
    except subprocess.TimeoutExpired as error:
        failure = str(error)
    else:
        if result.returncode == 0:
            return tested_sha
        failure = result.stderr.strip() or result.stdout.strip()

    # GitHub can return a generic error for a stale beforeOid. Read the refs to
    # distinguish contention, a lost response after success, and other failures.
    live_base = api_get(f"{api}/git/ref/heads/main")["object"]["sha"]
    if live_base == tested_sha:
        current = api_get(f"{api}/pulls/{number}")
        if (is_deployment_pr(current, repository) and current["merged"] is True
                and current["merge_commit_sha"] == tested_sha):
            return tested_sha
        raise ValueError(f"PR #{number} is not confirmed merged into main at {tested_sha}.")
    if live_base != base_sha:
        raise AwaitingTests(f"PR #{number}: main advanced before the merge. Waiting for new tests.")
    if api_get(f"{api}/git/ref/heads/staging")["object"]["sha"] != head_sha:
        raise StagingAdvanced("staging advanced; rechecking the deployment PR")
    raise RuntimeError(f"PR #{number}: GitHub could not land the tested merge commit: {failure[-300:]}")


def verify(event, repository):
    pr = event.get("pull_request")
    if event.get("action") != "closed" or not pr or pr["merged"] is not True:
        raise ValueError("Expected a merged pull request.")
    if not is_deployment_pr(pr, repository):
        raise ValueError("Expected a merged same-repository staging -> main PR.")

    head_sha = pr["head"]["sha"]
    merge_sha = pr["merge_commit_sha"]
    merged_at = pr["merged_at"]
    if not merged_at or not all(isinstance(sha, str) and re.fullmatch(SHA_PATTERN, sha)
                                for sha in (head_sha, merge_sha)):
        raise ValueError("The merge event is missing its final commit information.")

    run, tested_sha = successful_test(pr, repository, gh_api, merged_at=merged_at)
    api = f"repos/{repository}"
    tested = gh_api(f"{api}/git/commits/{tested_sha}")
    merged = gh_api(f"{api}/git/commits/{merge_sha}")
    if (len(tested["parents"]) != 2 or tested["parents"][1]["sha"] != head_sha
            or tested["tree"]["sha"] != merged["tree"]["sha"]):
        raise ValueError(f"Test run {run['id']} did not test the merged contents.")
    comparison = gh_api(f"{api}/compare/{merge_sha}...main?per_page=1")
    if comparison["status"] not in {"identical", "ahead"}:
        raise ValueError(f"Merge commit {merge_sha} is not in main.")

    print(f"Verified test run {run['id']} for PR #{pr['number']} and main commit {merge_sha}.")
    return merge_sha


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    try:
        sha = verify(event, os.environ["GITHUB_REPOSITORY"])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"sha={sha}", file=output)


if __name__ == "__main__":
    main()
