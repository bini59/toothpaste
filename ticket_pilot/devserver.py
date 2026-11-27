"""Dev servers on milestone branches (2026-11-04): the 마일스톤 page starts a repo's dev server on
milestone/<parent gid>, so the owner can try the subtasks together before the milestone PR ships them.

  check(gid)   worktree <worktree_root>/<repo>/<milestone ticket slug> on the milestone branch (the one open_pr uses
               later; an existing one is fast-forwarded), the .env check, the dev command and port, and who already
               listens on that port
  start(gid, port, kill)   refuses a port in use unless kill (stop those processes first) or another port is chosen;
               the server runs detached in its own session (survives dashboard restarts), log in data/dev/
  stop(gid)    SIGTERM / SIGKILL its process group

Command and port: repo setting dev {cmd, port} (cmd may hold {port}), else the package.json script dev / start:dev /
start through the lockfile's package manager, with --port for next / vite-based servers and PORT in the environment
for all. Port: setting, a -p/--port in the script, PORT in the worktree's .env, vite.config server.port, then the
framework default (next 3000, vite 5173). The dashboard keeps meta dev_servers keyed by milestone gid.
The agent CLI keeps worktree_dev_servers keyed by resolved path and waits for its own listener.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import milestone as M
from . import runner as R
from .core import DATA, db, event, meta_get, meta_set, now, run
from .dev_package import PORT_FLAG, bind_port

KEY = "dev_servers"
LOGS = DATA / "dev"
SCRIPTS = ("dev", "start:dev", "develop", "serve", "start")
EXAMPLE = re.compile(r"^\.env\.(example|sample|template|dist)$")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
VITE_CMD = re.compile(r"(^|[\s/])(vite|remix vite:dev|react-router dev)(?=\s|$)")
# env vars of the dashboard that a dev server has no business seeing
SECRET_ENV = ("ASANA_PAT", "GITHUB_TOKEN", "GH_TOKEN", "TP_DB", "TP_PORT", "TP_HOST", "CLAUDE_CODE_OAUTH_TOKEN",
              "ANTHROPIC_API_KEY")
_lock = threading.Lock()


class DevError(SystemExit):
    pass


# ---------------------------------------------------------------- target worktree

def _target(con, gid: str):
    if not M.is_ms(gid):
        raise DevError("마일스톤 티켓에서만 dev 서버를 띄울 수 있어요")
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
    if not t:
        raise DevError("이 마일스톤 티켓이 없어요")
    parent, repo = M.parse(gid)
    rp = con.execute("SELECT * FROM repos WHERE name=?", (repo,)).fetchone()
    if not (rp and rp["path"] and os.path.isdir(rp["path"])):
        raise DevError(f"{repo}: 이 Mac의 체크아웃 경로가 없어요 (설정 > 기본 > 경로)")
    return t, repo, rp["path"], M.branch(parent)


def worktree(con, gid: str) -> tuple[Path, str, str, list[str]]:
    """(path, repo, main checkout, notes): the milestone branch checked out where open_pr puts it, up to date."""
    from .pipeline import prepare_worktree, slug
    t, repo, repo_path, b = _target(con, gid)
    path = R.root() / repo / slug(t["key"])
    notes = []
    if path.is_dir():
        p = run(["git", "-C", str(path), "pull", "-q", "--ff-only"], timeout=180, check=False)
        notes.append(f"{b} 최신으로 맞췄어요" if not p.returncode else
                     f"git pull 실패 (로컬 변경이 있나요?): {(p.stderr or p.stdout).strip()[:200]}")
    else:
        R.checkout(repo, repo_path, path.name, b)
        setup, note = prepare_worktree(repo, repo_path, str(path))
        _setup_pending[str(path)] = setup
        notes.append(f"{b} 워크트리를 만들었어요" + (f" ({note})" if note else ""))
    return path, repo, repo_path, notes


# setup scripts of worktrees made by check(), run by the next start() before the dev command
_setup_pending: dict[str, str | None] = {}


# ---------------------------------------------------------------- .env check

def _env_keys(f: Path) -> dict[str, bool]:
    """{key: has a value}; values themselves are never read out."""
    out = {}
    try:
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for ln in lines:
        m = ENV_LINE.match(ln)
        if m:
            v = m.group(2).split(" #", 1)[0].strip().strip("'\"")
            out[m.group(1)] = bool(v)
    return out


def _env_files(top: Path) -> list[Path]:
    out = []
    for root, dirs, files in os.walk(top):
        rel = Path(root).relative_to(top)
        dirs[:] = [d for d in dirs if d not in R.ENV_SKIP_DIRS and len(rel.parts) < R.ENV_MAX_DEPTH]
        out += [Path(root) / f for f in files if f == ".env" or f.startswith(".env.")]
    return sorted(out)


def env_check(repo: str, repo_path: str, wt: Path) -> dict:
    """Copy the main checkout's .env files the worktree lacks (unless auto_setup is off), then compare every
    .env.example (sample/template) with the real .env files of its folder: keys missing or empty there."""
    from .pipeline import repo_extra
    copied = R.copy_env(repo_path, str(wt)) if repo_extra(repo).get("auto_setup", True) is not False else []
    files = _env_files(wt)
    real = [f for f in files if not EXAMPLE.match(f.name)]
    missing = {}
    for ex in (f for f in files if EXAMPLE.match(f.name)):
        have: dict[str, bool] = {}
        for f in real:
            if f.parent == ex.parent:
                for k, v in _env_keys(f).items():
                    have[k] = have.get(k, False) or v
        lack = sorted(k for k in _env_keys(ex) if not have.get(k))
        if lack:
            missing[str(ex.relative_to(wt))] = lack
    warn = []
    if not real and (missing or [f for f in _env_files(Path(repo_path)) if not EXAMPLE.match(f.name)]):
        warn.append(".env 파일이 없어요")
    for ex, keys in missing.items():
        warn.append(f"{ex}에 있는 {len(keys)}개 키가 .env에 없거나 비어 있어요: {', '.join(keys[:12])}"
                    + (" …" if len(keys) > 12 else ""))
    return {"files": [str(f.relative_to(wt)) for f in real], "copied": copied, "missing": missing, "warnings": warn}


# ---------------------------------------------------------------- command and port

def _pm(wt: Path) -> str:
    inst = R.install_cmd(str(wt))
    return inst.split()[0] if inst else "npm"


def _kind(body: str) -> str | None:
    if re.search(r"(^|[\s/])next(\s|$)", body):
        return "next"
    if VITE_CMD.search(body):
        return "vite"
    return None


def _env_port(wt: Path) -> int | None:
    for name in (".env.development.local", ".env.local", ".env.development", ".env"):
        f = wt / name
        if f.is_file():
            for ln in f.read_text(errors="replace").splitlines():
                m = re.match(r"^\s*(?:export\s+)?PORT\s*=\s*['\"]?(\d{2,5})", ln)
                if m:
                    return int(m.group(1))
    return None


def _vite_port(wt: Path) -> int | None:
    for f in wt.glob("vite.config.*"):
        m = re.search(r"server\s*:\s*\{[^}]*?\bport\s*:\s*(\d{2,5})", f.read_text(errors="replace"), re.S)
        if m:
            return int(m.group(1))
    return None


def detect(repo: str, wt: Path, *, port_hint: int | None = None, ticket: bool = False) -> dict:
    """{cmd (with {port}), port, how}: what start() runs and on which port by default."""
    from .pipeline import repo_extra
    conf = repo_extra(repo).get("dev") or {}
    try:
        scripts = json.loads((wt / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError):
        scripts = {}
    configured = conf.get("cmd")
    if configured:
        cmd = body = configured
        how = "설정"
        if ticket:
            package_run = re.fullmatch(r"(?:npm|pnpm|yarn)\s+(?:(?:run|run-script)\s+)?([\w:.-]+)", cmd.strip())
            if package_run and package_run[1] in scripts:
                script = package_run[1]
                body = scripts[script]
    else:
        script = next((s for s in SCRIPTS if s in scripts), None)
        if not script:
            raise DevError(f"{repo}: dev 명령을 못 찾았어요. 설정 > 기본 > dev 서버 명령을 채워주세요")
        body, pm = scripts[script], _pm(wt)
        cmd = f"{pm} run {script}"
        how = f"package.json {script}: {body}"
    kind = _kind(re.sub(r"[;&|()]", " ", body) if ticket else body)
    fixed = PORT_FLAG.search(body)
    port = (conf.get("port") or (int(fixed.group(1)) if fixed else None) or _env_port(wt)
            or (_vite_port(wt) if kind == "vite" else None) or {"next": 3000, "vite": 5173}.get(kind or "")
            or (3000 if "nest start" in body else port_hint))
    if not port:
        raise DevError(f"{repo}: dev 서버 포트를 모르겠어요. 설정 > 기본 > dev 서버 포트를 채워주세요")
    if not ticket and not configured and kind and not fixed:
        extra = "--port {port}" + (" --strictPort" if kind == "vite" else "")
        cmd += f" {'-- ' if pm == 'npm' else ''}{extra}"
    return {"cmd": bind_port(cmd, "{port}") if ticket else cmd,
            "port": int(port), "how": how, "fixed": bool(fixed) and "{port}" not in cmd}


# ---------------------------------------------------------------- ports

@contextmanager
def port_lock():
    """Serialize selection through registration, including servers that have yet to bind their port."""
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / ".ports.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def port_open(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            continue
    return False


def proc_listeners(port: int | None = None, directory: str | None = None) -> list[dict]:
    """Linux without lsof (including the slim Docker image): match listening socket inodes to open fds."""
    sockets = {}
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.exists():
            continue
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            number = int(fields[1].split(":")[1], 16)
            if fields[3] == "0A" and (port is None or port == number):
                sockets[f"socket:[{fields[9]}]"] = number
    out = []
    if not sockets:
        return out
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cwd = os.readlink(proc / "cwd")
            if directory and cwd != directory and not cwd.startswith(directory + "/"):
                continue
            for fd in (proc / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target in sockets:
                    cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
                    out.append({"pid": int(proc.name), "cmd": cmd[:300], "cwd": cwd, "port": sockets[target],
                                "name": (proc / "comm").read_text().strip()})
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return out


def listeners(port: int) -> list[dict]:
    """Processes listening on port: pid, command line, cwd."""
    if not shutil.which("lsof") and Path("/proc/net/tcp").exists():
        return proc_listeners(port)
    p = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], timeout=10, check=False)
    pids = sorted({int(ln[1:]) for ln in p.stdout.splitlines() if ln.startswith("p") and ln[1:].isdigit()})
    out = []
    for pid in pids:
        cmd = run(["ps", "-o", "command=", "-p", str(pid)], timeout=10, check=False).stdout.strip()
        cwd = next((ln[1:] for ln in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=10,
                                         check=False).stdout.splitlines() if ln.startswith("n")), None)
        out.append({"pid": pid, "cmd": cmd[:300], "cwd": cwd})
    return out


def _no_listener(port: int) -> bool:
    return not port_open(port) and not listeners(port)


def free_port(start: int, skip: set[int] = frozenset(), *, available=_no_listener) -> int:
    for offset in range(1, 200):
        p = 1024 + (start - 1024 + offset) % (65536 - 1024)
        if p not in skip and available(p):
            return p
    raise DevError(f"{start} 다음으로 빈 포트를 못 찾았어요")


def _used_ports(con, gid: str) -> set[int]:
    """Ports other milestone dev servers of ours hold (even while still starting)."""
    return {d["port"] for g, d in (meta_get(con, KEY) or {}).items() if g != gid and R._alive(d["pid"])}


def _port_users(con, port: int) -> tuple[list[dict], set[int]]:
    from .worktree_dev import reserved_ports
    tickets = reserved_ports(con)
    busy = listeners(port)
    if not busy and port in tickets:
        busy = [{"pid": None, "cmd": "티켓 dev 서버", "cwd": tickets[port]}]
    return busy, set(tickets)


# ---------------------------------------------------------------- check / start / stop

def _running(con, gid: str) -> dict | None:
    d = (meta_get(con, KEY) or {}).get(gid)
    return d if d and R._alive(d["pid"]) else None


def check(gid: str) -> dict:
    """Preflight for the dashboard: worktree, .env check, command and port, who holds the port."""
    with _lock:
        con = db()
        try:
            cur = _running(con, gid)
            if cur:
                return {"running": status_one(cur)}
            wt, repo, repo_path, notes = worktree(con, gid)
            env = env_check(repo, repo_path, wt)
            d = detect(repo, wt)
            used = _used_ports(con, gid)
            busy, tickets = _port_users(con, d["port"])
            if not busy and port_open(d["port"]):
                busy = [{"pid": None, "cmd": "(다른 사용자 프로세스라 볼 수 없어요)", "cwd": None}]
            ours = d["port"] in used
            res = {"path": str(wt), "repo": repo, "notes": notes, "env": env, "cmd": d["cmd"], "how": d["how"],
                   "port": d["port"], "fixed": d["fixed"], "busy": busy, "ours": ours}
            if busy or ours:
                res["free"] = free_port(d["port"], used | tickets)
            return res
        finally:
            con.close()


def _kill(pids: list[int], port: int) -> None:
    me = {os.getpid(), os.getppid()}
    if me & set(pids):
        raise DevError(f"{port} 포트는 ticket-pilot 대시보드가 쓰고 있어서 끌 수 없어요")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(50):
            if not port_open(port) and not listeners(port):
                return
            time.sleep(0.1)
    raise DevError(f"{port} 포트의 프로세스를 끄지 못했어요")


def _launch(wt: Path, repo_path: str, port: int, cmd: str, setup: str | None, log: Path, path: str,
            *, supervise: bool = False) -> int:
    script = ('if [ -n "$TP_SETUP" ]; then echo "--- setup"; sh -c "$TP_SETUP" < /dev/null '
              '|| echo "--- setup failed ($?)"; fi; echo "--- dev: $TP_DEV"; ')
    # Keep an identifiable session leader for worktree stop/reuse, even when npm spawns the listener.
    if supervise:
        # Keep a foreground parent for commands that start a background listener and redirect its output.
        script += """sh -c 'eval "$TP_DEV"; rc=$?; wait; exit "$rc"'; exit $?"""
    else:
        script += 'exec sh -c "$TP_DEV"'
    args = ["/bin/sh", "-c", script]
    if supervise:
        args.append(str(log))  # sh's $0; unique to this launch, also visible in ps after a CLI restart
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update({"PATH": path, "PORT": str(port), "TP_SETUP": setup or "", "TP_DEV": cmd,
                "TP_ROOT_PATH": repo_path, "TP_WORKTREE_PATH": str(wt)})
    if supervise:
        env["TP_DEV_PORT"] = str(port)
    with log.open("w") as out:
        # Keep a launch-specific fd in worktree children even when a script redirects stdout and stderr.
        return subprocess.Popen(args, cwd=str(wt), stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env,
                                pass_fds=(out.fileno(),) if supervise else ()).pid


def start(gid: str, port: int | None = None, kill: bool = False) -> dict:
    """Start the dev server; a port in use is refused (409 on the dashboard) unless kill. Returns its status."""
    with _lock:
        con = db()
        try:
            if _running(con, gid):
                raise DevError("이 마일스톤의 dev 서버가 이미 떠 있어요")
            wt, repo, repo_path, _ = worktree(con, gid)
            env_check(repo, repo_path, wt)
            d = detect(repo, wt)
            with port_lock():
                return _start_on_port(con, gid, wt, repo, repo_path, d, port, kill)
        finally:
            con.close()


def _start_on_port(con, gid, wt, repo, repo_path, d, port, kill):
    port = int(port or d["port"])
    if not 1024 <= port <= 65535:
        raise DevError("포트는 1024-65535 사이여야 해요")
    if port != d["port"] and d["fixed"]:
        raise DevError(f"{repo}의 dev 스크립트가 포트를 {d['port']}로 고정해요. 다른 포트로 띄우려면 설정 > 기본 > "
                       "dev 서버 명령에 {port}를 넣어주세요")
    used = _used_ports(con, gid)
    if port in used:
        raise DevError(f"{port} 포트는 다른 마일스톤 dev 서버가 쓰고 있어요")
    busy, tickets = _port_users(con, port)
    if busy or port_open(port):
        if not kill:
            raise PortBusy(port, busy, free_port(port, used | tickets))
        if not busy or busy[0]["pid"] is None:
            raise DevError(f"{port} 포트를 쓰는 프로세스를 볼 수 없어서 끌 수 없어요. 다른 포트를 골라주세요")
        _kill([b["pid"] for b in busy], port)
        event(con, "dev", f"{gid}: {port} 포트의 프로세스 {', '.join(str(b['pid']) for b in busy)} 종료 (dashboard)", gid)
    cmd = d["cmd"].replace("{port}", str(port))
    setup = _setup_pending.pop(str(wt), None)
    if setup is None and not (wt / "node_modules").exists():
        setup = R.install_cmd(str(wt))
    log = LOGS / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', gid)}-{datetime.now():%Y%m%d-%H%M%S}.log"
    pid = _launch(wt, repo_path, port, cmd, setup, log, R.agent_path())
    rec = {"gid": gid, "repo": repo, "path": str(wt), "port": port, "pid": pid, "log": str(log),
           "cmd": cmd, "started": now()}
    all_ = meta_get(con, KEY) or {}
    all_[gid] = rec
    meta_set(con, KEY, all_)
    event(con, "dev", f"{gid}: dev 서버 시작 :{port} ({cmd})", gid)
    con.commit()
    return status_one(rec)


class PortBusy(DevError):
    def __init__(self, port: int, busy: list[dict], free: int):
        self.info = {"port": port, "busy": busy, "free": free}
        super().__init__(f"{port} 포트가 이미 열려 있어요")


def stop(gid: str) -> bool:
    con = db()
    try:
        all_ = meta_get(con, KEY) or {}
        d = all_.pop(gid, None)
        if not d:
            return False
        if R._alive(d["pid"]):
            R.stop(f"{d['pid']} {d['log']}")
        meta_set(con, KEY, all_)
        event(con, "dev", f"{gid}: dev 서버 중지 :{d['port']} (dashboard)", gid)
        con.commit()
        return True
    finally:
        con.close()


def tail(log: str, n: int = 60) -> str:
    try:
        return "\n".join(Path(log).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def status_one(d: dict) -> dict:
    alive = R._alive(d["pid"])
    return d | {"alive": alive, "listening": alive and port_open(d["port"]),
                "tail": "" if alive and port_open(d["port"]) else tail(d["log"], 30)}


def status(con) -> list[dict]:
    """For /api/state: every dev server started from the dashboard (stopped ones until 중지 clears them)."""
    return [status_one(d) for d in (meta_get(con, KEY) or {}).values()]

