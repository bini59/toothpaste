"""tp check: ticket-pilot runs each repo's standard checks on a PR head commit itself (TP-47).

Evidence order: deterministic checks (this module) > independent AI review > the agent's own report in the PR body.

settings repo.<name>.checks (설정 page / tp config) = [{"name", "cmd", "dir"?}] runs locally; "ci": true reads the PR's
GitHub checks (gh pr checks) instead. A repo with neither has no checks and keeps the old flow (no gate).
Subtask PRs into a milestone branch skip the GitHub CI gate (2026-11-04): CI usually does not run on
milestone/* bases; the milestone PR into staging is gated by CI as usual. Local checks still run on them.

Flow (host review precheck, before the review queue):
  tick()   CI results are read in place; local checks start a detached `tp check --pr` (a repo's checks can
           outlive the 600 s precheck timeout). A failed head is sent back to the agent.
  run_pr() one PR head: `git archive <sha>` into a temp dir (worktree untouched), node_modules linked or installed,
           every check with a timeout, a failed one rerun once (pass on rerun = flaky), rows in `checks`,
           logs in data/checks/. The (pr_url, head_sha, name) unique key is the lock: one run per head.
  A failure is stored as a reviews row (source='check', verdict 'changes'), so the rework flow, its round cap and
  the escalation to the owner apply unchanged; review_queue skips the head until it passes.
  The owner can override a head's checks with a reason (tickets.check_override_sha = that head): the head then skips
  the rework, goes to the AI review, and its approval is not gated by the checks (an AI approve is still required).
  A head is run again when tp could not run it (error, once), when the configured checks changed, or when a run died.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import DATA, ROOT, db, event, now, project_map, run, settings

OK = ("pass", "flaky")
FAILED = ("fail", "timeout")      # the change broke it: back to the agent
PENDING = ("running", "ci_pending")
LOG_DIR = DATA / "checks"
LOCKS = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"))
INSTALL = {"pnpm": "pnpm install --frozen-lockfile", "yarn": "yarn install --frozen-lockfile", "npm": "npm ci"}
TAIL_LINES = 80
CI_MISSING_MIN = 30  # a CI repo whose PR still reports no checks this long after its last update is an error
CI_REREAD_MIN = 10   # a finished CI head that did not pass is read again this often (a re-run on GitHub shows up)
ERROR_RETRIES = 1    # a head whose checks tp could not run (error) is run again this many times


def conf() -> dict:
    c = settings().get("check") or {}
    return {"timeout_min": float(c.get("timeout_min", 10)), "max_parallel": int(c.get("max_parallel", 2))}


def repo_checks(repo: str | None) -> dict | None:
    """{"ci": bool, "checks": [...]} or None when the repo has no checks (no gate, old flow)."""
    r = (project_map().get("repos") or {}).get(repo or "") or {}
    if r.get("ci"):
        return {"ci": True, "checks": []}
    # off = switched off on the 설정 page with a reason (e.g. already failing on the base branch); not run
    on = [{k: c[k] for k in ("name", "cmd", "dir") if k in c} for c in r.get("checks") or [] if not c.get("off")]
    if on:
        return {"ci": False, "checks": on}
    return None


def pr_checks(pr) -> dict | None:
    """repo_checks for this PR: a CI repo's subtask PR into a milestone branch has no checks (CI does not run there)."""
    cfg = repo_checks(pr["repo"])
    if cfg and cfg["ci"] and (pr["base"] or "").startswith("milestone/"):
        return None
    return cfg


def head_rows(con, pr_url: str, sha: str | None) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM checks WHERE pr_url=? AND head_sha=? ORDER BY id", (pr_url, sha))]


def overridden(con, pr) -> bool:
    """The owner chose to ignore the tp checks of this head commit (with a reason)."""
    t = con.execute("SELECT check_override, check_override_sha FROM tickets WHERE gid=?", (pr["ticket_gid"],)).fetchone()
    return bool(t and t["check_override"] and t["check_override_sha"] and t["check_override_sha"] == pr["head_sha"])


def withdraw(con, pr) -> bool:
    """Drop this head's check failure review that was not sent to the agent yet (the head was overridden, or a rerun
    no longer fails). One already sent stays: it counts as a rework round."""
    return con.execute("DELETE FROM reviews WHERE pr_url=? AND head_sha=? AND source='check' AND rework_at IS NULL",
                       (pr["url"], pr["head_sha"])).rowcount > 0


def check_state(con, pr) -> dict:
    """Checks of the PR's current head: state none | pending | pass | fail | error, plus the rows.
    Pending wins over fail so a half-finished run is not reported (or sent back) early."""
    cfg = pr_checks(pr)
    if not cfg:
        return {"state": "none", "rows": []}
    rows = head_rows(con, pr["url"], pr["head_sha"])
    have = {r["name"] for r in rows}
    missing = [c["name"] for c in cfg["checks"] if c["name"] not in have]
    st = [r["status"] for r in rows]
    # a local head that is due for another run (error retry, changed config) is pending until that run claims it
    if missing or not rows or any(s in PENDING for s in st) or (not cfg["ci"] and _due(rows, cfg)):
        state = "pending"
    elif any(s in FAILED for s in st):
        state = "fail"
    elif any(s not in OK for s in st):
        state = "error"
    else:
        state = "pass"
    return {"state": state, "rows": rows}


def gate_error(con, pr, override_sha: str | None = None) -> str | None:
    """Approval gate (release.approval_target): every check of the head commit must be pass/flaky.
    The owner's override (a reason, per head commit) lets a failing head through."""
    st = check_state(con, pr)
    if st["state"] in ("none", "pass") or (override_sha and override_sha == pr["head_sha"]):
        return None
    names = lambda pred: ", ".join(r["name"] for r in st["rows"] if pred(r["status"]))  # noqa: E731
    if st["state"] == "fail":
        return f"tp 검사 실패: {names(lambda s: s in FAILED)}"
    if st["state"] == "error":
        return f"tp 검사 오류: {names(lambda s: s not in OK)}"
    return "tp 검사 아직 안 끝났어요"


def tail(path: str | None, n: int = TAIL_LINES) -> str:
    if not path or path.startswith("http") or not Path(path).is_file():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 200_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    return "\n".join(lines[-n:])


def _age_min(ts: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat((ts or "").replace("Z", "+00:00"))).total_seconds() / 60
    except ValueError:
        return 1e9


# ---------------------------------------------------------------- after a run: send a failed head back

def _failure_body(rows: list[dict]) -> tuple[str, str]:
    bad = [r for r in rows if r["status"] in FAILED]
    summary = f"tp 검사 실패: {', '.join(r['name'] for r in bad)}"
    parts = ["ticket-pilot ran the repo's standard checks on this head commit itself (`tp check`, a clean "
             "`git archive` copy) and they failed. Fix the code (or the test, if the test is wrong) so they pass, and "
             "run the same commands yourself before pushing. If a check also fails on the base branch without your "
             "change, do not work around it: explain it on the PR and mark the workspace BLOCKED for the owner."]
    for r in bad:
        what = "timed out" if r["status"] == "timeout" else f"exit {r['exit_code']}"
        parts.append(f"### {r['name']}: {r['status']} ({what})\n\n- command: `{r['cmd']}`\n- log: {r['log_path']}\n\n"
                     f"Last {TAIL_LINES} lines:\n\n~~~\n{tail(r['log_path']) or '(no log)'}\n~~~")
    return summary, "\n\n".join(parts)


def settle(con, pr, log=print) -> str:
    """Act on a finished head: failed -> a 'changes' review from tp (rework flow). Returns the check state, or
    'override' when the owner chose to ignore the checks of this head."""
    st = check_state(con, pr)
    over = st["state"] not in ("none", "pass") and overridden(con, pr)
    if (over or st["state"] in ("none", "pass", "error")) and withdraw(con, pr):
        # The owner overrode the head, or a rerun no longer fails, before the failure reached the agent
        from .pipeline import restage
        restage(con)
        con.commit()
    if over:
        return "override"
    if st["state"] != "fail":
        return st["state"]
    if con.execute("SELECT 1 FROM reviews WHERE pr_url=? AND head_sha=? AND source='check'",
                   (pr["url"], pr["head_sha"])).fetchone():
        return "fail"
    summary, body = _failure_body(st["rows"])
    con.execute("""INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict,summary,body,posted,created_at,source)
                   VALUES(?,?,?,'changes',?,?,0,?,'check')""",
                (pr["url"], pr["ticket_gid"], pr["head_sha"], summary, body, now()))
    # a dashboard approval of this head can no longer merge
    con.execute("UPDATE tickets SET approve_requested_at=NULL WHERE gid=?", (pr["ticket_gid"],))
    t = con.execute("SELECT key FROM tickets WHERE gid=?", (pr["ticket_gid"],)).fetchone()
    key = t["key"] if t else "?"
    event(con, "check_fail", f"{key}: PR #{pr['number']} ({(pr['head_sha'] or '')[:7]}) {summary} -> back to the agent",
          pr["ticket_gid"])
    from .pipeline import restage
    restage(con)
    con.commit()
    log(f"check fail {key} PR #{pr['number']}: {summary}")
    return "fail"


def _escalate_unchanged(con, p, log=print) -> bool:
    """The agent marked in-review again without a new commit after a check failure: the head still fails, so
    no review can pass it. Ask the owner (BLOCKED) instead of looping; their reply relaunches the agent."""
    rv = con.execute("SELECT * FROM reviews WHERE pr_url=? ORDER BY id DESC LIMIT 1", (p["url"],)).fetchone()
    if not (rv and rv["source"] == "check" and rv["head_sha"] == p["head_sha"] and rv["rework_at"]
            and p["ws_status"] in ("in-review", "completed") and not (p["ws_comment"] or "").upper().startswith("BLOCKED")
            and p["worktree_path"]):
        return False
    q = (f"BLOCKED: tp 검사가 계속 실패해요 (PR #{p['number']}, 새 커밋 없음). "
         f"에이전트 설명을 PR에서 확인하고 답장해 주세요. {rv['summary'] or ''}")[:240]
    from .pr_tracking import mirror_orca
    mirror_orca(con.execute("SELECT * FROM runs WHERE id=?", (p["run_id"],)).fetchone(), "todo", q, unread=True)
    con.execute("UPDATE runs SET ws_status='todo', ws_comment=?, updated_at=? WHERE id=?", (q, now(), p["run_id"]))
    from . import notifications as N
    N.blocked(con, p["ticket_gid"], q, p["repo"])
    event(con, "check_fail", f"{p['key']}: PR #{p['number']} still fails tp checks without a new commit -> asked the owner",
          p["ticket_gid"])
    from .pipeline import restage
    restage(con)
    con.commit()
    log(f"check escalate {p['key']}: asked the owner")
    return True


# ---------------------------------------------------------------- CI repos (gh pr checks)

CI_STATUS = {"pass": "pass", "fail": "fail", "pending": "ci_pending", "cancel": "error"}


def sync_ci(con, p) -> None:
    """Copy the PR head's GitHub checks into `checks` (same shape as local checks). Re-read while any is pending,
    and every CI_REREAD_MIN while any did not pass (a cancelled or failed run re-run on GitHub)."""
    rows = head_rows(con, p["url"], p["head_sha"])
    if rows and not any(r["status"] == "ci_pending" for r in rows):
        if all(r["status"] in OK for r in rows):
            return
        if _age_min(max(r["finished_at"] or r["created_at"] or "" for r in rows)) < CI_REREAD_MIN:
            return
    res = run(["gh", "pr", "checks", str(p["number"]), "--repo", p["github"], "--json", "name,state,bucket,link,workflow"],
              timeout=120, check=False)
    try:
        items = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        items = []
    new = []
    for i in items:
        st = CI_STATUS.get(i.get("bucket"))
        if st:  # "skipping" checks did not run; they neither pass nor block
            new.append((i.get("name"), f"GitHub CI: {i.get('workflow') or '-'} / {i.get('name')}", st, i.get("link")))
    if not new:
        if items:  # every check was skipped: nothing ran, so nothing is verified
            new = [("ci", "GitHub CI: every check skipped", "error", None)]
        elif "no checks reported" not in (res.stderr or ""):
            return  # gh failed (network, auth): try again next tick
        else:
            new = [("ci", "GitHub CI", "error" if _age_min(p["updated_at"]) > CI_MISSING_MIN else "ci_pending", None)]
    ts = now()
    con.execute("DELETE FROM checks WHERE pr_url=? AND head_sha=?", (p["url"], p["head_sha"]))
    for name, cmd, st, link in new:
        con.execute("""INSERT OR REPLACE INTO checks(pr_url,ticket_gid,head_sha,name,cmd,status,log_path,created_at,finished_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (p["url"], p["ticket_gid"], p["head_sha"], name, cmd, st, link, ts, None if st == "ci_pending" else ts))
    con.commit()


# ---------------------------------------------------------------- local checks

def _targets(con):
    """Same PRs review_queue looks at: open, agent done (in-review/completed), run not abandoned."""
    return con.execute("""
      SELECT p.*, t.key, r.worktree_path, r.ws_status, r.ws_comment, r.id run_id, rp.path repo_path
      FROM prs p JOIN runs r ON r.id=p.run_id JOIN tickets t ON t.gid=p.ticket_gid LEFT JOIN repos rp ON rp.name=p.repo
      WHERE p.state='OPEN' AND r.ws_status IN ('in-review','completed') AND r.state != 'abandoned'
    """).fetchall()


def _stale_min(cfg) -> float:
    # a run that died (reboot, kill) leaves 'running' rows; every check may take two timeouts (rerun) + install
    return (len(cfg["checks"]) * 2 + 2) * conf()["timeout_min"]


def _live(rows, cfg) -> bool:
    return any(r["status"] == "running" and _age_min(r["created_at"]) < _stale_min(cfg) for r in rows)


def _dead(rows, cfg) -> bool:
    """A run that stopped mid-way (some rows still 'running' but too old): start that head over."""
    return any(r["status"] == "running" for r in rows) and not _live(rows, cfg)


def _attempt(rows) -> int:
    return max((r.get("attempt") or 1 for r in rows), default=0)


def _due(rows, cfg) -> bool:
    """Start (again) the local checks of a head: none yet, a run that died, the configured checks changed since the
    run (added, renamed, new command), or tp could not run them (error) and they were not retried yet."""
    if not rows or _dead(rows, cfg):
        return True
    if _live(rows, cfg):
        return False
    if {(r["name"], r["cmd"]) for r in rows} != {(c["name"], c["cmd"]) for c in cfg["checks"]}:
        return True
    return any(r["status"] == "error" for r in rows) and _attempt(rows) <= ERROR_RETRIES


def tick(log=print, spawn=None) -> dict:
    """Review precheck step: refresh CI results, start local checks for heads without any, send failures back."""
    con = db()
    spawn = spawn or _spawn
    targets = [(p, pr_checks(p)) for p in _targets(con)]
    targets = [(p, cfg) for p, cfg in targets if cfg]
    busy = sum(1 for p, cfg in targets if not cfg["ci"] and _live(head_rows(con, p["url"], p["head_sha"]), cfg))
    started, out = [], {}
    for p, cfg in targets:
        if cfg["ci"]:
            try:
                sync_ci(con, p)
            except Exception as e:  # noqa: BLE001
                event(con, "error", f"{p['key']}: gh pr checks: {str(e)[:300]}", p["ticket_gid"])
                con.commit()
        else:
            rows = head_rows(con, p["url"], p["head_sha"])
            if _due(rows, cfg) and busy < conf()["max_parallel"]:
                spawn(p["url"], p["head_sha"])
                started.append(p["key"])
                busy += 1
        st = settle(con, p, log=log)
        if st == "fail":
            _escalate_unchanged(con, p, log=log)
        out[p["key"]] = st
    log(f"check: started={started} states={out}")
    return {"started": started, "states": out}


def _spawn(pr_url: str, sha: str):
    """Detached `tp check --pr`: survives the precheck's exit and its 600 s timeout."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "runner.log", "a") as f:
        subprocess.Popen([sys.executable, "-m", "ticket_pilot.cli", "check", "--pr", pr_url, "--sha", sha],
                         cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})


def _claim(con, p, cfg, logs: dict) -> bool:
    """Insert 'running' rows for every check of this head; False if another run holds (or finished) them."""
    con.commit()
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = head_rows(con, p["url"], p["head_sha"])
        if not _due(rows, cfg):  # running now, or already finished for this head
            con.rollback()
            return False
        # a dead run, changed config or an error retry: start over; attempt counts the error retries
        attempt = _attempt(rows) + (1 if any(r["status"] == "error" for r in rows) else 0) or 1
        con.execute("DELETE FROM checks WHERE pr_url=? AND head_sha=?", (p["url"], p["head_sha"]))
        for c in cfg["checks"]:
            con.execute("""INSERT INTO checks(pr_url,ticket_gid,head_sha,name,cmd,status,log_path,created_at,attempt)
                           VALUES(?,?,?,?,?,'running',?,?,?)""",
                        (p["url"], p["ticket_gid"], p["head_sha"], c["name"], c["cmd"], str(logs[c["name"]]), now(),
                         attempt))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        con.rollback()
        return False


def _tool_env() -> dict:
    """Automations and launchd start with a thin PATH; node usually comes from nvm."""
    env = {**os.environ, "CI": "1", "HUSKY": "0", "FORCE_COLOR": "0", "NO_COLOR": "1"}
    if not shutil.which("node", path=env.get("PATH")):
        vers = sorted((Path.home() / ".nvm" / "versions" / "node").glob("v*/bin"),
                      key=lambda b: [int(x) for x in b.parent.name[1:].split(".") if x.isdigit()])
        if vers:
            env["PATH"] = f"{vers[-1]}:{env.get('PATH', '')}"
    return env


def _exec(cmd: str, cwd: Path, log_path: Path, timeout_s: float, env: dict) -> tuple[str, int | None, float]:
    """Run one shell command, appending to its log. Returns (pass|fail|timeout, exit code, seconds)."""
    t0 = time.time()
    with open(log_path, "a") as f:
        f.write(f"$ {cmd}\n")
        f.flush()
        p = subprocess.Popen(["/bin/bash", "-c", cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        try:
            rc = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            f.write(f"\n[tp check] timed out after {timeout_s / 60:.0f} min\n")
            return "timeout", None, time.time() - t0
        f.write(f"\n[tp check] exit {rc} in {time.time() - t0:.1f}s\n")
    return ("pass" if rc == 0 else "fail"), rc, time.time() - t0


def _manager(d: Path) -> tuple[str | None, str | None]:
    """(lockfile, package manager) of a package dir: package.json packageManager first, then the lockfile present."""
    try:
        pm = (json.loads((d / "package.json").read_text()).get("packageManager") or "").split("@")[0]
    except (OSError, ValueError):
        pm = ""
    for lock, m in LOCKS:
        if (d / lock).is_file() and (not pm or pm == m):
            return lock, m
    return None, pm or None


def prepare(tmp: Path, dirs: list[str], sources: list[str], log_path: Path, env: dict, timeout_s: float):
    """node_modules for each package dir the checks use: link the worktree's (else the host checkout's) when its
    lockfile matches the commit's, else install from the lockfile inside the temp copy."""
    for d in dict.fromkeys(dirs):
        td = tmp / d
        if not (td / "package.json").is_file() or (td / "node_modules").exists():
            continue
        lock, mgr = _manager(td)
        for s in sources:
            nm, sl = Path(s) / d / "node_modules", Path(s) / d / (lock or "-")
            if nm.is_dir() and (not lock or (sl.is_file() and sl.read_bytes() == (td / lock).read_bytes())):
                (td / "node_modules").symlink_to(nm)
                with open(log_path, "a") as f:
                    f.write(f"[tp check] {d}/node_modules -> {nm}\n")
                break
        else:
            if not mgr:
                continue
            st, rc, _ = _exec(INSTALL[mgr], td, log_path, timeout_s, env)
            if st != "pass":
                raise RuntimeError(f"{INSTALL[mgr]} in {d} failed ({st}, exit {rc})")


def _archive(src: str, sha: str, dest: Path):
    p1 = subprocess.Popen(["git", "-C", src, "archive", sha], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=p1.stdout, capture_output=True)
    p1.stdout.close()
    err = p1.stderr.read().decode()
    if p1.wait() != 0 or p2.returncode != 0:
        raise RuntimeError(f"git archive {sha[:7]} in {src}: {err.strip()[-300:] or p2.stderr.decode()[-300:]}")


def _log_name(repo: str, number, sha: str, name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return LOG_DIR / f"{repo}-{number}-{sha[:7]}-{safe}.log"


def run_checks(src_dirs: list[str], sha: str, cfg: dict, logs: dict, on_done, fetch=None) -> None:
    """Archive `sha`, prepare dependencies, run every check (rerun a failure once). on_done(name, status, rc, secs)."""
    env, timeout_s = _tool_env(), conf()["timeout_min"] * 60
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for c in cfg["checks"]:
        logs[c["name"]].write_text(f"[tp check] {c['name']} @ {sha[:12]} ({now()})\n")
    tmp = Path(tempfile.mkdtemp(prefix="tp-check-"))
    done = set()
    try:
        srcs = [s for s in src_dirs if s and Path(s).is_dir()]
        if not srcs:
            raise RuntimeError("no checkout to archive from")
        for s in srcs:
            try:
                _archive(s, sha, tmp)
                break
            except RuntimeError:
                if s != srcs[-1]:
                    continue
                if not fetch:  # neither checkout has the commit
                    raise
                fetch(s)
                _archive(s, sha, tmp)
        first = logs[cfg["checks"][0]["name"]]
        prepare(tmp, [c.get("dir") or "." for c in cfg["checks"]], srcs, first, env, timeout_s)
        for c in cfg["checks"]:
            cwd = tmp / (c.get("dir") or ".")
            st, rc, secs = _exec(c["cmd"], cwd, logs[c["name"]], timeout_s, env)
            if st == "fail":
                with open(logs[c["name"]], "a") as f:
                    f.write("\n[tp check] failed; running it once more\n")
                st2, rc2, secs2 = _exec(c["cmd"], cwd, logs[c["name"]], timeout_s, env)
                st, rc, secs = ("flaky" if st2 == "pass" else st2), rc2, secs + secs2
            on_done(c["name"], st, rc, secs)
            done.add(c["name"])
    except Exception as e:  # noqa: BLE001  (tp could not run the checks: not the change's fault)
        for c in cfg["checks"]:
            if c["name"] not in done:
                with open(logs[c["name"]], "a") as f:
                    f.write(f"\n[tp check] error: {e}\n")
                on_done(c["name"], "error", None, 0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_PR_SQL = """SELECT p.*, t.key, r.worktree_path, r.ws_status, r.ws_comment, r.id run_id, rp.path repo_path
             FROM prs p JOIN runs r ON r.id=p.run_id JOIN tickets t ON t.gid=p.ticket_gid
             LEFT JOIN repos rp ON rp.name=p.repo WHERE p.url=?"""


def run_pr(pr_url: str, sha: str | None = None, log=print) -> str:
    """Run the local checks of one PR head (detached from tick(), or by hand). Returns the check state."""
    con = db()
    p = con.execute(_PR_SQL, (pr_url,)).fetchone()
    if not p:
        raise SystemExit(f"unknown PR {pr_url}; run tp scan first")
    cfg = pr_checks(p)
    if not cfg or cfg["ci"]:
        log(f"{p['repo']}: no local checks")
        return check_state(con, p)["state"]
    if sha and sha != p["head_sha"]:
        log(f"PR head moved to {(p['head_sha'] or '')[:7]}, skipping {sha[:7]}")
        return "stale"
    sha = p["head_sha"]
    logs = {c["name"]: _log_name(p["repo"], p["number"], sha, c["name"]) for c in cfg["checks"]}
    if not _claim(con, p, cfg, logs):
        log(f"{p['key']} {sha[:7]}: checks already running or done")
        return check_state(con, p)["state"]

    def on_done(name, st, rc, secs):
        con.execute("""UPDATE checks SET status=?, exit_code=?, duration_s=?, finished_at=?
                       WHERE pr_url=? AND head_sha=? AND name=?""", (st, rc, round(secs, 1), now(), p["url"], sha, name))
        con.commit()
        log(f"  {name}: {st} ({secs:.0f}s)")

    def fetch(src):  # objects only; no branch or working tree changes
        run(["git", "-C", src, "fetch", "origin", f"pull/{p['number']}/head"], timeout=300, check=False)

    log(f"check {p['key']} PR #{p['number']} {sha[:7]}")
    run_checks([p["worktree_path"], p["repo_path"]], sha, cfg, logs, on_done, fetch=fetch)
    p = con.execute(_PR_SQL, (pr_url,)).fetchone()
    return settle(con, p, log=log) if p["head_sha"] == sha else "stale"


def baseline(repo: str, ref: str, log=print) -> list[dict]:
    """Run a repo's configured checks on a ref of its host checkout (e.g. origin/staging) without touching the DB,
    to confirm they pass on the base before the gate is turned on."""
    pm = (project_map().get("repos") or {}).get(repo)
    cfg = repo_checks(repo)
    if not pm or not cfg or cfg["ci"]:
        raise SystemExit(f"{repo}: no local checks configured")
    src = os.path.expanduser(pm["path"])
    sha = run(["git", "-C", src, "rev-parse", ref]).stdout.strip()
    logs = {c["name"]: _log_name(repo, "base", sha, c["name"]) for c in cfg["checks"]}
    cmds = {c["name"]: c["cmd"] for c in cfg["checks"]}
    out = []

    def on_done(n, st, rc, s):
        out.append({"repo": repo, "ref": ref, "sha": sha[:7], "name": n, "cmd": cmds[n], "status": st,
                    "exit_code": rc, "duration_s": round(s, 1), "log": str(logs[n])})
        log(json.dumps(out[-1], ensure_ascii=False))

    run_checks([src], sha, cfg, logs, on_done)
    return out


def read_log(con, check_id: int) -> tuple[dict, str] | None:
    """(check row, log text) for the dashboard; only files under data/checks are served."""
    r = con.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
    if not r:
        return None
    f = Path(r["log_path"] or "-").resolve()
    if (r["log_path"] or "").startswith("http") or not f.is_relative_to(LOG_DIR.resolve()) or not f.is_file():
        return dict(r), ""
    with open(f, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 400_000))
        body = fh.read().decode("utf-8", "replace")
    return dict(r), ("…(앞부분 생략)\n" if size > 400_000 else "") + body
