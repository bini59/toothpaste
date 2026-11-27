"""Staging merge approvals + the weekday production release train + shared deploy verification.

Flow (2026-10-19):
  AI review "approve"  ->  the owner presses "staging 머지 승인" on the dashboard (request stored in the DB)
  ->  host tick merges that PR into the repo's staging branch (merge commit)                 stage: approving -> staged
      (any repo whose deploy rule has a staging branch and the `staging` mode, settings repo.<name>.deploy)
  ->  weekdays 10:00 KST: for each train repo (deploy.repos.*.train, config order) with staged PRs:
        abort if staging holds anything besides those PRs, or the staging pipeline is not green,
        else open + merge `staging -> main`, wait for the prod pipeline (+ ECS rollout), smoke-check,
        mark PRs released and complete the Asana tasks                                       stage: staged -> done
  A failure or abort stops the rest of that day; nothing is rolled back automatically.

Everything here is deterministic and runs on the host from the 5-minute automation prechecks (tp tick)
(`tp precheck-dispatch` / `tp precheck-review`); the dashboard only writes approval requests / the hold flag.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import checks as K
from . import milestone as M
from . import notifications as N
from .core import asana, current_actor, db, event, gh_json, meta_get, now, repo_rules, run, settings, train_repos
from .pipeline import active_deploy, active_release_pr_urls, restage
from .pr_tracking import VIOLATION, latest_run, mirror_orca, run_pr
from .release_checks import AwaitingTests, PUBLIC_REPOSITORY, StagingAdvanced, merge_deployment_pr
from .review import review_overridden


def cfg() -> dict:
    return settings()["release"]


def _tz():
    return ZoneInfo(cfg().get("timezone", "Asia/Seoul"))


def _gh(args: list[str], cwd: str | None = None, timeout: int = 180):
    return run(["gh", *args], cwd=cwd, timeout=timeout)


def _aws(args: list[str]) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}  # aws CLI ships its own Python
    p = run(["aws", *args, "--region", "us-east-1", "--output", "json"], timeout=120, env=env)
    return json.loads(p.stdout or "{}")


def next_release_at(t: datetime | None = None) -> datetime:
    c = cfg()
    t = t or datetime.now(_tz())
    hh, mm = map(int, c["at"].split(":"))
    d = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if d <= t:
        d += timedelta(days=1)
    while d.weekday() not in c["weekdays"]:
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------- approvals

def staging_branch(repo: str | None) -> str | None:
    rr = repo_rules(repo) if repo else None
    return rr["staging"]["branch"] if rr and rr["staging"] and "staging" in rr["modes"] else None


def approval_target(con, t):
    """Return (pr, error). Shared by the dashboard (validation) and the host merge."""
    if t["gid"].startswith("notion:") and t["milestone_error"]:
        return None, t["milestone_error"]
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not pr or pr["state"] != "OPEN":
        return None, "열린 PR이 없어요"
    if err := M.staging_config_error(t, pr["repo"]):
        return None, err
    stg = staging_branch(pr["repo"])
    if not stg:
        return None, f"{pr['repo']}는 staging 머지를 쓰지 않아요 (배포 규칙)"
    # a milestone subtask merges into its milestone branch (milestone.py), everything else into staging
    ms = M.base_for(t, pr["repo"])
    want = ms if ms and ms.startswith(M.PREFIX) else stg
    if pr["base"] != want:
        return None, f"PR base가 {pr['base'] or '?'}예요 ({want}만 가능)"
    if M.is_ms(t["gid"]):
        busy = M.unfinished(con, t["parent_gid"])
        if busy:
            return None, (f"하위작업 {len(busy)}개가 아직 진행 중이에요 ({', '.join(s['key'] for s in busy[:3])}). "
                          "모두 마일스톤에 들어오면 다시 승인해 주세요")
        if not t["ms_done_at"]:
            return None, "마일스톤 화면에서 '마일스톤 작업 완료'를 먼저 눌러 주세요"
    # the AI review of the head; a tp check failure (source='check') is the check gate's business below
    rv = con.execute("SELECT verdict FROM reviews WHERE pr_url=? AND head_sha=? AND source IS NULL ORDER BY id DESC LIMIT 1",
                     (pr["url"], pr["head_sha"])).fetchone()
    if not rv or rv["verdict"] != "approve" and not review_overridden(t, pr):
        return None, NO_AI_OK
    if pr["mergeable"] == "CONFLICTING":
        return None, CONFLICT_MSG.format(branch=want)
    # tp's own checks on the head (checks.py); repos without checks are not gated. Kept last: request_approval
    # lets the owner override exactly this error with a reason.
    err = K.gate_error(con, pr, override_sha=t["check_override_sha"] if "check_override_sha" in t.keys() else None)
    if err:
        return None, err
    return pr, None


NO_AI_OK = "최신 커밋에 AI '괜찮음' 판정이 없어요"


CONFLICT_MSG = "{branch}와 충돌해요. 충돌을 풀고 재리뷰를 받은 뒤 다시 승인해 주세요"
# gh pr merge output when GitHub cannot build the merge commit (mergeable was stale/UNKNOWN at check time)
_CONFLICT_HINTS = ("not mergeable", "cannot be cleanly created", "merge conflict")
# GitHub usually computes mergeability within a minute; give up on an approval that stays UNKNOWN this long
UNKNOWN_GIVE_UP_MIN = 30


class _Retry(Exception):
    """Not an error: keep the approval request and try again next tick."""


def _ticket(con, ticket_gid: str):
    t = con.execute("SELECT * FROM tickets WHERE gid=? OR lower(key)=lower(?)", (ticket_gid, ticket_gid)).fetchone()
    if not t:
        raise SystemExit("ticket not found")
    return t


def _override(con, t, reason: str):
    """Record the owner's reason to ignore the tp checks of the open PR's head (that commit only). The head's failure is
    not sent to the agent (if it was not yet), and the review queue hands the head to the AI review."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not pr or pr["state"] != "OPEN":
        raise SystemExit("열린 PR이 없어요")
    err = K.gate_error(con, pr)
    if not err:
        raise SystemExit("이 커밋은 tp 검사를 통과해서 무시할 게 없어요")
    con.execute("UPDATE tickets SET check_override=?, check_override_sha=? WHERE gid=?", (reason, pr["head_sha"], t["gid"]))
    K.withdraw(con, pr)
    event(con, "check_override", f"{t['key']}: PR #{pr['number']} ({pr['head_sha'][:7]}) checks ignored despite "
                                 f"'{err}': {reason}", t["gid"])


def override_checks(ticket_gid: str, reason: str):
    """Dashboard 'ignore checks' on a head that failed (or that tp could not check): it goes to the AI review instead
    of back to the agent. Merging still needs an AI approve and the approval button."""
    reason = (reason or "").strip()
    if not reason:
        raise SystemExit("사유를 적어 주세요")
    con = db()
    _override(con, _ticket(con, ticket_gid), reason)
    restage(con)
    con.commit()


def _override_review(con, t, reason: str):
    """Record the owner's reason to merge the open PR's head although the AI review said 'blocked' or 'changes'
    (TP-99; that commit only, a new commit needs a new review). Logged as an event and a PR comment."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not pr or pr["state"] != "OPEN":
        raise SystemExit("열린 PR이 없어요")
    if (r["ws_comment"] or "").startswith(VIOLATION):
        raise SystemExit("규칙 위반으로 멈춘 티켓은 답장하거나 그만두기로 정해 주세요")
    rv = con.execute("SELECT verdict FROM reviews WHERE pr_url=? AND head_sha=? AND source IS NULL ORDER BY id DESC LIMIT 1",
                     (pr["url"], pr["head_sha"])).fetchone()
    if not rv or rv["verdict"] not in ("blocked", "changes"):
        raise SystemExit("최신 커밋의 AI 판정이 '막힘'이나 '수정 필요'일 때만 사유를 적고 승인할 수 있어요")
    con.execute("UPDATE tickets SET review_override=?, review_override_sha=? WHERE gid=?", (reason, pr["head_sha"], t["gid"]))
    event(con, "review_override", f"{t['key']}: PR #{pr['number']} ({pr['head_sha'][:7]}) approved despite the AI "
                                  f"verdict '{rv['verdict']}': {reason}", t["gid"])
    return pr, rv["verdict"]


def _comment_review_override(con, pr, verdict: str, reason: str):
    """Posted last, right before the commit: a refused approval rolls back, a GitHub comment would not. The reason
    must be on the PR before it can merge, so a failed comment refuses (rolls back) the whole approval."""
    try:
        _gh(["pr", "comment", pr["url"], "--body",
             f"**ticket-pilot:** {current_actor() or settings()['owner_name']} approved `{pr['head_sha'][:7]}` despite the AI review verdict "
             f"`{verdict}`.\n\n**Reason:** {reason}\n\nThis applies to this commit only."])
    except Exception as e:  # noqa: BLE001
        con.rollback()
        raise SystemExit(f"PR에 승인 사유 코멘트를 남기지 못해 승인하지 않았어요. 다시 시도해 주세요: {str(e)[:200]}") from e


def request_approval(ticket_gid: str, cancel: bool = False, override: str | None = None,
                     override_review: str | None = None):
    """Dashboard approval. override = the owner's reason to merge a head whose tp checks did not pass; override_review =
    their reason to merge a head the AI review did not approve ('blocked'/'changes'). Both apply to that head only."""
    con = db()
    t = _ticket(con, ticket_gid)
    if cancel:
        con.execute("UPDATE tickets SET approve_requested_at=NULL, check_override=NULL, check_override_sha=NULL, "
                    "review_override=NULL, review_override_sha=NULL WHERE gid=?", (t["gid"],))
    else:
        _, err = approval_target(con, t)
        override_review = (override_review or "").strip()
        overrode = None
        if err == NO_AI_OK and override_review:
            overrode = _override_review(con, t, override_review)
            t = con.execute("SELECT * FROM tickets WHERE gid=?", (t["gid"],)).fetchone()
            _, err = approval_target(con, t)
        override = (override or "").strip()
        if err and override:
            r = latest_run(con, t["gid"])
            pr = r and run_pr(con, r["id"])
            if not pr or err != K.gate_error(con, pr):
                raise SystemExit(err)  # only the tp check gate can be overridden
            _override(con, t, override)
            t = con.execute("SELECT * FROM tickets WHERE gid=?", (t["gid"],)).fetchone()
            _, err = approval_target(con, t)
        if err:
            raise SystemExit(err)
        con.execute("UPDATE tickets SET approve_requested_at=?, approve_error=NULL WHERE gid=?", (now(), t["gid"]))
        event(con, "approve", f"{t['key']}: staging merge approved on the dashboard", t["gid"])
    restage(con)
    if not cancel and overrode:
        _comment_review_override(con, *overrode, override_review)
    con.commit()


def _mark_conflict(con, pr):
    con.execute("UPDATE prs SET mergeable='CONFLICTING' WHERE url=?", (pr["url"],))


def process_approvals(log=print) -> int:
    con = db()
    rows = con.execute("SELECT * FROM tickets WHERE approve_requested_at IS NOT NULL").fetchall()
    merged = 0
    for t in rows:
        pr, err = approval_target(con, t)
        r = latest_run(con, t["gid"])
        stg = pr["base"] if pr else None  # approval_target checked it: staging, or the subtask's milestone branch
        if not err:
            try:
                cwd = r["worktree_path"] if r and r["worktree_path"] and os.path.isdir(r["worktree_path"]) else None
                live = gh_json(["pr", "view", pr["url"], "--json", "state,isDraft,headRefOid,baseRefName,mergeable"], cwd=cwd)
                if live["headRefOid"] != pr["head_sha"]:
                    raise RuntimeError("승인 후 새 커밋이 올라왔어요. 재리뷰 후 다시 승인해 주세요")
                if live["baseRefName"] != stg:
                    raise RuntimeError(f"PR base가 {live['baseRefName']}로 바뀌었어요")
                if live["mergeable"] == "CONFLICTING":
                    _mark_conflict(con, pr)
                    raise RuntimeError(CONFLICT_MSG.format(branch=stg))
                if live["mergeable"] != "MERGEABLE":
                    # UNKNOWN: GitHub is still computing (e.g. staging just moved). The query starts the
                    # computation, so the next 5-minute tick normally gets a real answer.
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(t["approve_requested_at"].replace("Z", "+00:00"))
                    if age.total_seconds() < UNKNOWN_GIVE_UP_MIN * 60:
                        raise _Retry(f"mergeable={live['mergeable']}")
                    raise RuntimeError(f"{UNKNOWN_GIVE_UP_MIN}분 동안 GitHub가 충돌 여부를 계산하지 못했어요. 다시 승인해 주세요")
                if live["isDraft"]:
                    _gh(["pr", "ready", pr["url"]], cwd=cwd)
                try:
                    _gh(["pr", "merge", pr["url"], "--merge"], cwd=cwd)
                except Exception as e:  # noqa: BLE001
                    if any(h in str(e).lower() for h in _CONFLICT_HINTS):
                        _mark_conflict(con, pr)
                        raise RuntimeError(CONFLICT_MSG.format(branch=stg)) from e
                    raise
                info = gh_json(["pr", "view", pr["url"], "--json", "state,mergedAt,mergeCommit,baseRefName"], cwd=cwd)
                if info["state"] != "MERGED":
                    raise RuntimeError(f"merge did not complete (state={info['state']})")
                con.execute("UPDATE prs SET state='MERGED', is_draft=0, base=?, merged_at=?, merge_sha=?, mergeable=NULL WHERE url=?",
                            (info["baseRefName"], info["mergedAt"], (info.get("mergeCommit") or {}).get("oid"), pr["url"]))
                # staging moved: the other open PRs' mergeability is stale until the next scan re-reads it
                con.execute("UPDATE prs SET mergeable=NULL WHERE repo=? AND state='OPEN' AND base=?",
                            (pr["repo"], stg))
                con.execute("UPDATE runs SET state='finished', updated_at=? WHERE id=?", (now(), r["id"]))
                con.execute("UPDATE tickets SET approve_requested_at=NULL, approve_error=NULL WHERE gid=?", (t["gid"],))
                rr = repo_rules(pr["repo"])
                nxt = ("ships with its milestone PR" if stg.startswith(M.PREFIX)
                       else f"next release {next_release_at():%m/%d %H:%M}" if pr["repo"] in train_repos()
                       else f"{stg} is production; {rr['prod'].get('workflow') or 'deploy'} ships it" if stg == rr["target"]
                       else "prod via the 배포 page")
                event(con, "merge", f"{t['key']}: PR #{pr['number']} merged into {stg} ({nxt})", t["gid"])
                merged += 1
                log(f"approved {t['key']}: PR #{pr['number']} merged into {stg}")
                if r:
                    mirror_orca(r, "completed", f"PR #{pr['number']} merged to {stg}; waits for prod release")
                con.commit()
                continue
            except _Retry as e:
                log(f"approve {t['key']}: PR #{pr['number']} {e}; retry next tick")
                con.commit()
                continue
            except Exception as e:  # noqa: BLE001
                err = str(e)[:400]
        # the review override goes too (TP-99): else milestone.request_merges re-requests the head on its own
        # and a 'changes' head skips the rework; the owner approves again with a reason
        con.execute("UPDATE tickets SET approve_requested_at=NULL, approve_error=?, review_override=NULL, "
                    "review_override_sha=NULL WHERE gid=?", (err, t["gid"]))
        event(con, "error", f"{t['key']}: staging merge failed: {err}", t["gid"])
        log(f"ERROR approve {t['key']}: {err}")
        con.commit()
    restage(con)
    con.commit()
    return merged


# ---------------------------------------------------------------- release train

def staged_by_repo(con, repos: list[str] | None = None) -> dict[str, list]:
    """PRs merged into each repo's staging branch and still awaiting production."""
    rules = repo_rules()
    repos = train_repos() if repos is None else repos
    out: dict[str, list] = {}
    for name in repos:
        rule = rules.get(name)
        if not rule or not rule["staging"]:
            continue
        rows = con.execute("""SELECT p.*, t.key, t.name tname, t.gid tgid FROM prs p
                              JOIN tickets t ON t.gid=p.ticket_gid
                              WHERE p.repo=? AND p.base=? AND p.state='MERGED'
                                AND p.release_id IS NULL AND p.prod_at IS NULL
                              ORDER BY p.merged_at""", (name, rule["staging"]["branch"])).fetchall()
        if rows:
            out[name] = rows
    return out


def staged_pr_urls(con, repos: list[str] | None = None) -> dict[str, list[str]]:
    return {name: [pr["url"] for pr in prs] for name, prs in staged_by_repo(con, repos).items()}


def _today() -> str:
    return datetime.now(_tz()).strftime("%Y-%m-%d")


def _finish(con, rel, status, reason):
    prev = con.execute("SELECT status FROM releases WHERE id=?", (rel["id"],)).fetchone()
    if status in ("done", "failed") and rel["repo"] and not (prev and prev["status"] in ("done", "failed")):
        N.deploy(con, rel, status, reason)  # once per release, even if a later step calls _finish again
    con.execute("UPDATE releases SET status=?, reason=?, finished_at=? WHERE id=?", (status, reason, now(), rel["id"]))
    kind = "release" if status == "done" else "error"
    event(con, kind, f"release {rel['day']} {rel['repo']}: {status}{' - ' + reason if reason else ''}")
    con.commit()


def expire_stale_train(con) -> int:
    """release_tick only runs today's train, so pending train rows of past days never start; cancel them."""
    rows = con.execute("SELECT id, day, repo FROM releases WHERE mode IS NULL AND status='pending' AND day<?",
                       (_today(),)).fetchall()
    for r in rows:
        con.execute("UPDATE releases SET status='cancelled', step='취소됨', reason=?, finished_at=? WHERE id=?",
                    ("그날 트레인이 시작하지 못해서 자동 취소했어요", now(), r["id"]))
        event(con, "release", f"release {r['day']} {r['repo']} #{r['id']}: stale pending train row cancelled")
    con.commit()
    return len(rows)


def release_tick(log=print, force: bool = False) -> str:
    """Advance today's release train by one step. Safe to call every few minutes."""
    c = cfg()
    con = db()
    expire_stale_train(con)
    day = _today()
    # mode IS NULL = the train; dashboard deploys (deploy.py) have a mode and their own tick
    rels = con.execute("SELECT * FROM releases WHERE day=? AND mode IS NULL ORDER BY id", (day,)).fetchall()
    if any(r["status"] in ("aborted", "failed") for r in rels):
        return "stopped for today"
    active = next((r for r in rels if r["status"] == "deploying"), None)
    if active:
        return verify_deploy(con, active, log)
    pending = next((r for r in rels if r["status"] == "pending"), None)
    if not rels:
        t = datetime.now(_tz())
        hh, mm = map(int, c["at"].split(":"))
        start = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # only start inside the window (default 10:00-12:00); a missed window waits for the next weekday,
        # so a late approval never ships to production the same afternoon
        due = t.weekday() in c["weekdays"] and start <= t < start + timedelta(minutes=c.get("window_min", 120))
        if not force and (not due or meta_get(con, "release_hold", False)):
            return "not due" if not due else "on hold"
        groups = staged_by_repo(con)
        active_urls = active_release_pr_urls(con)
        for name, prs in groups.items():
            groups[name] = [p for p in prs if p["url"] not in active_urls]
        order = [name for name in train_repos()
                 if any(not M.awaiting_next_round(con, p["ticket_gid"]) for p in groups.get(name, []))]
        if not order:
            con.execute("INSERT INTO releases(day,repo,status,reason,started_at,finished_at) VALUES(?,?,?,?,?,?)",
                        (day, "", "empty", "staging에 머지된 승인 PR이 없어요", now(), now()))
            con.commit()
            return "nothing to release"
        for name in order:
            prs = [{"url": p["url"], "number": p["number"], "key": p["key"], "title": p["tname"],
                    "merge_sha": p["merge_sha"], "ticket_gid": p["tgid"]} for p in groups[name]]
            con.execute("INSERT INTO releases(day,repo,github,status,prs_json,started_at) VALUES(?,?,?,?,?,?)",
                        (day, name, groups[name][0]["github"], "pending", json.dumps(prs, ensure_ascii=False), now()))
        event(con, "release", f"release {day} started: " + ", ".join(f"{n} ({len(groups[n])} PR)" for n in order))
        con.commit()
        pending = con.execute("SELECT * FROM releases WHERE day=? AND mode IS NULL AND status='pending' ORDER BY id LIMIT 1",
                              (day,)).fetchone()
    if pending:
        return _start(con, pending, log)
    return "done for today"


def _open_pr(gh, base, head, title, body) -> str:
    if (gh, base, head) == (PUBLIC_REPOSITORY, "main", "staging"):
        # Qualify the head by owner so a fork PR with a branch named staging is never reused.
        owner = gh.split("/", 1)[0]
        existing = gh_json(["api", f"repos/{gh}/pulls?state=open&base=main&head={owner}%3Astaging",
                            "--jq", "map({url: .html_url})"])
    else:
        existing = gh_json(["pr", "list", "--repo", gh, "--base", base, "--head", head, "--state", "open", "--json", "url"])
    if existing:
        return existing[0]["url"]
    return _gh(["pr", "create", "--repo", gh, "--base", base, "--head", head, "--title", title[:200],
                "--body", body]).stdout.strip().splitlines()[-1]


def _start(con, rel, log) -> str:
    rc = repo_rules(rel["repo"]) or {}
    gh = rel["github"]
    prs = json.loads(rel["prs_json"])
    stg, main = rc["staging"]["branch"], rc["prod"]["branch"]
    busy = con.execute("SELECT id FROM releases WHERE repo=? AND mode IS NOT NULL AND status IN ('running','deploying')",
                       (rel["repo"],)).fetchone()
    if busy:
        log(f"release {rel['repo']}: waiting for dashboard deploy #{busy['id']}")
        return "waiting for dashboard deploy"
    try:
        # 1) staging must contain only the approved PRs (the owner: anything else -> stop)
        approved_head = None
        if (gh, stg, main) == (PUBLIC_REPOSITORY, "staging", "main"):
            # Compare the same staging snapshot that the deployment PR must test and merge.
            approved_head = gh_json(["api", f"repos/{gh}/commits/{stg}"])["sha"]
        cmp = gh_json(["api", f"repos/{gh}/compare/{main}...{approved_head or stg}"])
        pending_commits = [x["sha"] for x in cmp.get("commits", [])]
        if not pending_commits:
            _finish(con, rel, "aborted", f"{stg}와 {main}에 차이가 없어요 (이미 배포됐거나 머지가 안 됨)")
            return "aborted"
        allowed = set()
        for p in prs:
            if M.awaiting_next_round(con, p.get("ticket_gid")):
                continue
            info = gh_json(["pr", "view", p["url"], "--json", "commits,mergeCommit"])
            allowed |= {x["oid"] for x in info.get("commits", [])}
            if info.get("mergeCommit"):
                allowed.add(info["mergeCommit"]["oid"])
                if M.is_ms(p.get("ticket_gid")):
                    # a milestone PR brings its subtasks' commits and merges (often past GitHub's 250-commit list)
                    allowed |= M.commits(con, rel["repo"], main, info["mergeCommit"]["oid"])
        extra = [s for s in pending_commits if s not in allowed]
        if extra:
            _finish(con, rel, "aborted", f"{stg}에 승인 안 된 커밋 {len(extra)}개: " + ", ".join(s[:7] for s in extra[:8]))
            return "aborted"
        head = approved_head or gh_json(["api", f"repos/{gh}/commits/{stg}"])["sha"]
        # 2) staging pipeline must be green for the current staging head
        if rc["staging"].get("pipeline"):
            ex = _execution_for(rc["staging"]["pipeline"], head)
            if not ex or ex["status"] in ("InProgress", "Stopping"):
                log(f"release {rel['repo']}: waiting for staging pipeline on {head[:7]}")
                return "waiting for staging"  # stay pending; next tick retries
            if ex["status"] != "Succeeded":
                _finish(con, rel, "aborted", f"staging 파이프라인 {ex['status']} ({head[:7]})")
                return "aborted"
        # 3) open + merge staging -> main
        keys = ", ".join(p["key"] for p in prs)
        title = f"Release {rel['day']}: " + ", ".join((p["title"] or p["key"]).split(" ")[0] for p in prs)
        body = "Weekday release train (ticket-pilot).\n\n" + "\n".join(
            f"- #{p['number']} {p['title']} ({p['key']})" for p in prs) + f"\n\nTickets: {keys}\n"
        url = _open_pr(gh, main, stg, title, body)
        try:
            sha = merge_deployment_pr(gh, url.rsplit("/", 1)[-1], base=main, method="merge", approved_head=approved_head)
        except StagingAdvanced as error:
            log(f"release {rel['repo']}: {error}")
            return "waiting for staging recheck"
        except AwaitingTests as error:
            log(f"release {rel['repo']}: {error}")
            return "waiting for deployment PR tests"
        if sha:
            merged_at = now()
        else:
            _gh(["pr", "merge", url, "--merge"])
            info = gh_json(["pr", "view", url, "--json", "state,mergedAt,mergeCommit"])
            if info["state"] != "MERGED":
                _finish(con, rel, "failed", f"{url} merge 실패 (state={info['state']})")
                return "failed"
            sha, merged_at = info["mergeCommit"]["oid"], info["mergedAt"]
        con.execute("UPDATE releases SET status='deploying', release_pr_url=?, merge_sha=?, merged_at=? WHERE id=?",
                    (url, sha, merged_at, rel["id"]))
        event(con, "release", f"release {rel['day']} {rel['repo']}: {url} merged ({keys}); waiting for the prod pipeline")
        con.commit()
        log(f"release {rel['repo']}: merged {url}")
        return "deploying"
    except Exception as e:  # noqa: BLE001
        _finish(con, rel, "failed", str(e)[:400])
        return "failed"


def _execution_for(pipeline: str, sha: str) -> dict | None:
    ex = _aws(["codepipeline", "list-pipeline-executions", "--pipeline-name", pipeline, "--max-items", "10"])
    for e in ex.get("pipelineExecutionSummaries", []):
        if any(s.get("revisionId") == sha for s in e.get("sourceRevisions", [])):
            return e
    return None


def deploy_timeout_min() -> int:
    return int(settings().get("deploy", {}).get("deploy_timeout_min", 45))


def _workflow_run_for(github: str, workflow: str, branch: str, sha: str) -> dict | None:
    runs = gh_json(["run", "list", "--repo", github, "--workflow", workflow, "--branch", branch, "--commit", sha,
                    "-L", "5", "--json", "status,conclusion,headSha,databaseId"]) or []
    return runs[0] if runs else None


def _live(rel, rc) -> bool:
    """Fallback when our own pipeline execution / workflow run was superseded or cancelled by a newer push:
    is the merge commit contained in what production runs now?"""
    from .track import _ancestor, deployed_sha, track_rc
    sha, _ = deployed_sha(rel["github"], track_rc(rc))
    return bool(sha) and _ancestor(rel["github"], rel["merge_sha"], sha)


def verify_deploy(con, rel, log=print) -> str:
    """Wait for production to run `rel.merge_sha` (prod pipeline/workflow, ECS rollout, smoke URLs), then finalize.
    Shared by the weekday train and dashboard deploys."""
    rc = repo_rules(rel["repo"]) or {}
    prod = rc.get("prod", {})
    merged = datetime.fromisoformat(rel["merged_at"].replace("Z", "+00:00"))
    tmo = deploy_timeout_min()
    late = datetime.now(merged.tzinfo) - merged > timedelta(minutes=tmo)
    try:
        if prod.get("pipeline"):
            ex = _execution_for(prod["pipeline"], rel["merge_sha"])
            if ex and ex["status"] == "Superseded":
                if not _live(rel, rc):
                    if late:
                        _finish(con, rel, "failed", f"prod 파이프라인이 {tmo}분 안에 이 커밋을 배포하지 않았어요 (superseded)")
                        return "failed"
                    return "waiting for prod pipeline (superseded)"
            elif not ex or ex["status"] in ("InProgress", "Stopping"):
                if late:
                    _finish(con, rel, "failed", f"prod 파이프라인이 {tmo}분 안에 안 끝났어요")
                    return "failed"
                return "waiting for prod pipeline"
            elif ex["status"] != "Succeeded":
                _finish(con, rel, "failed", f"prod 파이프라인 {ex['status']} ({rel['merge_sha'][:7]})")
                return "failed"
        elif prod.get("workflow"):
            wr = _workflow_run_for(rel["github"], prod["workflow"], prod["branch"], rel["merge_sha"])
            if not wr or wr["status"] != "completed":
                if late:
                    _finish(con, rel, "failed", f"{prod['workflow']}가 {tmo}분 안에 안 끝났어요")
                    return "failed"
                return "waiting for deploy workflow"
            if wr["conclusion"] != "success":
                if wr["conclusion"] in ("cancelled", "skipped") and _live(rel, rc):
                    pass  # a newer run deployed it
                else:
                    _finish(con, rel, "failed", f"{prod['workflow']} {wr['conclusion']} ({rel['merge_sha'][:7]})")
                    return "failed"
        elif prod.get("release"):
            if not _live(rel, rc):  # in the latest GitHub Release
                if late:
                    _finish(con, rel, "failed", f"{tmo}분 안에 이 커밋을 담은 릴리즈가 안 나왔어요")
                    return "failed"
                return "waiting for a release"
        else:
            _finish(con, rel, "failed", "배포 확인 방법이 없어요 (deploy.repos.*.prod.pipeline/workflow/release)")
            return "failed"
        if prod.get("ecs"):
            svc = _aws(["ecs", "describe-services", "--cluster", prod["ecs"]["cluster"], "--services", prod["ecs"]["service"]])
            dep = next((d for d in svc["services"][0]["deployments"] if d["status"] == "PRIMARY"), None)
            if not dep or datetime.fromisoformat(dep["createdAt"]) < merged:
                if late:
                    _finish(con, rel, "failed", "ECS에 새 배포가 안 잡혔어요")
                    return "failed"
                return "waiting for ECS deployment"
            if dep.get("rolloutState") == "FAILED":
                _finish(con, rel, "failed", f"ECS 롤아웃 실패: {dep.get('rolloutStateReason', '')[:200]}")
                return "failed"
            if dep.get("rolloutState") != "COMPLETED":
                if late:
                    _finish(con, rel, "failed", "ECS 롤아웃이 시간 안에 안 끝났어요")
                    return "failed"
                return "waiting for ECS rollout"
        bad = []
        for u in prod.get("smoke", []):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Macintosh) ticket-pilot-smoke"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status != 200:
                        bad.append(f"{u} {resp.status}")
            except Exception as e:  # noqa: BLE001
                bad.append(f"{u} {getattr(e, 'code', e)}")
        if bad:
            _finish(con, rel, "failed", "스모크 체크 실패: " + "; ".join(bad))
            return "failed"
    except Exception as e:  # noqa: BLE001
        _finish(con, rel, "failed", str(e)[:400])
        return "failed"
    finalize(con, rel)
    log(f"release {rel['repo']}: done")
    return "done"


def complete_asana(con, ticket_gid: str, *, pr_url: str | None = None) -> str | None:
    """(2026-10-19): complete the Asana task once its change is verified in production. Returns an error or None.
    GitHub issue tickets (gid gh:<repo>#<n>) get the issue closed instead. An older PR can ship while the ticket
    still has work in its latest run."""
    from .pipeline import issue_ref
    if M.is_ms(ticket_gid):
        return M.complete(con, ticket_gid, pr_url=pr_url)
    if pr_url is not None:
        latest = latest_run(con, ticket_gid)
        if latest:
            current = run_pr(con, latest["id"])
            if not current or current["url"] != pr_url:
                return None
    try:
        ref = issue_ref(ticket_gid)
        if ref:
            _gh(["issue", "close", ref[1], "--repo", ref[0], "--comment", "Live in production (ticket-pilot)."])
        elif not ticket_gid.startswith("notion:"):  # Notion writes its configured done status after the local commit.
            asana("PUT", f"/tasks/{ticket_gid}", body={"completed": True})
        con.execute("UPDATE tickets SET completed=1 WHERE gid=?", (ticket_gid,))
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)[:200]


def finalize(con, rel):
    """Released: mark the PRs, complete the Asana tasks (dependent tickets unblock on the next sync)."""
    asana_errors = []
    for p in json.loads(rel["prs_json"] or "[]"):
        con.execute("UPDATE prs SET release_id=?, prod_at=COALESCE(prod_at, ?) WHERE url=?", (rel["id"], now(), p["url"]))
        if p.get("ticket_gid"):
            err = complete_asana(con, p["ticket_gid"], pr_url=p["url"])
            if err:
                asana_errors.append(f"{p.get('key')}: {err}")
    note = (rel["reason"] + " / ") if rel["reason"] else ""
    _finish(con, rel, "done", (note + "Asana 완료 처리 실패: " + "; ".join(asana_errors)) if asana_errors else (rel["reason"] or None))
    restage(con)
    con.commit()
