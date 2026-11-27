"""Tick scheduling and dashboard state transitions."""
from __future__ import annotations

from datetime import datetime, timedelta

# The dashboard maps these names to roles (web/src/lib.ts ROLES).
# Each entry contains the ID, display name, prompt filename and interval in minutes.
AUTOMATIONS = (
    ("dispatch", "ticket-pilot: Asana dispatch", "automation_dispatch.md", 5),
    ("review", "ticket-pilot: PR review", "automation_review.md", 5),
    ("diff", "ticket-pilot: staging diff", "automation_diff.md", 10),
    ("cleanup", "ticket-pilot: worktree cleanup", "automation_cleanup.md", 5),
)
AI_STEP_MAX_MIN = 60
RUN_HISTORY = 40


def _due(aid, last, every, at, only, force):
    if only:
        return aid in only
    return force or not last or (at - datetime.fromisoformat(last)).total_seconds() >= every * 60 - 30


def advance(snapshot, history, *, only, force, clock, runner, precheck, launch, on_timeout):
    """Advance each automation in order and return its dashboard snapshot and run history.

    The caller supplies the UTC clock and the effects. `precheck(aid)` returns an exit code,
    `launch(aid, prompt)` returns a handle, and `on_timeout(aid)` records a stopped AI step.
    """
    previous = {row["id"]: row for row in snapshot.get("automations", [])}
    usage = {row["automationId"]: row.get("usageSummary") for row in snapshot.get("items", [])}
    history = [row.copy() for row in history]
    automations, items = [], []
    tick_at = clock()
    for aid, name, prompt, every in AUTOMATIONS:
        schedule = {**(previous.get(aid) or {"id": aid}), "name": name, "rrule": f"{every}분마다", "enabled": True}
        summary = (usage.get(aid) or {"knownRuns": 0}).copy()
        automations.append(schedule)
        items.append({"automationId": aid, "usageSummary": summary})

        if handle := summary.get("handle"):
            alive, code = runner.agent_state(handle)
            started = runner.started_at(handle)
            if alive and started and clock().timestamp() - started > AI_STEP_MAX_MIN * 60:
                runner.stop(handle)
                alive, code = False, None
                on_timeout(aid)
            if alive:
                continue
            status = "completed" if code == 0 else f"failed ({code})"
            summary["lastRunStatus"] = status
            for row in history:
                if row.get("handle") == handle:
                    row["status"] = status
            summary.pop("handle")

        if not _due(aid, schedule.get("lastRunAt"), every, tick_at, only, force):
            continue
        schedule["lastRunAt"] = clock().isoformat(timespec="seconds")
        schedule["nextRunAt"] = (tick_at + timedelta(minutes=every)).isoformat(timespec="seconds")
        code = precheck(aid)
        if code == 0:
            handle = launch(aid, prompt)
            summary["handle"] = handle
            summary["lastRunStatus"] = "running"
            summary["knownRuns"] = summary.get("knownRuns", 0) + 1
            history.append({"automationId": aid, "name": name, "startedAt": clock().isoformat(timespec="seconds"),
                            "status": "running", "handle": handle})
        elif code == 1:
            summary["lastRunStatus"] = "skipped_precheck"
        else:
            summary["lastRunStatus"] = f"precheck failed ({code})"

    return {"automations": automations, "items": items, "captured_at": clock().isoformat(timespec="seconds")}, history[-RUN_HISTORY:]
