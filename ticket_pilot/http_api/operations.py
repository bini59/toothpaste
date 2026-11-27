"""Dashboard update, run mode, dev server and monitoring HTTP handlers."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import closing

from .. import devserver as DS
from .. import docker_update as DU
from .. import metrics as MET
from .. import sysjobs as SJ
from .. import update as UP
from ..core import DOCKER_OFF, ROOT, db, docker, event, meta_get, repo_rules

_check_lock = threading.Lock()


def update_info(since: int | None, admin: bool = True) -> dict:
    """업데이트 page: versions, release notes, why it cannot run; with since (update.log size when the run started)
    the log lines written after it."""
    con = db()
    try:
        chk = meta_get(con, "update_check") or {}
    finally:
        con.close()
    tag = chk.get("latest")
    docker_update = DU.status() if docker() else {}
    managed = bool(DU.directory()) if docker() else False
    why = UP.blocked() if not docker() else None
    if docker() and not admin:
        why = "Docker 업데이트는 TP_ADMINS에 등록된 관리자만 실행할 수 있어요"
    elif managed and not docker_update.get("ready"):
        why = "호스트의 Docker 업데이트 관리자가 응답하지 않아요"
    available = bool(tag and UP.newer(ROOT, tag))
    out = {"current": UP.current(), "latest": tag, "checked_at": chk.get("at"), "blocked": why,
           "available": available,
           "command": DOCKER_UPDATE if docker() and admin and not managed else None,
           "notes": UP.notes(ROOT, "HEAD", tag) if since is None and tag and available else []}
    if managed:
        out["docker_update"] = {k: docker_update[k] for k in ("id", "tag", "state", "message") if k in docker_update}
    if since is not None:
        try:
            with open(UP.log_path(), "rb") as f:
                f.seek(since)
                out["log"] = f.read().decode(errors="replace").splitlines()
        except OSError:
            out["log"] = []
    return out


def check_update(admin: bool = True) -> dict:
    """POST /api/update/check (지금 확인, TP-153): fetch the tags now, then update_info(). Checks run one at a time: a
    click during another check waits for it, then fetches itself (so its error reaches it too). Raises UpdateError
    when the fetch fails."""
    with _check_lock:
        con = db()
        try:
            UP.check_latest(con, force=True)
            con.commit()
        finally:
            con.close()
    return update_info(None, admin=admin)


DOCKER_UPDATE = "git pull --ff-only && python3 -m ticket_pilot.docker_update_manager install"


def start_update() -> int:
    """POST /api/update: `tp update` in its own session (install.sh restarts this dashboard mid-way). Returns the
    update.log size before it starts, for update_info(since). Docker mode: the host rebuilds the image instead."""
    why = f"{DOCKER_OFF}. 호스트에서 실행하세요: {DOCKER_UPDATE}" if docker() else UP.blocked()
    if why:
        raise SystemExit(why)
    f = UP.log_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    size = f.stat().st_size if f.exists() else 0
    with open(f, "a") as err:  # stdout repeats what update.log gets; stderr (a crash) goes there too
        subprocess.Popen([sys.executable, "-m", "ticket_pilot.cli", "update"], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=err, start_new_session=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    return size


def mode_info(since: int | None) -> dict:
    """설정 → 실행 방식 (TP-165): the current way, the Docker environment check (host only: the container has no
    docker), the last switch; with since (mode.log size when the run started) the log lines after it."""
    from .. import mode as MO
    cur = MO.current()
    out = {"current": cur, "state": MO._state() or None,
           "command": f"bin/tp mode host --apply   (호스트의 {ROOT.name} checkout에서)" if docker() else None,
           "checks": None if cur == "docker" or since is not None else MO.check(MO.mounts())}
    if since is not None:
        try:
            with open(MO.log_path(), "rb") as f:
                f.seek(since)
                out["log"] = f.read().decode(errors="replace").splitlines()
        except OSError:
            out["log"] = []
    return out


def _mode_argv(to, admins) -> list[str]:
    if docker():  # no docker inside the container: the switch back runs on the host
        raise SystemExit(f"{DOCKER_OFF}. 호스트의 checkout에서 실행하세요: bin/tp mode host --apply")
    if to not in ("docker", "host"):
        raise SystemExit("to는 docker 또는 host여야 해요")
    argv = [sys.executable, "-m", "ticket_pilot.cli", "mode", to]
    if admins:
        from .. import mode as MO
        if not isinstance(admins, str) or not MO.ADMINS_RE.fullmatch(admins):
            raise SystemExit("관리자는 GitHub 아이디를 쉼표로 이어 쓰세요")
        argv += ["--admins", admins]
    return argv


def mode_preview(to, admins=None) -> dict:
    """POST /api/mode/preview: `tp mode <to>` without --apply (changes nothing)."""
    try:
        p = subprocess.run(_mode_argv(to, admins), cwd=ROOT, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "미리보기가 2분 안에 끝나지 않았어요 (docker가 응답하지 않아요?)"}
    return {"ok": p.returncode == 0, "text": (p.stdout + p.stderr).strip()}


def start_mode(to, admins=None) -> int:
    """POST /api/mode: `tp mode <to> --apply` in its own session, like start_update (it stops this dashboard).
    Returns the mode.log size before it starts, for mode_info(since)."""
    from .. import mode as MO
    argv = _mode_argv(to, admins) + ["--apply"]
    f = MO.log_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    size = f.stat().st_size if f.exists() else 0
    with open(f, "a") as out:
        out.write(f"\n== {time.strftime('%Y-%m-%dT%H:%M:%S')} tp mode {to} --apply\n")
        out.flush()
        subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True, env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"})
    return size


def get_health(req):
    # The host manager probes via docker exec. Keep the candidate off SQLite until it is accepted.
    ready = (ROOT / "web" / "dist" / "index.html").is_file()
    return req._send(200 if ready else 503, {"ready": ready})


def get_metrics(req):
    query = req.query()
    try:
        mark_id = int(query["marker"][0]) if "marker" in query else None
        days = int(query.get("days", [MET.DEFAULT_DAYS])[0])
        rules = repo_rules()
        with closing(db()) as con:
            con.execute("BEGIN")
            data = MET.report(con, mark_id=mark_id, days=days, rules=rules)
    except ValueError as e:
        return req._send(400, {"error": str(e)})
    return req._send(200, data)


def get_events(req):
    with closing(db()) as con:
        events = con.execute("SELECT * FROM events WHERE kind != 'view' ORDER BY id DESC LIMIT 500")
        return req._send(200, {"events": [dict(e) for e in events]})


def get_mode(req):
    q = req.query()
    try:
        since = int(q["since"][0]) if q.get("since") else None
    except ValueError:
        return req._send(400, {"error": "since"})
    return req._send(200, mode_info(since))


def get_update(req):
    q = req.query()
    try:
        since = int(q["since"][0]) if q.get("since") else None
    except ValueError:
        return req._send(400, {"error": "since"})
    return req._send(200, update_info(since, admin=req.admin))


def get_automations(req):
    # snapshot `tp tick` writes every minute (launchd)
    con = db()
    try:
        snap = meta_get(con, "automations")
    finally:
        con.close()
    return req._send(200, snap or {"error": "tp tick가 아직 실행되지 않았어요 (launchd/install.sh)"})


def get_automation_runs(req):
    con = db()
    try:
        return req._send(200, {"runs": list(reversed(meta_get(con, "automation_runs") or []))})
    finally:
        con.close()


def get_system_automations(req):
    # 시스템 자동화 page: launchd / cron / Codex app automations on this Mac, collected on request
    return req._send(200, SJ.collect())


def get_system_automation_log(req):
    q = req.query()
    try:
        got = SJ.read_log((q.get("id") or [""])[0], int((q.get("i") or ["0"])[0]))
    except ValueError:
        got = None
    return req._send(404, {"error": "not found"}) if got is None else req._send(200, got)


def post_mode_preview(req, body):
    return req._send(200, mode_preview(body.get("to"), body.get("admins")))


def post_mode(req, body):
    since = start_mode(body.get("to"), body.get("admins"))
    con = db()
    event(con, "config", f"실행 방식 전환 시작: tp mode {body.get('to')} --apply")
    con.commit()
    return req._send(200, {"ok": True, "since": since})


def post_update(req, _body):
    if docker():
        if DU.directory():
            con = db()
            try:
                tag = (meta_get(con, "update_check") or {}).get("latest")
                job = DU.request(tag)
                event(con, "update", f"Docker 업데이트 요청: {job['tag']}")
                con.commit()
            except UP.UpdateError as e:
                return req._send(409, {"error": str(e)})
            finally:
                con.close()
            return req._send(200, {"ok": True, "job": job})
    since = start_update()
    con = db()
    event(con, "update", "업데이트 실행 (tp update)")
    con.commit()
    return req._send(200, {"ok": True, "since": since})


def post_update_check(req, _body):
    try:
        return req._send(200, check_update(admin=req.admin))
    except UP.UpdateError as e:
        return req._send(502, {"error": str(e)})


def post_dev_check(req, body):
    return req._send(200, {"ok": True, **DS.check(body["gid"])})


def post_dev_start(req, body):
    try:
        return req._send(200, {"ok": True, "server": DS.start(body["gid"], body.get("port") or None,
                                                              kill=bool(body.get("kill")))})
    except DS.PortBusy as e:
        return req._send(409, {"error": str(e), "conflict": e.info})


def post_dev_stop(req, body):
    DS.stop(body["gid"])
    return req._send(200, {"ok": True})


def post_metric_marker(req, body):
    with closing(db()) as con:
        return req._send(201, MET.create_marker(con, body.get("label")))


def post_view(req, body):
    # tab visits for pruning unused pages (TP-136); shortcut: delete with the cleanup ticket around 2026-11-26
    tab = body.get("tab")
    if not isinstance(tab, str) or not tab or len(tab) > 100:
        return req._send(400, {"error": "tab"})
    con = db()
    event(con, "view", tab)
    con.commit()
    return req._send(200, {"ok": True})
