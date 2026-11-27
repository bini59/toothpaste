"""Local dashboard: http://127.0.0.1:<port> (read-mostly view over the SQLite DB)."""
from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import docker_update as DU
from . import lan as L
from .config import ConfigError
from .core import DATA, ROOT, docker, set_actor, settings
from .http_api import Forbidden, routes
from .notion import NotionError

# Existing Python entry points used by the CLI, integrations and tests.
from .http_api.connections import (  # noqa: F401
    DEVICE_ERRORS, GITHUB_CLIENT_ID, GITHUB_OAUTH_FILE, GITHUB_OAUTH_KEYCHAIN, _client_id, _github_form,
    agent_usage, asana_disconnect, asana_save, asana_status, github_device_poll, github_device_start,
    github_disconnect, github_status, lan_share_set, lan_status,
)
from .http_api.operations import (  # noqa: F401
    DOCKER_UPDATE, _mode_argv, check_update, mode_info, mode_preview, start_mode, start_update, update_info,
)
from .http_api.tickets import _state, factory_repo, state, version  # noqa: F401

MAX_BODY = 64 * 1024 * 1024
MAINTENANCE_ERROR = "Docker 업데이트 중이에요. 잠시 뒤 다시 시도하세요"


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
        if self.login or path in routes.PUBLIC_PATHS or not path.startswith("/api/"):
            return True
        return self._deny(401, "로그인이 필요해요")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        finally:
            # Keep the actor through dispatch, event writes and the response flush.
            set_actor(None)

    def query(self):
        return parse_qs(urlsplit(self.path).query)

    def _dispatch(self, *args):
        route = routes.match(self.command, self.path)
        if route is None or route.docker_probe:
            return self._send(404, {"error": "not found"})
        if not self.admin:
            error = route.admin_error
            if route.docker_admin_error and docker():
                error = route.docker_admin_error
            if error:
                return self._send(403, {"error": error})
        return route.handler(self, *args)

    def do_GET(self):
        if docker():
            if self.client_address[0] == "127.0.0.1":
                probe = routes.ROUTES["GET"].get(self.path)
                if probe is not None and probe.docker_probe:
                    return probe.handler(self)
            if DU.maintenance():
                return self._send(503, {"error": MAINTENANCE_ERROR})
        if not self._gate():
            return
        if not self.path.startswith("/api/"):
            return self._static()
        return self._dispatch()

    def do_POST(self):
        if docker() and DU.maintenance():
            self._deny(503, MAINTENANCE_ERROR)
            return
        if not self._gate():
            return
        # same-origin guard on 127.0.0.1 (as before TP-152; LAN requests passed the stricter check in _gate)
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
                    return self._send(503, {"error": MAINTENANCE_ERROR})
                return self._post(body)
        return self._post(body)

    def _post(self, body):
        try:
            return self._dispatch(body)
        except ConfigError as e:
            return self._send(400, {"error": str(e), "errors": e.errors})
        except NotionError as e:
            return self._send(400, {"error": str(e)})
        except Forbidden as e:
            return self._send(403, {"error": str(e)})
        except L.LoginError as e:
            return self._send(400, {"error": str(e)})
        except SystemExit as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})


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
