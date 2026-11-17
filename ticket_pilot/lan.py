"""로컬망 공유 (TP-152): teammates on the office network use the dashboard after a GitHub login.

- 127.0.0.1 / ::1 pass without a login, as before. Other addresses must be private (10/8, 172.16/12, 192.168/16, IPv6
  ULA and link-local) and carry a session cookie of an active member of settings lan_share.org.
- Login is the GitHub Device Flow with scope read:org, one pending login per browser (not server.py's single slot for
  tp's own GitHub connection). The user's token is used for GET /user and /user/memberships/orgs/<org>, then dropped:
  never stored, logged or mixed with tp's token.
- Sessions: a random cookie; the DB keeps only its sha256 (core sessions table). 14 days at most, 7 idle days end it.
  Once a day the membership is checked again with tp's own GitHub token; a non-member (or a failed check) loses the
  session and logs in again.
- Docker mode (TP-164, core.docker()): every address logs in, 127.0.0.1 too. Allowed: the GitHub logins in TP_ADMINS
  (comma separated) and, with TP_ORG set, that org's members. Admins also do what only 127.0.0.1 does on a host install.
- Public address (TP-166, Docker mode only): TP_PUBLIC_URL (https://tp.example.com) is where the cloudflared tunnel
  (compose profile tunnel) serves the dashboard. With it the cookie gets Secure and a POST's Origin must be that address
  or http://127.0.0.1:<port>. Cf-Connecting-IP only goes to the sessions list, the login event and the login rate limit.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .core import db, docker, github_token, now, settings

PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10")]
COOKIE = "tp_session"
TUNNEL_READY = "http://cloudflared:2000/ready"  # docker/compose.yaml: cloudflared --metrics 0.0.0.0:2000
MAX_DAYS, IDLE_DAYS, RECHECK = 14, 7, timedelta(days=1)
LOGINS_PER_MIN, POLLS_PER_MIN = 5, 30  # per IP

_lock = threading.Lock()
_pending: dict[str, dict] = {}  # pending login id -> {cid, code, interval, exp, ip}; a dashboard restart drops them
_hits: dict[tuple, list[float]] = {}


class LoginError(RuntimeError):
    pass


def admins() -> set[str]:
    return {x.strip().lower() for x in os.environ.get("TP_ADMINS", "").split(",") if x.strip()}


def org() -> str:
    """The org whose members may log in: TP_ORG in Docker mode, else the lan_share setting."""
    return os.environ.get("TP_ORG", "").strip() if docker() else settings()["lan_share"]["org"]


def is_admin(login: str | None) -> bool:
    return bool(login) and docker() and login.lower() in admins()


def docker_problem() -> str | None:
    """Why the Docker-mode dashboard must not start (nobody could log in), or None."""
    if docker() and not admins() and not org():
        return "TP_ADMINS와 TP_ORG가 둘 다 비어 있어서 아무도 로그인할 수 없어요. docker/.env에 TP_ADMINS(GitHub 아이디, 쉼표 구분)를 넣으세요"
    if docker() and os.environ.get("TP_PUBLIC_URL", "").strip() and not public_url():
        return "TP_PUBLIC_URL은 https://tp.example.com처럼 https 주소만 (경로 없이) 넣어야 해요"
    return None


def public_url() -> str | None:
    """TP_PUBLIC_URL as an origin (https://host[:port], no path), Docker mode only; None when unset or invalid."""
    raw = os.environ.get("TP_PUBLIC_URL", "").strip().rstrip("/")
    if not (docker() and raw):
        return None
    try:
        p = urlsplit(raw)
        p.port  # raises on a bad port
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or "@" in p.netloc or p.path or p.query or p.fragment:
        return None
    return f"https://{p.hostname}" + (f":{p.port}" if p.port not in (None, 443) else "")  # as browsers send Origin


def origin_ok(origin: str | None, host: str | None) -> bool:
    """A POST's Origin: the page's own address (Origin == Host); with TP_PUBLIC_URL only that or http://127.0.0.1:<port>."""
    pub = public_url()
    if origin and origin == pub:
        return True
    return bool(origin) and origin == f"http://{host}" and (not pub or urlsplit(origin).hostname == "127.0.0.1")


def seen_ip(peer: str, cf: str | None) -> str:
    """Who to record and rate limit: Cf-Connecting-IP when it is an IP and the peer is not 127.0.0.1 (behind the tunnel
    the peer is cloudflared, and Cloudflare overwrites the header), else the peer. Never used to let anyone in."""
    if classify(peer) == "local":
        return peer
    try:
        return str(ipaddress.ip_address((cf or "").strip()))
    except ValueError:
        return peer


def tunnel_status() -> dict:
    """설정 → 고급 "외부 공개": the cloudflared container's /ready (only reachable with the compose profile tunnel)."""
    st = {"public_url": public_url(), "running": False, "connections": 0}
    try:
        with urllib.request.urlopen(TUNNEL_READY, timeout=3) as r:
            body = r.read()
    except urllib.error.HTTPError as e:  # 503: cloudflared runs but has no connection to Cloudflare
        body = e.read()
    except (OSError, ValueError):
        return st
    try:
        n = int(json.loads(body or b"{}").get("readyConnections") or 0)
    except (ValueError, TypeError, AttributeError):
        n = 0
    return {**st, "running": True, "connections": n}


def classify(addr: str) -> str | None:
    """'local' (loopback), 'lan' (private network) or None (anything else: refused)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "local"
    return "lan" if any(ip in n for n in PRIVATE) else None


def address() -> str | None:
    """This Mac's private IPv4 address (the default route's interface; a UDP connect sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip if classify(ip) == "lan" else None


def _ts(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")  # same format as core.now()


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _con():
    """db() opens a new connection per call outside the main thread (the dashboard's request threads): close those."""
    con = db()
    return con, threading.current_thread() is not threading.main_thread()


def _api(path: str, tok: str) -> dict:
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def _still_member(login: str, org: str) -> bool:
    """Daily re-check with tp's own token: 204 = member. No token, 404, a redirect to the public list that says no, or
    a network error all end the session (the user logs in again, which checks with their own token)."""
    try:
        tok = github_token()
    except RuntimeError:  # secrets.json unreadable: like no token
        return False
    if not tok:
        return False
    try:
        _api(f"/orgs/{org}/members/{login}", tok)
        return True
    except (urllib.error.HTTPError, OSError, ValueError):
        return False


def session_login(sid: str | None, org: str) -> str | None:
    """The GitHub login of a live session, else None. Expired, other-org and no-longer-member sessions are deleted."""
    if not sid:
        return None
    h = _hash(sid)
    con, close = _con()
    try:
        r = con.execute("SELECT * FROM sessions WHERE id_hash=?", (h,)).fetchone()
        if not r:
            return None
        stale = r["checked_at"] < _ts(RECHECK) and not is_admin(r["login"])
        if (r["org"] != org or r["created_at"] < _ts(timedelta(days=MAX_DAYS))
                or r["last_seen"] < _ts(timedelta(days=IDLE_DAYS))
                or not (is_admin(r["login"]) or org)  # Docker mode: dropped from TP_ADMINS, no org
                or (stale and not _still_member(r["login"], org))):
            con.execute("DELETE FROM sessions WHERE id_hash=?", (h,))
            con.commit()
            return None
        if stale or r["last_seen"] < _ts(timedelta(minutes=1)):  # one write a minute at most
            t = now()
            con.execute("UPDATE sessions SET last_seen=?, checked_at=? WHERE id_hash=?", (t, t if stale else r["checked_at"], h))
            con.commit()
        return r["login"]
    finally:
        if close:
            con.close()


def new_session(login: str, org: str, ip: str) -> str:
    sid = secrets.token_urlsafe(32)
    t = now()
    con, close = _con()
    try:
        con.execute("DELETE FROM sessions WHERE created_at < ? OR last_seen < ?",  # expired ones of anyone
                    (_ts(timedelta(days=MAX_DAYS)), _ts(timedelta(days=IDLE_DAYS))))
        con.execute("INSERT INTO sessions(id_hash,login,org,created_at,last_seen,checked_at,ip) VALUES(?,?,?,?,?,?,?)",
                    (_hash(sid), login, org, t, t, t, ip))
        con.commit()
    finally:
        if close:
            con.close()
    return sid


def end_session(sid: str | None = None, rowid: int | None = None):
    """Log out (own cookie) or, from 127.0.0.1, force out a listed session (its rowid)."""
    con, close = _con()
    try:
        if sid:
            con.execute("DELETE FROM sessions WHERE id_hash=?", (_hash(sid),))
        elif rowid is not None:
            con.execute("DELETE FROM sessions WHERE rowid=?", (int(rowid),))
        con.commit()
    finally:
        if close:
            con.close()


def sessions() -> list[dict]:
    con, close = _con()
    try:
        cut_max, cut_idle = _ts(timedelta(days=MAX_DAYS)), _ts(timedelta(days=IDLE_DAYS))
        return [dict(r) for r in con.execute(
            "SELECT rowid AS id, login, org, created_at, last_seen, ip FROM sessions "
            "WHERE created_at >= ? AND last_seen >= ? ORDER BY last_seen DESC", (cut_max, cut_idle))]
    finally:
        if close:
            con.close()


def _limit(key: tuple, n: int):
    t = time.time()
    with _lock:
        h = [x for x in _hits.get(key, []) if x > t - 60]
        if len(h) >= n:
            _hits[key] = h
            raise LoginError("로그인 시도가 너무 잦아요. 1분 뒤에 다시 해 주세요")
        _hits[key] = h + [t]


def login_start(ip: str, seen: str | None = None) -> dict:
    """A GitHub device code for this browser; the device_code stays here under a random pending id. The rate limit is
    per seen_ip: behind the tunnel every visitor's peer is cloudflared, one shared bucket would let anyone lock out all."""
    from .server import _client_id, _github_form
    _limit(("start", seen or ip), LOGINS_PER_MIN)
    cid = _client_id()
    j = _github_form("https://github.com/login/device/code", {"client_id": cid, "scope": "read:org"})
    if not j.get("device_code") or not j.get("user_code"):
        raise LoginError(f"GitHub가 코드를 주지 않았어요: {j.get('error_description') or j.get('error') or '알 수 없음'}")
    expires, interval = int(j.get("expires_in") or 900), int(j.get("interval") or 5)
    pid = secrets.token_urlsafe(16)
    with _lock:
        for k in [k for k, d in _pending.items() if d["exp"] < time.time()]:
            del _pending[k]
        _pending[pid] = {"cid": cid, "code": j["device_code"], "interval": interval, "exp": time.time() + expires, "ip": ip}
    return {"id": pid, "user_code": j["user_code"], "expires_in": expires, "interval": interval,
            "verification_uri": j.get("verification_uri") or "https://github.com/login/device"}


def _member_login(tok: str, org: str) -> str:
    login = _api("/user", tok).get("login")
    if is_admin(login):
        return login
    if not org:  # Docker mode without TP_ORG
        raise LoginError(f"{login or '이 계정'}은(는) 이 대시보드에 로그인할 수 없어요 (TP_ADMINS에 없어요)")
    try:
        m = _api(f"/user/memberships/orgs/{org}", tok)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise LoginError(f"{login or '이 계정'}은(는) GitHub 조직 {org}의 멤버가 아니에요") from None
        raise
    if not login or m.get("state") != "active":
        raise LoginError(f"{login or '이 계정'}은(는) GitHub 조직 {org}의 활성 멤버가 아니에요 (상태: {m.get('state')})")
    return login


def login_poll(pid: str, ip: str, org: str, seen: str | None = None) -> dict:
    """{"pending": True, "interval"} until GitHub hands out the token; then the org check and {"login", "sid"}.
    The token is only used for the two checks. ip (the peer) binds the login; seen (seen_ip) is recorded and rate limited."""
    from .server import DEVICE_ERRORS, _github_form
    _limit(("poll", seen or ip), POLLS_PER_MIN)
    with _lock:
        d = dict(_pending.get(pid) or {})
    if not d or d["ip"] != ip:
        raise LoginError("진행 중인 로그인이 없어요. 다시 시도해 주세요")
    if d["exp"] < time.time():
        _pending.pop(pid, None)
        raise LoginError("코드가 만료됐어요. 다시 시도해 주세요")
    try:
        j = _github_form("https://github.com/login/oauth/access_token", {
            "client_id": d["cid"], "device_code": d["code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except OSError:
        return {"pending": True, "interval": d["interval"]}
    err = j.get("error")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            d["interval"] = int(j.get("interval") or d["interval"] + 5)
            with _lock:
                if pid in _pending:
                    _pending[pid]["interval"] = d["interval"]
        return {"pending": True, "interval": d["interval"]}
    _pending.pop(pid, None)
    if err:
        raise LoginError(DEVICE_ERRORS.get(err) or f"로그인하지 못했어요: {j.get('error_description') or err}")
    tok = j.get("access_token")
    if not tok:
        raise LoginError("GitHub가 토큰을 보내지 않았어요")
    try:
        login = _member_login(tok, org)
    except (urllib.error.HTTPError, OSError, ValueError) as e:
        raise LoginError(f"GitHub에서 조직 멤버인지 확인하지 못했어요: {e}") from None
    return {"login": login, "sid": new_session(login, org, seen or ip)}
