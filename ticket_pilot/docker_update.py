"""The Docker dashboard's file exchange with the update manager on the host."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import update

REGISTRY_IMAGE = "ghcr.io/bini59/toothpaste"
REPOSITORY = "bini59/toothpaste"
HEARTBEAT_TIMEOUT = 25
RUNNING = ("queued", "pulling", "switching")
_request_lock = threading.Lock()


def directory() -> Path | None:
    path = os.environ.get("TP_DOCKER_UPDATE_DIR")
    return Path(path) if path else None


def atomic_json(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o644)
            json.dump(value, file)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def status() -> dict:
    path = directory()
    if path is None:
        return {}
    try:
        data = json.loads((path / "state" / "status.json").read_text())
        return {**data, "ready": time.time() - data["heartbeat"] < HEARTBEAT_TIMEOUT}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def maintenance() -> bool:
    return status().get("state") == "switching"


def request(tag: str | None) -> dict:
    path = directory()
    with _request_lock:
        state = status()
        if not path or not state.get("ready"):
            raise update.UpdateError("Docker 업데이트 관리자가 연결되지 않았어요")
        if not tag or not update.TAG.fullmatch(tag) or not update.newer(tag=tag):
            raise update.UpdateError("설치할 새 릴리즈가 없어요. 지금 확인을 눌러 주세요")
        request_file = path / "requests" / "request.json"
        if state.get("state") in RUNNING or request_file.exists():
            raise update.UpdateError("이미 Docker 업데이트가 진행 중이에요")
        job = {"id": uuid.uuid4().hex, "tag": tag, "requested_at": time.time()}
        try:
            atomic_json(request_file, job)
        except OSError:
            raise update.UpdateError("Docker 업데이트 관리자에게 요청을 전달하지 못했어요") from None
        return job
