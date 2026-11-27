"""Dev servers started by ticket agents. A short cross-process lock covers port allocation and registration.
Waiting clients share the registered launch and release the lock while it starts listening.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from . import devserver as D
from . import runner as R
from .core import meta_get, meta_set, run

KEY = "worktree_dev_servers"
READY_TIMEOUT = 60


def _version(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"v?\d+(?:\.\d+){0,2}", value):
        return tuple(int(n) for n in value.lstrip("v").split("."))
    return ()


def _node_output(node: Path | str, *args: str) -> str:
    try:
        return run([str(node), *args], check=False, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _matches_lts(node: Path | str, want: str) -> bool:
    if not want:
        return True
    name = _node_output(node, "-p", "process.release.lts || ''").lower()
    return bool(name) and (want == "*" or want == name)


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
    parts = _version(version)
    lts = version[4:].lower() if re.fullmatch(r"lts/(?:\*|[A-Za-z][\w-]*)", version) else ""
    if not parts and not lts and version not in ("node", "stable"):
        raise D.DevError(f"{wt / '.nvmrc'}: Node 버전을 해석할 수 없어요 ({want!r})")
    matches = []
    for folder in (nvm / "versions" / "node").glob("v*"):
        nums = _version(folder.name)
        if len(nums) == 3 and nums[:len(parts)] == parts and os.access(folder / "bin" / "node", os.X_OK):
            matches.append((nums, folder / "bin"))
    for _, folder in sorted(matches, reverse=True):
        if _matches_lts(folder / "node", lts):
            return str(folder) + os.pathsep + path
    # The Docker image supplies Node directly on PATH.
    node = shutil.which("node", path=path)
    if node:
        installed = _version(_node_output(node, "--version"))
        if installed and installed[:len(parts)] == parts and _matches_lts(node, lts):
            return path
    raise D.DevError(f"{wt / '.nvmrc'}: {nvm}에 Node {want} 버전을 설치해주세요")


def in_group(pid: int, group: int | None) -> bool:
    try:
        return group is not None and os.getpgid(pid) == group
    except ProcessLookupError:
        return False


def managed_pid(rec: dict | None) -> int | None:
    """A stale PID may now belong to someone else. Our session leader carries its unique log path as sh's $0."""
    if not rec:
        return None
    pid, log = rec["pid"], rec["log"]
    if R._alive(pid) and in_group(pid, pid):
        args = run(["ps", "-ww", "-p", str(pid), "-o", "args="], check=False).stdout.rstrip("\n")
        if args.endswith(" " + log):
            return pid
    # A shell script may leave a child serving after its leader exits. Check both the group and its unique log.
    if not Path(log).is_file():
        return None
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # The group may still have our log open even if we cannot signal it.
    if Path("/proc/self/fd").is_dir():
        for proc in Path("/proc").glob("[0-9]*"):
            if in_group(int(proc.name), pid):
                try:
                    if any(os.path.realpath(fd) == log for fd in (proc / "fd").iterdir()):
                        return pid
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
    elif run(["lsof", "-t", "-a", "-g", str(pid), "--", log], check=False, timeout=10).stdout.strip():
        return pid
    return None


def record(con, path: Path | str) -> dict | None:
    return (meta_get(con, KEY) or {}).get(str(Path(path).resolve()))


def reserved_ports(con) -> dict[int, str]:
    return {r["port"]: path for path, r in (meta_get(con, KEY) or {}).items() if managed_pid(r)}


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


def _launch(con, wt: Path, port: int | None) -> dict:
    row = con.execute("""SELECT r.repo, rp.path FROM runs r LEFT JOIN repos rp ON rp.name=r.repo
                         WHERE r.worktree_path=? ORDER BY r.id DESC LIMIT 1""", (str(wt),)).fetchone()
    repo = row["repo"] if row else wt.name
    detected = D.detect(repo, wt, port_hint=port)
    port = detected["port"] if port is None else port
    if not 1024 <= port <= 65535:
        raise D.DevError("포트는 1024-65535 사이여야 해요")
    used = D._used_ports(con, "") | reserved_ports(con).keys()
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


def _live_children(group: int) -> set[int]:
    """Children may redirect every log fd. Zombies have already released their sockets and files."""
    rows = run(["ps", "-A", "-o", "pid=,pgid=,stat="]).stdout.strip().splitlines()
    return {int(pid) for pid, pgid, state in (row.split() for row in rows)
            if pgid == str(group) and pid != pgid and not state.startswith("Z")}


def _terminate(rec: dict):
    group = rec["pid"]
    children = _live_children(group)
    if managed_pid(rec) is None:
        return

    def still_running() -> bool:
        if children and not children.isdisjoint(_live_children(group)):
            return True
        return managed_pid(rec) is not None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS can report EPERM when the last live member exits between the check and the signal.
            if still_running():
                raise
            return
        deadline = time.monotonic() + 5
        while still_running():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        else:
            return
    raise RuntimeError(f"{rec['port']} 포트의 dev 서버를 종료하지 못했어요")


def stop(con, path: Path | str, expected: dict | None = None) -> bool:
    key = str(Path(path).resolve())
    with D.port_lock():
        servers = meta_get(con, KEY) or {}
        rec = servers.get(key)
        if not rec or (expected is not None and rec != expected):
            return False
        _terminate(rec)
        del servers[key]
        meta_set(con, KEY, servers)
        con.commit()
        return True


def _start_or_reuse(con, wt: Path, port: int | None) -> tuple[dict, bool]:
    with D.port_lock():
        if not wt.is_dir():
            raise D.DevError(f"워크트리 폴더가 없어요: {wt}")
        if con.execute("SELECT 1 FROM cleanups WHERE path=? AND status='removing'", (str(wt),)).fetchone():
            raise D.DevError(f"정리 중인 워크트리예요: {wt}")
        servers = meta_get(con, KEY) or {}
        rec = servers.get(str(wt))
        reused = managed_pid(rec) is not None
        if not reused:
            rec = _launch(con, wt, port)
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
    deadline = None
    for _ in range(2):  # a foreign process can bind between port selection and the child's listen()
        rec, reused = _start_or_reuse(con, wt, port)
        if deadline is None:
            deadline = time.monotonic() + READY_TIMEOUT
        while (state := _state(rec)) == "starting" and time.monotonic() < deadline:
            time.sleep(0.2)
        if state == "running":
            return {**rec, "status": state, "url": f"http://localhost:{rec['port']}", "reused": reused}
        if not stop(con, wt, expected=rec):
            break  # an explicit stop or a newer launch won
        if state == "stopped" and D.port_open(rec["port"]):
            state = "port-in-use"  # the child may have already exited with EADDRINUSE
        if state != "port-in-use" or time.monotonic() >= deadline:
            break
    why = {"stopped": "서버가 종료됐어요", "port-in-use": "다른 프로세스가 포트를 사용 중이에요"}.get(
        state, f"{READY_TIMEOUT}초 안에 포트가 열리지 않았어요")
    raise D.DevError(f"{why} (포트 {rec['port']}). 로그: {rec['log']}")
