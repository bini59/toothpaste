"""Production tracking for merged PRs that were not shipped by ticket-pilot itself.

A merged PR is not "done" until its change is in what production last deployed (2026-10-19).
Merged PRs (stage `merged`, or `staged` when they sit in a staging branch) are checked on every review precheck;
once the commit is in production this sets `prs.prod_at` and completes the Asana task (the owner approved Asana
completion after production verification for every repo, 2026-10-19), which unblocks dependent tickets.

How "deployed" is read, per repo (settings `repo.<name>.deploy.prod`):
  pipeline  latest Succeeded execution of the prod CodePipeline -> its source revision
  workflow  latest successful GitHub Actions run of the deploy workflow on the prod branch -> headSha
  release   (true) the latest GitHub Release's tag -> its commit (TP-186: installs update to releases, `tp update`)
  ecs       (optional) the ECS PRIMARY deployment must be COMPLETED and created after that pipeline execution started
A PR counts as deployed when its merge commit is an ancestor of the deployed sha, or (cherry-pick releases)
every one of its own non-merge commits has a patch-equivalent commit there (`git cherry` in the local clone).
Repos without a config are not tracked: their merged PRs stay in 배포 대기 until the Asana task is completed.
Nothing is deployed or merged from here.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import notifications as N
from .core import db, event, gh_json, now, repo_rules, run


def track_rc(rr: dict) -> dict:
    """deploy rule -> {prod_branch, pipeline|workflow|release, ecs?} for deployed_sha()."""
    p = rr["prod"]
    return {"prod_branch": p["branch"], **{k: p[k] for k in ("pipeline", "workflow", "release", "ecs") if p.get(k)}}


def track_config() -> dict[str, dict]:
    return {name: track_rc(rr) for name, rr in repo_rules().items()
            if rr["prod"].get("pipeline") or rr["prod"].get("workflow") or rr["prod"].get("release")}


def _aws(args: list[str]) -> dict:
    from .release import _aws as aws  # one place for the PYTHONHOME/PYTHONPATH workaround
    return aws(args)


def deployed_sha(github: str, rc: dict) -> tuple[str | None, str]:
    """(sha production runs now, how it was read). None = not deployed / still rolling out / unknown."""
    if rc.get("pipeline"):
        ex = _aws(["codepipeline", "list-pipeline-executions", "--pipeline-name", rc["pipeline"], "--max-items", "20"])
        ok = next((e for e in ex.get("pipelineExecutionSummaries", []) if e.get("status") == "Succeeded"), None)
        if not ok or not ok.get("sourceRevisions"):
            return None, f"{rc['pipeline']}: 성공한 실행 없음"
        sha = ok["sourceRevisions"][0]["revisionId"]
        if rc.get("ecs"):
            svc = _aws(["ecs", "describe-services", "--cluster", rc["ecs"]["cluster"], "--services", rc["ecs"]["service"]])
            dep = next((d for d in svc["services"][0]["deployments"] if d["status"] == "PRIMARY"), None)
            started = datetime.fromisoformat(ok["startTime"])
            if not dep or datetime.fromisoformat(dep["createdAt"]) < started or dep.get("rolloutState") != "COMPLETED":
                # still rolling out: fall back to nothing rather than claiming the new sha
                return None, f"{rc['pipeline']} {sha[:7]}: ECS 롤아웃 대기"
        return sha, f"{rc['pipeline']} {sha[:7]}"
    if rc.get("workflow"):
        runs = gh_json(["run", "list", "--repo", github, "--workflow", rc["workflow"], "--branch", rc.get("prod_branch", "main"),
                        "--status", "success", "-L", "1", "--json", "headSha"])
        if not runs:
            return None, f"{rc['workflow']}: 성공한 배포 없음"
        return runs[0]["headSha"], f"{rc['workflow']} {runs[0]['headSha'][:7]}"
    if rc.get("release"):
        rel = gh_json(["release", "list", "--repo", github, "--exclude-drafts", "--exclude-pre-releases", "-L", "1",
                       "--json", "tagName"])
        if not rel:
            return None, "GitHub Release 없음"
        tag = rel[0]["tagName"]
        return gh_json(["api", f"repos/{github}/commits/{tag}", "--jq", "{sha: .sha}"])["sha"], f"release {tag}"
    return None, "배포 확인 방법 없음"


def _ancestor(github: str, sha: str, of: str) -> bool:
    if sha == of:
        return True
    st = gh_json(["api", f"repos/{github}/compare/{of}...{sha}", "--jq", "{status: .status}"])
    return st.get("status") in ("behind", "identical")  # sha is behind `of` = already contained


def _cherry_picked(repo_path: str | None, github: str, number: int, head: str, deployed: str) -> bool:
    """True when every non-merge commit of the PR has a patch-equivalent commit in `deployed` (cherry-pick release)."""
    if not repo_path or not os.path.isdir(repo_path):
        return False
    commits = run(["gh", "api", f"repos/{github}/pulls/{number}/commits", "--paginate",
                   "--jq", ".[] | select((.parents | length) == 1) | .sha"], timeout=120).stdout.split()
    if not commits:
        return False
    # objects only: updates FETCH_HEAD, no branch or working tree changes in the clone
    run(["git", "-C", repo_path, "fetch", "-q", "--no-tags", "origin", deployed, f"refs/pull/{number}/head"],
        timeout=180)
    out = run(["git", "-C", repo_path, "cherry", deployed, head], timeout=60).stdout
    missing = {ln[2:].strip() for ln in out.splitlines() if ln.startswith("+ ")}
    return not any(c in missing for c in commits)


def track_deploys(log=print) -> int:
    from .pipeline import restage

    con = db()
    restage(con)  # a PR merged since the last scan gets stage `merged` first
    cfg = track_config()
    rows = con.execute("""SELECT p.*, t.key, rp.path repo_path FROM prs p JOIN tickets t ON t.gid=p.ticket_gid
                          LEFT JOIN repos rp ON rp.name=p.repo
                          WHERE p.state='MERGED' AND p.prod_at IS NULL AND p.release_id IS NULL
                            AND t.stage IN ('merged','staged')""").fetchall()
    cache: dict[str, tuple[str | None, str]] = {}
    shipped = 0
    for p in rows:
        try:
            if not p["merge_sha"] or not p["base"]:  # PRs merged before these columns existed
                info = gh_json(["pr", "view", p["url"], "--json", "baseRefName,mergedAt,mergeCommit"])
                con.execute("UPDATE prs SET base=?, merged_at=?, merge_sha=? WHERE url=?",
                            (info["baseRefName"], info["mergedAt"], (info.get("mergeCommit") or {}).get("oid"), p["url"]))
                p = con.execute("SELECT p.*, ? key, ? repo_path FROM prs p WHERE url=?", (p["key"], p["repo_path"], p["url"])).fetchone()
            rc = cfg.get(p["repo"])
            if not rc:
                continue
            if p["repo"] not in cache:
                cache[p["repo"]] = deployed_sha(p["github"], rc)
            sha, how = cache[p["repo"]]
            if not sha or sha == p["track_sha"]:  # nothing new deployed since the last check
                continue
            ok = _ancestor(p["github"], p["merge_sha"], sha) or _cherry_picked(p["repo_path"], p["github"], p["number"], p["head_sha"], sha)
            con.execute("UPDATE prs SET track_sha=?, prod_at=?, prod_note=? WHERE url=?",
                        (sha, now() if ok else None, how if ok else None, p["url"]))
            if ok:
                shipped += 1
                from .release import complete_asana
                err = complete_asana(con, p["ticket_gid"])
                event(con, "release", f"{p['key']}: PR #{p['number']} is live in production ({how})"
                                      + (f"; Asana 완료 처리 실패: {err}" if err else "; Asana completed"), p["ticket_gid"])
                N.add(con, "deploy", f"{p['key']} 프로덕션 반영", f"PR #{p['number']} ({how})", "#release", p["ticket_gid"])
                log(f"track {p['key']}: PR #{p['number']} live ({how})")
            con.commit()
        except Exception as e:  # noqa: BLE001
            con.rollback()
            event(con, "error", f"track {p['key']} PR #{p['number']}: {str(e)[:300]}", p["ticket_gid"])
            con.commit()
    restage(con)
    con.commit()
    log(f"track: {len(rows)} merged PR(s) waiting, {shipped} newly live")
    return shipped
