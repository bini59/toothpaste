"""활성 계정 (설정 > 에이전트): CLI installation, current account and dashboard sign-in.

Checks CLI login status and reads local account labels for GET /api/agents/accounts.
Login URLs and one-time codes are scoped to the dashboard's login endpoints."""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import antigravity as AGY, usage as U
from .core import docker, secret_delete, secret_get, secret_set
from .runner import agent_path

# tp's own Claude credential (TP-179): one of the two accounts, the other is deleted when one is saved
CLAUDE_SVC = "ticket-pilot-claude"
OAUTH, API_KEY = "oauth_token", "api_key"
ENV = {OAUTH: "CLAUDE_CODE_OAUTH_TOKEN", API_KEY: "ANTHROPIC_API_KEY"}


def _version(exe: str) -> str | None:
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (p.stdout.strip().splitlines() or [None])[0] if p.returncode == 0 else None


def _json(path: Path) -> dict:
    try:
        j = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return j if isinstance(j, dict) else {}


def _jwt_claims(tok) -> dict:
    """Payload of a JWT, not verified: only to show the email / plan codex logged in with."""
    try:
        p = tok.split(".")[1]
        j = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}
    return j if isinstance(j, dict) else {}


def claude_cli(exe: str | None = None, *, fallback_on_probe_failure: bool = False) -> tuple[bool, list[str]]:
    """The CLI's own login (~/.claude, keychain, env), without tp's stored token."""
    report = {}
    if exe:
        try:
            proc = subprocess.run([exe, "auth", "status", "--json"], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=5)
            value = json.loads(proc.stdout)
            report = value if isinstance(value, dict) else {}
        except (OSError, subprocess.TimeoutExpired):
            if not fallback_on_probe_failure:
                return False, []
        except ValueError:
            pass  # older CLI, fall back to its local credential files
    if report.get("loggedIn") is False:
        return False, []
    if report.get("authMethod") == "api_key":
        return True, ["API 키"]
    if report.get("authMethod") == "oauth_token" and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, ["토큰 (CLAUDE_CODE_OAUTH_TOKEN)"]
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    creds = U._creds() or [c for c in [_json(cfg / ".credentials.json").get("claudeAiOauth")] if isinstance(c, dict) and c.get("accessToken")]
    if creds:
        acct = _json(Path.home() / ".claude.json").get("oauthAccount") or {}
        plan = creds[0].get("subscriptionType")
        return True, [x for x in (acct.get("emailAddress"), acct.get("organizationName"), plan and f"{plan} 플랜") if x]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, ["API 키 (ANTHROPIC_API_KEY)"]
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, ["토큰 (CLAUDE_CODE_OAUTH_TOKEN)"]
    for k, label in (("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"), ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI")):
        if os.environ.get(k) not in (None, "", "0"):
            return True, [label]
    return report.get("loggedIn") is True, []


def codex(exe: str | None = None) -> tuple[bool, list[str]]:
    signed_in = False
    if exe:
        try:
            p = subprocess.run([exe, "login", "status"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=5)
            status = (p.stdout + p.stderr).lower()
            if "error checking login status" in status:
                return False, []
            if "not logged in" in status:
                return (True, ["API 키"]) if os.environ.get("OPENAI_API_KEY") else (False, [])
            signed_in = p.returncode == 0 and "logged in using" in status
            if signed_in and "api key" in status:
                return True, ["API 키"]
        except (OSError, subprocess.TimeoutExpired):
            return False, []
    auth = _json(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json")
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    if tokens.get("id_token") or tokens.get("access_token"):
        c = _jwt_claims(tokens.get("id_token"))
        plan = (c.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
        return True, [x for x in (c.get("email"), plan and f"ChatGPT {plan}") if x]
    if not signed_in and (auth.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        return True, ["API 키"]
    return signed_in, []


def claude_saved() -> str | None:
    """Which credential tp holds for claude (OAUTH | API_KEY), None without one."""
    return next((k for k in (OAUTH, API_KEY) if secret_get(CLAUDE_SVC, k)), None)


def detect(*, versions: bool = True) -> dict:
    """Installed tools and their active credentials, shared by settings and the sidebar."""
    out = {}
    for tool in ("claude", "codex"):
        exe = shutil.which(tool, path=agent_path())
        ok, acct = (claude_cli(exe) if tool == "claude" else codex(exe)) if exe else (False, [])
        saved = claude_saved() if tool == "claude" else None
        method = "cli" if ok else saved if exe else None
        out[tool] = {"installed": bool(exe), "version": _version(exe) if exe and versions else None, "logged_in": bool(method),
                     "account": acct, "method": method, "saved": saved}
    exe = shutil.which("agy", path=agent_path())
    agy = AGY.account(exe) if exe else {"method": None, "saved": None, "account": [], "models": [], "neon_reason": None}
    out["agy"] = {**agy, "installed": bool(exe), "logged_in": bool(agy["method"]),
                  "version": _version(exe) if exe and versions else None}
    return {"docker": docker(), "tools": out}


def agent_env(tool: str) -> dict:
    """Env runner.launch adds for an agent (ticket, chat, automation): tp's stored Claude token or API key, only when
    the CLI has no login of its own (an existing install keeps working exactly as before). Codex: nothing."""
    saved = claude_saved() if tool == "claude" else None  # first: no keychain read of Claude's login without one
    # A failed status probe does not override the user's local credentials when launching Claude.
    if not saved or claude_cli(shutil.which("claude", path=agent_path()), fallback_on_probe_failure=True)[0]:
        return {}
    return {ENV[saved]: secret_get(CLAUDE_SVC, saved)}


# ---------------------------------------------------------------- login buttons (TP-179)
# One login per tool at a time, in memory (shortcut: a dashboard restart drops it, press 로그인 again). The CLI runs in
# its own session (start_new_session) so cancel / expiry kill its whole process group. Codes, tokens and keys stay in
# this process: never in a response, log, event or a command line (the CLI reads them from its stdin).
_locks = {tool: threading.Lock() for tool in ("claude", "codex", "agy")}
_logins: dict[str, dict] = {}
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[78=>]")
OSC8 = re.compile(r"\x1b\]8;[^;\x07]*;(https://[^\x07\x1b]+)")
CODEX_CODE = re.compile(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b")
CLAUDE_TOKEN = re.compile(r"sk-ant-oat[A-Za-z0-9_-]{20,400}")
PRINTED_TOKEN = re.compile(CLAUDE_TOKEN.pattern + r"(?=\s)")  # whole: a read can stop in the middle of it
API_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,400}")
PASTE_CODE = re.compile(r"[A-Za-z0-9_#.~-]{8,2048}")
START_WAIT_S, CODE_WAIT_S = 30, 60
CLAUDE_EXP_S = 15 * 60  # setup-token waits for the pasted code; the OAuth code itself is short-lived


def _exe(tool: str) -> str:
    exe = shutil.which(tool, path=agent_path())
    if not exe:
        raise RuntimeError(f"PATH에서 {tool} 명령을 찾지 못했어요")
    return exe


def _env() -> dict:
    # BROWSER=true: the dashboard shows the link; no browser opens on the server (Docker, Linux) or behind the owner
    return {**os.environ, "PATH": agent_path(), "BROWSER": "true", "NO_COLOR": "1"}


def _read(s: dict, src: int):
    """Reader thread: the CLI's output into s["out"] until it closes (a pty raises OSError at the end). It closes src
    itself: closing it from _stop could hand the number to another file while this thread still reads it."""
    try:
        while True:
            try:
                b = os.read(src, 65536)
            except OSError:
                break
            if not b:
                break
            s["out"] += b.decode(errors="replace")
    finally:
        os.close(src)


def _stop(tool: str):
    """End the tool's login (cancel, expiry, done): kill its process group (the reader then closes its end).
    Callers hold _locks[tool]."""
    s = _logins.pop(tool, None)
    if not s:
        return
    try:
        os.killpg(s["p"].pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        s["p"].wait(5)
    except subprocess.TimeoutExpired:
        pass


def _wait(s: dict, found, secs: float):
    """found(plain text) until it returns something, the CLI exits or secs pass."""
    end = time.time() + secs
    while True:
        got = found(s["out"])
        if got or time.time() > end:
            return got
        if s["p"].poll() is not None:
            time.sleep(0.3)  # the reader may not have taken its last output yet
            return found(s["out"])
        time.sleep(0.1)


def _tail(text: str) -> str:
    lines = [" ".join(ln.split()) for ln in ANSI.sub(" ", text).splitlines() if ln.strip()]
    return " / ".join(lines[-3:])[:300] or "출력 없음"


def _pending(tool: str) -> dict | None:
    s = _logins.get(tool)
    return s if s and s["p"].poll() is None and s["exp"] > time.time() else None


def codex_start() -> dict:
    """POST /api/agents/codex/login: `codex login --device-auth` in the background; its link and one-time code."""
    with _locks["codex"]:
        s = _pending("codex")
        if not s:
            _stop("codex")
            r, w = os.pipe()
            try:
                p = subprocess.Popen([_exe("codex"), "login", "--device-auth"], stdin=subprocess.DEVNULL, stdout=w,
                                     stderr=w, start_new_session=True, env=_env())
            except BaseException:
                os.close(r)
                raise
            finally:
                os.close(w)
            s = _logins["codex"] = {"p": p, "out": "", "exp": time.time() + 15 * 60}
            threading.Thread(target=_read, args=(s, r), daemon=True).start()

            def parse(out):
                t = ANSI.sub("", out)
                url, code = re.search(r"https://\S+", t), CODEX_CODE.search(t)
                return url and code and (url.group(0), code.group(0), re.search(r"expires in (\d+) min", t))
            got = _wait(s, parse, START_WAIT_S)
            if not got:
                why = _tail(s["out"])
                _stop("codex")
                raise RuntimeError(f"codex가 로그인 코드를 주지 않았어요: {why}")
            s["url"], s["code"] = got[0], got[1]
            s["exp"] = time.time() + (int(got[2].group(1)) * 60 if got[2] else 15 * 60)
        return {"state": "pending", "url": s["url"], "code": s["code"], "expires_in": int(s["exp"] - time.time())}


def codex_status() -> tuple[dict, bool]:
    """GET /api/agents/codex/login: (state pending | done | failed | expired | idle, just finished). Done means
    `codex login status` says so after the login process ended."""
    with _locks["codex"]:
        s = _logins.get("codex")
        if not s:
            return {"state": "idle"}, False
        rc = s["p"].poll()
        if rc is None and s["exp"] > time.time():
            return {"state": "pending", "url": s["url"], "code": s["code"],
                    "expires_in": int(s["exp"] - time.time())}, False
        why = _tail(s["out"].replace(s["code"], "…"))
        _stop("codex")
        if rc is None:
            return {"state": "expired", "error": "코드가 만료됐어요. 다시 로그인해 주세요"}, False
    st = subprocess.run([_exe("codex"), "login", "status"], capture_output=True, text=True, timeout=30, env=_env())
    if rc == 0 and st.returncode == 0:
        return {"state": "done"}, True
    return {"state": "failed", "error": f"로그인하지 못했어요: {why}"}, False


def claude_start() -> dict:
    """POST /api/agents/claude/login: `claude setup-token` on a pty (without one it prints nothing); its sign-in
    link. The code the owner gets in the browser comes back through claude_code()."""
    with _locks["claude"]:
        s = _pending("claude")
        if not s:
            _stop("claude")
            master, slave = os.openpty()
            # wide: the link and later the token stay on one line instead of wrapping
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
            try:
                p = subprocess.Popen([_exe("claude"), "setup-token"], stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, env={**_env(), "TERM": "xterm-256color"})
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            s = _logins["claude"] = {"p": p, "fd": master, "out": "", "exp": time.time() + CLAUDE_EXP_S}
            threading.Thread(target=_read, args=(s, master), daemon=True).start()

            def parse(out):
                m = OSC8.search(out) or re.search(r"https://\S*oauth/authorize\S+", ANSI.sub(" ", out))
                return m and m.group(1 if m.re is OSC8 else 0)
            url = _wait(s, parse, START_WAIT_S)
            if not url:
                why = _tail(s["out"])
                _stop("claude")
                raise RuntimeError(f"claude가 로그인 주소를 주지 않았어요: {why}")
            s["url"] = url
        return {"state": "pending", "url": s["url"]}


def claude_code(code) -> str:
    """POST /api/agents/claude/login/code: hand the browser's code to setup-token, store the token it prints."""
    code = code.strip() if isinstance(code, str) else ""
    if not PASTE_CODE.fullmatch(code):
        raise RuntimeError("코드 형식이 맞지 않아요. 브라우저에 나온 코드를 그대로 붙여넣으세요")
    with _locks["claude"]:
        s = _pending("claude")
        if not s:
            _stop("claude")
            raise RuntimeError("진행 중인 로그인이 없어요. 다시 로그인해 주세요")
        mark = len(s["out"])
        try:
            os.write(s["fd"], code.encode() + b"\r")
        except OSError:  # the CLI ended (and its reader closed the pty) just now
            _stop("claude")
            raise RuntimeError("로그인이 끝나 버렸어요. 다시 로그인해 주세요") from None

        def parse(out):
            t = ANSI.sub(" ", out[mark:])  # Ink moves the cursor instead of printing spaces
            return PRINTED_TOKEN.search(t) or re.search(r"error[^\r\n]*", t, re.I)
        got = _wait(s, parse, CODE_WAIT_S)
        _stop("claude")
    if not got or got.re is not PRINTED_TOKEN:
        # only CLI's error line: the rest of the screen echoes part of the pasted code
        why = " ".join(got.group(0).split())[:300] if got else "응답 없음"
        raise RuntimeError(f"토큰을 받지 못했어요 ({why}). 다시 로그인해 주세요")
    got = got.group(0)
    _save(OAUTH, got)
    return OAUTH


GOOGLE_OAUTH_URL = re.compile(r"https://accounts\.google\.com/o/oauth2/(?:v2/)?auth\?[^\s\x1b]+")


def agy_start() -> dict:
    """Open the CLI's manual Google OAuth flow through the non-generating /model command."""
    with _locks["agy"]:
        if AGY.credentials()[0]:
            raise RuntimeError("Gemini API 키 연결을 해제한 뒤 Google 계정으로 로그인하세요")
        s = _pending("agy")
        if not s:
            _stop("agy")
            master, slave = os.openpty()
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 4000, 0, 0))
                p = subprocess.Popen([_exe("agy"), *AGY.GOOGLE_SETTINGS, "-p", "/model"],
                                     stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                     env={**_env(), "TERM": "xterm-256color"})
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            s = _logins["agy"] = {"p": p, "fd": master, "out": "", "exp": time.time() + 60}
            threading.Thread(target=_read, args=(s, master), daemon=True).start()
            match = _wait(s, lambda out: GOOGLE_OAUTH_URL.search(ANSI.sub("", out)), START_WAIT_S)
            if not match:
                _stop("agy")
                if AGY.account(_exe("agy"))["method"] == "cli":
                    return {"state": "done"}
                raise RuntimeError("Google 로그인 주소를 받지 못했어요. 터미널에서 agy를 실행해 로그인할 수 있어요")
            s["url"] = match.group()
        return {"state": "pending", "url": s["url"]}


def agy_code(code) -> None:
    code = code.strip() if isinstance(code, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9_./+=~-]{8,2048}", code):
        raise RuntimeError("Google 로그인 코드를 그대로 붙여넣으세요")
    with _locks["agy"]:
        s = _pending("agy")
        if not s:
            _stop("agy")
            raise RuntimeError("로그인 세션이 만료됐어요. 다시 로그인해 주세요")
        try:
            os.write(s["fd"], code.encode() + b"\r")
            s["p"].wait(timeout=CODE_WAIT_S)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            _stop("agy")
        if AGY.account(_exe("agy"))["method"] != "cli":
            raise RuntimeError("Google 로그인을 확인하지 못했어요. 새 코드를 받아 다시 로그인해 주세요")


def agy_save_key(key) -> None:
    with _locks["agy"]:
        _exe("agy")
        AGY.save_key(key)
        _stop("agy")


def cancel(tool: str):
    with _locks[tool]:
        _stop(tool)


def _save(kind: str, value: str):
    secret_set(CLAUDE_SVC, kind, value)
    for other in (OAUTH, API_KEY):
        if other != kind:
            secret_delete(CLAUDE_SVC, other)


def claude_token(tok) -> str:
    """POST /api/agents/claude/token: the token `claude setup-token` printed in a terminal (the 고급 way)."""
    tok = tok.strip() if isinstance(tok, str) else ""
    if not CLAUDE_TOKEN.fullmatch(tok):
        raise RuntimeError("claude setup-token이 출력한 토큰(sk-ant-oat…)을 그대로 붙여넣으세요")
    _save(OAUTH, tok)
    return OAUTH


ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models?limit=1"


def claude_api_key(key) -> str:
    """POST /api/agents/claude/apikey: an ANTHROPIC_API_KEY, saved only after Anthropic accepts it (list models:
    free, no tokens used)."""
    key = key.strip() if isinstance(key, str) else ""
    if not API_KEY_RE.fullmatch(key):
        raise RuntimeError("Anthropic API 키(sk-ant-…)를 그대로 붙여넣으세요")
    req = urllib.request.Request(ANTHROPIC_MODELS, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Anthropic이 이 키를 받지 않았어요 ({e.code}). 저장하지 않았어요") from None
    except OSError as e:
        raise RuntimeError(f"Anthropic에 닿지 못해 키를 확인하지 못했어요 ({type(e).__name__}). 저장하지 않았어요") from None
    _save(API_KEY, key)
    return API_KEY


def logout(tool: str):
    """Disconnect the tool's dashboard credential, or run codex logout for a Codex CLI account."""
    if tool == "agy":
        with _locks["agy"]:
            _stop("agy")
            secret_delete(AGY.SERVICE, AGY.API_KEY)
        return
    cancel(tool)
    if tool == "codex":
        exe = _exe("codex")
        p = subprocess.run([exe, "logout"], capture_output=True, text=True, timeout=30, env=_env())
        if p.returncode != 0 and codex(exe)[0]:
            raise RuntimeError(f"codex logout이 실패했어요: {_tail(p.stdout + p.stderr)}")
    else:
        for k in (OAUTH, API_KEY):
            secret_delete(CLAUDE_SVC, k)
