"""TP-56: read-only, bounded Codex App Server adapter (ported from 330_ticket_pilot). Never log protocol payloads.

`codex app-server` only ever receives initialize / initialized / account/rateLimits/read. The last successful read is
stored in meta.codex_usage by usage.cached, so the sidebar keeps drawing it across failures and restarts."""
from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import time

from . import usage as U


class UsageError(Exception):
    def __init__(self, code):
        self.code = code


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def normalize(result):
    buckets = []
    default = result.get('rateLimits')
    multiple = result.get('rateLimitsByLimitId')
    candidates = []
    if isinstance(default, dict):
        candidates.append(('codex', default))
    if isinstance(multiple, dict):
        candidates.extend(sorted(multiple.items(), key=lambda item: item[0] != 'codex'))
    seen = set()
    for key, bucket in candidates:
        if not isinstance(bucket, dict):
            continue
        ident = bucket.get('limitId') or key
        if not isinstance(ident, str) or ident in seen:
            continue
        windows = []
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = number(window.get('usedPercent'))
            duration = number(window.get('windowDurationMins'))
            reset = number(window.get('resetsAt'))
            windows.append({'window': name, 'windowDurationMins': duration if duration and duration > 0 else None,
                            'remainingPercent': max(0, min(100, 100-used)) if used is not None else None,
                            'resetAt': reset if reset is not None and 0 < reset < 253402300800 else None})
        if windows:
            seen.add(ident)
            label = bucket.get('limitName')
            buckets.append({'id': ident[:120], 'name': label[:120] if isinstance(label, str) and label else ident[:120],
                            'windows': windows})
        if len(buckets) >= 12:
            break
    return buckets


def collect(timeout=10, command=None):
    """Inherit agent HOME/CODEX_HOME; only initialize and rateLimits/read."""
    try:
        process = subprocess.Popen(command or ['codex', 'app-server'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        raise UsageError('cli_missing') from None
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b''
    total = 0
    expected_id = 1
    auth_mode = None

    def send(message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'ticket_pilot_usage', 'version': '1'}}})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise UsageError('timeout')
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise UsageError('unsupported')
            total += len(chunk)
            if total > 1024 * 1024:
                raise UsageError('invalid_response')
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    message = json.loads(line)
                except ValueError:  # a banner or notice on stdout is not the protocol's fault
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get('method') == 'account/updated':
                    params = message.get('params')
                    if isinstance(params, dict) and 'authMode' in params:
                        mode = params['authMode']
                        if mode is None:
                            raise UsageError('login_required')
                        if mode in ('apikey', 'bedrockApiKey'):
                            raise UsageError('unsupported_auth')
                        if auth_mode is not None and mode != auth_mode:
                            raise UsageError('account_changed')
                        # Current CLIs also announce existing auth on startup.
                        auth_mode = mode
                    continue
                if type(message.get('id')) is not int or message['id'] != expected_id:
                    continue
                if 'error' in message:
                    error = message['error']
                    text = str(error.get('message', '')).lower() if isinstance(error, dict) else ''
                    code = 'unsupported'
                    if ('api key' in text or 'api-key' in text or 'apikey' in text
                            or 'chatgpt authentication required to read rate limits' in text):
                        code = 'unsupported_auth'
                    elif any(word in text for word in ('authentication required', 'not logged', 'unauthorized', '401', 'login required')):
                        code = 'login_required'
                    elif isinstance(error, dict) and error.get('code') != -32601:
                        code = 'unavailable'
                    raise UsageError(code)
                if message['id'] == 1:
                    send({'method': 'initialized'})
                    send({'id': 2, 'method': 'account/rateLimits/read'})
                    expected_id = 2
                else:
                    result = message.get('result')
                    if not isinstance(result, dict):
                        raise UsageError('invalid_response')
                    return normalize(result), result.get('accountId')
    except (ValueError, OSError, TypeError):
        raise UsageError('invalid_response') from None
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdout.close()


def fetch() -> dict:
    """-> {buckets} or {error}. The account id collect() returns is dropped here: it never leaves the process."""
    try:
        buckets, _account = collect()
    except UsageError as e:
        return {"error": e.code}
    except Exception:  # a background daemon thread must not die on an unexpected CLI/runtime failure
        return {"error": "unavailable"}
    # no readable window (renamed fields, a plan without limits): an error, so merge() keeps the last good buckets
    return {"buckets": buckets} if buckets else {"error": "invalid_response"}


def cached(con) -> dict | None:
    return U.cached(con, "codex_usage", fetch)
