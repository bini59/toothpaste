"""SQLite connection ownership and initialization order."""

import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from .schema import SCHEMA

# Only the main thread reuses a connection. Each request thread owns and closes each connection it opens.
# Key by PID so a forked child opens its own connection without using or explicitly closing the inherited
# handle. Fork callers must leave inherited handles alone and use open_db() again (or exec a new process).
_main_connections: dict[int, sqlite3.Connection] = {}


def open_db(path: Path, migrate_schema: Callable[[sqlite3.Connection], None],
            migrate_settings: Callable[[sqlite3.Connection], None]) -> sqlite3.Connection:
    pid = os.getpid()
    main = threading.current_thread() is threading.main_thread()
    if main and pid in _main_connections:
        return _main_connections[pid]

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        # Writers must be on the same kernel (TP-8). DELETE keeps tp.db a single file for cp/backup/VACUUM INTO.
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(SCHEMA)
        migrate_schema(con)
        migrate_settings(con)
    except BaseException:
        con.close()
        raise

    if main:
        _main_connections[pid] = con
    return con


def close_db(con: sqlite3.Connection) -> None:
    """Close a connection and discard it if it is the process's cached connection."""
    con.close()
    pid = os.getpid()
    if _main_connections.get(pid) is con:
        del _main_connections[pid]
