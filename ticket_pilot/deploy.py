"""Dashboard deploys (배포 page) driven by per-repo rules (settings repo.<name>.deploy, 설정 page).

Modes (2026-10-19):
  staging  ticket PR -> staging branch      = the existing approval (release.request_approval);
                                              repos without a staging branch but a `preview_label` get that label
  direct   ticket -> production, skipping staging
  promote  a ticket already merged into staging -> production (only its own commits)
  all      everything in staging -> production

The dashboard only inserts a `releases` row (mode set, status `requested`). The host prechecks (every 5 min) run it right
away (no weekday window), step by step and resumable:
  requested -> running (phase merge: wait for checks, merge; a repo with a via branch: phase promote = main -> production + tag/release)
            -> deploying (release.verify_deploy: prod pipeline/workflow, ECS, smoke)
            -> done (PRs marked released, Asana tasks completed -> dependent tickets unblock on the next sync) | failed
When the ticket's commits cannot be merged as-is (PR base is not the target branch, or the PR is already in staging),
a `release/<key>-<stamp>` branch is cut from the target, the PR's own non-merge commits are cherry-picked (-x) onto it,
(+ package.json patch bump for `version: npm-patch`) and that branch is merged instead. Nothing is rolled back.
Git work happens in a throwaway `git clone --shared` of the local checkout (never in the checkout or an Orca worktree).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import milestone as M
from .core import db, event, gh_json, now, repo_rules, run
from .pipeline import latest_run, restage, run_pr, slug
from .release import (_execution_for, _finish, _gh, _today, deploy_timeout_min, expire_stale_train,
                      request_approval, staged_by_repo, staging_branch, verify_deploy)

MODES = {
    "staging": "티켓 → staging",
    "direct": "티켓 → prod (staging 건너뜀)",
    "promote": "staging의 티켓 → prod",
    "all": "staging 전체 → prod",
    "preview": "티켓 → 프리뷰",
}
LIVE = ("requested", "running", "deploying")
LOCK_MIN = 15
POLL_S = 30  # how long one tick waits for GitHub mergeability / checks before yielding to the next tick
_FAILED_CHECK = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


# ---------------------------------------------------------------- request (dashboard / CLI)

def _ticket(con, ref):
    return con.execute("SELECT * FROM tickets WHERE gid=? OR lower(key)=lower(?)", (ref, ref)).fetchone()


def _approved(con, pr) -> bool:
    rv = con.execute("SELECT verdict FROM reviews WHERE pr_url=? AND head_sha=? ORDER BY id DESC LIMIT 1",
                     (pr["url"], pr["head_sha"])).fetchone()
    return bool(rv) and rv["verdict"] == "approve"


def staged_prs(con, repo: str) -> list[dict]:
    rows = staged_by_repo(con, [repo]).get(repo, [])
    return [_pr_item(p, p["key"], p["tname"]) for p in rows]


def _pr_item(pr, key, title) -> dict:
    return {"url": pr["url"], "number": pr["number"], "key": key, "title": title, "ticket_gid": pr["ticket_gid"],
            "head_sha": pr["head_sha"], "merge_sha": pr["merge_sha"]}


def busy_release(con, repo: str, before_id: int | None = None):
    """An unfinished deploy/train run on this repo (older than `before_id` when given). Requests queue up per repo.
    A pending train row only counts when release_tick can still start it: today, and today's train has not stopped."""
    q = """SELECT id, mode FROM releases r WHERE repo=? AND (status IN ('requested','running','deploying')
           OR (status='pending' AND mode IS NULL AND day=? AND NOT EXISTS (
               SELECT 1 FROM releases x WHERE x.day=r.day AND x.mode IS NULL AND x.status IN ('aborted','failed'))))"""
    args = (repo, _today()) + ((before_id,) if before_id else ())
    return con.execute(q + (" AND id<?" if before_id else "") + " ORDER BY id LIMIT 1", args).fetchone()


def deploy_target(con, mode: str, ticket: str | None = None, repo: str | None = None):
    """Return (info, error). Shared by the dashboard (validation) and the CLI."""
    if mode not in MODES or mode == "staging":
        return None, f"알 수 없는 배포 방식: {mode}"
    t = pr = None
    if ticket:
        t = _ticket(con, ticket)
        if not t:
            return None, "티켓을 찾을 수 없어요"
        r = latest_run(con, t["gid"])
        pr = r and run_pr(con, r["id"])
        if not pr:
            return None, "PR이 없어요"
        repo = pr["repo"]
    rr = repo_rules(repo) if repo else None
    if not rr:
        return None, f"{repo or '?'}에 배포 규칙이 없어요 (설정 → 레포 → 배포)"
    if (mode if mode != "preview" else "staging") not in rr["modes"]:
        return None, f"{repo}는 '{MODES[mode]}' 배포를 쓰지 않아요"
    if mode == "all":
        dup = con.execute("SELECT id FROM releases WHERE repo=? AND mode='all' AND status IN ('requested','running','deploying')",
                          (repo,)).fetchone()
        if dup:
            return None, f"이미 요청된 {MODES['all']} 배포(#{dup['id']})가 있어요"
        if not rr["staging"]:
            return None, f"{repo}에는 staging 브랜치가 없어요"
        return {"repo": repo, "rr": rr, "prs": staged_prs(con, repo)}, None
    if not t:
        return None, "티켓이 필요해요"
    dup = con.execute("SELECT id FROM releases WHERE ticket_gid=? AND status IN ('requested','running','deploying')", (t["gid"],)).fetchone()
    if dup:
        return None, f"이 티켓은 이미 배포 중이에요 (#{dup['id']})"
    if mode in ("direct", "preview"):
        if pr["state"] != "OPEN":
            return None, "열린 PR이 없어요"
        if not _approved(con, pr):
            return None, "최신 커밋에 AI '괜찮음' 판정이 없어요"
        if mode == "preview" and not rr.get("preview_label"):
            return None, f"{repo}에는 프리뷰 라벨 규칙이 없어요"
        if mode == "direct" and pr["base"] == rr["target"] and pr["mergeable"] == "CONFLICTING":
            return None, f"PR이 {pr['base']}와 충돌해요. 충돌을 풀고 재리뷰 후 다시 요청해 주세요"
    if mode == "promote":
        stg = rr["staging"]["branch"] if rr["staging"] else None
        if pr["state"] != "MERGED" or not stg or pr["base"] != stg:
            return None, f"{stg or 'staging'}에 머지된 PR이 아니에요"
        if pr["release_id"] is not None or pr["prod_at"] is not None:
            return None, "이미 프로덕션에 반영됐어요"
        if M.awaiting_next_round(con, t["gid"]):
            return None, "다음 마일스톤 PR이 staging에 머지될 때까지 기다려 주세요"
    return {"repo": repo, "rr": rr, "t": t, "pr": pr, "prs": [_pr_item(pr, t["key"], t["name"])]}, None


def request_deploy(mode: str, ticket: str | None = None, repo: str | None = None) -> int | None:
    con = db()
    if mode == "staging":
        t = _ticket(con, ticket or "")
        r = t and latest_run(con, t["gid"])
        pr = r and run_pr(con, r["id"])
        if (pr and (repo_rules(pr["repo"]) or {}).get("preview_label")
                and pr["base"] != staging_branch(pr["repo"])):
            mode = "preview"  # a pr_into_staging repo (e.g. main + dev): PRs not based on dev (opened before it existed) still use the single preview slot
        else:
            request_approval(ticket)  # validates and records; the host merges it into staging within 5 min
            return None
    info, err = deploy_target(con, mode, ticket, repo)
    if err:
        raise SystemExit(err)
    gh = con.execute("SELECT github FROM repos WHERE name=?", (info["repo"],)).fetchone()
    t = info.get("t")
    cur = con.execute("""INSERT INTO releases(day,repo,github,status,mode,ticket_gid,step,prs_json,requested_at,started_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (_today(), info["repo"], gh and gh["github"], "requested", mode, t and t["gid"], "요청됨 (5분 안에 시작)",
                       json.dumps(info["prs"], ensure_ascii=False), now(), now()))
    event(con, "deploy", f"deploy #{cur.lastrowid} requested: {MODES[mode]} {t['key'] if t else info['repo']}", t and t["gid"])
    restage(con)
    con.commit()
    return cur.lastrowid


def cancel_deploy(rel_id: int):
    con = db()
    # a dashboard request before it starts, or a train row (mode NULL) still waiting for its turn
    n = con.execute("UPDATE releases SET status='cancelled', step='취소됨', finished_at=? WHERE id=? "
                    "AND (status='requested' OR (status='pending' AND mode IS NULL)) "
                    "AND (lock_until IS NULL OR lock_until<?)", (now(), rel_id, now())).rowcount
    if not n:
        raise SystemExit("이미 시작된 배포는 취소할 수 없어요")
    event(con, "deploy", f"deploy #{rel_id} cancelled")
    restage(con)
    con.commit()


# ---------------------------------------------------------------- host tick

def deploy_tick(log=print) -> int:
    con = db()
    expire_stale_train(con)
    rows = con.execute(f"SELECT * FROM releases WHERE mode IS NOT NULL AND status IN ({','.join('?' * len(LIVE))}) ORDER BY id",
                       LIVE).fetchall()
    moved = 0
    for rel in rows:
        if rel["status"] == "requested" and rel["mode"] != "preview" and busy_release(con, rel["repo"], rel["id"]):
            _save(con, rel["id"], step="같은 레포의 앞선 배포가 끝나길 기다리는 중")
            continue  # one deploy per repo at a time, in request order
        if not _claim(con, rel["id"]):
            continue  # the other precheck is working on it
        try:
            res = _step(con, _row(con, rel["id"]), log)
        except Exception as e:  # noqa: BLE001
            res = "failed"
            con.rollback()
            _finish(con, _row(con, rel["id"]), "failed", str(e)[:400])
            _save(con, rel["id"], step="실패")
        finally:
            con.execute("UPDATE releases SET lock_until=NULL WHERE id=?", (rel["id"],))
            con.commit()
        log(f"deploy #{rel['id']} {rel['mode']} {rel['repo']}: {res}")
        moved += 1
    restage(con)
    con.commit()
    return moved


def _claim(con, rel_id) -> bool:
    t = datetime.now(timezone.utc)
    n = con.execute("UPDATE releases SET lock_until=? WHERE id=? AND (lock_until IS NULL OR lock_until<?)",
                    ((t + timedelta(minutes=LOCK_MIN)).isoformat(timespec="seconds"), rel_id, t.isoformat(timespec="seconds"))).rowcount
    con.commit()
    return n == 1


def _row(con, rel_id):
    return con.execute("SELECT * FROM releases WHERE id=?", (rel_id,)).fetchone()


def _state(rel) -> dict:
    return json.loads(rel["state_json"] or "{}")


def _save(con, rel_id, status=None, step=None, **cols):
    sets, vals = [], []
    if status:
        sets.append("status=?")
        vals.append(status)
    if step:
        sets.append("step=?")
        vals.append(step)
    for k, v in cols.items():
        sets.append(f"{k}=?")
        vals.append(json.dumps(v, ensure_ascii=False) if k == "state_json" else v)
    con.execute(f"UPDATE releases SET {','.join(sets)} WHERE id=?", (*vals, rel_id))
    con.commit()
    return _row(con, rel_id)


def _step(con, rel, log) -> str:
    rr = repo_rules(rel["repo"])
    if not rr:
        raise RuntimeError(f"{rel['repo']} 배포 규칙이 없어졌어요")
    if rel["status"] == "requested":
        return _prepare(con, rel, rr, log)
    if rel["status"] == "running":
        st = _state(rel)
        return _promote(con, rel, rr, st, log) if st.get("phase") == "promote" else _merge(con, rel, rr, st, log)
    return verify_deploy(con, rel, log)


# ---------------------------------------------------------------- steps

def _prepare(con, rel, rr, log) -> str:
    mode, gh, prs = rel["mode"], rel["github"], json.loads(rel["prs_json"] or "[]")
    repo_path = (con.execute("SELECT path FROM repos WHERE name=?", (rel["repo"],)).fetchone() or {"path": None})["path"]
    target, prod = rr["target"], rr["prod"]["branch"]
    key = prs[0]["key"] if prs else rel["repo"]
    stamp = datetime.now().strftime("%Y%m%d%H%M")

    if mode == "preview":
        p = prs[0]
        run(["gh", "api", "-X", "POST", f"repos/{gh}/issues/{p['number']}/labels", "-f", f"labels[]={rr['preview_label']}"],
            timeout=120)
        _save(con, rel["id"], step="프리뷰 라벨 추가됨")
        _finish(con, rel, "done", f"PR #{p['number']}에 '{rr['preview_label']}' 라벨을 달았어요 (프리뷰 슬롯은 하나라 이전 프리뷰를 대체)")
        return "done"

    if rr["via"]:
        _require_via_clean(gh, rr)

    if mode == "direct":
        p = prs[0]
        live = gh_json(["pr", "view", p["url"], "--json", "state,baseRefName,headRefOid,title"])
        if live["state"] != "OPEN":
            raise RuntimeError(f"PR #{p['number']}이 열려 있지 않아요 ({live['state']})")
        if live["headRefOid"] != p["head_sha"]:
            raise RuntimeError("요청 후 새 커밋이 올라왔어요. 재리뷰 후 다시 요청해 주세요")
        if live["baseRefName"] == target and not rr.get("version"):
            return _to_merge(con, rel, rr, log, pr=p["url"], into=target, method=rr["merge"],
                             step=f"PR #{p['number']} → {target} 머지")
        branch, new, old = _release_branch(repo_path, gh, target, f"release/{slug(key)}-{stamp}",
                                           picks=[(p["number"], p["head_sha"])], bump=bool(rr.get("version")))
        if not branch:
            raise RuntimeError(f"PR #{p['number']}의 변경이 이미 {target}에 있어요")
        url = _open_pr(gh, target, branch, _title(rr, new, f"[{key}] {p['title']}"),
                       _body(rel, prs, f"`{live['baseRefName']}` 대상 PR #{p['number']}의 커밋만 cherry-pick (-x)", new))
        backsync = p["url"] if live["baseRefName"] != target else None
        return _to_merge(con, rel, rr, log, pr=url, into=target, method=rr["merge"], backsync=backsync,
                         version=new, old_version=old, step=f"{branch} → {target} 머지")

    if mode == "promote":
        p = prs[0]
        branch, new, old = _release_branch(repo_path, gh, target, f"release/{slug(key)}-{stamp}",
                                           picks=[(p["number"], p["head_sha"])], bump=bool(rr.get("version")))
        if not branch:  # its commits are already in the target branch
            if target == prod:
                head = gh_json(["api", f"repos/{gh}/commits/{prod}", "--jq", "{sha: .sha}"])["sha"]
                _save(con, rel["id"], status="deploying", step="이미 반영됨, 프로덕션 확인 중", merge_sha=head, merged_at=now())
                return verify_deploy(con, _row(con, rel["id"]), log)
            raise RuntimeError(f"PR #{p['number']}의 변경이 이미 {target}에 있어요. '{MODES['all']}'로 내보내 주세요")
        url = _open_pr(gh, target, branch, _title(rr, new, f"[{key}] {p['title']}"),
                       _body(rel, prs, f"staging에 머지된 PR #{p['number']}의 커밋만 cherry-pick (-x)", new))
        return _to_merge(con, rel, rr, log, pr=url, into=target, method=rr["merge"], version=new, old_version=old,
                         step=f"{branch} → {target} 머지")

    # mode == "all"
    stg = rr["staging"]["branch"]
    cmp = gh_json(["api", f"repos/{gh}/compare/{target}...{stg}", "--jq", "{ahead: .ahead_by}"])
    if not cmp.get("ahead"):
        _finish(con, rel, "aborted", f"{stg}에 {target}보다 앞선 커밋이 없어요")
        return "aborted"
    head = gh_json(["api", f"repos/{gh}/commits/{stg}", "--jq", "{sha: .sha}"])["sha"]
    if rr["staging"].get("pipeline"):
        ex = _execution_for(rr["staging"]["pipeline"], head)
        if not ex or ex["status"] in ("InProgress", "Stopping"):
            _save(con, rel["id"], step=f"{stg} 파이프라인 대기 ({head[:7]})")
            return "waiting for staging pipeline"
        if ex["status"] != "Succeeded":
            _finish(con, rel, "aborted", f"{stg} 파이프라인 {ex['status']} ({head[:7]})")
            return "aborted"
    prs = staged_prs(con, rel["repo"])  # whatever reached staging since the request
    _save(con, rel["id"], prs_json=json.dumps(prs, ensure_ascii=False))
    rel = _row(con, rel["id"])
    note = f"{stg} 전체 ({cmp['ahead']} 커밋, 추적 중인 티켓 {len(prs)}개)"
    if rr.get("version"):
        branch, new, old = _release_branch(repo_path, gh, target, f"release/staging-{stamp}", merge_ref=stg, bump=True)
        url = _open_pr(gh, target, branch, _title(rr, new, f"{stg} 전체"), _body(rel, prs, note, new))
        return _to_merge(con, rel, rr, log, pr=url, into=target, method="merge", version=new, old_version=old,
                         step=f"{branch} → {target} 머지")
    url = _open_pr(gh, target, stg, f"Release {rel['day']}: {stg} → {target} (배포 페이지)", _body(rel, prs, note, None))
    return _to_merge(con, rel, rr, log, pr=url, into=target, method="merge", step=f"{stg} → {target} 머지")


def _to_merge(con, rel, rr, log, pr, into, method, step, **extra) -> str:
    st = {"phase": "merge", "pr": pr, "into": into, "method": method, "since": now(), **extra}
    rel = _save(con, rel["id"], status="running", step=step, state_json=st, release_pr_url=pr,
                version=extra.get("version") or rel["version"])
    event(con, "deploy", f"deploy #{rel['id']} {rel['repo']}: {pr} → {into}", rel["ticket_gid"])
    con.commit()
    return _merge(con, rel, rr, st, log)


def _checks(info) -> tuple[list[str], list[str]]:
    failed, pending = [], []
    for c in info.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context") or "?"
        if c.get("__typename") == "StatusContext" or "state" in c and "conclusion" not in c:
            s = (c.get("state") or "").upper()
            (failed if s in _FAILED_CHECK else pending if s in ("PENDING", "EXPECTED") else []).append(name)
        else:
            if (c.get("status") or "").upper() != "COMPLETED":
                pending.append(name)
            elif (c.get("conclusion") or "").upper() in _FAILED_CHECK:
                failed.append(name)
    return failed, pending


def _merge(con, rel, rr, st, log) -> str:
    url, into = st["pr"], st["into"]
    fields = "state,isDraft,mergeable,statusCheckRollup,mergeCommit,baseRefName,number"
    deadline = datetime.fromisoformat(st["since"]) + timedelta(minutes=deploy_timeout_min())
    t0 = time.time()
    while True:
        info = gh_json(["pr", "view", url, "--json", fields])
        if info["state"] == "MERGED":
            break
        if info["state"] == "CLOSED":
            raise RuntimeError(f"PR #{info['number']}이 닫혔어요")
        if info["baseRefName"] != into:
            raise RuntimeError(f"PR #{info['number']} base가 {info['baseRefName']}로 바뀌었어요 ({into}여야 해요)")
        if info["isDraft"]:
            _gh(["pr", "ready", url])
        if info["mergeable"] == "CONFLICTING":
            raise RuntimeError(f"PR #{info['number']}이 {into}와 충돌해요")
        failed, pending = _checks(info)
        if failed:
            raise RuntimeError(f"PR #{info['number']} 체크 실패: {', '.join(failed[:5])}")
        if info["mergeable"] == "MERGEABLE" and not pending:
            _gh(["pr", "merge", url, f"--{st['method']}"])
            continue  # re-read the merge commit
        if datetime.now(timezone.utc) > deadline:
            raise RuntimeError(f"PR #{info['number']}: {deploy_timeout_min()}분 동안 머지할 수 없었어요 "
                               f"(mergeable={info['mergeable']}, 대기 체크: {', '.join(pending[:5]) or '-'})")
        if time.time() - t0 > POLL_S:
            _save(con, rel["id"], step=f"PR #{info['number']} 체크/머지 가능 여부 대기" + (f" ({', '.join(pending[:3])})" if pending else ""))
            return "waiting for checks"
        time.sleep(5)
    sha = info["mergeCommit"]["oid"]
    notes = []
    if st.get("backsync"):  # the ticket PR also lands where it was aimed (staging/dev) so branches do not drift
        try:
            b = gh_json(["pr", "view", st["backsync"], "--json", "state,isDraft,number,baseRefName"])
            if b["state"] == "OPEN":
                if b["isDraft"]:
                    _gh(["pr", "ready", st["backsync"]])
                _gh(["pr", "merge", st["backsync"], "--merge"])
                notes.append(f"원래 PR #{b['number']}도 {b['baseRefName']}에 머지")
        except Exception as e:  # noqa: BLE001
            notes.append(f"원래 PR {st['backsync'].rsplit('/', 1)[-1]} {e!s:.120} (직접 머지 필요)")
    if st.get("tag"):
        try:
            v = st["tag"]["version"]
            _gh(["release", "create", f"v{v}", "--repo", rel["github"], "--target", st["tag"]["sha"], "--title", f"v{v}",
                 "--notes", st["tag"]["notes"]])
            notes.append(f"v{v} 태그·릴리스 생성")
        except Exception as e:  # noqa: BLE001
            notes.append(f"태그/릴리스 실패: {e!s:.160}")
    reason = "; ".join(filter(None, [rel["reason"], *notes])) or None
    event(con, "deploy", f"deploy #{rel['id']} {rel['repo']}: PR #{info['number']} merged into {into} ({sha[:7]})", rel["ticket_gid"])
    if into == rr["prod"]["branch"]:
        _save(con, rel["id"], status="deploying", step="프로덕션 배포 확인 중", merge_sha=sha, merged_at=now(), reason=reason,
              state_json={**st, "phase": "deploying"})
        restage(con)
        return verify_deploy(con, _row(con, rel["id"]), log)
    rel = _save(con, rel["id"], step=f"{into} → {rr['prod']['branch']} 준비", reason=reason,
                state_json={**st, "phase": "promote", "via_sha": sha, "via_pr": url, "backsync": None})
    return _promote(con, rel, rr, _state(rel), log)


def _promote(con, rel, rr, st, log) -> str:
    """A repo with a via branch (staging -> main -> production): via (main) -> prod (production) after the release branch landed in via; tag + GitHub release on merge."""
    gh, via, prod = rel["github"], rr["via"], rr["prod"]["branch"]
    ahead = run(["gh", "api", f"repos/{gh}/compare/{prod}...{via}", "--jq", ".commits[].sha"], timeout=120).stdout.split()
    n = st["via_pr"].rstrip("/").rsplit("/", 1)[-1]
    allowed = set(run(["gh", "api", f"repos/{gh}/pulls/{n}/commits", "--paginate", "--jq", ".[].sha"],
                      timeout=120).stdout.split()) | {st["via_sha"]}
    extra = [s for s in ahead if s not in allowed]
    if extra:
        raise RuntimeError(f"{via}에 이번 배포가 아닌 커밋 {len(extra)}개가 있어요: " + ", ".join(s[:7] for s in extra[:8]))
    v = st.get("version")
    prs = json.loads(rel["prs_json"] or "[]")
    title = f"release: v{v} 프로덕션 배포 ({via} → {prod})" if v else f"Release {rel['day']}: {via} → {prod}"
    url = _open_pr(gh, prod, via, title, _body(rel, prs, f"{via} → {prod}", v))
    tag = None
    if v:
        lines = "\n".join(f"- {p['title']} ({p['key']}, #{p['number']})" for p in prs) or "- staging 변경 사항"
        old = st.get("old_version")
        tag = {"version": v, "sha": st["via_sha"], "notes": f"## v{v}\n\n{lines}\n\n" + (
            f"**Full Changelog**: https://github.com/{gh}/compare/v{old}...v{v}\n" if old else "")}
    st = {**st, "phase": "merge", "pr": url, "into": prod, "method": "merge", "since": now(), "tag": tag}
    rel = _save(con, rel["id"], step=f"{via} → {prod} 머지", state_json=st, release_pr_url=url)
    return _merge(con, rel, rr, st, log)


def _require_via_clean(gh, rr):
    """A repo with a via branch: main must not hold commits that production does not have, or they would ship unreviewed."""
    via, prod = rr["via"], rr["prod"]["branch"]
    ahead = gh_json(["api", f"repos/{gh}/compare/{prod}...{via}", "--jq", "{ahead: .ahead_by}"])["ahead"]
    if ahead:
        raise RuntimeError(f"{via}에 {prod}에 없는 커밋 {ahead}개가 있어요. 먼저 정리해 주세요")


# ---------------------------------------------------------------- git / gh helpers

def _git(cwd, *args, check=True, timeout=300):
    return run(["git", "-C", cwd, *args], timeout=timeout, check=check)


def _unmerged(cwd) -> list[str]:
    return _git(cwd, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()


def _release_branch(repo_path, gh, base, name, picks=(), merge_ref=None, bump=False):
    """Cut `name` from origin/<base>, cherry-pick each PR's own commits not yet in base, optionally merge
    origin/<merge_ref> and bump the package.json patch version; push it. Returns (branch|None, new_version, old_version);
    branch None = nothing to ship."""
    if not repo_path or not os.path.isdir(repo_path):
        raise RuntimeError(f"로컬 clone이 없어요: {repo_path}")
    origin = _git(repo_path, "remote", "get-url", "origin").stdout.strip()
    tmp = tempfile.mkdtemp(prefix="tp-release-")
    try:
        # objects are borrowed from the local checkout; nothing is written there
        run(["git", "clone", "-q", "--no-checkout", "--shared", repo_path, tmp], timeout=600)
        _git(tmp, "remote", "set-url", "origin", origin)
        refs = [f"+refs/heads/{base}:refs/remotes/origin/{base}"]
        if merge_ref:
            refs.append(f"+refs/heads/{merge_ref}:refs/remotes/origin/{merge_ref}")
        refs += [f"+refs/pull/{n}/head:refs/remotes/pr/{n}" for n, _ in picks]
        _git(tmp, "fetch", "-q", "--no-tags", "origin", *refs, timeout=900)
        _git(tmp, "checkout", "-q", "-B", name, f"origin/{base}")
        for n, _head in picks:
            own = run(["gh", "api", f"repos/{gh}/pulls/{n}/commits", "--paginate", "--jq",
                       ".[] | select((.parents | length) == 1) | .sha"], timeout=120).stdout.split()
            plus = {ln[2:].strip() for ln in _git(tmp, "cherry", f"origin/{base}", f"pr/{n}").stdout.splitlines() if ln.startswith("+ ")}
            todo = [c for c in own if c in plus]
            if todo:
                p = _git(tmp, "cherry-pick", "-x", *todo, check=False)
                if p.returncode != 0:
                    files = _unmerged(tmp)
                    _git(tmp, "cherry-pick", "--abort", check=False)
                    raise RuntimeError(f"PR #{n}를 {base}에 cherry-pick하다 충돌: {', '.join(files[:5]) or p.stderr.strip()[-200:]}")
        if merge_ref:
            p = _git(tmp, "merge", "--no-ff", "--no-edit", "-m", f"Merge {merge_ref} into {name}", f"origin/{merge_ref}", check=False)
            if p.returncode != 0:
                files = _unmerged(tmp)
                if bump and files == ["package.json"]:  # only the version line: keep the base side, bumped below
                    _git(tmp, "checkout", "--ours", "package.json")
                    _git(tmp, "add", "package.json")
                    _git(tmp, "commit", "-q", "-n", "--no-edit")
                else:
                    _git(tmp, "merge", "--abort", check=False)
                    raise RuntimeError(f"{merge_ref}를 {base}에 머지하다 충돌: {', '.join(files[:5]) or p.stderr.strip()[-200:]}")
        new = old = None
        if bump:
            pj = Path(tmp) / "package.json"
            text = pj.read_text()
            m = re.search(r'("version"\s*:\s*")(\d+)\.(\d+)\.(\d+)(")', text)
            if not m:
                raise RuntimeError("package.json에 version이 없어요")
            old = f"{m.group(2)}.{m.group(3)}.{m.group(4)}"
            new = f"{m.group(2)}.{m.group(3)}.{int(m.group(4)) + 1}"
            pj.write_text(text[:m.start()] + m.group(1) + new + m.group(5) + text[m.end():])
            _git(tmp, "add", "package.json")
            _git(tmp, "commit", "-q", "-n", "-m", new)
        if _git(tmp, "rev-parse", "HEAD").stdout.strip() == _git(tmp, "rev-parse", f"origin/{base}").stdout.strip():
            return None, None, None
        _git(tmp, "push", "-q", "--no-verify", "origin", f"HEAD:refs/heads/{name}", timeout=600)
        return name, new, old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _open_pr(gh, base, head, title, body) -> str:
    existing = gh_json(["pr", "list", "--repo", gh, "--base", base, "--head", head, "--state", "open", "--json", "url"])
    if existing:
        return existing[0]["url"]
    return _gh(["pr", "create", "--repo", gh, "--base", base, "--head", head, "--title", title[:200], "--body", body]).stdout.strip().splitlines()[-1]


def _title(rr, version, what) -> str:
    return f"release: v{version} {what}" if version else f"{what} (prod 배포)"


def _body(rel, prs, note, version) -> str:
    lines = "\n".join(f"- #{p['number']} {p['title']} ({p['key']})" for p in prs) or "- (추적 중인 티켓 없음)"
    return (f"ticket-pilot 배포 페이지 #{rel['id']}: {MODES.get(rel['mode'], rel['mode'])}"
            + (f" (v{version})" if version else "") + f"\n\n{note}\n\n{lines}\n")
