"""Host-side Docker updates. Requests can select only the latest public toothpaste release."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import docker_update as channel, update

ROOT = Path(__file__).resolve().parent.parent
POLL_SECONDS = 3
PROBE_SECONDS = 75
REQUEST_MAX_AGE = 120
PREFLIGHT = """
import fcntl
from ticket_pilot import core, migrate
with (core.DATA / 'tick.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('자동화가 실행 중이에요. 끝난 뒤 다시 시도하세요')
    with (core.DATA / 'docker-update.lock').open('a') as writes:
        try:
            fcntl.flock(writes, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('대시보드 요청을 처리 중이에요. 잠시 뒤 다시 시도하세요')
        con = core.db()
        try:
            busy = migrate.busy(con)
        finally:
            con.close()
        if busy:
            raise SystemExit('실행 중인 작업이 있어요: ' + ', '.join(busy))
"""
PROBE = """
import json, urllib.request
from ticket_pilot import update
with urllib.request.urlopen('http://127.0.0.1:3032/api/update/health', timeout=3) as response:
    if not json.load(response)['ready']:
        raise SystemExit('대시보드가 준비되지 않았어요')
print(update.current())
"""


def _run(*args, cwd=None, input=None, timeout=120) -> str:
    proc = subprocess.run(args, cwd=cwd, input=input, text=True, capture_output=True, timeout=timeout)
    if proc.returncode:
        detail = proc.stderr.strip()[-500:] or f"종료 코드 {proc.returncode}"
        raise RuntimeError(detail)
    return proc.stdout.strip()


def latest_release() -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{channel.REPOSITORY}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "toothpaste-updater"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["tag_name"]


class Manager:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.docker_dir = self.root / "docker"
        self.base = self.docker_dir / ".update"
        self.requests = self.base / "requests"
        self.state = self.base / "state"
        self.status = self.state / "status.json"
        self.journal = self.base / "rollback.json"
        self.docker = shutil.which("docker") or "docker"

    def prepare(self):
        self.base.mkdir(mode=0o700, exist_ok=True)
        self.requests.mkdir(exist_ok=True)
        self.requests.chmod(0o1777)  # the existing container can have a different uid from the host
        self.state.mkdir(mode=0o755, exist_ok=True)

    def compose(self, *args, **kwargs):
        files = ["-f", "compose.yaml"]
        if (self.docker_dir / "compose.override.yaml").is_file():
            files.extend(("-f", "compose.override.yaml"))
        files.extend(("-f", "compose.update.yaml"))
        return _run(self.docker, "compose", *files, *args, cwd=self.docker_dir, **kwargs)

    def publish(self, **status):
        channel.atomic_json(self.status, {**status, "heartbeat": time.time()})

    def snapshot(self):
        try:
            return json.loads(self.status.read_text())
        except (OSError, ValueError):
            return {"state": "idle"}

    def claim(self):
        file = self.requests / "request.json"
        claimed = self.base / "claimed.json"
        try:
            file.replace(claimed)
        except FileNotFoundError:
            return None
        try:
            fd = os.open(claimed, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("request is not a file")
                job = json.loads(source.read(2048))
            if (not re.fullmatch(r"[0-9a-f]{32}", job["id"])
                    or not update.TAG.fullmatch(job["tag"])
                    or not -5 <= time.time() - job["requested_at"] <= REQUEST_MAX_AGE):
                raise ValueError("invalid or expired request")
            return {k: job[k] for k in ("id", "tag")}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return None
        finally:
            if claimed.is_dir() and not claimed.is_symlink():
                # A directory cannot overwrite this empty, protected destination on the next claim.
                quarantine = self.base / "invalid-requests"
                quarantine.mkdir(mode=0o700, exist_ok=True)
                claimed.rename(quarantine / str(time.time_ns()))
            else:
                claimed.unlink(missing_ok=True)

    def image_name(self):
        service = json.loads(self.compose("config", "--format", "json", "tp"))["services"]["tp"]
        for mount in service.get("volumes", []):
            if mount.get("type") != "bind" or mount.get("read_only"):
                continue
            source = Path(mount["source"]).resolve()
            if source == self.requests:
                continue
            # The app shares its filesystem permissions with agents. Keep the host manager and Compose files
            # outside any writable bind that covers the install checkout, even in a migrated host setup.
            if source.is_relative_to(self.root) or self.root.is_relative_to(source):
                raise RuntimeError("설치 저장소가 컨테이너에 쓰기 가능하게 연결돼 있어요. 호스트 전용 설치 경로가 필요해요")
        return service["image"]

    def container(self):
        containers = self.compose("ps", "-q", "tp").splitlines()
        if len(containers) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", containers[0]):
            raise RuntimeError("업데이트할 tp 컨테이너를 하나 찾을 수 있어야 해요")
        return containers[0]

    def exec(self, container, *args):
        return _run(self.docker, "exec", container, *args, timeout=12)

    def wait(self, tag=None):
        until = time.monotonic() + PROBE_SECONDS
        while time.monotonic() < until:
            try:
                version = self.exec(self.container(), "python3", "-c", PROBE)
                if version and (tag is None or version == tag):
                    return
            except (RuntimeError, OSError, subprocess.SubprocessError):
                pass
            time.sleep(2)
        raise RuntimeError("대시보드가 제시간에 시작되지 않았어요")

    def pull(self, tag, container):
        ref = f"{channel.REGISTRY_IMAGE}:{tag}"
        _run(self.docker, "pull", ref, timeout=900)
        image = _run(self.docker, "image", "inspect", "--format", "{{.Id}}", ref)
        uid = self.exec(container, "id", "-u")
        gid = self.exec(container, "id", "-g")
        if uid == gid == "1000":
            return image
        if not re.fullmatch(r"[0-9]{1,10}", uid) or not re.fullmatch(r"[0-9]{1,10}", gid) or int(uid) == 0:
            raise RuntimeError("기존 tp 컨테이너의 사용자 ID를 유지할 수 없어요")
        digest = _run(self.docker, "image", "inspect", "--format", "{{index .RepoDigests 0}}", ref)
        if not re.fullmatch(re.escape(channel.REGISTRY_IMAGE) + r"@sha256:[0-9a-f]{64}", digest):
            raise RuntimeError("내려받은 릴리즈 이미지의 주소를 확인할 수 없어요")
        # Only the node account and image-owned files are adjusted. Existing volumes keep their owners.
        dockerfile = f"""FROM {digest}
USER root
RUN groupmod -o -g {gid} node && usermod -o -u {uid} -g {gid} node \\
 && chown -R {uid}:{gid} /opt/tp /home/node
USER node
"""
        # A separate empty build context keeps the host checkout and its settings out of the build.
        return _run(self.docker, "build", "--quiet", "-f", "-", ".", cwd=self.state, input=dockerfile, timeout=900)

    def replace(self, image, name):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeError("이미지 ID를 확인할 수 없어요")
        _run(self.docker, "image", "tag", image, name)
        self.compose("up", "-d", "--no-build", "--pull", "never", "--no-deps", "--force-recreate", "tp", timeout=400)

    def recover(self, failure):
        rollback = json.loads(self.journal.read_text())
        self.replace(rollback["image"], rollback["name"])
        self.wait()
        self.journal.unlink()
        self.publish(**rollback["job"], state="failed", message=f"{failure} 이전 이미지로 복구했어요")

    def apply(self, job):
        tag = job["tag"]
        self.publish(**job, state="pulling", message=f"{tag} 이미지를 받는 중이에요")
        try:
            if tag != latest_release():
                raise RuntimeError("최신 릴리즈가 바뀌었어요. 지금 확인을 누르고 다시 시도하세요")
            container = self.container()
            current = self.exec(container, "python3", "-c", PROBE)
            # git describe adds -<commits>-g<sha> to a local build past a tag; it requires a newer release.
            installed = re.match(r"^v[0-9]+\.[0-9]+\.[0-9]+", current)
            if installed and update.latest([installed[0], tag]) == installed[0]:
                raise RuntimeError("이 릴리즈 또는 더 최신 버전이 이미 설치되어 있어요")
            name = self.image_name()
            old = _run(self.docker, "inspect", "--format", "{{.Image}}", container)
            candidate = self.pull(tag, container)
            self.publish(**job, state="switching", message="대시보드를 새 이미지로 교체하는 중이에요")
            if container != self.container():
                raise RuntimeError("업데이트 준비 중 tp 컨테이너가 바뀌었어요")
            self.exec(container, "python3", "-c", PREFLIGHT)
            channel.atomic_json(self.journal, {"image": old, "name": name, "job": job})
            self.replace(candidate, name)
            self.wait(tag)
            self.journal.unlink()
            self.publish(**job, state="done", message=f"{tag} 업데이트를 완료했어요")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            print(f"Docker update {tag}: {e}", file=sys.stderr, flush=True)
            if self.journal.exists():
                try:
                    self.recover("새 컨테이너를 시작하지 못했어요.")
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as rollback:
                    print(f"Docker update recovery: {rollback}", file=sys.stderr, flush=True)
                    self.publish(**job, state="switching", message="복구를 다시 시도하고 있어요")
            else:
                self.publish(**job, state="failed", message=str(e))

    def run(self):
        self.prepare()
        with (self.base / "manager.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                if self.journal.exists():
                    try:
                        self.recover("업데이트가 중단됐어요.")
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
                        print(f"Docker update recovery: {e}", file=sys.stderr, flush=True)
                        time.sleep(15)
                        continue
                state = self.snapshot()
                if state.get("state") in channel.RUNNING:
                    stopped = {k: state[k] for k in ("id", "tag") if k in state}
                    self.publish(**stopped, state="failed", message="관리자가 다시 시작됐어요. 업데이트를 다시 요청해 주세요")
                else:
                    self.publish(**state)
                job = self.claim()
                if job:
                    self.apply(job)
                time.sleep(POLL_SECONDS)


def install(manager):
    manager.prepare()
    manager.image_name()  # check the install checkout before starting a host service
    # Build the first managed container from the checked-out release; later updates pull the published image.
    if not manager.compose("ps", "-q", "tp") or not manager.compose("images", "-q", "tp"):
        raise RuntimeError("먼저 docker compose up -d --build로 기존 tp를 실행해 주세요")
    container = manager.container()
    if manager.exec(container, "sh", "-c", 'printf "%s" "$TP_DOCKER_UPDATE_DIR"') != "/run/tp-update":
        manager.compose("up", "-d", "--build", "--no-deps", "tp", timeout=1800)
        manager.wait()

    key = hashlib.sha256(os.fsencode(manager.root)).hexdigest()[:12]
    label = f"com.ticket-pilot.docker-updater.{key}"
    args = [sys.executable, "-m", "ticket_pilot.docker_update_manager", "run"]
    env = {"PATH": os.environ["PATH"]}
    for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_CONFIG", "COMPOSE_PROJECT_NAME"):
        if name in os.environ:
            env[name] = os.environ[name]
    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        path = agents / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args,
                                        "WorkingDirectory": str(manager.root), "EnvironmentVariables": env,
                                        "KeepAlive": True, "ThrottleInterval": 10,
                                        "StandardOutPath": str(manager.base / "manager.log"),
                                        "StandardErrorPath": str(manager.base / "manager.log")}))
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        _run("launchctl", "bootstrap", domain, str(path))
    elif sys.platform == "linux":
        units = Path.home() / ".config" / "systemd" / "user"
        units.mkdir(parents=True, exist_ok=True)
        unit = f"ticket-pilot-docker-updater-{key}.service"

        def quoted(value):
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n") + '"'

        service = [
            "[Unit]", "Description=Toothpaste Docker update manager", "[Service]",
            f"WorkingDirectory={quoted(str(manager.root))}",
            "ExecStart=" + " ".join(quoted(a.replace("$", "$$")) for a in args),
            "Restart=always", "RestartSec=10",
            *[f"Environment={quoted(k + '=' + v)}" for k, v in env.items()],
            "[Install]", "WantedBy=default.target",
        ]
        (units / unit).write_text("\n".join(service) + "\n")
        _run("systemctl", "--user", "daemon-reload")
        _run("systemctl", "--user", "enable", unit)
        _run("systemctl", "--user", "restart", unit)
    else:
        raise RuntimeError("시스템 서비스 등록은 macOS 또는 Linux에서 지원해요. run으로 직접 실행할 수 있어요")


if __name__ == "__main__":
    if os.environ.get("TP_MODE") == "docker":
        sys.exit("Docker 업데이트 관리자는 호스트에서 실행하세요")
    from argparse import ArgumentParser

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "run"))
    action = parser.parse_args().action
    try:
        if action == "install":
            install(Manager())
            print("Docker 업데이트 관리자를 시작했어요")
        else:
            Manager().run()
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        sys.exit(str(e))
