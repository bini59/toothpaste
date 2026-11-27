"""Process and storage adapters for host ticks and the Docker tick loop."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone

from . import automation, chat, metrics_usage, notion_stages, runner, update
from .core import DATA, ROOT, db, docker, event, meta_get, meta_set


def _cli_command(command):
    # launchd's /bin/sh can be denied access to scripts under ~/Desktop (TCC).
    return [sys.executable, "-m", "ticket_pilot.cli", command]


def _cli_env():
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def _precheck(con, aid):
    con.commit()
    result = subprocess.run(_cli_command(f"precheck-{aid}"), cwd=ROOT, env=_cli_env(),
                            capture_output=True, text=True, timeout=1800)
    print(f"{aid}: precheck exit {result.returncode} {result.stdout.strip()[-300:]}", flush=True)
    if result.returncode not in (0, 1):
        print(result.stderr[-2000:], file=sys.stderr, flush=True)
    return result.returncode


def _launch(con, aid, prompt):
    handle = runner.launch(str(ROOT), (ROOT / "prompts" / prompt).read_text(), f"auto-{aid}",
                           role=runner.AUTOMATION_ROLES[aid])
    if aid == "review":
        metrics_usage.register(con, handle, "review")
    return handle


def _run_automations(con, *, only, force):
    snapshot, history = automation.advance(
        meta_get(con, "automations") or {}, meta_get(con, "automation_runs") or [],
        only=only, force=force, clock=lambda: datetime.now(timezone.utc), runner=runner,
        precheck=lambda aid: _precheck(con, aid), launch=lambda aid, prompt: _launch(con, aid, prompt),
        on_timeout=lambda aid: event(con, "error", f"automation {aid}: AI step ran over {automation.AI_STEP_MAX_MIN} min, stopped"),
    )
    meta_set(con, "automations", snapshot)
    meta_set(con, "automation_runs", history)
    con.commit()


def run_tick(*, only=(), force=False):
    """Run one tick with the shared host and Docker maintenance lock held."""
    with (DATA / "tick.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("tick: another tick is running")
            return
        if docker():
            from .docker_update import maintenance
            if maintenance():
                return

        con = db()
        try:
            chat.compact_idle(con)
        except Exception as e:  # keep the regular tick running if chat compaction fails
            con.rollback()
            print(f"chat compaction: {e}", file=sys.stderr, flush=True)
        _run_automations(con, only=only, force=force)
        _after_automations(con)


def _after_automations(con):
    try:
        notion_stages.start_worker(con)
    except OSError as e:
        print(f"Notion 상태 반영 작업 시작 실패: {e}", file=sys.stderr, flush=True)
    try:
        # every update.check_min: fetch the tags, keep the latest release for the dashboard's 업데이트 item
        update.record_installed(con)
        chk = update.check_latest(con)
        con.commit()
        if not docker():
            from .server import start_update
            update.auto_update(con, chk, start_update)
        con.commit()
    except Exception as e:
        con.rollback()
        print(f"update check: {e}", file=sys.stderr, flush=True)
    try:
        metrics_usage.collect(con)
    except Exception as e:  # a missing or malformed transcript must not stop regular automation
        con.rollback()
        print(f"metrics: {e}", file=sys.stderr, flush=True)


def run_loop(stop: threading.Event, spawn, every: float = 60.0):
    """Start a tick every `every` seconds when the previous tick has finished. Wait for it on shutdown."""
    proc = None
    while not stop.is_set():
        if proc is None or proc.poll() is not None:
            proc = spawn()
        else:
            print("tick: the last tick is still running, skipped", flush=True)
        stop.wait(every)
    if proc is not None and proc.poll() is None:
        print("tp run: waiting for the running tick", flush=True)
        proc.wait()


def run():
    """Docker starts the dashboard and periodic ticks together."""
    from . import server
    srv = server.serve(block=False)  # refuses to start in Docker mode when nobody could log in
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run_loop(stop, lambda: subprocess.Popen(_cli_command("tick"), cwd=ROOT, env=_cli_env()))
    srv.shutdown()
    print("tp run: stopped", flush=True)
