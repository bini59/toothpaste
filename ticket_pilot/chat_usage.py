"""Claude token accounting for project chat turns and detached /compact runs."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
Usage = tuple[int, int, int, int]


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _json(line) -> dict:
    try:
        return _object(json.loads(line))
    except ValueError:
        return {}


def _counts(usage: dict) -> Usage:
    values = [usage.get(key) for key in FIELDS]
    return tuple(n if type(n) is int and n >= 0 else 0 for n in values)


def _total(calls) -> tuple:
    calls = list(calls)
    if not calls:
        return (None,) * 5
    return (*map(sum, zip(*calls)), sum(calls[-1][:3]))


def transcript(cwd: str, session: str, start: float, end: float | None) -> tuple:
    """Sum each API message once. Claude can write several blocks with the same message id."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", session):
        return _total(())
    cwd_path = Path(cwd).resolve()
    projects = cwd_path / Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(cwd_path))
    path = projects / folder / f"{session}.jsonl"
    calls = {}
    try:
        if not path.is_file():
            # Claude can shorten the folder name for long checkout paths.
            paths = list(projects.glob(f"*/{session}.jsonl"))
            if len(paths) != 1:
                return _total(())
            path = paths[0]
        with path.open("rb") as f:
            for line in f:
                row = _json(line)
                if row.get("type") != "assistant" or row.get("isSidechain"):
                    continue
                try:
                    ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
                except (KeyError, AttributeError, ValueError, OverflowError):
                    continue
                if ts < start or (end is not None and ts > end):
                    continue
                message = _object(row.get("message"))
                counts = _counts(_object(message.get("usage")))
                ident = message.get("id") or row.get("requestId") or row.get("uuid")
                if isinstance(ident, str) and any(counts):
                    calls[ident] = tuple(map(max, zip(calls.get(ident, counts), counts)))
    except OSError:
        pass
    return _total(calls.values())


def compaction(log: Path) -> tuple:
    """The local collector records per-request usage. Older CLIs can report it in the result."""
    try:
        with Path(f"{log}.usage").open("rb") as f:
            calls = [row for line in f if any(_counts(row := _json(line)))]
        if calls:
            if all(type(row.get("sequence")) is int for row in calls):
                # The exporter can retry batches or deliver them out of order.
                calls = sorted({row["sequence"]: row for row in calls}.values(), key=lambda row: row["sequence"])
            return _total(map(_counts, calls))
    except OSError:
        pass
    try:
        result = _json(log.read_bytes())
    except OSError:
        return _total(())
    counts = _counts(_object(result.get("usage")))
    # modelUsage includes restored session spend. Result usage alone has no per-call context.
    return (*counts, None) if result.get("type") == "result" and any(counts) else _total(())


OTEL_FIELDS = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")


def _api_usage(payload: dict):
    for resource in payload.get("resourceLogs", []):
        for scope in resource.get("scopeLogs", []):
            for record in scope.get("logRecords", []):
                if record.get("body", {}).get("stringValue") != "claude_code.api_request":
                    continue
                attrs = {a["key"]: a["value"] for a in record.get("attributes", [])}
                counts = {}
                for field, key in zip(FIELDS, OTEL_FIELDS):
                    value = attrs.get(key, {})
                    counts[field] = int(value.get("intValue", value.get("stringValue", 0)))
                if any(counts.values()):
                    sequence = attrs.get("event.sequence", {}).get("intValue")
                    if sequence is not None:
                        counts["sequence"] = int(sequence)
                    yield counts


def run_compaction(log: Path, cmd: list[str]) -> int:
    """Collect usage on loopback for this subprocess, keeping only numeric token counts on disk."""
    route = f"/{uuid.uuid4().hex}/v1/logs"
    usage_path = Path(f"{log}.usage")

    class Collector(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != route:
                return self.send_error(404)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 2_000_000:
                    return self.send_error(413)
                self.connection.settimeout(2)
                rows = list(_api_usage(_json(self.rfile.read(size))))
                if rows:
                    with usage_path.open("a", encoding="utf-8") as f:
                        f.write("".join(json.dumps(row) + "\n" for row in rows))
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                return self.send_error(400)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    except OSError:
        return subprocess.call(cmd)
    with server:
        env = {**os.environ, "CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_LOGS_EXPORTER": "otlp",
               "OTEL_METRICS_EXPORTER": "none", "OTEL_TRACES_EXPORTER": "none",
               "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json", "OTEL_EXPORTER_OTLP_LOGS_COMPRESSION": "none",
               "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"http://127.0.0.1:{server.server_port}{route}",
               "OTEL_LOG_USER_PROMPTS": "0", "OTEL_LOG_ASSISTANT_RESPONSES": "0",
               "OTEL_LOG_TOOL_DETAILS": "0", "OTEL_LOG_RAW_API_BODIES": "0"}
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        try:
            return subprocess.call(cmd, env=env)
        finally:
            server.shutdown()


if __name__ == "__main__":
    raise SystemExit(run_compaction(Path(sys.argv[1]), sys.argv[2:]))
