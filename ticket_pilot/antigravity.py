"""Antigravity CLI account, model and quota probes, and the headless runner adapter."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SERVICE = "ticket-pilot-antigravity"
API_KEY = "api_key"
SETTINGS = ("--settings", '{"modelProvider":"gemini"}')
GOOGLE_SETTINGS = ("--settings", '{"modelProvider":""}')
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
CONVERSATION = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I)
_model_cache: tuple[bytes, float, frozenset[str]] | None = None


def credentials() -> tuple[str | None, bool]:
    """(key in use, whether tp must set the provider for this invocation)."""
    from .core import secret_get

    saved = secret_get(SERVICE, API_KEY)
    if saved:
        return saved, True
    try:
        settings = json.loads((Path.home() / ".gemini/antigravity-cli/settings.json").read_text())
    except (OSError, ValueError):
        settings = None
    uses_key = isinstance(settings, dict) and settings.get("modelProvider") == "gemini"
    return os.environ.get("GEMINI_API_KEY") if uses_key else None, False


def _api_models(key: str) -> frozenset[str]:
    """Validate the key and read the Gemini API catalog without running a billable prompt."""
    global _model_cache
    digest = hashlib.sha256(key.encode()).digest()
    cached = _model_cache
    if cached and cached[0] == digest and time.monotonic() - cached[1] < 60:
        return cached[2]
    names = set()
    query = {"pageSize": 1000}
    while True:
        req = urllib.request.Request(f"{MODELS_URL}?{urllib.parse.urlencode(query)}",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=8) as res:
            body = json.load(res)
        for model in body.get("models", []):
            if "generateContent" in model.get("supportedGenerationMethods", []):
                names.add(model["name"].removeprefix("models/"))
        token = body.get("nextPageToken")
        if not token or token == query.get("pageToken"):
            break
        query["pageToken"] = token
    catalog = frozenset(names)
    _model_cache = (digest, time.monotonic(), catalog)
    return catalog


def save_key(value) -> None:
    from .core import secret_set

    key = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{20,512}", key):
        raise RuntimeError("Google AI Studio의 Gemini API 키를 그대로 붙여넣으세요")
    try:
        _api_models(key)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gemini API가 이 키를 확인하지 못했어요 ({exc.code})") from None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise RuntimeError("Gemini API에 연결해 키를 확인하지 못했어요") from None
    secret_set(SERVICE, API_KEY, key)


def pinned_effort(slug) -> str | None:
    match = EFFORT_SUFFIX.search(slug) if isinstance(slug, str) else None
    return match[1] if match else None


def parse_models(output: str) -> list[dict]:
    models = {}
    for line in output.splitlines():
        match = re.fullmatch(r"([a-zA-Z0-9][\w.:-]*)[\t ]+(.+)", line.strip())
        if match and (match[1].count("-") >= 1 or "\t" in line):
            slug, name = match.groups()
            effort = pinned_effort(slug)
            models[slug] = {"id": slug, "name": name.strip(), "efforts": [effort] if effort else [],
                            "default_effort": effort}
    return list(models.values())


def account(exe: str) -> dict:
    key, saved = credentials()
    out = {"method": None, "saved": API_KEY if saved else None, "account": [],
           "models": [], "neon_reason": None}
    env = {**os.environ, "GEMINI_API_KEY": key} if key else None
    try:
        proc = subprocess.run([exe, *(SETTINGS if saved else ()), "models"], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        models = parse_models(proc.stdout) if proc.returncode == 0 else []
        if key and proc.returncode == 0:
            api_models = _api_models(key)
            models = [m for m in models if EFFORT_SUFFIX.sub("", m["id"]) in api_models or m["id"] in api_models]
            out.update(method=API_KEY, account=["Gemini API"])
        elif models:
            out.update(method="cli", account=["Google 계정"])
        elif not key and "please sign in" in (proc.stdout + proc.stderr).lower():
            out["neon_reason"] = "Google 계정에 로그인하거나 Gemini API 키를 연결하세요."
        else:
            out["neon_reason"] = "Antigravity 계정 또는 모델 목록을 확인하지 못했어요."
    except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.TimeoutExpired):
        out["neon_reason"] = "계정 또는 모델 목록을 확인하지 못했어요."
        return out
    out["models"] = models
    if key and out["method"] and not models:
        out["neon_reason"] = "이 Gemini API 키와 호환되는 Antigravity 모델이 없어요. 프로젝트의 모델 권한과 결제 설정을 확인하세요."
    elif out["method"] and not any(m["id"].startswith("gemini-4-neon") for m in models):
        out["neon_reason"] = ("Gemini 4 Neon은 이 키의 모델 목록에 없어요. Neon을 제공하는 유료 Gemini API 프로젝트의 키가 필요해요."
                              if key else "Gemini 4 Neon은 이 계정의 모델 목록에 없어요. Google AI Free·Plus는 미지원이며, "
                              "유료 Pro, 대상 기기의 Pro 체험, Ultra 또는 Enterprise 권한이 필요해요.")
    return out


def _list(value):
    return value if isinstance(value, list) else []


def _reset(value) -> str | None:
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.isoformat() if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


def parse_usage(envelope: dict) -> list[dict]:
    """Read the /usage command data from the CLI's JSON result envelope."""
    command = envelope.get("command") or {}
    if envelope.get("status") != "SUCCESS" or command.get("name") not in ("usage", "quota"):
        return []
    groups = []
    for group in _list((command.get("data") or {}).get("groups")):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        limits = []
        for bucket in _list(group.get("buckets")):
            if not isinstance(bucket, dict) or bucket.get("disabled") or not isinstance(bucket.get("name"), str):
                continue
            fraction = bucket.get("remaining_fraction")
            percent = (1 - fraction) * 100 if type(fraction) in (int, float) and 0 <= fraction <= 1 else None
            amount = bucket.get("remaining_amount")
            amount = amount if type(amount) is int and 0 <= amount <= 2**53 - 1 else None
            reset = _reset(bucket.get("reset_time"))
            if percent is not None or amount is not None or reset is not None:
                limits.append({"name": bucket["name"], "used": percent, "remaining": amount, "reset_at": reset})
        if limits:
            groups.append({"name": group["name"], "limits": limits})
    return groups


def usage(exe: str, method: str) -> dict:
    if method == API_KEY:
        return {"error": "unsupported_auth"}
    try:
        proc = subprocess.run([exe, "-p", "/usage", "--output-format", "json"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=12)
        if proc.returncode == 0:
            groups = parse_usage(json.loads(proc.stdout))
            if groups:
                return {"groups": groups}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except (ValueError, TypeError, AttributeError, OSError):
        pass
    return {"error": "unavailable"}


def run_turn(log: str, cmd: list[str]) -> int:
    """Adapt one stdin prompt to agy's streaming protocol and publish its final response and conversation ID."""
    request = {"event": "user", "message": {"content": sys.stdin.read()}}
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8") as proc:
        try:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass  # read the CLI's result/diagnostic on startup failure
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        result = {}
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "result" and isinstance(event.get("result"), dict):
                result = event["result"]
        code = proc.wait()
    if result.get("status") != "SUCCESS" or code:
        print(result.get("error") or "Antigravity did not return a successful result.", file=sys.stderr)
        return code or 1
    sid = result.get("conversation_id")
    if isinstance(sid, str) and CONVERSATION.fullmatch(sid):
        Path(f"{log}.conversation").write_text(sid)
    response = result.get("response") or ""
    Path(f"{log}.last").write_text(response, encoding="utf-8")
    print(response)
    return 0


if __name__ == "__main__":
    sys.exit(run_turn(sys.argv[1], sys.argv[2:]))
