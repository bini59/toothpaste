"""tp mode (TP-165): how ticket-pilot runs, host (launchd on macOS, systemd on Linux) <-> Docker (docker/, TP-164).

  tp mode [check]               the Docker environment check (also 설정 → 실행 방식)
  tp mode docker [--apply]      host -> Docker; without --apply only the plan, nothing changes
  tp mode host [--apply]        Docker -> host

Host -> Docker: stop the services (tick lock held, nothing may run) -> back up tp.db -> docker/compose.override.yaml
(gitignored: the DB's repos.path, worktree_root, clone_root and ~/.claude, ~/.codex mounted at the same absolute paths,
the container user = the host uid/gid, HOME = the host home, so no DB path changes) and docker/.env (TP_ADMINS, TP_PORT)
-> build -> the DB into the tp-data volume -> the tokens into the volume's secrets file (over stdin, never printed)
-> compose up, /api/state answers 401 -> the host tp.db becomes read-only (TP-8: one writer only).
Docker -> host does the reverse: compose stop -> the volume's DB and tokens back to the host -> compose down (volumes
stay) -> the services again (launchd/install.sh, what tp setup runs).
Progress goes to ~/tp-work/mode-state.json: a failure prints the finished steps and how to undo them, the same command
again resumes. The finished state also tells the host checkout that it runs in Docker now (current()).
"""
from __future__ import annotations

import fcntl
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import core, migrate

DOCKER_DIR = core.ROOT / "docker"
OVERRIDE = DOCKER_DIR / "compose.override.yaml"
ENV_FILE = DOCKER_DIR / ".env"
DATA_IN = "/opt/tp/data"  # the tp-data volume in the container (Dockerfile)
DISK_NEED = 6 * 2**30  # the image (~3GB: node, gh, claude, codex) and its build cache, plus room for the DB
MAC_SHARED = ["/Users", "/Volumes", "/private", "/tmp", "/var/folders"]  # Docker Desktop's default file sharing
ADMINS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(,[A-Za-z0-9][A-Za-z0-9-]*)*")  # GitHub logins
UP_WAIT_S = 120
MAC = sys.platform == "darwin"


def state_path() -> Path:
    return Path.home() / "tp-work" / "mode-state.json"


def log_path() -> Path:
    """The dashboard's 전환 button writes the run here (update.log's folder)."""
    return Path(os.environ.get("TP_LOG_DIR") or Path.home() / "tp-work" / "logs") / "mode.log"


def _state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def current() -> str:
    """'docker', 'macos' or 'linux'. On the host checkout: docker after a finished `tp mode docker`, and while a
    switch back to the host has not finished."""
    if core.docker():
        return "docker"
    j = _state()
    if (j.get("to") == "docker" and j.get("finished")) or (j.get("to") == "host" and not j.get("finished")):
        return "docker"
    return "macos" if MAC else "linux"


def _sh(cmd: list[str], check=True, quiet=True, input: str | None = None, timeout=1800, cwd=None):
    """Every docker / systemctl call. quiet: capture the output (always for anything carrying a token); otherwise it
    goes to this process's stdout (mode.log when the dashboard started it). RuntimeError when check and it fails."""
    sys.stdout.flush()
    try:
        p = subprocess.run(cmd, cwd=cwd or DOCKER_DIR, input=input, text=True, timeout=timeout,
                           capture_output=quiet, stdin=None if input is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        p = subprocess.CompletedProcess(cmd, 127, "", str(e))
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])} 실패 ({p.returncode}): {((p.stderr or '') + (p.stdout or '')).strip()[-500:]}")
    return p


def _vol(src, target: str) -> str:
    """-v for docker compose run (it has no --mount): the short syntax splits on ':'."""
    if ":" in str(src):
        raise RuntimeError(f"경로에 ':'가 있어 docker compose run -v로 연결할 수 없어요: {src}")
    return f"{src}:{target}"


def _dc(*args, **kw):
    """docker compose in docker/: compose.yaml + the compose.override.yaml this writes (compose loads both)."""
    return _sh(["docker", "compose", *args], **kw)


# ---------------------------------------------------------------- the Docker environment check

def _mac_shared() -> list[str]:
    """Docker Desktop's file sharing folders (Settings > Resources > File sharing), its defaults when unreadable."""
    base = Path.home() / "Library" / "Group Containers" / "group.com.docker"
    for name, key in (("settings-store.json", "FilesharingDirectories"), ("settings.json", "filesharingDirectories")):
        try:
            v = json.loads((base / name).read_text()).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(v, list) and v:
            return [str(x) for x in v]
    return MAC_SHARED


def check(paths=()) -> list[dict]:
    """[{id, title, ok, detail, fix}]: every item must pass before `tp mode docker --apply`."""
    out = []

    def item(id_, title, ok, detail, fix=""):
        out.append({"id": id_, "title": title, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix})

    have = bool(shutil.which("docker"))
    item("docker", "docker 명령", have, "있어요" if have else "docker가 없어요",
         "Docker Desktop 설치: https://docs.docker.com/desktop/setup/install/mac-install/" if MAC
         else "Docker Engine 설치: https://docs.docker.com/engine/install/")
    skip = "docker가 없어서 확인하지 못했어요"
    p = _sh(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, timeout=30) if have else None
    item("daemon", "Docker 실행 중", p and p.returncode == 0,
         f"Docker {p.stdout.strip()}" if p and p.returncode == 0 else
         (p.stderr.strip()[-200:] or "docker info 실패") if p else skip,
         "Docker Desktop을 실행하세요" if MAC else
         "sudo systemctl start docker (권한 오류면: sudo usermod -aG docker $USER 후 다시 로그인)")
    p = _sh(["docker", "compose", "version", "--short"], check=False, timeout=30) if have else None
    m = p and p.returncode == 0 and re.match(r"v?(\d+)", p.stdout.strip())
    item("compose", "docker compose v2", m and int(m.group(1)) >= 2,
         f"compose {p.stdout.strip()}" if m else "docker compose가 없어요" if p else skip,
         "Docker Desktop에 들어 있어요" if MAC else "compose 플러그인 설치: https://docs.docker.com/compose/install/linux/")
    free = shutil.disk_usage(Path.home()).free
    try:
        need = DISK_NEED + 2 * os.path.getsize(core.DB_PATH)  # the backup and the volume's copy
    except OSError:
        need = DISK_NEED
    item("disk", "디스크 여유 공간", free >= need, f"{free / 2**30:.1f}GB 남음, 필요 {need / 2**30:.1f}GB",
         "공간을 비우세요 (예: docker system prune, 오래된 worktree 정리)")
    arch = platform.machine().lower()
    item("arch", "CPU 아키텍처", arch in ("x86_64", "amd64", "arm64", "aarch64"), arch,
         "이미지가 x86_64(amd64)와 arm64만 지원해요")
    if MAC:
        shared = _mac_shared()
        outside = [p for p in paths if not any(p == s or p.startswith(s.rstrip("/") + "/") for s in shared)]
        item("sharing", "Docker Desktop 파일 공유", not outside,
             "연결할 경로가 모두 공유 범위 안에 있어요" if not outside else "공유 범위 밖: " + ", ".join(outside),
             "Docker Desktop > Settings > Resources > File sharing에 추가하세요: " + ", ".join(outside))
    return out


# ---------------------------------------------------------------- host -> Docker pieces

def mounts() -> list[str]:
    """The folders the container sees at the same absolute paths: every repos.path, worktree_root, clone_root,
    ~/.claude, ~/.codex. Children of another entry are left out (the parent's mount covers them)."""
    s = core.settings()
    ps = [r.get("path") for r in (core.project_map().get("repos") or {}).values()]
    ps += [s.get("worktree_root"), s.get("clone_root"), "~/.claude", "~/.codex"]
    out: list[str] = []
    for p in sorted({os.path.abspath(os.path.expanduser(p)) for p in ps if p}):
        if not any(p.startswith(o + "/") for o in out):
            out.append(p)
    return out


def override_yaml(paths, uid: int, gid: int, home: str, gitconfig: str | None = None) -> str:
    j = json.dumps  # a JSON string is a YAML string: spaces, colons and quotes in paths stay intact
    lines = ["# Written by `tp mode docker` (TP-165), gitignored. Host folders at the same absolute paths, so the DB's",
             "# paths stay valid; the container user has the host uid/gid and HOME is the host home.",
             "services:", "  tp:", "    build:", "      args:",
             f"        TP_UID: {j(str(uid))}", f"        TP_GID: {j(str(gid))}", f"        TP_HOME: {j(home)}",
             "    environment:", f"      HOME: {j(home)}", "    volumes:"]
    for p in paths:
        lines += ["      - type: bind", f"        source: {j(p)}", f"        target: {j(p)}"]
    if gitconfig:  # the agents' commit name / email
        lines += ["      - type: bind", f"        source: {j(gitconfig)}", f"        target: {j(gitconfig)}",
                  "        read_only: true"]
    return "\n".join(lines) + "\n"


def env_text(old: str, values: dict) -> str:
    """docker/.env with values set; other lines (TP_ORG, comments) stay."""
    out, seen = [], set()
    for ln in old.splitlines():
        k = ln.split("=", 1)[0].strip()
        if "=" in ln and not ln.lstrip().startswith("#") and k in values:
            out.append(f"{k}={values[k]}")
            seen.add(k)
        else:
            out.append(ln)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    return "\n".join(out) + "\n"


def _env_get(key: str) -> str:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return ""
    return next((ln.split("=", 1)[1].strip() for ln in lines if ln.split("=", 1)[0].strip() == key and "=" in ln), "")


def _github_login() -> str | None:
    from .server import github_status
    return github_status().get("login")


def _secret_names() -> list[tuple[str, str]]:
    """(service, account) of every token tp keeps (core secrets: GitHub, Asana, a custom OAuth client id)."""
    from .server import GITHUB_OAUTH_KEYCHAIN
    return [(core.GITHUB_KEYCHAIN, "github"), (core.settings()["asana_keychain_service"], "asana"),
            (GITHUB_OAUTH_KEYCHAIN, "client_id")]


def host_secrets() -> dict:
    """{service: {account: value}} of the stored tokens. Never printed."""
    out: dict = {}
    for svc, acct in _secret_names():
        v = core.secret_get(svc)  # any account: older Asana entries were saved under another name
        if v:
            out.setdefault(svc, {})[acct] = v
    return out


# reads {service: {account: value}} from stdin and stores each in the container's secrets file
SECRETS_IN = ("import json, sys\nfrom ticket_pilot import core\n"
              "for s, a in json.load(sys.stdin).items():\n    for k, v in a.items():\n        core.secret_set(s, k, v)\n"
              "print('ok')")
SECRETS_OUT = "import json\nfrom ticket_pilot import core\nprint(json.dumps(core._secrets_read()))"
BUSY_PY = "from ticket_pilot import core, migrate\nprint('\\n'.join(migrate.busy(core.db())))"


def warnings() -> list[str]:
    """What may fail in the container with the host's ~/.claude, ~/.codex (shown, never changed)."""
    out = []
    home = Path.home()
    try:
        hooks = json.loads((home / ".claude" / "settings.json").read_text()).get("hooks") or {}
        n = sum(len(h.get("hooks") or []) for groups in hooks.values() for h in groups if isinstance(h, dict))
    except (OSError, ValueError, AttributeError, TypeError):
        n = 0
    if n:
        out.append(f"~/.claude/settings.json에 훅 명령 {n}개: 컨테이너(Linux)에 없는 프로그램이나 맥 경로를 쓰면 실패해요")
    try:
        toml = (home / ".codex" / "config.toml").read_text()
    except OSError:
        toml = ""
    # the keys only (a value may be a token); [projects."<path>"] headers are trust entries, harmless
    keys = list(dict.fromkeys(ln.split("=", 1)[0].strip() for ln in toml.splitlines()
                              if "=" in ln and not ln.lstrip().startswith(("#", "["))
                              and re.search(r"localhost|127\.0\.0\.1|/Users/|/opt/homebrew|/Applications/", ln)))
    if keys:
        out.append(f"~/.codex/config.toml에 localhost나 맥 경로를 쓰는 값 {len(keys)}개 ({', '.join(keys[:8])}"
                   + (", …" if len(keys) > 8 else "") + "): 컨테이너에서는 안 닿을 수 있어요")
    if MAC:
        out.append("macOS의 Claude 로그인은 keychain에 있어 컨테이너는 ~/.claude/.credentials.json만 봐요. 컨테이너에서 "
                   "claude 인증이 실패하면 대시보드 설정 → 에이전트 → 활성 계정에서 로그인하세요")
    return out


def _wait(port: int, want: int, secs=UP_WAIT_S):
    url = f"http://127.0.0.1:{port}/api/state"
    code = None
    for _ in range(secs):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except OSError:
            code = None
        if code == want:
            return
        time.sleep(1)
    raise RuntimeError(f"{url}이 {secs}초 안에 {want}로 답하지 않았어요 (마지막: {code}). cd docker && docker compose logs tp")


def _stop_services():
    if MAC:
        migrate._launchd_stop()
    else:
        _sh(["systemctl", "--user", "disable", "--now", "ticket-pilot-tick.timer", "ticket-pilot-dashboard.service"],
            check=False)


def _install():
    _sh([str(core.ROOT / "launchd" / "install.sh")], quiet=False, cwd=core.ROOT)


def _services_installed() -> bool:
    from . import setup as S
    return S.installed()


def _backup_db() -> str:
    dst = core.DB_PATH.parent / "backups" / f"tp-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return str(dst)


def _container_running() -> bool:
    return bool(_dc("ps", "--status", "running", "-q", "tp", check=False).stdout.strip())


def _container_busy() -> list[str]:
    if not _container_running():
        return []
    p = _dc("exec", "-T", "tp", "python3", "-P", "-c", BUSY_PY)
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


# ---------------------------------------------------------------- the switch

def _plan_docker(j: dict, admins: str | None) -> tuple[list[str], list[str], list[str]]:
    """(lines, problems, warnings) for host -> Docker; fills j with what the steps need."""
    s = core.settings()
    if not j.get("done"):
        j["mounts"] = mounts()
        j["port"] = int(s["dashboard_port"])
        j["services"] = _services_installed()
        gc = Path.home() / ".gitconfig"
        j["gitconfig"] = str(gc) if gc.is_file() else None
    roots = {os.path.abspath(os.path.expanduser(s.get(k) or "")) for k in ("worktree_root", "clone_root")}
    missing = [p for p in j["mounts"] if not os.path.exists(p) and p not in roots]
    j["admins"] = admins or j.get("admins") or _env_get("TP_ADMINS") or _github_login() or ""
    lines = ["같은 절대경로로 연결 (docker/compose.override.yaml):"] + [
        f"  {p}" + ("   (없어요: 건너뜀)" if p in missing else "") for p in j["mounts"]]
    if j["gitconfig"]:
        lines.append(f"  {j['gitconfig']}   (읽기 전용: 에이전트 커밋의 이름과 이메일)")
    lines += [f"컨테이너 사용자: uid {os.getuid()}, gid {os.getgid()}, HOME {Path.home()}",
              f"관리자 (TP_ADMINS): {j['admins'] or '(없음)'}",
              f"주소: http://127.0.0.1:{j['port']} (모든 접속에 GitHub 로그인)",
              f"DB: {core.DB_PATH} → 백업 → tp-data volume, 이후 호스트 DB는 읽기 전용",
              "토큰: " + (", ".join(sorted(host_secrets())) or "(없음)") + " → volume의 비밀값 파일",
              "서비스: " + ("launchd" if MAC else "systemd") + (" 멈춤" if j["services"] else " (설치돼 있지 않아요)")]
    problems = []
    if not ADMINS_RE.fullmatch(j["admins"]):
        problems.append("관리자 GitHub 아이디가 없어요: --admins <아이디,아이디> (GitHub 연결이 있으면 그 계정이 기본값)")
    if "up" not in j.get("done", []):  # once the DB is in the volume, the host one is not the live one anymore
        problems += [f"실행 중: {b}" for b in migrate.busy(core.db())]
    problems += [f"Docker 환경: {c['title']}: {c['detail']} → {c['fix']}" for c in check(j["mounts"]) if not c["ok"]]
    return lines, problems, [f"{p}가 없어 연결하지 않아요" for p in missing] + warnings()


def _steps_docker(j: dict) -> list:
    uid, gid = os.getuid(), os.getgid()

    def files():
        s = core.settings()
        for k in ("worktree_root", "clone_root"):  # mounted, so they must exist (docker would make them root's)
            os.makedirs(os.path.expanduser(s[k]), exist_ok=True)
        paths = [p for p in j["mounts"] if os.path.exists(p)]
        OVERRIDE.write_text(override_yaml(paths, uid, gid, str(Path.home()), j["gitconfig"]))
        old = ENV_FILE.read_text() if ENV_FILE.exists() else ""
        ENV_FILE.write_text(env_text(old, {"TP_ADMINS": j["admins"], "TP_PORT": j["port"]}))
        print(f"  {OVERRIDE}\n  {ENV_FILE}")

    def db_in():
        name = os.path.basename(j["backup"])
        # as root: a tp-data volume from an earlier Docker install may belong to another uid; an older tp.db stays
        _dc("run", "--rm", "--no-deps", "-T", "--user", "0", "-v", _vol(os.path.dirname(j['backup']), "/import:ro"),
            "tp", "sh", "-c",
            f'set -e; cd {DATA_IN}; mkdir -p backups; '
            f'if [ -f tp.db ]; then mv tp.db "backups/tp-before-mode-$(date +%s).db"; fi; '
            f'cp "/import/{name}" tp.db; chown -R {uid}:{gid} {DATA_IN}')

    def secrets():
        d = host_secrets()
        if d:
            _dc("run", "--rm", "--no-deps", "-T", "tp", "python3", "-P", "-c", SECRETS_IN,
                input=json.dumps(d))
        print(f"  {len(d)}개")

    def up():
        _dc("up", "-d", quiet=False)
        _wait(j["port"], 401)

    def readonly():
        os.chmod(core.DB_PATH, 0o444)  # TP-8: from now on only the container writes a tp.db

    def stop():
        if j["services"]:
            _stop_services()
        con = core.db()
        again = migrate.busy(con)  # the last tick may have started one
        con.close()
        core._CON = None  # nothing below writes the host DB
        if again:
            raise RuntimeError("그 사이에 시작된 작업이 있어요: " + ", ".join(again))

    return [("stop", "서비스 멈춤" + ("" if j["services"] else " (설치돼 있지 않아요)"), stop),
            ("backup", "DB 백업", lambda: (j.__setitem__("backup", _backup_db()), print(f"  {j['backup']}"))),
            ("files", "compose override, docker/.env", files),
            ("build", "이미지 빌드 (docker compose build)", lambda: _dc("build", quiet=False)),
            ("db", "DB를 tp-data volume으로", db_in),
            ("secrets", "토큰을 volume의 비밀값 파일로", secrets),
            ("up", "docker compose up -d (/api/state가 401로 답하기를 기다림)", up),
            ("readonly", "호스트 DB를 읽기 전용으로", readonly)]


def _undo_docker(j: dict) -> list[str]:
    steps = []
    if any(k in j["done"] for k in ("build", "db", "secrets", "up")):
        steps.append(f"cd {DOCKER_DIR} && docker compose down   # volume은 남아요")
    if "readonly" in j["done"]:
        steps.append(f"chmod 644 {core.DB_PATH}")
    if "files" in j["done"]:
        steps.append(f"rm {OVERRIDE}   # docker compose가 자동으로 읽어요")
    if "stop" in j["done"] and j.get("services"):
        steps.append(f"{core.ROOT}/launchd/install.sh   # 서비스 다시 켜기")
    return steps


def _plan_host(j: dict) -> tuple[list[str], list[str], list[str]]:
    if not j.get("done"):
        j["port"] = int(_env_get("TP_PORT") or 3032)
        j["services"] = j.get("services", True)
    lines = [f"DB: tp-data volume → {core.DB_PATH} (지금 호스트 DB는 백업)",
             "토큰: volume의 비밀값 파일 → " + core.secret_store(),
             "compose: docker compose stop, down (volume은 남아요)",
             "서비스: " + (f"{core.ROOT}/launchd/install.sh ({'launchd' if MAC else 'systemd'})" if j["services"]
                         else "설치하지 않아요 (Docker로 옮기기 전에도 없었어요)"),
             "주소: http://127.0.0.1:<dashboard_port> (127.0.0.1은 로그인 없이)"]
    problems = [f"Docker 환경: {c['title']}: {c['detail']} → {c['fix']}" for c in check()
                if not c["ok"] and c["id"] in ("docker", "daemon", "compose")]
    if not problems and "stop" not in j.get("done", []):
        try:
            problems += [f"실행 중 (컨테이너): {b}" for b in _container_busy()]
        except RuntimeError as e:
            problems.append(f"컨테이너에서 실행 중인 작업을 확인하지 못했어요: {e}")
    return lines, problems, []


def _steps_host(j: dict) -> list:
    uid, gid = os.getuid(), os.getgid()

    def backup():
        if core.DB_PATH.exists():
            dst = core.DB_PATH.parent / "backups" / f"tp-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-host.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(core.DB_PATH, dst)
            j["backup"] = str(dst)
            print(f"  {dst}")

    def db_out():
        out = core.DB_PATH.parent / "backups" / f"docker-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        out.mkdir(parents=True, exist_ok=True)
        _dc("run", "--rm", "--no-deps", "-T", "--user", "0", "-v", _vol(out, "/export"), "tp", "sh", "-c",
            f'set -e; python3 -c "import sqlite3; d = sqlite3.connect(\'/export/tp.db\'); '
            f'sqlite3.connect(\'{DATA_IN}/tp.db\').backup(d); d.close()"; chown {uid}:{gid} /export/tp.db')
        tmp = core.DB_PATH.with_name("tp.db.from-docker")
        shutil.copyfile(out / "tp.db", tmp)
        os.chmod(tmp, 0o644)
        for x in ("-journal", "-wal", "-shm"):
            Path(f"{core.DB_PATH}{x}").unlink(missing_ok=True)
        os.replace(tmp, core.DB_PATH)
        print(f"  {core.DB_PATH} (volume의 사본: {out}/tp.db)")

    def secrets():
        p = _dc("run", "--rm", "--no-deps", "-T", "--user", "0", "tp", "python3", "-P", "-c", SECRETS_OUT)
        d = json.loads(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}")
        n = 0
        for svc, accts in d.items():
            for acct, v in accts.items():
                core.secret_set(svc, acct, v)
                n += 1
        print(f"  {n}개")

    return [("stop", "docker compose stop (돌던 tick이 끝나기를 기다림)", lambda: _dc("stop", quiet=False)),
            ("backup", "호스트 DB 백업", backup),
            ("db", "volume의 DB를 호스트로", db_out),
            ("secrets", "volume의 토큰을 " + core.secret_store() + "로", secrets),
            ("down", "docker compose down (volume은 남아요)", lambda: _dc("down", quiet=False)),
            ("install", "서비스 다시 설치 (launchd/install.sh)",
             lambda: _install() if j["services"] else print("  건너뜀 (설치돼 있지 않았어요)"))]


def _undo_host(j: dict) -> list[str]:
    steps = []
    if "db" in j["done"] and j.get("backup"):
        steps.append(f"cp {j['backup']} {core.DB_PATH} && chmod 444 {core.DB_PATH}   # 호스트 DB를 전과 같이")
    if "install" in j["done"] and j.get("services"):
        steps.append("서비스 멈추기: " + ("launchctl bootout gui/$(id -u)/com.ticket-pilot.tick (dashboard도)" if MAC
                                     else "systemctl --user disable --now ticket-pilot-tick.timer ticket-pilot-dashboard.service"))
    if "stop" in j["done"]:
        steps.append(f"cd {DOCKER_DIR} && docker compose up -d")
    return steps


def switch(to: str, apply: bool = False, admins: str | None = None):
    if not apply:
        return _switch(to, apply, admins)
    # one run at a time: a second one (double click, CLI) would redo finished steps over the live DB
    state_path().parent.mkdir(parents=True, exist_ok=True)
    with open(state_path().with_name("mode.lock"), "w") as run_lock:
        try:
            fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("다른 tp mode가 실행 중이에요. 끝난 뒤 다시 실행하세요 (기록: ~/tp-work/logs/mode.log)") from None
        return _switch(to, apply, admins)


def _switch(to: str, apply: bool, admins: str | None):
    sp = state_path()
    prev = _state()
    if prev and not prev.get("finished") and prev.get("to") != to:
        raise SystemExit(f"{'Docker' if prev['to'] == 'docker' else '호스트'}로 바꾸던 중이에요 ({sp}). 먼저 끝내세요: "
                         f"bin/tp mode {prev['to']} --apply   (그만두려면 그 실행이 알려준 대로 되돌린 뒤 {sp}을 지우세요)")
    resumed = bool(prev) and not prev.get("finished") and prev.get("to") == to
    if not resumed and (current() == "docker") == (to == "docker"):
        print(f"이미 {'Docker' if to == 'docker' else '호스트'}에서 돌고 있어요. 바꿀 것이 없어요.")
        return
    j = prev if resumed else {"to": to, "done": [], "started": core.now(),
                              # going back: install the services only when they were there before Docker
                              **({"services": prev.get("services", True)} if to == "host" else {})}
    lines, problems, warns = _plan_docker(j, admins) if to == "docker" else _plan_host(j)
    print(("호스트 → Docker" if to == "docker" else "Docker → 호스트") + ("   (이어서 실행: 끝난 단계는 건너뛰어요)" if resumed else ""))
    for ln in lines:
        print(ln)
    for w in warns:
        print(f"주의: {w}")
    sys.stdout.flush()  # the plan before the reason on a shared terminal / log
    if problems:
        raise SystemExit("\n바꿀 수 없어요 (아무것도 바꾸지 않았어요):\n" + "\n".join(f"  - {p}" for p in problems))
    if not apply:
        print("\n미리보기예요. 바꾼 것은 없어요. 실제로 바꾸려면 --apply를 붙이세요.")
        return

    def save():
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(j, ensure_ascii=False, indent=1))

    lock = None
    try:
        if to == "docker" and "up" not in j["done"]:
            lock = migrate.tick_lock()  # held until the container runs: a host tick must not start an agent
        save()
        for key, title, fn in _steps_docker(j) if to == "docker" else _steps_host(j):
            if key in j["done"]:
                print(f"- {title}: 이미 끝남")
                continue
            print(f"- {title}", flush=True)
            fn()
            j["done"].append(key)
            save()
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001
        sys.stdout.flush()
        if to == "docker" and "db" in j["done"] and "up" not in j["done"]:
            _dc("stop", check=False)  # a container that came up would write the volume's copy while the host one is live
        undo = (_undo_docker if to == "docker" else _undo_host)(j) + [f"rm {sp}   # 안 지우면 다음 실행이 끝난 단계를 건너뛰어요"]
        raise SystemExit(f"\n실패: {e}\n끝난 단계: {', '.join(j['done']) or '(없음)'}\n\n"
                         f"고친 뒤 이어서 하려면 (끝난 단계는 건너뛰어요):\n  {core.ROOT}/bin/tp mode {to} --apply\n\n"
                         "되돌리려면 (순서대로):\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(undo, 1)))
    finally:
        if lock:
            lock.close()
    j["finished"] = core.now()
    save()
    if to == "docker":
        print(f"\n끝났어요 (Docker). http://127.0.0.1:{j['port']} 에서 GitHub로 로그인하세요 (관리자: {j['admins']}).\n"
              f"  에이전트 로그인: 대시보드 설정 → 에이전트 → 활성 계정   업데이트: git pull && "
              f"(cd docker && docker compose up -d --build)\n"
              f"  호스트 DB {core.DB_PATH}는 이제 읽기 전용 백업이에요. 돌아가려면: bin/tp mode host --apply")
    else:
        print(f"\n끝났어요 (호스트). 대시보드: http://127.0.0.1:{j['port']} (로그인 없이). "
              "Docker volume(tp-data)은 남아 있어요: 필요 없으면 cd docker && docker compose down -v")


def main(what: str, apply: bool = False, admins: str | None = None):
    if core.docker():
        raise SystemExit(f"{core.DOCKER_OFF}: 호스트의 checkout에서 실행하세요 (bin/tp mode {what})")
    if what == "check":
        print(f"지금 방식: {({'docker': 'Docker', 'macos': 'macOS 직접', 'linux': 'Linux 직접'})[current()]}")
        items = check(mounts() if current() != "docker" else [])
        for c in items:
            print(f"{'통과' if c['ok'] else '실패'}  {c['title']}: {c['detail']}" + (f"\n      → {c['fix']}" if c["fix"] else ""))
        raise SystemExit(0 if all(c["ok"] for c in items) else 1)
    switch(what, apply, admins)
