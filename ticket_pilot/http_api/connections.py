"""Dashboard login, account connections and LAN sharing HTTP handlers."""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import accounts as A
from .. import antigravity as AGY
from .. import codex_usage as CU
from .. import config as G
from .. import lan as L
from .. import runner
from .. import usage as U
from ..core import (DATA, DOCKER_OFF, GITHUB_KEYCHAIN, ROOT, AsanaError, asana_token, current_actor, db, docker, event,
                    github_token, secret_delete, secret_get, secret_set, set_actor, settings)


def agent_usage() -> dict:
    """Use the same current authentication check as 활성 계정 before reading any cached quota."""
    accounts = A.detect(versions=False)["tools"]
    out = {}
    if accounts["agy"]["logged_in"]:
        exe = shutil.which("agy", path=runner.agent_path())
        if exe:
            out["agy"] = AGY.usage(exe, accounts["agy"]["method"])
    con = db()
    try:
        if accounts["claude"]["logged_in"]:
            supported = accounts["claude"]["method"] == "cli" and not any(
                label.startswith(("API 키", "토큰", "Amazon Bedrock", "Google Vertex AI"))
                for label in accounts["claude"]["account"])
            out["claude"] = U.cached(con) if supported else {"error": "unsupported_auth"}
        if accounts["codex"]["logged_in"]:
            out["codex"] = {"error": "unsupported_auth"} if "API 키" in accounts["codex"]["account"] else CU.cached(con)
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- GitHub 연결 (TP-112, Device Flow TP-143)
# Device Flow on the shared OAuth App "Ticket-pilot" (the maintainer's, Device Flow enabled): no client secret. A
# client_id in data/github-oauth.json or keychain ticket-pilot-github-oauth (account client_id), left from the old web
# flow or for another OAuth App, wins over the built-in one. The token goes to keychain ticket-pilot-github
# (core.github_token); it and the device_code never go into a response, log or event.

GITHUB_CLIENT_ID = "Ov23ctfLeH7Zm9ZDeT3p"  # not a secret
GITHUB_OAUTH_KEYCHAIN = "ticket-pilot-github-oauth"
GITHUB_OAUTH_FILE = DATA / "github-oauth.json"
# shortcut: one pending login in memory; a dashboard restart drops it (press 연결 again)
_device: dict = {}


def _client_id() -> str:
    try:
        cid = json.loads(GITHUB_OAUTH_FILE.read_text()).get("client_id")
    except (OSError, ValueError, AttributeError):
        cid = None
    return cid or secret_get(GITHUB_OAUTH_KEYCHAIN, "client_id") or GITHUB_CLIENT_ID


def _github_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, method="POST", headers={"Accept": "application/json"},
                                 data=urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _store_github_token(tok: str):
    if not tok:
        raise RuntimeError("GitHub가 토큰을 보내지 않았어요")
    if not tok.replace("_", "").isalnum():  # it goes into a `security -i` command line (secret_set)
        raise RuntimeError("GitHub가 예상과 다른 형식의 토큰을 보냈어요")
    secret_set(GITHUB_KEYCHAIN, "github", tok)


def github_device_start() -> dict:
    """POST /api/github/device: asks GitHub for a user code; the device_code stays here."""
    cid = _client_id()
    j = _github_form("https://github.com/login/device/code", {"client_id": cid, "scope": "repo workflow"})
    if not j.get("device_code") or not j.get("user_code"):
        raise RuntimeError(f"GitHub가 코드를 주지 않았어요: {j.get('error_description') or j.get('error') or '알 수 없음'}")
    expires, interval = int(j.get("expires_in") or 900), int(j.get("interval") or 5)
    _device.clear()
    _device.update(cid=cid, code=j["device_code"], interval=interval, exp=time.time() + expires)
    return {"user_code": j["user_code"], "verification_uri": j.get("verification_uri") or "https://github.com/login/device",
            "expires_in": expires, "interval": interval}


DEVICE_ERRORS = {"expired_token": "코드가 만료됐어요. 다시 연결해 주세요",
                 "access_denied": "GitHub에서 연결을 거부했어요. 다시 연결해 주세요"}


def github_device_poll() -> dict:
    """POST /api/github/device/poll: {"pending": True, "interval"} until GitHub hands out the token, then stores it
    and returns {"connected": True}. Expired / denied / other errors end the login and raise."""
    d = dict(_device)
    if not d:
        raise RuntimeError("진행 중인 연결이 없어요. 다시 연결해 주세요")
    if d["exp"] < time.time():
        _device.clear()
        raise RuntimeError(DEVICE_ERRORS["expired_token"])
    try:
        j = _github_form("https://github.com/login/oauth/access_token", {
            "client_id": d["cid"], "device_code": d["code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except OSError:  # a network blip must not drop a code the owner may already have entered; the expiry above ends it
        return {"pending": True, "interval": d["interval"]}
    same = _device.get("code") == d["code"]  # 새 코드 받기 during this call replaced it: leave the new one alone
    err = j.get("error")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":  # GitHub sends the new interval; RFC 8628 says +5s otherwise
            d["interval"] = int(j.get("interval") or d["interval"] + 5)
            if same:
                _device["interval"] = d["interval"]
        return {"pending": True, "interval": d["interval"]}
    if same:
        _device.clear()
    if err:
        raise RuntimeError(DEVICE_ERRORS.get(err) or f"연결하지 못했어요: {j.get('error_description') or err}")
    _store_github_token(j.get("access_token") or "")
    return {"connected": True}


def github_status() -> dict:
    """Connected account login and scopes; never the token."""
    try:
        tok = github_token()
    except RuntimeError as e:  # secrets.json unreadable: show why on 설정
        return {"connected": True, "error": str(e)}
    if not tok:
        return {"connected": False}
    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"connected": True, "login": json.loads(r.read()).get("login"),
                    "scopes": [x.strip() for x in (r.headers.get("X-OAuth-Scopes") or "").split(",") if x.strip()]}
    except urllib.error.HTTPError as e:
        return {"connected": True, "error": f"GitHub가 토큰을 거절했어요 ({e.code}). 다시 연결해 주세요"}
    except OSError as e:
        return {"connected": True, "error": f"GitHub에 닿지 못했어요: {e}"}


def github_disconnect():
    secret_delete(GITHUB_KEYCHAIN, "github")


# Asana 연결 (TP-115): the PAT the owner pastes on the 설정 page goes to the keychain service asana_keychain_service, account
# asana (where core.asana_token reads it; ASANA_PAT still wins). Like the GitHub token it never goes into a response,
# log or event.
_ASANA_PAT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/:_.-]{15,255}")  # Asana PATs look like 2/<gid>/<gid>:<hex>


def _asana_me(tok: str) -> dict:
    req = urllib.request.Request("https://app.asana.com/api/1.0/users/me?opt_fields=name,email,workspaces.name",
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read()).get("data") or {}
    return {"connected": True, "name": d.get("name"), "email": d.get("email"),
            "workspaces": [{"gid": str(w["gid"]), "name": w.get("name") or ""} for w in d.get("workspaces") or []]}


def asana_status() -> dict:
    """Connected account name and email, its workspaces and the chosen one (TP-148); never the token."""
    if not settings().get("asana_enabled", True):
        return {"connected": False, "enabled": False}
    try:
        tok = asana_token()
    except AsanaError:
        return {"connected": False}
    extra = {"env": True} if os.environ.get("ASANA_PAT") else {}  # disconnect cannot clear an env token
    extra["workspace"] = settings()["asana_workspace_gid"]
    try:
        return {**_asana_me(tok), **extra}
    except urllib.error.HTTPError as e:
        return {"connected": True, **extra, "error": f"Asana가 토큰을 거절했어요 ({e.code}). PAT를 교체해 주세요"}
    except (OSError, ValueError) as e:  # ValueError: a non-JSON reply (proxy, captive portal)
        return {"connected": True, **extra, "error": f"Asana에 닿지 못했어요: {e}"}


def _asana_svc() -> str:
    svc = settings()["asana_keychain_service"]
    if not re.fullmatch(r"[\w.-]+", svc):  # it goes into a `security -i` command line (secret_set)
        raise RuntimeError(f"asana_keychain_service 값이 이상해요: {svc!r}")
    return svc


def asana_save(tok) -> dict:
    """Checks the PAT against users/me first; only a working one replaces the stored token."""
    tok = (tok or "").strip() if isinstance(tok, str) else ""
    if not _ASANA_PAT_RE.fullmatch(tok):  # it goes into a `security -i` command line (secret_set); never echo it back
        raise RuntimeError("Asana PAT 형식이 아니에요. 발급받은 토큰을 그대로 붙여넣어 주세요")
    try:
        me = _asana_me(tok)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Asana가 이 PAT를 거절했어요 ({e.code}). 저장하지 않았어요") from None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Asana에 닿지 못해 PAT를 확인하지 못했어요: {e}") from None
    svc = _asana_svc()
    secret_set(svc, "asana", tok)
    # TP-148: one workspace and none chosen yet -> choose it; a chosen one is never replaced (more: picked on screen)
    # (not with ASANA_PAT set: that token, maybe another account's, is the one tp uses)
    if len(me["workspaces"]) == 1 and not settings()["asana_workspace_gid"] and not os.environ.get("ASANA_PAT"):
        G.update("global", {"asana_workspace_gid": me["workspaces"][0]["gid"]}, actor=current_actor() or "dashboard")
    return {**me, "workspace": settings()["asana_workspace_gid"]}


def asana_disconnect():
    secret_delete(_asana_svc(), "asana")


# ---------------------------------------------------------------- 로컬망 공유 (TP-152, sessions and login: lan.py)

def _plist_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def lan_status(local: bool, admin: bool = False) -> dict:
    """local: may change the sharing and force logouts (127.0.0.1; Docker mode: an admin, sharing itself is off)."""
    ls = settings()["lan_share"]
    if docker():
        return {**ls, "enabled": False, "local": admin, "docker": True, "address": None, "sessions": L.sessions()}
    ip = L.address()
    port = int(os.environ.get("TP_PORT") or settings()["dashboard_port"])
    return {**ls, "local": local, "address": ip and f"http://{ip}:{port}", "sessions": L.sessions()}


def lan_share_set(enabled, org, actor: str) -> dict:
    """POST /api/lan/share (127.0.0.1 only): save lan_share, then rerun launchd/install.sh (detached: it restarts
    this dashboard) so the dashboard listens on 0.0.0.0 or 127.0.0.1 again. Only when this dashboard is the launchd
    one of this checkout: a dev server in a worktree must not reinstall the host's agents."""
    if docker():
        raise SystemExit(DOCKER_OFF)
    if not isinstance(enabled, bool):
        raise SystemExit("enabled는 true 또는 false여야 해요")
    was = settings()["lan_share"]["enabled"]
    G.update("global", {"lan_share": {"enabled": enabled, "org": org.strip() if isinstance(org, str) else ""}}, actor=actor)
    if enabled == was:
        return {"ok": True, "restart": False}
    from .. import setup as SU
    if not SU.installed(_plist_dir() / "com.ticket-pilot.dashboard.plist"):  # Linux: its systemd unit
        return {"ok": True, "restart": False,
                "note": "이 대시보드는 launchd(Linux: systemd)로 뜬 것이 아니라 다시 시작하지 않았어요. launchd/install.sh를 실행하세요"}
    log = Path(os.environ.get("TP_LOG_DIR") or Path.home() / "tp-work" / "logs") / "lan-share.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen(["/bin/sh", str(ROOT / "launchd" / "install.sh")], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return {"ok": True, "restart": True}


def get_me(req):
    me = {"local": req.local, "login": req.login, "actor": req.actor}
    return req._send(200, {**me, "docker": True, "admin": req.admin, "public_url": L.public_url()} if docker() else me)


def get_lan_status(req):
    return req._send(200, lan_status(req.local, req.admin))


def get_tunnel(req):
    if not docker():
        return req._send(200, {"docker": False})
    return req._send(200, L.tunnel_status())


def get_github_status(req):
    return req._send(200, github_status())


def get_accounts(req):
    d = A.detect()
    if not req.admin:  # LAN visitors: no owner email / plan
        for v in d["tools"].values():
            v["account"] = []
    return req._send(200, {**d, "admin": req.admin})


def get_agent_usage(req):
    return req._send(200, agent_usage())


def get_codex_login(req):
    try:
        st, done = A.codex_status()
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return req._send(400, {"state": "failed", "error": f"로그인 상태를 확인하지 못했어요: {e}"})
    if done:
        con = db()
        event(con, "agent_login", "codex 로그인 (CLI 로그인)")
        con.commit()
    return req._send(200, st)


def get_asana_status(req):
    return req._send(200, asana_status())


def _login_peer(req):
    if req.local:
        raise SystemExit("이 맥(127.0.0.1)에서는 로그인하지 않아요")
    peer = req.client_address[0]
    return peer, L.seen_ip(peer, req.headers.get("Cf-Connecting-IP"))


def post_login(req, _body):
    peer, seen = _login_peer(req)
    return req._send(200, L.login_start(peer, seen))


def post_login_poll(req, body):
    peer, seen = _login_peer(req)
    r = L.login_poll(str(body.get("id") or ""), peer, L.org(), seen)
    if "sid" not in r:
        return req._send(200, r)
    set_actor(r["login"])
    con = db()
    event(con, "login", f"{r['login']} logged in from {seen}")
    con.commit()
    return req._send(200, {"login": r["login"]}, headers=[("Set-Cookie", (
        f"{L.COOKIE}={r['sid']}; {req._cookie_attrs()}; Max-Age={L.MAX_DAYS * 86400}"))])


def post_logout(req, _body):
    L.end_session(sid=req._cookie())
    return req._send(200, {"ok": True}, headers=[("Set-Cookie", (
        f"{L.COOKIE}=; {req._cookie_attrs()}; Max-Age=0"))])


def post_lan_share(req, body):
    return req._send(200, lan_share_set(body.get("enabled"), body.get("org"), req.actor))


def post_session_delete(req, body):
    if not isinstance(body.get("id"), int):
        raise SystemExit("id")
    L.end_session(rowid=body["id"])
    return req._send(200, {"ok": True})


def post_agent_login(req, body, *, tool: str, act: str):
    """TP-179 에이전트 로그인 buttons: 127.0.0.1 only (Docker mode: TP_ADMINS). Codes, tokens and keys only go
    into accounts.py; the event says who logged which tool in or out, nothing else."""
    try:
        if act == "login/cancel":
            A.cancel(tool)
            return req._send(200, {"ok": True})
        if act == "login":
            return req._send(200, {"codex": A.codex_start, "claude": A.claude_start, "agy": A.agy_start}[tool]())
        if act == "logout":
            A.logout(tool)
            msg = f"{tool} 로그아웃" + (" (tp에 저장한 토큰·키만 지움)" if tool == "claude" else "")
        elif tool == "agy" and act in ("login/code", "apikey"):
            if act == "login/code":
                A.agy_code(body.get("code"))
            else:
                A.agy_save_key(body.get("key"))
            msg = f"agy 로그인 ({'API 키' if act == 'apikey' else 'Google 계정'})"
        elif tool != "claude":
            return req._send(404, {"error": "not found"})
        else:
            kind = {"login/code": A.claude_code, "token": A.claude_token, "apikey": A.claude_api_key}[act](
                body.get("key" if act == "apikey" else "code" if act == "login/code" else "token"))
            msg = f"claude 로그인 ({'API 키' if kind == A.API_KEY else 'tp 토큰'})"
    except RuntimeError as e:
        return req._send(400, {"error": str(e)})
    con = db()
    event(con, "agent_login", msg)
    con.commit()
    return req._send(200, {"ok": True})


def post_github_device(req, _body):
    return req._send(200, github_device_start())


def post_github_device_poll(req, _body):
    return req._send(200, github_device_poll())


def post_github_disconnect(req, _body):
    github_disconnect()
    return req._send(200, {"ok": True})


def post_asana_token(req, body):
    return req._send(200, asana_save(body.get("token")))


def post_asana_disconnect(req, _body):
    asana_disconnect()
    return req._send(200, {"ok": True})
