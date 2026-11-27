"""Dev servers started by ticket agents. A short cross-process lock covers port allocation and registration.
Waiting clients share the registered launch and release the lock while it starts listening.
"""
from __future__ import annotations

import fcntl
import os
import re
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import devserver as D
from . import runner as R
from .core import meta_get, meta_set, run

KEY = "worktree_dev_servers"
READY_TIMEOUT = 60


def node_path(wt: Path) -> str:
    path = R.agent_path()
    try:
        lines = (wt / ".nvmrc").read_text().splitlines()
    except FileNotFoundError:
        return path
    nvm = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    want = " ".join(v for line in lines if (v := line.partition("#")[0].strip()))
    version = want
    seen = set()
    while version not in seen and re.fullmatch(r"(?:lts/)?(?:[a-zA-Z][\w-]*|\*)", version):
        seen.add(version)
        alias = nvm / "alias" / version
        if not alias.is_file():
            break
        version = alias.read_text().strip()
    if version in ("node", "stable"):
        parts = []
    elif re.fullmatch(r"v?\d+(?:\.\d+){0,2}", version):
        parts = [int(n) for n in version.lstrip("v").split(".")]
    else:
        raise D.DevError(f"{wt / '.nvmrc'}: Node 버전을 해석할 수 없어요 ({want!r})")
    matches = []
    for folder in (nvm / "versions" / "node").glob("v*"):
        if re.fullmatch(r"v\d+\.\d+\.\d+", folder.name) and os.access(folder / "bin" / "node", os.X_OK):
            nums = [int(n) for n in folder.name[1:].split(".")]
            if nums[:len(parts)] == parts:
                matches.append((nums, folder / "bin"))
    if not matches:
        raise D.DevError(f"{wt / '.nvmrc'}: {nvm}에 Node {want} 버전을 설치해주세요")
    return str(max(matches)[1]) + os.pathsep + path


@contextmanager
def _registry_lock():
    D.LOGS.mkdir(parents=True, exist_ok=True)
    with (D.LOGS / ".worktrees.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def in_group(pid: int, group: int | None) -> bool:
    try:
        return group is not None and os.getpgid(pid) == group
    except ProcessLookupError:
        return False


def managed_pid(rec: dict | None) -> int | None:
    """A stale PID may now belong to someone else. Our session leader carries its unique log path as sh's $0."""
    if rec and R._alive(rec["pid"]) and in_group(rec["pid"], rec["pid"]):
        args = run(["ps", "-ww", "-p", str(rec["pid"]), "-o", "args="], check=False).stdout.rstrip("\n")
        if args.endswith(" " + rec["log"]):
            return rec["pid"]
    return None


def record(con, path: Path | str) -> dict | None:
    return (meta_get(con, KEY) or {}).get(str(Path(path).resolve()))


def _state(rec: dict | None) -> str:
    pid = managed_pid(rec)
    if not pid:
        return "stopped"
    if D.port_open(rec["port"]):
        listeners = D.listeners(rec["port"])
        if listeners:
            return "running" if all(in_group(p["pid"], pid) for p in listeners) else "port-in-use"
    return "starting"


def status(con, path: Path) -> dict:
    rec = record(con, path)
    state = _state(rec)
    result = {"status": state}
    if rec:
        result.update(port=rec["port"], log=rec["log"], pid=rec["pid"])
        if state == "running":
            result["url"] = f"http://localhost:{rec['port']}"
    return result


def _launch(con, wt: Path, servers: dict, port: int | None) -> dict:
    row = con.execute("""SELECT r.repo, rp.path FROM runs r LEFT JOIN repos rp ON rp.name=r.repo
                         WHERE r.worktree_path=? ORDER BY r.id DESC LIMIT 1""", (str(wt),)).fetchone()
    repo = row["repo"] if row else wt.name
    detected = D.detect(repo, wt, port_hint=port)
    port = detected["port"] if port is None else port
    if not 1024 <= port <= 65535:
        raise D.DevError("포트는 1024-65535 사이여야 해요")
    used = D._used_ports(con, "") | {r["port"] for r in servers.values() if managed_pid(r)}
    if port in used or D.port_open(port) or D.listeners(port):
        port = D.free_port(port, used)
    cmd = detected["cmd"]
    if port != detected.get("fixed_port"):
        cmd = detected.get("port_cmd", cmd)
    cmd = cmd.replace("{port}", str(port))
    log = D.LOGS / f"{wt.name[:64]}-{uuid4().hex}.log"
    root = row["path"] if row and row["path"] else str(wt)
    pid = D._launch(wt, root, port, cmd, None, log, node_path(wt), supervise=True)
    return {"pid": pid, "port": port, "log": str(log)}


def _terminate(rec: dict):
    pid = managed_pid(rec)
    if pid:
        R.stop(f"{pid} {rec['log']}")
        # A child may ignore TERM even after its parent exits.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while D.port_open(rec["port"]) and any(in_group(p["pid"], pid) for p in D.listeners(rec["port"])):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{rec['port']} 포트의 dev 서버를 종료하지 못했어요")
            time.sleep(0.05)


@contextmanager
def stopped(con, path: Path | str, expected: dict | None = None):
    """Hold off new starts until the caller has finished removing this worktree."""
    key = str(Path(path).resolve())
    with _registry_lock():
        servers = meta_get(con, KEY) or {}
        rec = servers.get(key)
        if rec and (expected is None or rec == expected):
            _terminate(rec)
            del servers[key]
            meta_set(con, KEY, servers)
            con.commit()
        yield


def _start_or_reuse(con, wt: Path, port: int | None) -> tuple[dict, bool]:
    with _registry_lock():
        if not wt.is_dir():
            raise D.DevError(f"워크트리 폴더가 없어요: {wt}")
        servers = meta_get(con, KEY) or {}
        rec = servers.get(str(wt))
        reused = managed_pid(rec) is not None
        if not reused:
            rec = _launch(con, wt, servers, port)
            try:
                servers[str(wt)] = rec
                meta_set(con, KEY, servers)
                con.commit()
            except BaseException:
                con.rollback()
                _terminate(rec)
                raise
        return rec, reused


def start(con, path: Path, port: int | None = None) -> dict:
    wt = path.resolve()
    deadline = time.monotonic() + READY_TIMEOUT
    for _ in range(2):  # a foreign process can bind between port selection and the child's listen()
        rec, reused = _start_or_reuse(con, wt, port)
        while (state := _state(rec)) == "starting" and time.monotonic() < deadline:
            time.sleep(0.2)
        if state == "running":
            return {**rec, "status": state, "url": f"http://localhost:{rec['port']}", "reused": reused}
        with stopped(con, wt, expected=rec):
            if state != "port-in-use" or time.monotonic() >= deadline:
                break
    why = {"stopped": "서버가 종료됐어요", "port-in-use": "다른 프로세스가 포트를 사용 중이에요"}.get(
        state, f"{READY_TIMEOUT}초 안에 포트가 열리지 않았어요")
    raise D.DevError(f"{why} (포트 {rec['port']}). 로그: {rec['log']}")
