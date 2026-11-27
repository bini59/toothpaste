"""Local dashboard: http://127.0.0.1:<port> (read-mostly view over the SQLite DB)."""
from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from . import accounts as A
from . import antigravity as AGY
from . import deploy as D
from . import docker_update as DU
from . import chat as CH
from . import checks as K
from . import codex_usage as CU
from . import config as G
from . import devserver as DS
from . import guide as GD
from . import lan as L
from . import metrics as MET
from . import milestone as MS
from . import notifications as N
from . import notion, notion_tickets
from . import onboard as OB
from . import pipeline as P
from . import release as R
from . import runner
from . import themes as TH
from . import sysjobs as SJ
from . import update as UP
from . import usage as U
from .core import (DATA, DOCKER_OFF, GITHUB_KEYCHAIN, ROOT, AsanaError, asana_token, current_actor, db, docker, event,
                   github_token, meta_get, meta_set, project_map, repo_rules, secret_delete, secret_get, secret_set,
                   set_actor, settings)

_sync_lock = threading.Lock()
_check_lock = threading.Lock()
MAX_BODY = 64 * 1024 * 1024


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


def state() -> dict:
    con = db()
    try:
        return _state(con)
    finally:
        con.close()


def _state(con) -> dict:
    rows = lambda q, *a: [dict(r) for r in con.execute(q, a)]  # noqa: E731
    rules = repo_rules()
    staged_pr_urls = R.staged_pr_urls(con, list(rules))
    staged_urls = {url for urls in staged_pr_urls.values() for url in urls}
    prs = rows("SELECT * FROM prs")
    staged_gids = {p["ticket_gid"] for p in prs if p["url"] in staged_urls}
    tickets = rows(f"""SELECT gid,key,name,url,project_gid,project_name,section,parent_gid,parent_name,parent_url,milestone_error,due_on,tags,has_tag,
                      deps_json,open_deps,completed,assigned,stage,repo_override,skip,first_seen,last_seen,
                      approve_requested_at,approve_error,reply,check_override,check_override_sha,in_milestone,ms_done_at,
                      review_override,review_override_sha,done_at,asana_modified_at
                      FROM tickets WHERE (completed=0 AND assigned=1) OR gid IN (SELECT ticket_gid FROM runs)
                      OR gid IN ({','.join('?' for _ in staged_gids)})""", *staged_gids)
    landed = {r["ticket_gid"]: r for r in con.execute(MS.LANDED_NOTION)}
    for t in tickets:
        if t["stage"] == "needs_staging" and (repo := P.chosen_repo(con, t)):
            t["staging_config_error"] = MS.staging_config_error(t, repo["repo"])
        elif t["stage"] == "blocked":
            if t["milestone_error"]:
                t["milestone_kind_blocked"] = notion_tickets.uses_milestone_kind(con, t)
            else:
                t["staging_config_error"] = P.resume_staging_error(con, t)
        if (ms := landed.get(t["gid"])) and t["parent_gid"] != ms["parent_gid"]:
            t.update(parent_gid=ms["parent_gid"], parent_name=ms["parent_name"], parent_url=ms["url"], in_milestone=1)
        if t["open_deps"]:  # deps merged into the feature base no longer block (TP-20)
            deps = P.deps_status(con, t)
            t["deps_json"] = json.dumps(deps, ensure_ascii=False)
            t["open_deps"] = sum(1 for d in deps if not d["completed"] and not d["landed"])
    tickets_by_gid = {t["gid"]: t for t in tickets}
    for p in prs:
        # tp checks of the open head (checks.py): state none|pending|pass|fail|error, rows, and the approval gate text
        if p["state"] == "OPEN":
            cs = K.check_state(con, p)
            p["checks"] = {"state": cs["state"], "rows": cs["rows"], "gate": K.gate_error(con, p)}
            if t := tickets_by_gid.get(p["ticket_gid"]):
                errors = D.milestone_deploy_errors(t, p["repo"], p["base"])
                p["staging_deploy_error"] = errors.get("staging")
                p["direct_deploy_error"] = errors.get("direct")
        elif p["url"] in staged_urls and MS.awaiting_next_round(con, p["ticket_gid"]):
            p["awaiting_milestone_round"] = True
    return {
        "meta": {"last_sync": meta_get(con, "last_sync"), "last_scan": meta_get(con, "last_scan"),
                 "settings": settings(),
                 "release_hold": bool(meta_get(con, "release_hold", False)),
                 "next_release_at": R.next_release_at().isoformat(),
                 "rules": rules, "modes": D.MODES, "last_diff_scan": meta_get(con, "last_diff_scan"),
                 "version": version(con)},
        "staging_diffs": rows("SELECT * FROM staging_diffs ORDER BY repo"),
        "tickets": tickets,
        "runs": rows("SELECT id,ticket_gid,repo,worktree_name,worktree_path,branch,terminal,ws_status,ws_comment,state,origin,created_at,updated_at FROM runs"),
        "prs": prs,
        "staged_pr_urls": staged_pr_urls,
        "reviews": rows("SELECT * FROM reviews ORDER BY id DESC"),
        "repos": [{**r, **factory_repo(r["name"])} for r in rows("SELECT * FROM repos")],
        "project_map": rows("SELECT * FROM project_map"),
        "events": rows("SELECT * FROM events WHERE kind != 'view' ORDER BY id DESC LIMIT 500"),
        "releases": rows("SELECT * FROM releases ORDER BY id DESC LIMIT 100"),
        "dev_servers": DS.status(con),
    }


def version(con) -> dict:
    """Sidebar version + 업데이트 item: `update` is the latest release tag tp tick saw (every 6 h) when not in HEAD."""
    tag = (meta_get(con, "update_check") or {}).get("latest")
    return {"current": UP.current(), "update": tag if tag and UP.newer(ROOT, tag) else None}


def update_info(since: int | None, admin: bool = True) -> dict:
    """업데이트 page: versions, release notes, why it cannot run; with since (update.log size when the run started)
    the log lines written after it."""
    con = db()
    try:
        chk = meta_get(con, "update_check") or {}
    finally:
        con.close()
    tag = chk.get("latest")
    docker_update = DU.status() if docker() else {}
    managed = bool(DU.directory()) if docker() else False
    why = UP.blocked() if not docker() else None
    if docker() and not admin:
        why = "Docker 업데이트는 TP_ADMINS에 등록된 관리자만 실행할 수 있어요"
    elif managed and not docker_update.get("ready"):
        why = "호스트의 Docker 업데이트 관리자가 응답하지 않아요"
    available = bool(tag and UP.newer(ROOT, tag))
    out = {"current": UP.current(), "latest": tag, "checked_at": chk.get("at"), "blocked": why,
           "available": available,
           "command": DOCKER_UPDATE if docker() and admin and not managed else None,
           "notes": UP.notes(ROOT, "HEAD", tag) if since is None and tag and available else []}
    if managed:
        out["docker_update"] = {k: docker_update[k] for k in ("id", "tag", "state", "message") if k in docker_update}
    if since is not None:
        try:
            with open(UP.log_path(), "rb") as f:
                f.seek(since)
                out["log"] = f.read().decode(errors="replace").splitlines()
        except OSError:
            out["log"] = []
    return out


def check_update(admin: bool = True) -> dict:
    """POST /api/update/check (지금 확인, TP-153): fetch the tags now, then update_info(). Checks run one at a time: a
    click during another check waits for it, then fetches itself (so its error reaches it too). Raises UpdateError
    when the fetch fails."""
    with _check_lock:
        con = db()
        try:
            UP.check_latest(con, force=True)
            con.commit()
        finally:
            con.close()
    return update_info(None, admin=admin)


DOCKER_UPDATE = "git pull --ff-only && python3 -m ticket_pilot.docker_update_manager install"


def start_update() -> int:
    """POST /api/update: `tp update` in its own session (install.sh restarts this dashboard mid-way). Returns the
    update.log size before it starts, for update_info(since). Docker mode: the host rebuilds the image instead."""
    why = f"{DOCKER_OFF}. 호스트에서 실행하세요: {DOCKER_UPDATE}" if docker() else UP.blocked()
    if why:
        raise SystemExit(why)
    f = UP.log_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    size = f.stat().st_size if f.exists() else 0
    with open(f, "a") as err:  # stdout repeats what update.log gets; stderr (a crash) goes there too
        subprocess.Popen([sys.executable, "-m", "ticket_pilot.cli", "update"], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=err, start_new_session=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    return size


def mode_info(since: int | None) -> dict:
    """설정 → 실행 방식 (TP-165): the current way, the Docker environment check (host only: the container has no
    docker), the last switch; with since (mode.log size when the run started) the log lines after it."""
    from . import mode as MO
    cur = MO.current()
    out = {"current": cur, "state": MO._state() or None,
           "command": f"bin/tp mode host --apply   (호스트의 {ROOT.name} checkout에서)" if docker() else None,
           "checks": None if cur == "docker" or since is not None else MO.check(MO.mounts())}
    if since is not None:
        try:
            with open(MO.log_path(), "rb") as f:
                f.seek(since)
                out["log"] = f.read().decode(errors="replace").splitlines()
        except OSError:
            out["log"] = []
    return out


def _mode_argv(to, admins) -> list[str]:
    if docker():  # no docker inside the container: the switch back runs on the host
        raise SystemExit(f"{DOCKER_OFF}. 호스트의 checkout에서 실행하세요: bin/tp mode host --apply")
    if to not in ("docker", "host"):
        raise SystemExit("to는 docker 또는 host여야 해요")
    argv = [sys.executable, "-m", "ticket_pilot.cli", "mode", to]
    if admins:
        from . import mode as MO
        if not isinstance(admins, str) or not MO.ADMINS_RE.fullmatch(admins):
            raise SystemExit("관리자는 GitHub 아이디를 쉼표로 이어 쓰세요")
        argv += ["--admins", admins]
    return argv


def mode_preview(to, admins=None) -> dict:
    """POST /api/mode/preview: `tp mode <to>` without --apply (changes nothing)."""
    try:
        p = subprocess.run(_mode_argv(to, admins), cwd=ROOT, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "미리보기가 2분 안에 끝나지 않았어요 (docker가 응답하지 않아요?)"}
    return {"ok": p.returncode == 0, "text": (p.stdout + p.stderr).strip()}


def start_mode(to, admins=None) -> int:
    """POST /api/mode: `tp mode <to> --apply` in its own session, like start_update (it stops this dashboard).
    Returns the mode.log size before it starts, for mode_info(since)."""
    from . import mode as MO
    argv = _mode_argv(to, admins) + ["--apply"]
    f = MO.log_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    size = f.stat().st_size if f.exists() else 0
    with open(f, "a") as out:
        out.write(f"\n== {time.strftime('%Y-%m-%dT%H:%M:%S')} tp mode {to} --apply\n")
        out.flush()
        subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True, env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"})
    return size


def factory_repo(name: str) -> dict:
    """The repo settings the 작업실 factory draws (TP-127), read-only: agent tool/model, check names (no commands),
    CI, the deploy rule's train/modes/merge and the rework cap."""
    a = runner.resolve(((project_map().get("repos") or {}).get(name) or {}).get("agent"))
    rc = K.repo_checks(name) or {"ci": False, "checks": []}
    rule = repo_rules(name)
    return {"agent": {"tool": a["tool"], "model": a["model"]}, "checks": [c["name"] for c in rc["checks"]],
            "ci": rc["ci"], "deploy": rule and {k: rule[k] for k in ("train", "modes", "merge")},
            "max_rework": P.max_rework_rounds(name)}


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
    from . import setup as SU
    if not SU.installed(_plist_dir() / "com.ticket-pilot.dashboard.plist"):  # Linux: its systemd unit
        return {"ok": True, "restart": False,
                "note": "이 대시보드는 launchd(Linux: systemd)로 뜬 것이 아니라 다시 시작하지 않았어요. launchd/install.sh를 실행하세요"}
    log = Path(os.environ.get("TP_LOG_DIR") or Path.home() / "tp-work" / "logs") / "lan-share.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen(["/bin/sh", str(ROOT / "launchd" / "install.sh")], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return {"ok": True, "restart": True}


class Forbidden(Exception):
    pass


# open to a private-network address without a login: what the login screen needs (/api/* other than these needs one;
# everything else is web/dist, the app bundle without data)
LAN_PUBLIC = ("/api/lan/me", "/api/lan/login", "/api/lan/login/poll")


AGENT_LOGIN_PATH = re.compile(r"/api/agents/(claude|codex|agy)/(login|login/code|login/cancel|token|apikey|logout)")
AGENT_LOGIN_403 = "에이전트 로그인은 이 컴퓨터(127.0.0.1)에서만 할 수 있어요 (Docker 모드: TP_ADMINS의 사람만)"


class H(BaseHTTPRequestHandler):
    timeout = 60  # per socket read: a client that announces a body and sends nothing must not hold a thread forever
    local = True
    admin = True  # may do what only 127.0.0.1 does (Docker mode: TP_ADMINS)
    login = None
    actor = None

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=()):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):  # quiet
        pass

    def _static(self):
        """Serve the built React app (web/dist, `npm run build`); unknown paths fall back to index.html."""
        dist = (ROOT / "web" / "dist").resolve()
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        f = (dist / rel).resolve()
        if not f.is_relative_to(dist) or not f.is_file():
            f = dist / "index.html"
        if not f.is_file():
            return self._send(503, b"web/dist missing: run `cd web && npm ci && npm run build`",
                              "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        b = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # hashed assets are immutable; index.html must always revalidate
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _deny(self, code: int, msg: str) -> bool:
        if self.command == "POST":  # drain first: closing with unread data resets the socket before the reply arrives
            try:
                self.rfile.read(max(0, min(int(self.headers.get("Content-Length") or 0), MAX_BODY)))
            except (ValueError, OSError):
                self.close_connection = True
        self._send(code, {"error": msg})
        return False

    def _cookie(self, key=L.COOKIE) -> str | None:
        # TP-178: not SimpleCookie, which silently drops every cookie after one it cannot parse (a JSON value
        # another service on the parent domain sets), so ours is looked up by name and the rest is ignored
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.partition("=")
            if name.strip() == key:
                return value.strip() or None
        return None

    @staticmethod
    def _cookie_attrs() -> str:
        return "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if L.public_url() else "")  # TP-166: served over https

    def _gate(self) -> bool:
        """TP-152, before anything else: who is asking (self.local, self.login, self.actor and core's actor for
        events) and may they. 127.0.0.1 passes as before; a private-network address needs lan_share on and a session
        (and Origin == Host on a POST); anything else gets 403. Docker mode (TP-164): 127.0.0.1 is no exception and
        lan_share does not matter: every visitor needs a session, admins are the TP_ADMINS logins."""
        kind = L.classify(self.client_address[0])
        dock = docker()
        self.local, self.login = kind == "local" and not dock, None
        self.admin = self.local
        self.actor = settings()["owner_name"] if self.local else None
        set_actor(self.actor)
        if self.local:
            return True
        if kind is None or not (dock or settings()["lan_share"]["enabled"]):
            return self._deny(403, "forbidden")
        if self.command == "POST" and not L.origin_ok(self.headers.get("Origin"), self.headers.get("Host")):
            return self._deny(403, "forbidden")
        self.login = self.actor = L.session_login(self._cookie(), L.org())
        self.admin = L.is_admin(self.login)
        set_actor(self.actor)
        path = self.path.split("?", 1)[0]
        if self.login or path in LAN_PUBLIC or not path.startswith("/api/"):
            return True
        return self._deny(401, "로그인이 필요해요")

    def do_GET(self):
        if docker() and self.path == "/api/update/health" and self.client_address[0] == "127.0.0.1":
            # The host manager probes via docker exec. Keep the candidate off SQLite until it is accepted.
            ready = (ROOT / "web" / "dist" / "index.html").is_file()
            return self._send(200 if ready else 503, {"ready": ready})
        if docker() and DU.maintenance():
            return self._send(503, {"error": "Docker 업데이트 중이에요. 잠시 뒤 다시 시도하세요"})
        if not self._gate():
            return
        if self.path == "/api/lan/me":
            me = {"local": self.local, "login": self.login, "actor": self.actor}
            return self._send(200, {**me, "docker": True, "admin": self.admin, "public_url": L.public_url()} if docker() else me)
        if self.path == "/api/lan/status":
            return self._send(200, lan_status(self.local, self.admin))
        if self.path == "/api/lan/tunnel":  # TP-166: Docker mode, TP_ADMINS only; host mode answers {"docker": false}
            if not docker():  # TP-172: host mode says so, the card points to Docker
                return self._send(200, {"docker": False})
            return self._send(200, L.tunnel_status()) if self.admin else self._send(403, {"error": "TP_ADMINS의 사람만 볼 수 있어요"})
        if not self.path.startswith("/api/"):
            return self._static()
        if self.path == "/api/github/status":
            return self._send(200, github_status())
        if self.path == "/api/agents/accounts":
            d = A.detect()
            if not self.admin:  # LAN visitors: no owner email / plan
                for v in d["tools"].values():
                    v["account"] = []
            return self._send(200, {**d, "admin": self.admin})
        if self.path == "/api/agents/usage":
            return self._send(200, agent_usage())
        if self.path.split("?", 1)[0] == "/api/metrics":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                mark_id = int(query["marker"][0]) if "marker" in query else None
                days = int(query.get("days", [MET.DEFAULT_DAYS])[0])
                rules = repo_rules()
                with closing(db()) as con:
                    con.execute("BEGIN")
                    data = MET.report(con, mark_id=mark_id, days=days, rules=rules)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, data)
        if self.path == "/api/agents/codex/login":  # TP-179: shows the one-time code, so admins only
            if not self.admin:
                return self._send(403, {"error": AGENT_LOGIN_403})
            from . import accounts
            try:
                st, done = accounts.codex_status()
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                return self._send(400, {"state": "failed", "error": f"로그인 상태를 확인하지 못했어요: {e}"})
            if done:
                con = db()
                event(con, "agent_login", "codex 로그인 (CLI 로그인)")
                con.commit()
            return self._send(200, st)
        if self.path == "/api/asana/status":
            return self._send(200, asana_status())
        if self.path == "/api/state":
            return self._send(200, state())
        if self.path == "/api/config":
            return self._send(200, G.api_state())
        if self.path == "/api/config/history":  # 모니터링 > 설정 이력 (TP-168): every scope, newest first
            return self._send(200, {"rows": G.history(limit=5000)})
        if self.path == "/api/themes":
            return self._send(200, TH.api())
        if self.path == "/api/guide":
            return self._send(200, {"text": GD.text()})
        if self.path == "/api/projects":
            return self._send(200, {"projects": CH.projects(), "notifications_unread": N.unread_count()})
        if self.path.split("?", 1)[0] == "/api/chat/compactions":
            from urllib.parse import parse_qs, urlsplit
            period = parse_qs(urlsplit(self.path).query).get("days", ["30"])[0]
            if period not in ("7", "30", "90", "all"):
                return self._send(400, {"error": "기간은 7일, 30일, 90일 또는 전체로 선택해 주세요"})
            return self._send(200, {"rows": CH.compaction_report(None if period == "all" else int(period))})
        if self.path.startswith("/api/onboard/"):
            # 프로젝트 추가 wizard (onboard.py): picker lists (?refresh=1 skips the cache) and clone job status (?id=)
            from urllib.parse import parse_qs, urlsplit
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            refresh = (q.get("refresh") or [""])[0] == "1"
            if u.path == "/api/onboard/github-repos":
                return self._send(200, OB.github_repos(refresh))
            if u.path == "/api/onboard/asana-projects":
                return self._send(200, OB.asana_projects(refresh))
            if u.path == "/api/onboard/clone":
                try:
                    job = OB.clone_status(int((q.get("id") or ["0"])[0]))
                except ValueError:
                    job = None
                return self._send(404, {"error": "not found"}) if job is None else self._send(200, job)
        if self.path.startswith(("/api/chat/conversations?", "/api/chat/messages?")):
            # 프로젝트 page 채팅 (chat.py); reading also finalizes turns whose agent has exited
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            try:
                if self.path.startswith("/api/chat/conversations?"):
                    return self._send(200, {"conversations": CH.conversations((q.get("project") or [""])[0])})
                return self._send(200, CH.messages((q.get("id") or [""])[0]))
            except SystemExit as e:
                return self._send(404, {"error": str(e)})
        if self.path == "/api/chat/inbox":
            return self._send(200, {"items": CH.inbox(), "notifications": N.unread()})
        if self.path.startswith("/api/chat/file?"):
            # a chat attachment (chat.file_path: only files inside data/chat-uploads/<conversation>/). Only images,
            # pdf and plain text open inline; anything else (html, svg, ...) downloads so it never runs as this origin
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            f = CH.file_path((q.get("id") or [""])[0], (q.get("name") or [""])[0])
            if not f:
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(f.name)[0] or ""
            inline = ctype in CH.IMAGES or ctype in ("application/pdf", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", (ctype + "; charset=utf-8" if ctype == "text/plain" else ctype)
                             if inline else "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f.name.split('-', 1)[-1])}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            return self.wfile.write(f.read_bytes())
        if self.path.startswith("/api/config/check-log?"):
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            try:
                body = G.trial_log(int((q.get("run") or ["0"])[0]), (q.get("name") or [None])[0])
            except ValueError:
                body = None
            return self._send(404, {"error": "not found"}) if body is None else self._send(200, {"log": body})
        if self.path == "/api/config/export":
            b = json.dumps(G.export(), ensure_ascii=False, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="ticket-pilot-config-{R._today()}.json"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(b)
        if self.path.startswith("/api/check-log?"):
            from urllib.parse import parse_qs, urlsplit
            con = db()
            try:
                got = K.read_log(con, int((parse_qs(urlsplit(self.path).query).get("id") or ["0"])[0]))
            except ValueError:
                got = None
            finally:
                con.close()
            if not got:
                return self._send(404, {"error": "not found"})
            row, body = got
            return self._send(200, {"check": row, "log": body})
        if self.path.split("?", 1)[0] == "/api/mode":
            if not self.admin:  # mount paths, admins: only for those who may switch
                return self._send(403, {"error": "실행 방식은 이 컴퓨터(127.0.0.1)나 TP_ADMINS만 볼 수 있어요"})
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                since = int(q["since"][0]) if q.get("since") else None
            except ValueError:
                return self._send(400, {"error": "since"})
            return self._send(200, mode_info(since))
        if self.path.split("?", 1)[0] == "/api/update":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                since = int(q["since"][0]) if q.get("since") else None
            except ValueError:
                return self._send(400, {"error": "since"})
            return self._send(200, update_info(since, admin=self.admin))
        if self.path == "/api/automations":
            # snapshot `tp tick` writes every minute (launchd)
            con = db()
            try:
                snap = meta_get(con, "automations")
            finally:
                con.close()
            return self._send(200, snap or {"error": "tp tick가 아직 실행되지 않았어요 (launchd/install.sh)"})
        if self.path == "/api/automation-runs":
            con = db()
            try:
                return self._send(200, {"runs": list(reversed(meta_get(con, "automation_runs") or []))})
            finally:
                con.close()
        if self.path == "/api/system-automations":
            # 시스템 자동화 page: launchd / cron / Codex app automations on this Mac, collected on request
            return self._send(200, SJ.collect())
        if self.path.startswith("/api/system-automations/log?"):
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            try:
                got = SJ.read_log((q.get("id") or [""])[0], int((q.get("i") or ["0"])[0]))
            except ValueError:
                got = None
            return self._send(404, {"error": "not found"}) if got is None else self._send(200, got)
        self._send(404, {"error": "not found"})

    def _config(self, fn, body: dict, **kw) -> list:
        """A settings change as this request's actor. lan_share only changes from 127.0.0.1: a LAN user's change is
        dry-run first and refused if it touches lan_share (an import that drops it included)."""
        dry = bool(body.get("dry_run"))
        if not self.admin and any(c["scope"] == "global" and c["key"].split(".")[0] == "lan_share"
                                  for c in fn(actor=self.actor, dry_run=True, **kw)):
            raise Forbidden("로컬망 공유 설정은 이 맥(127.0.0.1)에서만 바꿀 수 있어요")
        return fn(actor=self.actor, dry_run=dry, **kw)

    def _agent_login(self, tool: str, act: str, body: dict):
        """TP-179 에이전트 로그인 buttons: 127.0.0.1 only (Docker mode: TP_ADMINS). Codes, tokens and keys only go
        into accounts.py; the event says who logged which tool in or out, nothing else."""
        if not self.admin:
            return self._send(403, {"error": AGENT_LOGIN_403})
        try:
            if act == "login/cancel":
                A.cancel(tool)
                return self._send(200, {"ok": True})
            if act == "login":
                return self._send(200, {"codex": A.codex_start, "claude": A.claude_start, "agy": A.agy_start}[tool]())
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
                return self._send(404, {"error": "not found"})
            else:
                kind = {"login/code": A.claude_code, "token": A.claude_token, "apikey": A.claude_api_key}[act](
                    body.get("key" if act == "apikey" else "code" if act == "login/code" else "token"))
                msg = f"claude 로그인 ({'API 키' if kind == A.API_KEY else 'tp 토큰'})"
        except RuntimeError as e:
            return self._send(400, {"error": str(e)})
        con = db()
        event(con, "agent_login", msg)
        con.commit()
        return self._send(200, {"ok": True})

    def do_POST(self):
        if docker() and DU.maintenance():
            self._deny(503, "Docker 업데이트 중이에요. 잠시 뒤 다시 시도하세요")
            return
        if not self._gate():
            return
        # same-origin guard on 127.0.0.1 (as before TP-152; LAN requests passed the stricter check in _gate)
        from urllib.parse import urlsplit
        origin = self.headers.get("Origin") or ""
        n = int(self.headers.get("Content-Length") or 0)
        if self.local and origin and not (origin.startswith("http://") and urlsplit(origin).hostname in ("127.0.0.1", "localhost")):
            self.rfile.read(min(n, MAX_BODY))  # drain first: closing with unread data resets the socket before the 403 arrives
            return self._send(403, {"error": "forbidden"})
        if n > MAX_BODY:
            # chat attachments arrive base64 in the JSON body (chat.MAX_FILES x MAX_FILE is checked per file there)
            return self._send(413, {"error": f"요청이 너무 커요 ({n / 1048576:.0f}MB, 한 번에 {MAX_BODY // 1048576}MB까지)"})
        body = json.loads(self.rfile.read(n) or b"{}")
        if docker() and DU.directory():
            with (DATA / "docker-update.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                if DU.maintenance():
                    return self._send(503, {"error": "Docker 업데이트 중이에요. 잠시 뒤 다시 시도하세요"})
                return self._post(body)
        return self._post(body)

    def _post(self, body):
        try:
            if self.path.startswith("/api/notion/"):
                return self._notion(self.path.removeprefix("/api/notion/"), body)
            if self.path in ("/api/config", "/api/config/import"):
                # 설정 page: dry_run returns the changes for the confirm step; invalid values come back as errors
                if self.path == "/api/config" and "updates" in body:  # several scopes as one change (프로젝트 추가)
                    ch = self._config(G.update_many, body, items=body["updates"], dashboard=True)
                elif self.path == "/api/config":
                    ch = self._config(G.update, body, scope=body["scope"], value=body.get("value"),
                                      create=bool(body.get("create")), dashboard=True)
                else:
                    ch = self._config(G.import_doc, body, doc=G._read_doc(json.dumps(body.get("doc"))))
                return self._send(200, {"ok": True, "changes": ch})
            if self.path in ("/api/lan/login", "/api/lan/login/poll") and self.local:
                raise SystemExit("이 맥(127.0.0.1)에서는 로그인하지 않아요")
            seen = L.seen_ip(self.client_address[0], self.headers.get("Cf-Connecting-IP"))
            if self.path == "/api/lan/login":
                return self._send(200, L.login_start(self.client_address[0], seen))
            if self.path == "/api/lan/login/poll":
                r = L.login_poll(str(body.get("id") or ""), self.client_address[0], L.org(), seen)
                if "sid" not in r:
                    return self._send(200, r)
                set_actor(r["login"])
                con = db()
                event(con, "login", f"{r['login']} logged in from {seen}")
                con.commit()
                return self._send(200, {"login": r["login"]}, headers=[("Set-Cookie", (
                    f"{L.COOKIE}={r['sid']}; {self._cookie_attrs()}; Max-Age={L.MAX_DAYS * 86400}"))])
            if self.path == "/api/lan/logout":
                L.end_session(sid=self._cookie())
                return self._send(200, {"ok": True}, headers=[("Set-Cookie", (
                    f"{L.COOKIE}=; {self._cookie_attrs()}; Max-Age=0"))])
            if self.path in ("/api/lan/share", "/api/lan/sessions/delete"):
                if not self.admin:
                    return self._send(403, {"error": "로컬망 공유 설정과 강제 로그아웃은 이 맥(127.0.0.1)에서만 할 수 있어요"
                                                    + (" (Docker 모드: TP_ADMINS의 사람만)" if docker() else "")})
                if self.path == "/api/lan/share":
                    return self._send(200, lan_share_set(body.get("enabled"), body.get("org"), self.actor))
                if not isinstance(body.get("id"), int):
                    raise SystemExit("id")
                L.end_session(rowid=body["id"])
                return self._send(200, {"ok": True})
            if self.path in ("/api/mode", "/api/mode/preview"):
                if not self.admin:
                    raise Forbidden("실행 방식은 이 컴퓨터(127.0.0.1)에서만 바꿀 수 있어요 (Docker 모드: TP_ADMINS의 사람만)")
                if self.path == "/api/mode/preview":
                    return self._send(200, mode_preview(body.get("to"), body.get("admins")))
                since = start_mode(body.get("to"), body.get("admins"))
                con = db()
                event(con, "config", f"실행 방식 전환 시작: tp mode {body.get('to')} --apply")
                con.commit()
                return self._send(200, {"ok": True, "since": since})
            if self.path == "/api/update":
                if docker():
                    if not self.admin:
                        raise Forbidden("Docker 업데이트는 TP_ADMINS에 등록된 관리자만 실행할 수 있어요")
                    if DU.directory():
                        con = db()
                        try:
                            tag = (meta_get(con, "update_check") or {}).get("latest")
                            job = DU.request(tag)
                            event(con, "update", f"Docker 업데이트 요청: {job['tag']}")
                            con.commit()
                        except UP.UpdateError as e:
                            return self._send(409, {"error": str(e)})
                        finally:
                            con.close()
                        return self._send(200, {"ok": True, "job": job})
                since = start_update()
                con = db()
                event(con, "update", "업데이트 실행 (tp update)")
                con.commit()
                return self._send(200, {"ok": True, "since": since})
            if self.path == "/api/update/check":
                try:
                    return self._send(200, check_update(admin=self.admin))
                except UP.UpdateError as e:
                    return self._send(502, {"error": str(e)})
            m = AGENT_LOGIN_PATH.fullmatch(self.path)
            if m:
                return self._agent_login(m.group(1), m.group(2), body)
            if self.path == "/api/github/device":
                return self._send(200, github_device_start())
            if self.path == "/api/github/device/poll":
                return self._send(200, github_device_poll())
            if self.path == "/api/github/disconnect":
                github_disconnect()
                return self._send(200, {"ok": True})
            if self.path == "/api/asana/token":
                return self._send(200, asana_save(body.get("token")))
            if self.path == "/api/asana/disconnect":
                asana_disconnect()
                return self._send(200, {"ok": True})
            if self.path == "/api/config/check-run":
                return self._send(200, {"ok": True, "id": G.start_check_run(body["repo"])})
            if self.path == "/api/onboard/check-path":
                return self._send(200, OB.check_path(body.get("path"), body.get("github")))
            if self.path == "/api/onboard/clone":
                return self._send(200, OB.start_clone(body.get("github"), body.get("name")))
            if self.path == "/api/chat/new":
                return self._send(200, {"ok": True, "id": CH.create(body["project"], body.get("repo") or None)})
            if self.path == "/api/chat/send":
                turn = CH.send(body["id"], body.get("text") or "", body.get("files") or None)
                con = db()
                event(con, "chat", f"chat {body['id']}: message sent")
                con.commit()
                return self._send(200, {"ok": True, "turn": turn})
            if self.path == "/api/chat/rename":
                CH.rename(body["id"], body.get("title") or "")
                return self._send(200, {"ok": True})
            if self.path == "/api/chat/stop":
                CH.stop(body["id"])
                return self._send(200, {"ok": True})
            if self.path == "/api/chat/read":
                CH.read(body["id"], body.get("upto"))
                return self._send(200, {"ok": True})
            if self.path == "/api/chat/read-all":
                upto = body.get("notifications_upto")  # 수신함 alerts listed with them (TP-105)
                if upto is not None:
                    N.check_id(upto)  # before the chat marks commit
                CH.read_all(body.get("items"))
                if upto is not None:
                    N.read_all(upto)
                return self._send(200, {"ok": True})
            if self.path == "/api/notifications/read":
                N.read(body.get("id"))
                return self._send(200, {"ok": True})
            if self.path == "/api/chat/delete":
                CH.delete(body["id"])
                return self._send(200, {"ok": True})
            # 마일스톤 dev 서버 (devserver.py): check = worktree + .env check + port owner, start refuses a port in use
            # (409 with the processes and a free port) unless kill
            if self.path == "/api/dev/check":
                return self._send(200, {"ok": True, **DS.check(body["gid"])})
            if self.path == "/api/dev/start":
                try:
                    return self._send(200, {"ok": True, "server": DS.start(body["gid"], body.get("port") or None,
                                                                          kill=bool(body.get("kill")))})
                except DS.PortBusy as e:
                    return self._send(409, {"error": str(e), "conflict": e.info})
            if self.path == "/api/dev/stop":
                DS.stop(body["gid"])
                return self._send(200, {"ok": True})
            if self.path == "/api/assign":
                P.assign(body["ticket"], body.get("repo") or None)
            elif self.path == "/api/skip":
                P.set_skip(body["ticket"], bool(body.get("skip", True)))
            elif self.path == "/api/reply":
                # only records the answer; the host prechecks (every 5 min) relaunch the agent with it
                P.request_reply(body["ticket"], body.get("text") or "")
            elif self.path == "/api/drop":
                P.drop(body["ticket"])
            elif self.path == "/api/milestone-done":
                # only records it; the host tick opens the milestone PRs (milestone.py)
                MS.mark_done(body["parent"], cancel=bool(body.get("cancel")))
            elif self.path == "/api/approve":
                # only records the request; the host prechecks (every 5 min) do the actual staging merge
                # override: the owner's reason to merge although tp checks did not pass (stored + logged as check_override)
                # override_review: their reason to merge although the AI review said blocked/changes (TP-99, that head only)
                R.request_approval(body["ticket"], cancel=bool(body.get("cancel")), override=body.get("override") or None,
                                   override_review=body.get("override_review") or None)
            elif self.path == "/api/check-override":
                # The owner ignores the tp checks of the open head with a reason: it goes to the AI review, not back to the agent
                R.override_checks(body["ticket"], body.get("reason") or "")
            elif self.path == "/api/deploy":
                # only records the request; the host prechecks (every 5 min) run it
                D.request_deploy(body["mode"], body.get("ticket") or None, body.get("repo") or None, pr_url=body.get("pr_url"))
            elif self.path == "/api/deploy-cancel":
                D.cancel_deploy(int(body["id"]))
            elif self.path == "/api/metrics/mark":
                with closing(db()) as con:
                    return self._send(201, MET.create_marker(con, body.get("label")))
            elif self.path == "/api/view":
                # tab visits for pruning unused pages (TP-136); shortcut: delete with the cleanup ticket around 2026-11-26
                tab = body.get("tab")
                if not isinstance(tab, str) or not tab or len(tab) > 100:
                    return self._send(400, {"error": "tab"})
                con = db()
                event(con, "view", tab)
                con.commit()
            elif self.path == "/api/release-hold":
                con = db()
                meta_set(con, "release_hold", bool(body.get("hold")))
                con.commit()
            elif self.path == "/api/refresh":
                if not _sync_lock.acquire(blocking=False):
                    return self._send(409, {"error": "already running"})
                try:
                    P.sync(log=lambda *_: None)
                    P.scan(log=lambda *_: None)
                finally:
                    _sync_lock.release()
            else:
                return self._send(404, {"error": "not found"})
        except G.ConfigError as e:
            return self._send(400, {"error": str(e), "errors": e.errors})
        except notion.NotionError as e:
            return self._send(400, {"error": str(e)})
        except Forbidden as e:
            return self._send(403, {"error": str(e)})
        except L.LoginError as e:
            return self._send(400, {"error": str(e)})
        except SystemExit as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
        self._send(200, {"ok": True})

    def _notion(self, action, body):
        if action in ("connect", "finish") and not self.admin:
            raise Forbidden("Notion 계정 연결은 이 컴퓨터 또는 Docker 관리자만 할 수 있어요")
        if action == "connect":
            origin = self.headers.get("Origin")
            if not L.origin_ok(origin, self.headers.get("Host")):
                raise Forbidden("대시보드에서 연결을 시작하세요")
            url, state, proof = notion.start(self.actor, origin + notion.CALLBACK)
            attrs = "HttpOnly; SameSite=Strict; Path=/api/notion/finish" + ("; Secure" if origin.startswith("https:") else "")
            return self._send(200, {"url": url}, headers=[
                ("Set-Cookie", f"tp_notion_{state}={proof}; Max-Age={notion.TTL}; {attrs}")])
        with closing(db()) as con:
            if action == "finish":
                state = body.get("state")
                result = notion.finish(con, self.actor, state, self._cookie(f"tp_notion_{state}"), body.get("code"))
            elif action == "accounts":
                result = {**notion.accounts(con), "admin": self.admin}
            elif action == "preview":
                repos = body.get("repos")
                if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
                    raise notion.NotionError("프로젝트 레포 목록을 확인하세요")
                draft = {"id": body.get("project_id"), "name": "", "notion": body.get("notion"), "repos": repos}
                result = notion_tickets.preview(con, project_map()["projects"], draft, settings()["asana_tag"])
            elif action in ("sources", "people", "properties", "rows"):
                client = notion.Client(con, body.get("connection_id"))
                if action == "sources":
                    result = client.sources()
                elif action == "people":
                    result = client.people()
                elif action == "rows":
                    result = client.rows(body.get("data_source_id"))
                else:
                    result = client.properties(body.get("data_source_id"))
            else:
                return self._send(404, {"error": "not found"})
        return self._send(200, result)


def serve(port: int | None = None, block: bool = True) -> ThreadingHTTPServer:
    """block False (tp run): serve in a daemon thread and return the server."""
    why = L.docker_problem()
    if why:
        raise SystemExit(why)
    port = port or int(os.environ.get("TP_PORT") or settings()["dashboard_port"])
    host = os.environ.get("TP_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), H)
    print(f"ticket-pilot dashboard on http://{host}:{port}" + (" (Docker 모드: 모든 접속에 GitHub 로그인)" if docker() else ""),
          flush=True)
    if block:
        srv.serve_forever()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
