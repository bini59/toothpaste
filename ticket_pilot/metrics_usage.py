"""Collect numeric metrics from completed ticket and review executions, once per runner log."""
from __future__ import annotations

import bisect
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import chat_usage, runner
from .core import meta_get, meta_set

LOG_NAME = re.compile(r"(.+)-\d{8}-\d{6}-\d{6}\.log\.json$")
HELP = re.compile(r"\btp\s+(?:guide\b|screenshot\s+(?:[^\n;&|]*?\s+)?--help\b)")
SHELL_START = r"(?:^|[;&|\n]|\bdo\s+)\s*(?:(?:\w+=\S+|exec|nohup|npx)\s+)*"
DEV = re.compile(SHELL_START + r"(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start)\b|"
                 r"(?:\S*/)?vite\b(?!\s+(?:build|preview|optimize)\b)"
                 r"(?=\s|$|[;&|])|"
                 r"(?:next|nuxt|astro)\s+dev\b|python(?:3)?\s+(?:-m\s+http\.server|manage\.py\s+runserver)\b|"
                 r"(?:uvicorn|gunicorn)\s+\w|flask\s+run\b|docker\s+compose\s+(?:up|restart)\b)")
WAIT = re.compile(SHELL_START + r"(?:sleep\s+(?:\d|\$)|for\s+\w+\s+in\s+\$\(\s*seq\b|until\s+)")
SHELL_TOOLS = {"bash", "exec_command", "run_command", "shell", "terminal"}
CODEX_TOOL_ITEMS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call",
                    "image_generation_call", "mcp_tool_call"}
SCRIPT_CMD = re.compile(r"""["']?cmd["']?\s*:\s*(?:String\.raw\s*)?(?P<q>["'`])(?P<body>(?:\\.|(?!(?P=q))[\s\S])*)(?P=q)""")


def timestamp(value) -> float | None:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
    except (AttributeError, ValueError, OverflowError, OSError):
        return None


def _meta(log: Path) -> dict:
    try:
        return chat_usage._object(json.loads(Path(f"{log}.json").read_text()))
    except (OSError, ValueError):
        return {}


def _start(meta):
    start = meta.get("started")
    return start if type(start) in (float, int) and 0 < start <= time.time() else None


def register(con, handle, kind, ticket_gid=None, *, track_ws_set=False):
    if log := runner.log_path(handle):
        con.execute("""INSERT OR IGNORE INTO metric_agent_runs(log,handle,kind,ticket_gid,started_at,has_ws_set)
                       VALUES(?,?,?,?,?,?)""",
                    (str(log), handle, kind, ticket_gid, _start(_meta(log)), 0 if track_ws_set else None))


def workspace_signal(con, handle, *, exit_event=False):
    if log := runner.log_path(handle):
        sql = "exit_event=last_insert_rowid()" if exit_event else "has_ws_set=1"
        con.execute(f"UPDATE metric_agent_runs SET {sql} WHERE log=? AND has_ws_set IS NOT NULL", (str(log),))


def start_review(con, pr_url: str):
    """The review automation can cover several tickets in one process."""
    log = os.environ.get("TP_REVIEW_LOG")
    if os.environ.get("TP_AGENT") != "auto-review" or not log:
        return
    row = con.execute("SELECT ticket_gid FROM prs WHERE url=?", (pr_url,)).fetchone()
    if row and row["ticket_gid"]:
        con.execute("INSERT INTO metric_review_starts(log,at,ticket_gid) VALUES(?,?,?)",
                    (log, time.time(), row["ticket_gid"]))
        con.commit()


def _discover_history(con):
    """Import retained runner logs, including relaunches whose handles were overwritten in runs."""
    if meta_get(con, "metrics_history_discovered"):
        return
    from .pipeline import slug

    worktrees = defaultdict(set)
    handles = []
    for row in con.execute("""SELECT r.*, t.key FROM runs r LEFT JOIN tickets t ON t.gid=r.ticket_gid"""):
        if runner.is_handle(row["terminal"]):
            handles.append((row["terminal"], "work", row["ticket_gid"]))
        if row["worktree_path"] and row["ticket_gid"]:
            tag = slug(row["key"] or "")
            for name in (row["worktree_name"], tag, *(f"{tag}-{kind}" for kind in ("rework", "conflict", "unblock"))):
                if name:
                    worktrees[os.path.realpath(row["worktree_path"]), name].add(row["ticket_gid"])
    for item in (meta_get(con, "automations") or {}).get("items", []):
        if item.get("automationId") == "review":
            handles.append(((item.get("usageSummary") or {}).get("handle"), "review", None))
    for item in meta_get(con, "automation_runs", []):
        if item.get("automationId") == "review":
            handles.append((item.get("handle"), "review", None))

    entries = []
    for path in runner.LOGS.glob("*.log.json"):
        match = LOG_NAME.fullmatch(path.name)
        if not match:
            continue
        log = path.with_suffix("")
        tag = match[1]
        meta = _meta(log)
        if tag == "auto-review":
            entries.append((str(log), "review", None, _start(meta)))
        elif worktrees:
            cwd = meta.get("cwd")
            gids = worktrees.get((os.path.realpath(cwd), tag)) if isinstance(cwd, str) and "\0" not in cwd else None
            if gids and len(gids) == 1:
                entries.append((str(log), "work", next(iter(gids)), _start(meta)))
    with con:
        for handle, kind, gid in handles:
            register(con, handle, kind, gid)
        con.executemany("INSERT OR IGNORE INTO metric_agent_runs(log,kind,ticket_gid,started_at) VALUES(?,?,?,?)", entries)
        meta_set(con, "metrics_history_discovered", True)


def _review_windows(con, log, start, end):
    """Return boundaries and owners. Charge shared setup/teardown to the first/last review."""
    starts = [tuple(r) for r in con.execute("""SELECT at,ticket_gid FROM metric_review_starts
                                               WHERE log=? AND at BETWEEN ? AND ? ORDER BY at""", (str(log), start, end))]
    saves = con.execute("SELECT created_at,ticket_gid FROM reviews WHERE agent_log=? ORDER BY id", (str(log),)).fetchall()
    if not (starts or saves):
        # Older automations saved reviews in order. The saved second (inclusive) ends that PR's share.
        saves = con.execute("""SELECT created_at,ticket_gid FROM reviews
                               WHERE agent_log IS NULL AND source IS NULL AND NOT COALESCE(manual,0)
                                 AND ticket_gid IS NOT NULL AND CAST(strftime('%s',created_at) AS INTEGER) BETWEEN ? AND ?
                               ORDER BY strftime('%s',created_at),id""", (int(start), int(end))).fetchall()
    previous_save = start
    for save in saves:
        at = timestamp(save["created_at"])
        gid = save["ticket_gid"]
        if at is None or not gid:
            continue
        end_of_review = min(at + 1, end)
        if not any(previous_save <= boundary < end_of_review and owner == gid for boundary, owner in starts):
            starts.append((min(previous_save, end_of_review), gid))
        previous_save = end_of_review
    if not starts:
        return [], []
    starts.sort(key=lambda row: row[0])
    starts[0] = (start, starts[0][1])
    boundaries, owners = zip(*starts)
    return boundaries, owners


def _codex_log(meta, session):
    if not isinstance(session, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
        return None
    home = Path(meta.get("transcript_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    home = Path(meta["cwd"]) / home
    start = datetime.fromtimestamp(meta["started"])
    pattern = f"rollout-*-{session}.jsonl"
    for offset in (0, -1, 1):
        day = start + timedelta(days=offset)
        folder = home / "sessions" / day.strftime("%Y/%m/%d")
        if path := next(folder.glob(pattern), None):
            return path
    return next((home / "archived_sessions").glob(pattern), None)


def _read_codex(path, start, end, on_call, on_tool):
    """Codex's cumulative input includes cached input. Take deltas to ignore repeated snapshots."""
    previous = (0, 0)
    readable = False
    response_at = None
    try:
        with path.open("rb") as f:
            for line in f:
                row = chat_usage._json(line)
                at = timestamp(row.get("timestamp"))
                if at is None or at > end:
                    continue
                payload = chat_usage._object(row.get("payload"))
                kind = payload.get("type")
                if row.get("type") == "session_meta":
                    readable = True
                elif row.get("type") == "event_msg" and kind == "token_count":
                    info = chat_usage._object(payload.get("info"))
                    usage = chat_usage._object(info.get("total_token_usage"))
                    counts = tuple(usage.get(k) for k in ("input_tokens", "output_tokens"))
                    if not usage or any(type(n) is not int or n < 0 for n in counts):
                        continue
                    readable = True
                    delta = tuple(max(0, n - old) for n, old in zip(counts, previous))
                    previous = tuple(map(max, zip(counts, previous)))
                    if any(delta):
                        # The snapshot often follows tool execution. Attribute the request at its response,
                        # before a review-start tool call can advance the ticket boundary.
                        used_at = at if response_at is None else response_at
                        if used_at >= start:
                            on_call(used_at, delta)
                        response_at = None
                elif row.get("type") == "response_item" and isinstance(kind, str):
                    is_tool = kind in CODEX_TOOL_ITEMS
                    if response_at is None and (is_tool or kind == "reasoning" or payload.get("role") == "assistant"):
                        response_at = at
                    if is_tool and at >= start:
                        readable = True
                        name = payload.get("name")
                        args = payload.get("arguments", payload.get("input"))
                        if isinstance(args, str) and isinstance(name, str) and name.lower().rsplit(".", 1)[-1] in SHELL_TOOLS:
                            args = chat_usage._json(args)
                        on_tool(payload.get("call_id") or payload.get("id"), name, args)
    except OSError:
        return False
    return readable


def measure(log: Path, meta: dict, start: float, end: float, boundaries=(), owners=()):
    """Read the session's transcripts. Return tokens, numeric tool counts and review token shares."""
    seen = set()
    patterns = [0, 0, 0]
    tokens = None
    shares = dict.fromkeys(owners)

    def on_call(at, counts):
        nonlocal tokens
        used = sum(counts)
        tokens = (tokens or 0) + used
        if owners:
            owner = owners[max(0, bisect.bisect_right(boundaries, at) - 1)]
            shares[owner] = (shares[owner] or 0) + used

    def on_tool(ident, name, args):
        if isinstance(ident, str):
            if ident in seen:
                return
            seen.add(ident)
        else:
            return
        name = name.lower().rsplit(".", 1)[-1] if isinstance(name, str) else ""
        if name == "exec" and isinstance(args, str):  # Codex's script tool can invoke several shell calls
            commands = [m["body"].replace(r"\n", "\n") for m in SCRIPT_CMD.finditer(args)]
        elif name in SHELL_TOOLS and isinstance(args, dict):
            command = args.get("command", args.get("cmd"))
            if isinstance(command, list):
                command = " ".join(x for x in command if isinstance(x, str))
            commands = [command] if isinstance(command, str) else []
        else:
            commands = []
        for i, pattern in enumerate((HELP, DEV, WAIT)):
            patterns[i] += any(pattern.search(command) for command in commands)

    tool, session = runner.log_session(log, meta)
    tool = tool if isinstance(tool, str) else None
    readable = False
    if tool == "claude" and isinstance(session, str) and isinstance(meta.get("cwd"), str):
        usage = chat_usage.transcript(meta["cwd"], session, start, end, on_call=on_call, on_tool=on_tool,
                                      config_dir=meta.get("transcript_home"), include_subagents=True)
        readable = usage[0] is not None or bool(seen)
    elif (tool == "codex" and isinstance(meta.get("cwd"), str) and session
          and (path := _codex_log(meta, session))):
        readable = _read_codex(path, start, end, on_call, on_tool)
    return tool, tokens, len(seen) if readable else None, patterns, shares


def _end(row, log, start):
    handle = row["handle"]
    if handle and runner.running(handle):
        return None
    try:
        return max(start, Path(f"{log}.exit").stat().st_mtime)
    except OSError:
        # Killed or historical overwritten handles may lack an exit file.
        try:
            return max(start, log.stat().st_mtime)
        except OSError:
            return start


def collect(con, batch=40, budget=8):
    """Called under the tick lock. Commit one execution at a time without a DB lock during transcript I/O."""
    _discover_history(con)
    deadline = time.monotonic() + budget
    done = 0
    for row in con.execute("SELECT * FROM metric_agent_runs WHERE finished_at IS NULL ORDER BY started_at DESC").fetchall():
        log = Path(row["log"])
        meta = _meta(log)
        start = _start(meta)
        if start is None:
            if row["handle"] and runner.running(row["handle"]):
                continue
            # Retain a coverage entry for the missing execution. It has no usable weekly timestamp.
            con.execute("UPDATE metric_agent_runs SET finished_at=0 WHERE log=?", (str(log),))
            con.commit()
        elif (end := _end(row, log, start)) is not None:
            boundaries, owners = _review_windows(con, log, start, end) if row["kind"] == "review" else ((), ())
            try:
                tool, tokens, calls, patterns, shares = measure(log, meta, start, end, boundaries, owners)
            except (OSError, ValueError, TypeError, OverflowError):
                # An unreadable retained log still counts toward coverage and must not hold up newer runs.
                tool, tokens, calls, patterns, shares = None, None, None, (0, 0, 0), dict.fromkeys(owners)
            with con:
                con.execute("""UPDATE metric_agent_runs SET started_at=?,finished_at=?,tool=?,tokens=?,tool_calls=?,
                               help_calls=?,dev_calls=?,wait_calls=? WHERE log=?""",
                            (start, end, tool, tokens, calls, *patterns, str(log)))
                for gid, used in shares.items():
                    con.execute("INSERT OR REPLACE INTO metric_review_tokens VALUES(?,?,?,?)",
                                (str(log), gid, boundaries[owners.index(gid)], used))
        else:
            continue
        done += 1
        if done >= batch or time.monotonic() >= deadline:
            break
    return done
