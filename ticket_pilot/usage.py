"""TP-32/TP-56: Claude (Max) usage for the dashboard sidebar, cached in meta.claude_usage.

Reads Claude Code's OAuth token read-only and never refreshes it: a refresh rotates the refresh token and can log
Claude Code out. An expired token just leaves the last good values in place (the UI keeps drawing them).

`merge`/`cached` are the generic last-value-wins meta cache; codex_usage.py reuses them for meta.codex_usage."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .core import db, meta_get, meta_set, now

URL = "https://api.anthropic.com/api/oauth/usage"
SERVICES = ("Claude Code-credentials-347d3249", "Claude Code-credentials", "Orca Claude Code Managed Credentials")
FILE = Path.home() / ".claude" / ".credentials.json"
TTL = 300  # seconds between checks
_locks: dict[str, threading.Lock] = {}


def _creds() -> list[dict]:
    raw = []
    user = os.environ.get("USER")
    for svc in SERVICES:
        # TP-56: several accounts can share one service name (a stale `root` entry next to the live one) and
        # `-s <svc>` alone only ever returns the first. Read the current user's entry too; fetch() picks the best.
        for acct in ([user] if user else []) + [None]:
            try:  # a keychain access prompt would block a background process; the timeout turns it into "no token"
                p = subprocess.run(["security", "find-generic-password", "-s", svc, *(("-a", acct) if acct else ()), "-w"],
                                   capture_output=True, text=True, timeout=5)
                if p.returncode == 0:
                    raw.append(p.stdout)
            except (subprocess.TimeoutExpired, OSError):  # no `security` binary etc: just "no token"
                pass
    try:
        raw.append(FILE.read_text())
    except OSError:
        pass
    out = []
    for r in raw:
        try:
            c = json.loads(r)["claudeAiOauth"]
            if c.get("accessToken"):
                out.append(c)
        except (ValueError, KeyError, TypeError):
            pass
    return out


def parse(body) -> dict:
    """Keep five_hour / seven_day {utilization, resets_at}; ignore unknown fields. ValueError if neither is usable."""
    out = {}
    for k in ("five_hour", "seven_day"):
        w = body.get(k) if isinstance(body, dict) else None
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            out[k] = {"utilization": float(w["utilization"]), "resets_at": w.get("resets_at")}
    if not out:
        raise ValueError("no usage windows")
    return out


def fetch(creds: list[dict] | None = None, opener=urllib.request.urlopen) -> dict:
    """-> {five_hour, seven_day} or {error}. The token stays in this function (never returned or logged).

    TP-56: several keychain items hold a Claude Code token. Longest-lived first, and a rejected one falls through
    to the next so a revoked token with a far expiry cannot shadow the live entry."""
    creds = _creds() if creds is None else creds
    if not creds:
        return {"error": "no_token"}
    live = [c for c in creds if (c.get("expiresAt") or 0) / 1000 >= time.time()]
    if not live:
        return {"error": "token_expired"}
    res = {"error": "unauthorized"}
    for c in sorted(live, key=lambda c: c.get("expiresAt") or 0, reverse=True):
        req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {c['accessToken']}",
                                                   "anthropic-beta": "oauth-2025-04-20"})
        try:
            with opener(req, timeout=5) as r:
                return parse(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                return {"error": f"http_{e.code}"}
            res = {"error": "unauthorized"}  # try the next credential
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"error": "network"}
        except ValueError:
            return {"error": "bad_response"}
    return res


def merge(prev: dict | None, res: dict) -> dict:
    """New values on success; on failure, and for a window the response left out, keep the last good value."""
    if "error" in res:
        return {**(prev or {}), "error": res["error"], "checked_at": now()}
    return {**(prev or {}), **res, "fetched_at": now(), "checked_at": now(), "error": None}


def _refresh(key, fn):
    try:
        res = fn()
        con = db()
        try:
            meta_set(con, key, merge(meta_get(con, key), res))
            con.commit()
        finally:
            con.close()
    finally:
        _locks[key].release()


def cached(con, key: str = "claude_usage", fn=fetch) -> dict | None:
    """Stored value; starts a background refresh when it is older than TTL so /api/state never waits on it."""
    v = meta_get(con, key)
    if os.environ.get("TP_ASANA_FIXTURE"):  # offline mode: no keychain, no network, no subprocess
        return v
    t = (v or {}).get("checked_at")
    stale = not t or (datetime.now(timezone.utc) - datetime.fromisoformat(t)).total_seconds() > TTL
    if stale and _locks.setdefault(key, threading.Lock()).acquire(blocking=False):
        threading.Thread(target=_refresh, args=(key, fn), daemon=True).start()
    return v
