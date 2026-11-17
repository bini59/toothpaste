"""tp setup (TP-144): the second of the two install lines (`gh repo clone ... ~/tp-work/app && ~/tp-work/app/bin/tp setup`).
Safe to run again: each step skips what is already done. Prechecks first (everything missing is listed, nothing is
installed), then web/dist (the release's web-dist.tar.gz, else a build), the dashboard port (dashboard_port), the launchd
agents (launchd/install.sh; on Linux it installs systemd user units, TP-162) and the browser on #start, where the rest
(Asana, GitHub, name, first project) happens."""
from __future__ import annotations

import getpass
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import config as G
from . import update as U
from .core import ROOT, db, read_config, settings
from .runner import agent_path

PY_ORG = "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"  # launchd/install.sh prefers it
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.ticket-pilot.dashboard.plist"
UNIT = Path.home() / ".config" / "systemd" / "user" / "ticket-pilot-dashboard.service"  # systemd/install.sh
PORT_FROM, PORT_TRIES = 3032, 100


def _run_out(run, cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def precheck(which=shutil.which, run=subprocess.run, system=platform.system,
             version=sys.version_info) -> tuple[list[str], list[str]]:
    """(missing, warnings): each line says what is missing and how to get it."""
    path = agent_path()  # claude / codex installed with nvm's npm are not on the login PATH
    miss, warn = [], []
    osname = system()
    mac = osname == "Darwin"
    if osname not in ("Darwin", "Linux"):
        miss.append(f"macOS나 Linux가 필요해요 (지금 {osname})")
    if version < (3, 11):  # bin/tp runs python3 -P, and tomllib: both 3.11
        miss.append(f"python3 3.11 이상 (지금 {version[0]}.{version[1]}): " +
                    ("https://www.python.org/downloads/macos/" if mac else "배포판 패키지 (예: sudo apt install python3)"))
    if mac and not os.access(PY_ORG, os.X_OK):
        warn.append("python.org Python이 없어 Homebrew Python으로 진행해요. 업그레이드마다 로컬 네트워크 권한이 풀릴 수 있어요: "
                    "https://www.python.org/downloads/macos/ (README Setup > 고급 > Python 참고)")
    if osname == "Linux":
        if not which("systemctl"):
            miss.append("systemd가 필요해요 (systemctl이 없어요. WSL이나 컨테이너는 지원하지 않아요)")
        elif _run_out(run, ["systemctl", "--user", "show-environment"]).returncode:
            miss.append("systemctl --user를 쓸 수 없어요: systemd 사용자 세션이 없어요 (WSL이나 컨테이너, 또는 su로 바꾼 셸이면 "
                        "그 사용자로 직접 로그인(ssh)해서 실행하세요)")
        elif _run_out(run, ["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"]).stdout.strip() != "yes":
            warn.append("linger가 꺼져 있어 로그아웃하면 대시보드와 tick이 멈춰요. 계속 돌리려면: "
                        f"sudo loginctl enable-linger {getpass.getuser()}")
        if not which("lsof"):  # worktree cleanup and dev servers look up processes with it (macOS ships it)
            miss.append("lsof: sudo apt install lsof (배포판 패키지)")
    if not which("git"):
        miss.append("git: " + ("xcode-select --install" if mac else "sudo apt install git (배포판 패키지)"))
    if not which("gh"):
        miss.append("gh: " + ("brew install gh" if mac else "배포판 패키지나 https://github.com/cli/cli/blob/trunk/docs/install_linux.md")
                    + " (https://cli.github.com)")
    elif run(["gh", "auth", "status"], capture_output=True, text=True).returncode:
        miss.append("gh 로그인: gh auth login")
    if not (which("claude", path=path) or which("codex", path=path)):
        miss.append("claude나 codex 중 하나: npm i -g @anthropic-ai/claude-code 또는 npm i -g @openai/codex")
    return miss, warn


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def installed(plist=None, unit=None, mac=sys.platform == "darwin") -> bool:
    """This checkout's dashboard agent is installed (launchd/install.sh ran here before; on Linux its systemd unit)."""
    try:
        if not mac:
            return f"WorkingDirectory={ROOT}\n" in Path(unit or UNIT).read_text()
        with open(plist or PLIST, "rb") as f:
            return plistlib.load(f).get("WorkingDirectory") == str(ROOT)
    except (OSError, plistlib.InvalidFileException):
        return False


def pick_port(free=None, is_installed=None) -> tuple[int, bool]:
    """(port, newly chosen). A stored dashboard_port is kept; an install from before the setting keeps the default it
    runs on (3032 on the owner's Mac); otherwise the first free port from 3032, saved to dashboard_port."""
    free, is_installed = free or _free, is_installed or installed
    con = db()
    stored = ((read_config(con) or {}).get("settings") or {}).get("dashboard_port")
    if stored:
        return stored, False
    port = settings()["dashboard_port"] if is_installed() else \
        next((p for p in range(PORT_FROM, PORT_FROM + PORT_TRIES) if free(p)), None)
    if port is None:
        raise SystemExit(f"{PORT_FROM}~{PORT_FROM + PORT_TRIES - 1}에 빈 포트가 없어요: tp config set global.dashboard_port <포트>")
    G.set_path("global.dashboard_port", port, actor="cli")
    return port, True


def up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
            return r.status == 200
    except OSError:
        return False


def web_dist(root=ROOT, say=print, gh="gh", npm="npm") -> None:
    """web/dist: the current tag's release asset (tp update's download), else npm ci + build."""
    web = Path(root) / "web"
    if (web / "dist" / "index.html").is_file():
        say("화면 파일: 있음, 건너뜀")
        return
    (Path(root) / "data").mkdir(exist_ok=True)  # _fetch_dist unpacks there; a fresh clone has no data/ yet
    env = {**os.environ, "CI": "1", "PATH": agent_path()}
    tag = U._git(root, "describe", "--tags", "--exact-match", "--match", "v*", check=False)
    if tag:
        try:
            if U._fetch_dist(Path(root), tag, gh, env, say):
                say(f"화면 파일: {tag} 릴리즈에서 받음")
                return
        except (U.UpdateError, OSError) as e:
            say(f"{e}: 직접 빌드")
    else:
        say("이 커밋에 릴리즈 태그가 없어 화면을 직접 빌드해요")
    say("npm ci && npm run build (web/)")
    U._run([npm, "ci", "--no-audit", "--no-fund"], web, env)
    U._run([npm, "run", "build"], web, env)


def open_browser(url: str, say=print, which=shutil.which, mac=sys.platform == "darwin", env=os.environ) -> None:
    """open (macOS) or xdg-open; with neither (a server) just the address."""
    # Debian's `open` is openvt, and without a display xdg-open may start a text browser in this terminal
    gui = mac or env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")
    opener = gui and which("open" if mac else "xdg-open")
    if not opener or subprocess.run([opener, url], capture_output=True).returncode:
        say(f"브라우저를 열지 못했어요. 이 주소를 여세요: {url}")


def setup(root=ROOT, say=print, install=None, opener=open_browser, check=precheck) -> int:
    miss, warn = check()
    for w in warn:
        say(f"주의: {w}")
    if miss:
        say("먼저 설치하거나 로그인해 주세요 (아무것도 바꾸지 않았어요):")
        for m in miss:
            say(f"  - {m}")
        return 1
    say("사전 점검: 통과")
    try:
        web_dist(root, say)
    except (U.UpdateError, OSError) as e:
        say(f"실패: 화면 파일을 만들지 못했어요: {e}")
        return 1
    port, new = pick_port()
    say(f"대시보드 포트: {port}{' (dashboard_port에 저장)' if new else ''}")
    if installed() and up(port):
        say("백그라운드 실행: 이미 설치돼 응답함, 건너뜀")
    else:
        say("launchd/install.sh" + ("" if sys.platform == "darwin" else " (Linux: systemd/install.sh)"))
        p = subprocess.run(install or [str(Path(root) / "launchd" / "install.sh")], cwd=root)
        if p.returncode:
            say("실패: launchd/install.sh (위 출력 참고)")
            return 1
    url = f"http://127.0.0.1:{port}/#start"
    say(f"시작하기: {url}")
    opener(url, say)
    return 0
