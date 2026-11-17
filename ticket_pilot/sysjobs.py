"""시스템 자동화 page: automations on this Mac that are not ticket-pilot's own tick (launchd, cron, Codex app).

Read-only. Everything is collected on request:
  - launchd: plists in ~/Library/LaunchAgents (plistlib) + live state from `launchctl list` / `launchctl print-disabled`
  - cron:    `crontab -l` of the current user
  - codex:   ~/.codex/automations/<id>/automation.toml (Codex app automations)
Logs are only readable through the files a collected job declares (StandardOutPath/StandardErrorPath, a cron
redirect target, files next to a Codex automation), never an arbitrary path from the request.
"""
from __future__ import annotations

import glob
import os
import plistlib
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .core import DOCKER_OFF, docker

LAUNCH_AGENTS = Path(os.environ.get("TP_LAUNCH_AGENTS", "~/Library/LaunchAgents")).expanduser()
CODEX_AUTOMATIONS = Path(os.environ.get("TP_CODEX_AUTOMATIONS", "~/.codex/automations")).expanduser()
TAIL_BYTES = 64 * 1024
DAYS = ["일", "월", "화", "수", "목", "금", "토"]  # launchd/cron weekday 0 = Sunday (7 too)
RRULE_DAYS = {"SU": "일", "MO": "월", "TU": "화", "WE": "수", "TH": "목", "FR": "금", "SA": "토"}


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _every(sec: int) -> str:
    if sec % 86400 == 0:
        return f"{sec // 86400}일마다"
    if sec % 3600 == 0:
        return f"{sec // 3600}시간마다"
    if sec % 60 == 0:
        return f"{sec // 60}분마다"
    return f"{sec}초마다"


def _calendar(c: dict) -> str:
    hh, mm = c.get("Hour"), c.get("Minute")
    at = f"{hh:02d}:{mm or 0:02d}" if hh is not None else (f"매시 {mm}분" if mm is not None else "")
    if "Weekday" in c:
        return f"매주 {DAYS[c['Weekday'] % 7]} {at}".strip()
    if "Day" in c:
        return f"매월 {c['Day']}일 {at}".strip()
    if hh is not None:
        return f"매일 {at}"
    return at or "달력 일정"


def launchd_schedule(d: dict) -> str:
    """Korean one-liner for when a launchd job runs."""
    parts = []
    if d.get("StartInterval"):
        parts.append(_every(int(d["StartInterval"])))
    cal = d.get("StartCalendarInterval")
    if cal:
        parts += [_calendar(c) for c in (cal if isinstance(cal, list) else [cal])]
    if d.get("WatchPaths"):
        parts.append("파일 변경 시")
    if d.get("KeepAlive"):
        parts.append("상시 실행" if d["KeepAlive"] is True else "종료 시 재시작")
    if d.get("RunAtLoad"):
        parts.append("로그인 시")
    return " · ".join(parts) or "요청 시"


def is_app_item(program: str) -> bool:
    """Updaters and helpers installed by apps (program inside an .app bundle, or none at all)."""
    return not program or ".app/Contents/" in program or program.startswith(("/Applications/", "/Library/", "/System/"))


def _launchctl_state() -> tuple[dict, set]:
    loaded = {}
    for line in _run(["launchctl", "list"]).splitlines()[1:]:
        f = line.split("\t")
        if len(f) == 3:
            loaded[f[2]] = {"pid": None if f[0] == "-" else int(f[0]), "last_exit": None if f[1] == "-" else int(f[1])}
    disabled = set(re.findall(r'"([^"]+)" => disabled', _run(["launchctl", "print-disabled", f"gui/{os.getuid()}"])))
    return loaded, disabled


def _file(path: str | None, label: str) -> dict | None:
    if not path:
        return None
    p = Path(path).expanduser()
    try:
        st = p.stat()
        return {"label": label, "path": str(p), "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")}
    except OSError:
        return {"label": label, "path": str(p), "size": None, "mtime": None}


def _logs(*pairs) -> list[dict]:
    """[(path, label)] -> file infos, one entry per distinct path (stdout and stderr often share a file)."""
    out, seen = [], set()
    for path, label in pairs:
        f = _file(path, label)
        if f and f["path"] not in seen:
            seen.add(f["path"])
            out.append(f)
        elif f:
            for o in out:
                if o["path"] == f["path"]:
                    o["label"] = "stdout+stderr"
    return out


def collect_launchd() -> list[dict]:
    loaded, disabled = _launchctl_state()
    jobs = []
    for f in sorted(glob.glob(str(LAUNCH_AGENTS / "*.plist"))):
        try:
            with open(f, "rb") as fh:
                d = plistlib.load(fh)
        except Exception as e:  # noqa: BLE001
            jobs.append({"id": f"launchd:{Path(f).stem}", "source": "launchd", "name": Path(f).stem, "file": f,
                         "error": f"plist를 읽지 못했어요: {e}", "app_item": False, "logs": [], "schedule": "-",
                         "status": "error", "command": ""})
            continue
        label = d.get("Label") or Path(f).stem
        args = [str(a) for a in d.get("ProgramArguments") or []]
        program = str(d.get("Program") or (args[0] if args else ""))
        live = loaded.get(label)
        if label in disabled or d.get("Disabled") is True:
            status = "disabled"
        elif not live:
            status = "not_loaded"
        elif live["pid"]:
            status = "running"
        elif live["last_exit"]:
            status = "failed"
        else:
            status = "idle"
        jobs.append({
            "id": f"launchd:{label}", "source": "launchd", "name": label, "file": f,
            "command": " ".join(args) if args else program,
            "schedule": launchd_schedule(d), "status": status,
            "pid": live and live["pid"], "last_exit": live and live["last_exit"],
            "app_item": is_app_item(program),
            "logs": _logs((d.get("StandardOutPath"), "stdout"), (d.get("StandardErrorPath"), "stderr")),
        })
    return jobs


CRON_ALIASES = {"@reboot": "부팅 시", "@hourly": "매시 정각", "@daily": "매일 00:00", "@midnight": "매일 00:00",
                "@weekly": "매주 일 00:00", "@monthly": "매월 1일 00:00", "@yearly": "매년 1월 1일", "@annually": "매년 1월 1일"}
REDIRECT = re.compile(r"(\d?)>>?\s*([^\s;&|]+)")


def cron_schedule(spec: str) -> str:
    if spec in CRON_ALIASES:
        return CRON_ALIASES[spec]
    m, h, dom, mon, dow = spec.split()
    if m.startswith("*/") and h == "*" and dom == mon == dow == "*":
        return f"{m[2:]}분마다"
    if m.isdigit() and h.startswith("*/") and dom == mon == dow == "*":
        return f"{h[2:]}시간마다 ({m}분)"
    if m.isdigit() and h == "*" and dom == mon == dow == "*":
        return f"매시 {m}분"
    if m.isdigit() and h.isdigit() and dom == mon == "*":
        at = f"{int(h):02d}:{int(m):02d}"
        if dow == "*":
            return f"매일 {at}"
        if dow.isdigit():
            return f"매주 {DAYS[int(dow) % 7]} {at}"
    return spec


def parse_crontab(text: str) -> list[dict]:
    jobs = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", line):
            continue
        if line.startswith("@"):
            spec, _, cmd = line.partition(" ")
        else:
            f = line.split(None, 5)
            if len(f) < 6:
                continue
            spec, cmd = " ".join(f[:5]), f[5]
        cmd = cmd.strip()
        files = [(p, "stderr" if fd == "2" else "stdout") for fd, p in REDIRECT.findall(cmd)
                 if not p.startswith("&") and p != "/dev/null"]
        try:
            schedule = cron_schedule(spec)
        except ValueError:
            schedule = spec
        jobs.append({"id": f"cron:{n}", "source": "cron", "name": cmd.split()[0].rsplit("/", 1)[-1] if cmd else spec,
                     "file": "crontab -l", "command": cmd, "schedule": schedule, "spec": spec, "status": "scheduled",
                     "app_item": False, "logs": _logs(*files)})
    return jobs


def collect_cron() -> list[dict]:
    return parse_crontab(_run(["crontab", "-l"]))


def rrule_schedule(rule: str) -> str:
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq, n = r.get("FREQ", ""), int(r.get("INTERVAL", "1"))
    at = f"{int(r['BYHOUR']):02d}:{int(r.get('BYMINUTE', 0)):02d}" if "BYHOUR" in r and "," not in r["BYHOUR"] else ""
    if freq == "MINUTELY":
        return f"{n}분마다"
    if freq == "HOURLY":
        return f"{n}시간마다" if n > 1 else "매시간"
    if freq == "DAILY":
        return f"매일 {at}".strip()
    if freq == "WEEKLY":
        days = ",".join(RRULE_DAYS.get(d, d) for d in r.get("BYDAY", "").split(",") if d)
        return f"매주 {days} {at}".strip()
    if freq == "MONTHLY":
        return f"매월 {r.get('BYMONTHDAY', '')}일 {at}".strip()
    return rule


def collect_codex() -> list[dict]:
    jobs = []
    for f in sorted(CODEX_AUTOMATIONS.glob("*/automation.toml")):
        try:
            d = tomllib.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            jobs.append({"id": f"codex:{f.parent.name}", "source": "codex", "name": f.parent.name, "file": str(f),
                         "error": f"toml을 읽지 못했어요: {e}", "app_item": False, "logs": [], "schedule": "-",
                         "status": "error", "command": ""})
            continue
        extra = sorted(p for p in f.parent.iterdir() if p.is_file() and p.name != "automation.toml")
        jobs.append({
            "id": f"codex:{d.get('id') or f.parent.name}", "source": "codex", "name": d.get("name") or f.parent.name,
            "file": str(f), "command": d.get("prompt") or "", "kind": d.get("kind"), "model": d.get("model"),
            "cwds": d.get("cwds") or [], "schedule": rrule_schedule(d.get("rrule") or ""), "spec": d.get("rrule"),
            "status": "scheduled" if d.get("status") == "ACTIVE" else "paused", "app_item": False,
            "logs": _logs(*[(str(p), p.name) for p in extra]),
        })
    return jobs


def collect(mac: bool = sys.platform == "darwin") -> dict:
    out, errors = [], []
    if docker():  # the container has no launchd, cron or Codex app (TP-164)
        return {"jobs": [], "errors": [f"{DOCKER_OFF}: 시스템 자동화는 호스트 설치에서만 보여요"],
                "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    sources = [("launchd", collect_launchd), ("cron", collect_cron), ("codex", collect_codex)]
    if not mac:  # TP-162: systemd timers are not listed
        sources.pop(0)
        errors.append("launchd: Linux에서는 지원하지 않아요. tp의 tick, 대시보드는 systemctl --user list-timers, "
                      "systemctl --user status ticket-pilot-dashboard로 보세요")
    for name, fn in sources:
        try:
            out += fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    return {"jobs": out, "errors": errors, "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def read_log(job_id: str, index: int, tail: int = TAIL_BYTES) -> dict | None:
    """Tail of the index-th log file a collected job declares; None when the job or file is unknown."""
    job = next((j for j in collect()["jobs"] if j["id"] == job_id), None)
    if not job or not 0 <= index < len(job["logs"]):
        return None
    f = job["logs"][index]
    try:
        with open(f["path"], "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            body = fh.read().decode("utf-8", "replace")
    except OSError as e:
        return {**f, "log": "", "error": f"파일을 열 수 없어요: {e.strerror or e}", "truncated": False}
    truncated = size > tail
    if truncated:  # drop the partial first line
        body = body.split("\n", 1)[-1]
    return {**f, "log": body, "truncated": truncated, "size": size}
