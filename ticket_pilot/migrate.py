"""tp migrate (TP-122): move an existing install under one folder (the ~/tp-work layout of TP-121).

  <to>/app         this checkout, with data/
  <to>/worktrees   the ticket worktrees (setting worktree_root)
  <to>/repos       where the 프로젝트 추가 wizard clones from now on (setting clone_root; existing clones stay)

Moving the folders alone breaks the install: absolute paths sit in the DB, in git's worktree links, in the launchd
plists and in the names of Claude's session folders. Without --apply this only prints the plan. With it:
stop launchd -> back up tp.db -> move -> `git worktree repair` -> rewrite the DB paths -> copy the Claude session
folders -> launchd/install.sh from the new checkout. Progress is kept in <to>/migrate-state.json, so the same command
run again (after the checkout moved: from <to>/app) skips what is done.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# everything this needs is imported here: once the checkout has moved, a lazy import from the old path fails
from . import config as G
from . import core, notion_stages, runner, worktree_dev

# the pre-TP-131 names too: a host that migrates before install.sh renamed its agents must still find and stop them
LABELS = ("com.ticket-pilot.tick", "com.ticket-pilot.dashboard", "com.bini59.ticket-pilot.tick", "com.bini59.ticket-pilot.dashboard")
STATE = "migrate-state.json"
TICK_WAIT_S = 300


def _tilde(p: str) -> str:
    for h in {os.path.expanduser("~"), os.path.realpath(os.path.expanduser("~"))}:
        if p == h or p.startswith(h + "/"):
            return "~" + p[len(h):]
    return p


def _dev(p) -> int:
    return os.stat(p).st_dev


def _remap(moves, p: str) -> str:
    for old, new in sorted(moves, key=lambda m: -len(m[0])):
        if p == old or p.startswith(old + "/"):
            return new + p[len(old):]
    return p


def _launchd() -> tuple[str | None, str | None]:
    """(checkout, log folder) the installed launchd agents point at; (None, None) when they are not installed."""
    for label in LABELS:
        try:
            d = plistlib.loads((Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        return (os.path.realpath(d.get("WorkingDirectory") or "/nonexistent"),
                os.path.dirname(d.get("StandardOutPath") or "") or None)
    return None, None


def _launchd_stop():
    for label in LABELS:  # not loaded = nothing to stop
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)


def _install(app: str, log_dir: str):
    subprocess.run([f"{app}/launchd/install.sh"], check=True, env={**os.environ, "TP_LOG_DIR": log_dir})


def busy(con, fix=lambda s: s) -> list[str]:
    """Agents (ticket, automation, chat), tp checks and dev servers that are running now. fix = the path rewrite of
    a resumed migration: the logs have moved but the handles in the DB may still hold the old paths."""
    def running(handle):
        return runner.running(fix(handle) if handle else handle)

    out = []
    if notion_stages.worker_running():
        out.append("Notion 상태 반영")
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    # Active runs, and runs touched in the last day (a rework agent may still run on a finished one). Not older ones:
    # an agent that was killed leaves no exit file, and its pid may belong to something else by now.
    for r in con.execute("""SELECT r.terminal, r.worktree_path, t.key FROM runs r LEFT JOIN tickets t ON t.gid=r.ticket_gid
                            WHERE r.state='active' OR r.updated_at > ?""", (day_ago,)):
        if running(r["terminal"]):
            out.append(f"티켓 에이전트 {r['key'] or ''} ({r['worktree_path']})")
    for i in (core.meta_get(con, "automations") or {}).get("items", []):
        if running((i.get("usageSummary") or {}).get("handle")):
            out.append(f"자동화 {i.get('automationId')}의 AI 단계")
    for r in con.execute("SELECT conversation_id, handle FROM chat_messages WHERE text IS NULL AND handle IS NOT NULL"):
        if running(r["handle"]):
            out.append(f"채팅 {r['conversation_id']}")
    for gid, d in (core.meta_get(con, "dev_servers") or {}).items():
        if runner._alive(d["pid"]):
            out.append(f"dev 서버 {gid} (포트 {d.get('port')})")
    for path, d in (core.meta_get(con, worktree_dev.KEY) or {}).items():
        if worktree_dev.managed_pid(d):
            out.append(f"dev 서버 {path} (포트 {d['port']})")
    # Detached `tp check` runs outlive the tick and write logs and rows by the old paths.
    # ponytail: checks has no pid, so "running" younger than 3 hours counts (longer than any check run; a crashed
    # one blocks until then, or until the next tick marks it)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    n = con.execute("SELECT COUNT(DISTINCT pr_url) FROM checks WHERE status='running' AND created_at > ?", (old,)).fetchone()[0]
    if n:
        out.append(f"tp check (PR {n}개)")
    for r in con.execute("SELECT id, repo, pid FROM config_check_runs WHERE status='running' AND pid IS NOT NULL"):
        if runner._alive(r["pid"]):
            out.append(f"설정 시험 실행 {r['repo']}")
    return out


def tick_lock():
    """Hold data/tick.lock (waits for a running tick, up to TICK_WAIT_S): a tick that starts later exits at once.
    Returns the open file (closing it releases the lock), None without data/. Also tp mode (TP-165)."""
    if not (core.ROOT / "data").is_dir():
        return None
    lock = open(core.ROOT / "data" / "tick.lock", "w")
    for i in range(TICK_WAIT_S):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except BlockingIOError:
            if i == 0:
                print("- 돌고 있는 tick이 끝나기를 기다려요", flush=True)
            time.sleep(1)
    lock.close()
    raise RuntimeError(f"tick이 {TICK_WAIT_S}초 넘게 돌고 있어요. 끝난 뒤 다시 실행하세요")


def _plan(to: str) -> dict:
    old_root, old_wt = str(core.ROOT), str(runner.root())
    new_root, new_wt = f"{to}/app", f"{to}/worktrees"
    moves = [[o, n] for o, n in ((old_wt, new_wt), (old_root, new_root)) if o != n and os.path.isdir(o)]
    # every spelling of a moved folder the DB may hold -> its new spelling (~ stays ~)
    raw = core.settings().get("worktree_root") or ""
    spell = {}
    for o, n in moves:
        for s in {o, os.path.abspath(os.path.expanduser(raw))} if o == old_wt and raw else {o}:
            spell[s] = n
            spell[_tilde(s)] = _tilde(n)
    for s, n in list(spell.items()):  # meta values are JSON with non-ASCII escaped
        spell.setdefault(json.dumps(s)[1:-1], json.dumps(n)[1:-1])
    # linked worktrees under the worktree root, by the repository they belong to (its common git dir)
    repair: dict = {}
    if [old_wt, new_wt] in moves:
        for g in sorted(Path(old_wt).glob("*/*/.git")):
            m = re.match(r"gitdir: (.+)/worktrees/[^/\n]+\s*$", g.read_text()) if g.is_file() else None
            if m:
                repair.setdefault(os.path.realpath(os.path.join(g.parent, m.group(1))), []).append(str(g.parent))
    if [old_root, new_root] in moves and os.path.isdir(f"{old_root}/.git"):
        repair.setdefault(os.path.realpath(f"{old_root}/.git"), [])  # its worktrees elsewhere point back at it
    ld_dir, ld_logs = _launchd()
    return {"to": to, "moves": moves, "root": [old_root, new_root], "wt": [old_wt, new_wt],
            "db": os.path.realpath(core.DB_PATH), "spell": spell, "repair": repair,
            "launchd": ld_dir == old_root, "launchd_dir": ld_dir, "old_logs": ld_logs, "backup": None, "done": []}


def _fixer(spell: dict):
    """str -> str with every old path replaced. A match must be a whole path prefix: `<old>` or `<old>/...`,
    not `<old>-2` or `/elsewhere<old>`."""
    if not spell:
        return lambda s: s
    rx = re.compile(r"(?<![\w.~-])(" + "|".join(re.escape(o) for o in sorted(spell, key=len, reverse=True))
                    + r")(?![\w-]|\.\w)")
    return lambda s: rx.sub(lambda m: spell[m.group(1)], s)


def rewrite(con, fix, apply: bool) -> dict:
    """Replace old paths in every text value of every table; returns {"table.column": rows}.
    ponytail: all text columns, not a list of path columns. A new path column can never be missed, and the old paths
    in log text (events, ticket notes, chat messages) change too, which is harmless."""
    counts: dict = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
        for row in con.execute(f'SELECT rowid AS _rid, * FROM "{t}"').fetchall():
            new = {c: fix(row[c]) for c in cols if isinstance(row[c], str)}
            new = {c: v for c, v in new.items() if v != row[c]}
            for c in new:
                counts[f"{t}.{c}"] = counts.get(f"{t}.{c}", 0) + 1
            if new and apply:
                con.execute('UPDATE "%s" SET %s WHERE rowid=?' % (t, ", ".join('"%s"=?' % c for c in new)),
                            (*new.values(), row["_rid"]))
    return counts


def _sessions(moves, apply: bool) -> list[tuple[Path, Path]]:
    """Claude finds a session by its working folder: ~/.claude/projects/<path with every non-alphanumeric as '-'>.
    Copy the folders of the moved checkout and worktrees to their new names (the originals stay)."""
    base = Path.home() / ".claude" / "projects"
    out = []
    for old, new in moves:
        mo, mn = (re.sub(r"[^A-Za-z0-9]", "-", p) for p in (old, new))
        for d in sorted(base.glob(mo + "*")):
            rest = d.name[len(mo):]
            # ponytail: the name is lossy ('/' and '_' both become '-'), so a sibling folder named <old>-x matches too;
            # that only copies a few extra session folders
            dst = base / (mn + rest)
            if d.is_dir() and rest[:1] in ("", "-") and not dst.exists():
                out.append((d, dst))
                if apply:
                    shutil.copytree(d, dst, symlinks=True)
    return out


def _move(old: str, new: str, copied) -> None:
    """rename; onto another disk: copy, call copied() (the state file remembers it), then delete the original."""
    try:
        os.rename(old, new)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        part = new + ".migrating"
        shutil.rmtree(part, ignore_errors=True)  # left by a copy that failed halfway
        shutil.copytree(old, part, symlinks=True)
        os.rename(part, new)
        copied()
        shutil.rmtree(old)


def _git(cwd: str, *args) -> str:
    # not core.run: its gh_env creates data/gh under the old checkout path, which has moved by now
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise RuntimeError(f"git -C {cwd} {' '.join(args[:2])} 실패: {p.stderr.strip()[-500:]}")
    return p.stdout


def _main(common: str) -> str:
    return common[:-5] if common.endswith("/.git") else common


def _repair(j: dict):
    for common, wts in j["repair"].items():
        common = _remap(j["moves"], common)
        if not os.path.isdir(common):
            print(f"  건너뜀: 저장소가 없어요 {common}")
            continue
        main = _main(common)
        new = [_remap(j["moves"], w) for w in wts]
        _git(main, "worktree", "repair", *new)
        listed = {os.path.realpath(ln[9:]) for ln in _git(main, "worktree", "list", "--porcelain").splitlines()
                  if ln.startswith("worktree ")}
        for w in new:
            if os.path.realpath(w) not in listed:
                raise RuntimeError(f"{main}: git worktree list에 {w}가 없어요")
            _git(w, "rev-parse", "--git-dir")  # the worktree finds its repository
        print(f"  {main}: worktree {len(new)}개 연결")


def _rewrite_db(j: dict, db: str):
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        counts = rewrite(con, _fixer(j["spell"]), apply=True)
        # the two roots are set outright: they may have been stored in a spelling the scan does not know
        for k, v in (("worktree_root", _tilde(j["wt"][1])), ("clone_root", _tilde(f"{j['to']}/repos"))):
            r = con.execute("SELECT v FROM config_global WHERE ns='settings' AND k=?", (k,)).fetchone()
            new = json.dumps(v)
            if r and r["v"] == new:
                continue
            con.execute("""INSERT INTO config_global(ns,k,v,pos) VALUES('settings',?,?,
                             (SELECT COALESCE(MAX(pos),-1)+1 FROM config_global WHERE ns='settings'))
                           ON CONFLICT(ns,k) DO UPDATE SET v=excluded.v""", (k, new))
            con.execute("INSERT INTO settings_history(ts,actor,scope,key,old,new) VALUES(?,?,?,?,?,?)",
                        (core.now(), "cli", "global", k, r["v"] if r else "null", new))
        # no old path in here: after this the DB holds none
        core.event(con, "config", f"tp migrate: 설치를 {j['to']}로 옮겼어요 (DB 경로 {sum(counts.values())}곳, "
                                  f"예전 위치는 {j['to']}/{STATE})")
        con.commit()
        try:  # the newest settings backup should hold the new paths
            core.DB_PATH = Path(db)
            core.config_cache_clear()
            G.backup(con)
        except Exception:  # noqa: BLE001  the migration itself is done
            pass
    finally:
        con.close()
    print(f"  {sum(counts.values())}곳" + "".join(f"\n    {k}: {n}" for k, n in sorted(counts.items())))


def _undo(j: dict) -> str:
    """How to go back by hand, for what has been done so far."""
    moved = [(o, n) for o, n in j["moves"] if not os.path.exists(o) and os.path.exists(n)]
    steps = []
    if j["launchd"]:  # install.sh may have started them from the new checkout already
        steps += [f"launchctl bootout gui/{os.getuid()}/{label}   # 떠 있으면 멈춤 (없다는 오류는 무시)" for label in LABELS]
    if "db" in j["done"] and j["backup"]:  # first: both paths are where the files are now
        steps.append(f"cp {j['backup']} {j['db']}   # DB를 옮기기 전 상태로")
    steps += [f"mv {n} {o}" for o, n in reversed(moved)]
    if moved:  # also when the repair step failed halfway: some repositories already point at the new paths
        steps += [f"git -C {_main(common)} worktree repair {' '.join(wts)}".rstrip() for common, wts in j["repair"].items()]
    if j["launchd"]:
        steps.append(f"{j['root'][0]}/launchd/install.sh   # launchd 다시 켜기")
    if os.path.exists(f"{j['to']}/{STATE}"):
        steps.append(f"rm {j['to']}/{STATE}   # 안 지우면 다음 실행이 끝난 단계를 건너뛰어요")
    return "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1))


def _show(j: dict, con, resumed: bool):
    print(f"대상 폴더: {j['to']}" + ("   (이어서 실행: 끝난 단계는 건너뛰어요)" if resumed else ""))
    print("폴더 이동:")
    for o, n in j["moves"]:
        done = not os.path.exists(o) and os.path.exists(n)
        print(f"  {o} → {n}" + ("   (끝남)" if done else ""))
    if not j["moves"]:
        print("  (없음: 이미 그 자리에 있어요)")
    print("git worktree 연결 복구:" + ("   (끝남)" if "repair" in j["done"] else ""))
    for common, wts in j["repair"].items():
        print(f"  {_main(_remap(j['moves'], common))}: worktree {len(wts)}개")
    counts = rewrite(con, _fixer(j["spell"]), apply=False)
    print("DB 경로 치환:" + ("   (끝남)" if "db" in j["done"] else f"   {sum(counts.values())}곳"))
    for k, n in sorted(counts.items()):
        print(f"  {k}: {n}")
    print(f"  설정 worktree_root → {_tilde(j['wt'][1])}, clone_root → {_tilde(j['to'] + '/repos')}")
    ses = _sessions(j["moves"], apply=False)
    print(f"Claude 세션 폴더 복사: {len(ses)}개" + ("   (끝남)" if "sessions" in j["done"] else ""))
    if j["launchd"]:
        print(f"launchd: tick, dashboard를 멈추고 {j['root'][1]}/launchd/install.sh로 다시 설치")
    else:
        print("launchd: 건드리지 않아요 (" + (f"다른 checkout {j['launchd_dir']}을 가리켜요" if j["launchd_dir"]
                                         else "설치돼 있지 않아요") + ")")


def migrate(to: str, apply: bool = False, confirm=input):
    if sys.platform != "darwin":  # TP-162
        print("tp migrate는 macOS 예전 설치(launchd)를 ~/tp-work로 옮기는 명령이에요. Linux에서는 아무것도 하지 않아요.")
        return
    to = os.path.realpath(os.path.expanduser(to))
    state = Path(to) / STATE
    con = core.db()
    resumed = state.is_file()
    j = json.loads(state.read_text()) if resumed else _plan(to)
    moves = j["moves"]
    j.setdefault("copied", [])  # copied to another disk, original not deleted yet
    pending = [(o, n) for o, n in moves if os.path.exists(o) and o not in j["copied"]]
    fix = _fixer(j["spell"]) if resumed else (lambda s: s)

    def save():
        state.write_text(json.dumps(j, ensure_ascii=False, indent=1))

    _show(j, con, resumed)

    # ---- checks: any problem stops here, with nothing changed
    problems = [f"실행 중: {b}" for b in busy(con, fix)]
    for o, n in pending:
        if os.path.lexists(n):
            problems.append(f"대상에 이미 있어요: {n}")
        if to == o or to.startswith(o + "/"):
            problems.append(f"대상 폴더가 옮길 폴더 안에 있어요: {o}")
    if len(pending) == 2 and j["root"][0].startswith(j["wt"][0] + "/"):
        problems.append(f"checkout이 worktree 폴더 안에 있어요: {j['wt'][0]}")
    if problems:
        raise SystemExit("\n옮길 수 없어요 (아무것도 바꾸지 않았어요):\n" + "\n".join(f"  - {p}" for p in problems))
    near = next(p for p in (Path(to), *Path(to).parents) if p.exists())
    other_disk = [o for o, _ in pending if _dev(o) != _dev(near)]
    for o in other_disk:
        print(f"경고: {o}는 대상과 다른 디스크에 있어요. 이름만 바꾸는 게 아니라 복사한 뒤 지워요 (오래 걸리고 공간이 필요해요).")
    if not apply:
        print("\n미리보기예요. 바꾼 것은 없어요. 실제로 옮기려면 --apply를 붙이세요.")
        return
    if other_disk:
        try:
            ok = confirm("다른 디스크로 복사해서 옮길까요? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            ok = False
        if not ok:
            raise SystemExit("그만뒀어요. 아무것도 바꾸지 않았어요.")

    def step(key: str, title: str, fn):
        if key in j["done"]:
            print(f"- {title}: 이미 끝남")
            return
        print(f"- {title}")
        fn()
        j["done"].append(key)
        save()

    def backup():
        dst = core.DB_PATH.parent / "backups" / f"tp-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
        dst.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(dst)
        try:
            con.backup(out)
        finally:
            out.close()
        j["backup"] = str(dst)
        print(f"  {dst}")

    lock = None
    try:
        os.makedirs(to, exist_ok=True)  # the state file is first written by the backup step, before anything moves
        # Hold the tick lock from here on: a tick that is running finishes first (launchd leaves its precheck alive
        # when it kills the tick, and that could start an agent), and one that starts later exits at once.
        lock = tick_lock()
        if j["launchd"]:
            print("- launchd 멈춤 (tick, dashboard)")
            _launchd_stop()
        again = busy(con, fix)  # the last tick may have started one
        if again:
            raise RuntimeError("그 사이에 시작된 작업이 있어요: " + ", ".join(again))
        step("backup", "DB 백업", backup)
        core.close_db(con)  # never core.db() after the move: it would create an empty data/tp.db at the old path
        for o, n in moves:
            if o in j["copied"]:
                shutil.rmtree(o, ignore_errors=True)  # the copy is complete; finish deleting the original
            elif os.path.exists(o):
                print(f"- 이동 {o} → {n}")
                _move(o, n, lambda: (j["copied"].append(o), save()))
            else:
                print(f"- 이동 {o}: 이미 끝남")
        if j["backup"] and not os.path.exists(j["backup"]):
            j["backup"] = _remap(moves, j["backup"])  # it moved along with data/
        db = j["db"] = _remap(moves, j["db"])
        save()
        step("repair", "git worktree 연결 복구", lambda: _repair(j))
        step("db", "DB 경로 치환", lambda: _rewrite_db(j, db))
        os.makedirs(f"{to}/repos", exist_ok=True)
        step("sessions", "Claude 세션 폴더 복사",
             lambda: print(f"  {len(_sessions(moves, apply=True))}개"))
        if j["launchd"]:
            step("install", "launchd 다시 설치 (/api/state 기다림)",
                 lambda: _install(j["root"][1], os.environ.get("TP_LOG_DIR") or f"{to}/logs"))
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001
        app = j["root"][0] if os.path.exists(j["root"][0]) else j["root"][1]
        raise SystemExit(f"\n실패: {e}\n끝난 단계: {', '.join(j['done']) or '(없음)'}\n\n"
                         f"고친 뒤 이어서 하려면 (끝난 단계는 건너뛰어요):\n  {app}/bin/tp migrate --to {to} --apply\n\n"
                         f"되돌리려면 (순서대로):\n{_undo(j)}")
    finally:
        if lock:
            lock.close()

    new_root = j["root"][1]
    print(f"\n끝났어요. 이제 {new_root}/bin/tp 를 쓰세요. 남은 수작업:")
    todo = [f"이 checkout을 찾는 외부 deploy.yml을 사용한다면 해당 저장소의 TP_DIR을 새 경로로:\n"
            f"     gh variable set TP_DIR --repo <owner>/<repo> --body {new_root}"]
    if not j["launchd"]:
        todo.append(f"dashboard / tick을 launchd로 돌린다면 {new_root}/launchd/install.sh")
    if to != os.path.realpath(os.path.expanduser("~/tp-work")) and not os.environ.get("TP_LOG_DIR"):
        todo.append(f"로그를 {to}/logs에 계속 두려면 재설치 시 TP_LOG_DIR을 지정하세요. 외부 deploy.yml을 쓰면 해당 저장소에도 설정하세요")
    if j["old_logs"] and j["old_logs"] != (os.environ.get("TP_LOG_DIR") or f"{to}/logs"):
        todo.append(f"예전 로그 폴더 정리: {j['old_logs']}")
    todo += ["worktree 안 Python 가상환경(.venv)은 절대경로를 담고 있어서 안 돌면 다시 설치하세요",
             "돌던 티켓의 에이전트 세션이나 예전 brief는 예전 bin/tp 경로를 기억해요. 필요하면: "
             f"ln -s {new_root} {j['root'][0]}",
             "예전 Claude 세션 폴더(~/.claude/projects, 복사 원본)와 빈 예전 폴더는 확인 뒤 지워도 돼요",
             f"문제가 있으면 DB 백업: {j['backup']}   기록: {state}"]
    for i, t in enumerate(todo, 1):
        print(f"  {i}. {t}")
