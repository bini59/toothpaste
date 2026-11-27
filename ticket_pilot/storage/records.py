"""Shared event log and metadata operations on an open connection."""

import json
import threading
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ACTOR = threading.local()


def set_actor(name: str | None):
    """The dashboard sets this per request thread (server.H._gate); event() records it."""
    _ACTOR.name = name


def current_actor() -> str | None:
    return getattr(_ACTOR, "name", None)


def event(con, kind: str, message: str, ticket_gid: str | None = None):
    con.execute("INSERT INTO events(ts,kind,ticket_gid,message,actor) VALUES(?,?,?,?,?)",
                (now(), kind, ticket_gid, message, current_actor()))


def meta_set(con, k: str, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def meta_get(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default
