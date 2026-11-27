"""Remove ticket worktrees once their change is live in production (2026-10-19).

Scope: only worktrees ticket-pilot knows (`runs.worktree_path`, dispatched or adopted). Manually created worktrees
(Orca or git) are never touched. Production is the only criterion: a merge into staging/dev is not enough.

For every ticket worktree whose PRs are all merged (none open), each merged PR is checked against the sha production
runs now (`track.deployed_sha`: prod CodePipeline / deploy workflow, ECS rollout) in this order:
  merge   the PR's merge/squash commit is an ancestor of the deployed sha
  cherry  every non-merge commit of the PR has a patch-equivalent commit there (`git cherry`; cherry-pick releases)
  patch   the PR's whole diff (merge-base..head) reverse-applies cleanly onto the deployed tree
          (cherry-picks with conflict fixes, multi-commit squashes)
  agent   none of the above, but production shows signs of the PR (its number/key/commit shas in commit messages, or
          its files changed in production after the merge): the automation "ticket-pilot: worktree cleanup"
          compares the code and records live / not_live for that deployed sha (`tp cleanup-verdict`)
When every merged PR is live, the worktree is removed right away (`git worktree remove`; worktrees Orca made before the
local runner with `orca worktree rm`, verified with `git worktree list` + the path), then the local branch is deleted. The
remote branch and the PR stay.

Skipped tickets (건너뜀, e.g. dropped with 그만두기; 2026-10-23) need no production check: their worktree is
removed as soon as no agent or unmanaged dev server runs in it, uncommitted/unpushed work included. A server
started with tp dev is stopped on removal. Remote branch and PR stay.

Kept (reason stored, checked again next tick) when: uncommitted changes, local commits that are in neither the PR heads
nor production, an open PR from the branch, a ticket agent still running (runner handle; Orca agent state for legacy
worktrees), or a process listening on a port with its cwd inside the worktree (dev server). Main worktrees and paths
outside runner.root() (and ~/orca/workspaces for legacy worktrees) are never removed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import runner as R
from . import devserver, worktree_dev
from .core import db, event, gh_json, now, orca_json, run
from .track import deployed_sha, track_config

WORKSPACES = os.path.expanduser("~/orca/workspaces") + "/"  # legacy: worktrees Orca made before the local runner
SAFE_AGENT_STATES = {"done"}
# agent CLIs keep a localhost port open while idle; the runner handle / Orca's agent state decides whether they are busy
AGENT_PROCS = {"codex", "claude", "agy", "node_repl", "orca"}


def _git(repo: str, *args: str, check: bool = False, timeout: int = 120, **kw):
    return run(["git", "-C", repo, *args], check=check, timeout=timeout, **kw)


def _fetch(repo_path: str, refs: list[str]):
    """Objects only (FETCH_HEAD): no branch or working tree changes in the clone."""
    _git(repo_path, "fetch", "-q", "--no-tags", "origin", *refs, timeout=240)


def _own_commits(github: str, number: int) -> list[str]:
    return run(["gh", "api", f"repos/{github}/pulls/{number}/commits", "--paginate",
                "--jq", ".[] | select((.parents | length) == 1) | .sha"], timeout=120).stdout.split()


def _merge_base(repo_path: str, pr: dict, prod: str) -> str | None:
    """Where the PR's diff starts: merge-base of its head with the base branch at merge time (merge_sha^1)."""
    base = f"{pr['merge_sha']}^1" if pr.get("merge_sha") else prod
    p = _git(repo_path, "merge-base", base, pr["head_sha"])
    return p.stdout.strip() or None if p.returncode == 0 else None


def _patch_live(repo_path: str, pr: dict, prod: str) -> bool:
    mb = _merge_base(repo_path, pr, prod)
    if not mb:
        return False
    diff = _git(repo_path, "diff", "--binary", mb, pr["head_sha"], timeout=120).stdout
    if not diff.strip():
        return True
    fd, idx = tempfile.mkstemp(prefix="tp-cleanup-idx-")
    os.close(fd)
    os.remove(idx)
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    try:
        _git(repo_path, "read-tree", prod, check=True, env=env)
        p = subprocess.run(["git", "-C", repo_path, "apply", "--cached", "--reverse", "--check", "-"], input=diff,
                           capture_output=True, text=True, env=env, timeout=120)
        return p.returncode == 0
    finally:
        if os.path.exists(idx):
            os.remove(idx)


def _signals(repo_path: str, pr: dict, key: str, own: list[str], prod: str) -> list[str]:
    """Hints that production may hold the PR although the mechanical checks failed (worth an agent comparison)."""
    mb = _merge_base(repo_path, pr, prod)
    if not mb:
        return []
    out = []
    log = _git(repo_path, "log", "--format=%H%x1f%B%x1e", f"{mb}..{prod}", timeout=120).stdout
    pats = [re.compile(rf"#{pr['number']}\b"), re.compile(re.escape(key), re.I)] + [re.compile(s[:12]) for s in own]
    for entry in log.split("\x1e"):
        if "\x1f" not in entry:
            continue
        sha, body = entry.strip().split("\x1f", 1)
        if any(p.search(body) for p in pats):
            out.append(f"prod commit {sha[:7]} mentions PR #{pr['number']}/{key}")
            break
    files = _git(repo_path, "diff", "--name-only", mb, pr["head_sha"]).stdout.split()
    if files and pr.get("merged_at"):
        touched = _git(repo_path, "log", "--format=%h", f"--since={pr['merged_at']}", f"{mb}..{prod}", "--", *files[:200]).stdout.split()
        if touched:
            out.append(f"{len(touched)} prod commit(s) since the merge touch the PR's files ({', '.join(touched[:3])})")
    return out


def _pr_info(con, p) -> dict:
    """prs row -> dict with merge_sha/base/merged_at (backfilled from GitHub for PRs scanned before those columns)."""
    d = dict(p)
    if not d.get("merge_sha") or not d.get("merged_at"):
        info = gh_json(["pr", "view", d["url"], "--json", "baseRefName,mergedAt,mergeCommit,headRefOid"])
        d.update(base=info["baseRefName"], merged_at=info["mergedAt"],
                 merge_sha=(info.get("mergeCommit") or {}).get("oid"), head_sha=info.get("headRefOid") or d["head_sha"])
        con.execute("UPDATE prs SET base=?, merged_at=?, merge_sha=?, head_sha=? WHERE url=?",
                    (d["base"], d["merged_at"], d["merge_sha"], d["head_sha"], d["url"]))
    return d


def check_pr(con, repo_path: str, github: str, p, key: str, prod: str, fetched: set) -> dict:
    pr = _pr_info(con, p)
    refs = [f"refs/pull/{pr['number']}/head"] + ([pr["merge_sha"]] if pr.get("merge_sha") else [])
    if prod not in fetched:
        refs.append(prod)
        fetched.add(prod)
    _fetch(repo_path, refs)
    res = {"number": pr["number"], "url": pr["url"], "merge_sha": pr.get("merge_sha"), "head_sha": pr["head_sha"],
           "merged_at": pr.get("merged_at"), "base": pr.get("base"), "live": None, "signals": []}
    if pr.get("merge_sha") and _git(repo_path, "merge-base", "--is-ancestor", pr["merge_sha"], prod).returncode == 0:
        res["live"] = "merge"
        return res
    own = _own_commits(github, pr["number"])
    if own:
        out = _git(repo_path, "cherry", prod, pr["head_sha"], timeout=120).stdout
        missing = {ln[2:].strip() for ln in out.splitlines() if ln.startswith("+ ")}
        if not any(c in missing for c in own):
            res["live"] = "cherry"
            return res
    if _patch_live(repo_path, pr, prod):
        res["live"] = "patch"
        return res
    res["signals"] = _signals(repo_path, pr, key, own, prod)
    res["own_commits"] = own
    return res


# ---------------------------------------------------------------- safety

def _managed(path: str) -> bool:
    real = os.path.realpath(path)
    return real.startswith(str(R.root()) + "/") or real.startswith(os.path.realpath(WORKSPACES) + "/")


def _legacy(path: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(WORKSPACES) + "/") and shutil.which("orca") is not None


def _ps_entries() -> dict[str, dict]:
    if not shutil.which("orca"):
        return {}
    try:
        r = orca_json(["worktree", "ps", "--limit", "1000"], timeout=60)
        return {w["path"]: w for w in r.get("worktrees", [])}
    except Exception:  # noqa: BLE001
        return {}


def _listening_in(path: str, managed: int | None = None) -> list[str]:
    """Listeners that need the worktree kept. Cleanup can terminate its registered tp dev process group."""
    if not shutil.which("lsof") and os.path.exists("/proc/net/tcp"):
        return [f"{p['name']}({p['pid']}) :{p['port']}" for p in devserver.proc_listeners(directory=path)
                if p["name"] not in AGENT_PROCS and not worktree_dev.in_group(p["pid"], managed)]
    p = run(["lsof", "-nP", "-d", "cwd", "-Fpcn"], check=False, timeout=60)
    pids, cur, name = {}, None, ""
    for ln in p.stdout.splitlines():
        if ln.startswith("p"):
            cur = ln[1:]
        elif ln.startswith("c"):
            name = ln[1:]
        elif ln.startswith("n") and cur and (ln[1:] == path or ln[1:].startswith(path + "/")):
            pids[cur] = name
    out = []
    for pid, name in pids.items():
        if name in AGENT_PROCS or worktree_dev.in_group(int(pid), managed):
            continue
        q = run(["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN", "-Fn"], check=False, timeout=30)
        ports = [ln[1:] for ln in q.stdout.splitlines() if ln.startswith("n")]
        if ports:
            out.append(f"{name}({pid}) {ports[0]}")
    return out


def safety(con, path: str, repo_path: str, github: str, branch: str, keep_shas: list[str] | None, wt: dict, ps: dict) -> str | None:
    """None when the worktree can go, else the reason to keep it. keep_shas=None (skipped ticket): the owner threw the
    work away, so local changes and open PRs do not keep it."""
    if wt.get("isMainWorktree") or not _managed(path) or os.path.realpath(path) == os.path.realpath(repo_path):
        return "메인 워크트리/관리 경로 밖"
    if keep_shas is None:
        return _busy(con, path, ps, wt)
    st = _git(path, "status", "--porcelain")
    if st.returncode != 0:
        return f"git status 실패: {st.stderr.strip()[:120]}"
    if st.stdout.strip():
        return f"커밋 안 된 변경 {len(st.stdout.strip().splitlines())}개"
    extra = _git(path, "rev-list", "--count", "HEAD", "--not", *keep_shas)
    if extra.returncode != 0 or extra.stdout.strip() != "0":
        return f"PR/프로덕션에 없는 로컬 커밋 {extra.stdout.strip() or '?'}개"
    if branch:
        open_prs = gh_json(["pr", "list", "--repo", github, "--head", branch, "--state", "open", "--json", "number"]) or []
        if open_prs:
            return f"열린 PR #{open_prs[0]['number']}"
    return _busy(con, path, ps, wt)


def _busy(con, path: str, ps: dict, wt: dict) -> str | None:
    if any(R.running(h) for h in wt.get("handles") or []):
        return "티켓 에이전트 실행 중"
    busy = [a.get("state") for a in (ps.get(path) or {}).get("agents", []) if a.get("state") not in SAFE_AGENT_STATES]
    if busy:
        return f"Orca 에이전트 실행 중 ({busy[0]})"
    srv = _listening_in(os.path.realpath(path), worktree_dev.managed_pid(worktree_dev.record(con, path)))
    if srv:
        return f"워크트리에서 서버 실행 중: {srv[0]}"
    return None


# ---------------------------------------------------------------- removal

def _worktree_paths(repo_path: str) -> set[str]:
    out = _git(repo_path, "worktree", "list", "--porcelain").stdout
    return {os.path.realpath(ln[9:]) for ln in out.splitlines() if ln.startswith("worktree ")}


def _gone(path: str, repo_path: str) -> bool:
    return not os.path.exists(path) and os.path.realpath(path) not in _worktree_paths(repo_path)


def _drop_branch(repo_path: str, branch: str) -> str:
    if not branch or _git(repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        return "로컬 브랜치 없음"
    used = _git(repo_path, "worktree", "list", "--porcelain").stdout + "\n"
    if f"branch refs/heads/{branch}\n" in used:
        return f"로컬 브랜치 {branch} 유지 (다른 워크트리가 사용 중)"
    _git(repo_path, "branch", "-D", branch, check=True)
    return f"로컬 브랜치 {branch} 삭제"


def remove(con, path: str, repo_path: str, branch: str, wait_s: int = 120, force: bool = False) -> str | None:
    """git worktree remove (force: a skipped ticket's local changes go too), then delete the local branch.
    Legacy Orca worktrees: orca worktree rm, then wait for it (Orca answers before the checkout is gone).
    Returns a note, or None while an Orca removal is still in progress (finished on a later tick)."""
    with worktree_dev.stopped(con, path):
        if not _legacy(path):
            R.remove(repo_path, path, force=force)
            return "워크트리 삭제, " + _drop_branch(repo_path, branch)
        orca_json(["worktree", "rm", "--worktree", f"path:{path}", *(["--force"] if force else [])], timeout=300)
        for _ in range(wait_s // 3):
            if _gone(path, repo_path):
                return "워크트리 삭제, " + _drop_branch(repo_path, branch)
            time.sleep(3)
        return None


def _finish(con, path: str, runs: list, base: dict, note: str, label: str, methods: str, how: str, key: str, gid: str,
            skipped: bool = False):
    ids = [r["id"] for r in runs]
    con.execute(f"UPDATE runs SET removed_at=?, state=CASE WHEN state='active' THEN 'finished' ELSE state END, "
                f"updated_at=? WHERE id IN ({','.join('?' * len(ids))})", (now(), now(), *ids))
    _upsert(con, path, status="removed", reason=note, removed_at=now(), **base)
    why = "건너뜀 티켓" if skipped else f"프로덕션 반영 확인 ({methods}; {how})"
    event(con, "cleanup", f"{key}: {why} → {label} {note}", gid)
    con.commit()


# ---------------------------------------------------------------- scan

def _upsert(con, path: str, **kw):
    cols = ["path", *kw]
    con.execute(f"INSERT INTO cleanups({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in kw)}", (path, *kw.values()))


def _candidates(con):
    """Ticket worktrees (one entry per path) of not yet removed runs."""
    rows = con.execute("""SELECT r.*, t.key, t.stage, rp.path repo_path, rp.github FROM runs r
                          JOIN tickets t ON t.gid=r.ticket_gid LEFT JOIN repos rp ON rp.name=r.repo
                          WHERE r.removed_at IS NULL AND r.worktree_path IS NOT NULL ORDER BY r.id""").fetchall()
    by_path: dict[str, list] = {}
    for r in rows:
        by_path.setdefault(r["worktree_path"], []).append(r)
    return by_path


def _clean_skipped(con, path: str, runs: list, wt: dict, ps: dict, dry_run: bool, stats: dict, log) -> None:
    """Remove the worktree of a skipped ticket (no production check, see module doc)."""
    last = runs[-1]
    branch = (wt.get("branch") or "").replace("refs/heads/", "") or last["branch"] or ""
    base = dict(ticket_gid=last["ticket_gid"], key=last["key"], repo=last["repo"], branch=branch, checked_at=now())
    stats["checked"] += 1
    prev = con.execute("SELECT status, reason FROM cleanups WHERE path=?", (path,)).fetchone()
    why = safety(con, path, last["repo_path"], last["github"], branch, None, wt, ps)
    if why:
        _upsert(con, path, status="kept", reason=why, **base)
        if not prev or prev["status"] != "kept" or prev["reason"] != why:
            event(con, "cleanup", f"{last['key']}: 건너뜀 티켓, 워크트리 유지: {why}", last["ticket_gid"])
        stats["kept"] += 1
        con.commit()
        log(f"cleanup {last['key']}: skipped, kept ({why})")
        return
    if dry_run:
        log(f"cleanup {last['key']}: would remove {path} (skipped ticket)")
        stats["removed"] += 1
        return
    _upsert(con, path, status="removing", reason="worktree 삭제 중", **base)
    con.commit()
    note = remove(con, path, last["repo_path"], branch, force=True)
    if note is None:
        log(f"cleanup {last['key']}: orca worktree rm still running, finishing next tick")
        return
    _finish(con, path, runs, base, note, wt.get("displayName") or os.path.basename(path), "", "", last["key"],
            last["ticket_gid"], skipped=True)
    stats["removed"] += 1
    log(f"cleanup {last['key']}: removed {path} (skipped ticket); {note}")


def cleanup_scan(dry_run: bool = False, log=print) -> dict:
    con = db()
    cfg = track_config()
    wts = {w["path"]: w for w in R.worktrees([(r["name"], r["path"]) for r in con.execute("SELECT name, path FROM repos")])}
    ps = _ps_entries()
    deployed: dict[str, tuple[str | None, str]] = {}
    stats = {"checked": 0, "removed": 0, "kept": 0, "waiting": 0, "ambiguous": 0}
    for path, runs in _candidates(con).items():
        last = runs[-1]
        wt = wts.get(os.path.realpath(path))
        if wt:
            wt = wt | {"handles": [r["terminal"] for r in runs if R.is_handle(r["terminal"])]}
        if not wt:
            prev = con.execute("SELECT * FROM cleanups WHERE path=?", (path,)).fetchone()
            if prev and prev["status"] == "removing" and last["repo_path"] and _gone(path, last["repo_path"]):
                note = "워크트리 삭제, " + _drop_branch(last["repo_path"], prev["branch"] or "")
                res = json.loads(prev["prs_json"] or "[]")
                _finish(con, path, runs, {k: prev[k] for k in ("ticket_gid", "key", "repo", "branch", "prs_json", "heads", "prod_sha", "prod_note", "checked_at")},
                        note, os.path.basename(path), ", ".join(f"#{r['number']} {r['live']}" for r in res), prev["prod_note"], last["key"], last["ticket_gid"],
                        skipped=last["stage"] == "skipped")
                stats["removed"] += 1
                log(f"cleanup {last['key']}: removal finished ({note})")
            continue  # gone (scan() handles active runs whose worktree vanished)
        if last["stage"] == "skipped" and last["repo_path"]:
            try:
                _clean_skipped(con, path, runs, wt, ps, dry_run, stats, log)
            except Exception as e:  # noqa: BLE001
                con.rollback()
                _upsert(con, path, status="error", reason=str(e)[:300], key=last["key"], repo=last["repo"], checked_at=now())
                event(con, "error", f"cleanup {last['key']}: {str(e)[:300]}", last["ticket_gid"])
                con.commit()
            continue
        if last["repo"] not in cfg or not last["repo_path"] or not last["github"]:
            continue
        ids = [r["id"] for r in runs]
        prs = con.execute(f"SELECT * FROM prs WHERE run_id IN ({','.join('?' * len(ids))})", ids).fetchall()
        if not prs or any(p["state"] == "OPEN" for p in prs):
            continue
        merged = [p for p in prs if p["state"] == "MERGED"]
        if not merged:
            continue  # closed without merge: nothing reaches production, keep it for the owner
        stats["checked"] += 1
        branch = (wt.get("branch") or "").replace("refs/heads/", "") or last["branch"] or ""
        try:
            if last["repo"] not in deployed:
                deployed[last["repo"]] = deployed_sha(last["github"], cfg[last["repo"]])
            prod, how = deployed[last["repo"]]
            if not prod:
                continue
            prev = con.execute("SELECT * FROM cleanups WHERE path=?", (path,)).fetchone()
            heads = sorted(p["head_sha"] or "" for p in merged)
            if prev and prev["prod_sha"] == prod and prev["status"] in ("waiting", "ambiguous") and prev["heads"] == ",".join(heads) \
                    and not (prev["agent_sha"] == prod and prev["agent_verdict"] == "live"):
                stats[prev["status"]] += 1
                continue  # same production and same PRs as last time: nothing new to compare
            if prev and prev["status"] == "kept" and prev["prod_sha"] == prod and prev["heads"] == ",".join(heads):
                results = json.loads(prev["prs_json"])  # already found live; only the safety checks can change
            else:
                fetched: set = set()
                results = [check_pr(con, last["repo_path"], last["github"], p, last["key"], prod, fetched) for p in merged]
            agent_ok = prev and prev["agent_sha"] == prod and prev["agent_verdict"] == "live" and prev["heads"] == ",".join(heads)
            if agent_ok:
                for r in results:
                    r["live"] = r["live"] or "agent"
            pending = [r for r in results if not r["live"]]
            base = dict(ticket_gid=last["ticket_gid"], key=last["key"], repo=last["repo"], branch=branch,
                        prs_json=json.dumps(results, ensure_ascii=False), heads=",".join(heads), prod_sha=prod,
                        prod_note=how, checked_at=now())
            if pending:
                status = "ambiguous" if any(r["signals"] for r in pending) and not (prev and prev["agent_sha"] == prod) else "waiting"
                _upsert(con, path, status=status, reason=", ".join(f"PR #{r['number']} 프로덕션 미반영" for r in pending), **base)
                stats[status] += 1
                con.commit()
                continue
            keep = [r["head_sha"] for r in results] + [prod]
            why = safety(con, path, last["repo_path"], last["github"], branch, keep, wt, ps)
            methods = ", ".join(f"#{r['number']} {r['live']}" for r in results)
            if why:
                _upsert(con, path, status="kept", reason=why, **base)
                if not prev or prev["status"] != "kept" or prev["reason"] != why:
                    event(con, "cleanup", f"{last['key']}: 프로덕션 반영 확인 ({methods}; {how}), 워크트리 유지: {why}", last["ticket_gid"])
                stats["kept"] += 1
                con.commit()
                log(f"cleanup {last['key']}: live, kept ({why})")
                continue
            if dry_run:
                log(f"cleanup {last['key']}: would remove {path} ({methods}; {how})")
                stats["removed"] += 1
                continue
            _upsert(con, path, status="removing", reason="worktree 삭제 중", **base)
            con.commit()
            note = remove(con, path, last["repo_path"], branch)
            if note is None:
                log(f"cleanup {last['key']}: orca worktree rm still running, finishing next tick")
                continue
            _finish(con, path, runs, base, note, wt.get("displayName") or os.path.basename(path), methods, how, last["key"], last["ticket_gid"])
            stats["removed"] += 1
            log(f"cleanup {last['key']}: removed {path} ({methods}; {how}); {note}")
        except Exception as e:  # noqa: BLE001
            con.rollback()
            _upsert(con, path, status="error", reason=str(e)[:300], key=last["key"], repo=last["repo"], checked_at=now())
            event(con, "error", f"cleanup {last['key']}: {str(e)[:300]}", last["ticket_gid"])
            con.commit()
    log("cleanup: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    if not dry_run:
        docker_prune(con, log)
    return stats


DOCKER_PRUNE = [  # 2026-10-22: agent test builds filled the disk (~20G build cache/day)
    ["builder", "prune", "-a", "-f", "--filter", "until=24h"],
    ["image", "prune", "-a", "-f", "--filter", "until=24h"],  # only images no container (running or stopped) uses
]


def docker_prune(con, log=print):
    """Drop Docker build cache and unused images older than a day. Containers and volumes are never touched."""
    if not shutil.which("docker"):  # no Docker here (e.g. inside tp's own container, TP-164)
        return
    if run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, timeout=30).returncode != 0:
        return  # Docker Desktop not running
    freed = []
    for args in DOCKER_PRUNE:
        p = run(["docker", *args], check=False, timeout=600)
        m = re.search(r"Total(?: reclaimed space)?:\s*(\S+)", p.stdout)
        if p.returncode != 0:
            event(con, "error", f"docker {args[0]} prune: {(p.stderr or p.stdout).strip()[-300:]}")
        elif m and m.group(1) not in ("0B", "0"):
            freed.append(f"{args[0]} {m.group(1)}")
    if freed:
        event(con, "cleanup", "Docker 정리: " + ", ".join(freed))
        log("cleanup docker: " + ", ".join(freed))
    con.commit()


def cleanup_queue() -> list[dict]:
    """Worktrees the agent should judge: PRs not mechanically found in production, but with signs that they may be."""
    con = db()
    out = []
    for c in con.execute("""SELECT c.*, rp.path repo_path, rp.github FROM cleanups c LEFT JOIN repos rp ON rp.name=c.repo
                            WHERE c.status='ambiguous' AND (c.agent_sha IS NULL OR c.agent_sha != c.prod_sha)"""):
        cfg = track_config().get(c["repo"], {})
        out.append({"path": c["path"], "key": c["key"], "repo": c["repo"], "github": c["github"], "repo_path": c["repo_path"],
                    "branch": c["branch"], "prod_branch": cfg.get("prod_branch"), "prod_sha": c["prod_sha"],
                    "prod_note": c["prod_note"], "prs": [p for p in json.loads(c["prs_json"] or "[]") if not p.get("live")]})
    return out


def save_verdict(path: str, prod_sha: str, verdict: str, note: str):
    con = db()
    c = con.execute("SELECT * FROM cleanups WHERE path=?", (path,)).fetchone()
    if not c:
        raise SystemExit(f"no cleanup row for {path}")
    if c["prod_sha"] != prod_sha:
        raise SystemExit(f"production moved on ({c['prod_sha'][:7]} != {prod_sha[:7]}); run the queue again")
    con.execute("UPDATE cleanups SET agent_verdict=?, agent_note=?, agent_sha=?, status=? WHERE path=?",
                (verdict, note, prod_sha, "ambiguous" if verdict == "live" else "waiting", path))
    event(con, "cleanup", f"{c['key']}: 에이전트 코드 비교 → {'프로덕션 반영' if verdict == 'live' else '미반영'} ({note})", c["ticket_gid"])
    con.commit()
    print(f"{c['key']}: {verdict}")
    if verdict == "live":
        cleanup_scan()  # removes it now if the safety checks pass
