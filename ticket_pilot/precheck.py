"""Deterministic work that runs before each automation's AI step."""
from __future__ import annotations

import traceback

from . import checks, cleanup, deploy, diffs, milestone, pipeline, release, ticket_sync, track
from .dispatch import dispatch as dispatch_tickets
from .core import db, event


def _record_error(name, error):
    con = db()
    con.rollback()
    event(con, "error", f"{name}: {error}")
    con.commit()


def _release_steps():
    for name, step in (("milestones", milestone.tick), ("approvals", release.process_approvals),
                       ("deploys", deploy.deploy_tick), ("release", release.release_tick)):
        try:
            step()
        except Exception as error:
            traceback.print_exc()
            try:
                _record_error(name, error)
            except Exception:
                pass  # Even a failed error write must leave the remaining release steps runnable.


def dispatch() -> int:
    """Sync and dispatch deterministically. Return the number of tickets needing AI triage."""
    ticket_sync.sync()
    _release_steps()
    dispatch_tickets()
    return len(pipeline.triage_list(mark=False))


def review() -> int:
    """Scan PRs and run checks before counting review candidates."""
    pipeline.scan()
    _release_steps()
    for name, step in (("track", track.track_deploys), ("check", checks.tick)):
        try:
            step()
        except Exception as error:
            traceback.print_exc()
            _record_error(name, error)
    return len(pipeline.review_queue())


def diff() -> bool:
    """Return whether a staging head needs an AI note."""
    return diffs.precheck()


def cleanup_worktrees() -> int:
    """Remove eligible worktrees and count the ones that still need AI comparison."""
    cleanup.cleanup_scan()
    return len(cleanup.cleanup_queue())
