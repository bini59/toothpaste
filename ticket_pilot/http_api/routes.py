"""Method, path and access policy for every dashboard API endpoint."""
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from typing import Callable
from urllib.parse import urlsplit

from . import chat, connections, operations, settings, tickets


class Query(Enum):
    EXACT = auto()
    OPTIONAL = auto()
    REQUIRED = auto()


@dataclass(frozen=True)
class Route:
    handler: Callable
    query: Query = Query.EXACT
    public: bool = False
    admin_error: str | None = None
    docker_admin_error: str | None = None
    docker_probe: bool = False


AGENT_LOGIN_403 = "에이전트 로그인은 이 컴퓨터(127.0.0.1)에서만 할 수 있어요 (Docker 모드: TP_ADMINS의 사람만)"
LAN_ADMIN_403 = "로컬망 공유 설정과 강제 로그아웃은 이 맥(127.0.0.1)에서만 할 수 있어요"
MODE_ADMIN_403 = "실행 방식은 이 컴퓨터(127.0.0.1)에서만 바꿀 수 있어요 (Docker 모드: TP_ADMINS의 사람만)"
NOTION_ADMIN_403 = "Notion 계정 연결은 이 컴퓨터 또는 Docker 관리자만 할 수 있어요"

# By default, an API request needs a session (or a host-mode loopback peer).
# Public paths still pass the network and Origin checks. The Docker probe runs before those checks.
ROUTES = {
    "GET": {
        "/api/lan/me": Route(connections.get_me, public=True),
        "/api/lan/status": Route(connections.get_lan_status),
        "/api/lan/tunnel": Route(connections.get_tunnel, docker_admin_error="TP_ADMINS의 사람만 볼 수 있어요"),
        "/api/github/status": Route(connections.get_github_status),
        "/api/asana/status": Route(connections.get_asana_status),
        "/api/agents/accounts": Route(connections.get_accounts),  # account names are redacted for members
        "/api/agents/usage": Route(connections.get_agent_usage),
        "/api/agents/codex/login": Route(connections.get_codex_login, admin_error=AGENT_LOGIN_403),
        "/api/config": Route(settings.get_config),
        "/api/config/history": Route(settings.get_config_history),
        "/api/config/check-log": Route(settings.get_check_log, Query.REQUIRED),
        "/api/config/export": Route(settings.get_export),
        "/api/themes": Route(settings.get_themes),
        "/api/guide": Route(settings.get_guide),
        "/api/projects": Route(settings.get_projects),
        "/api/onboard/github-repos": Route(settings.get_github_repos, Query.OPTIONAL),
        "/api/onboard/asana-projects": Route(settings.get_asana_projects, Query.OPTIONAL),
        "/api/onboard/clone": Route(settings.get_clone, Query.OPTIONAL),
        "/api/chat/conversations": Route(chat.get_conversations, Query.REQUIRED),
        "/api/chat/messages": Route(chat.get_messages, Query.REQUIRED),
        "/api/chat/file": Route(chat.get_file, Query.REQUIRED),
        "/api/chat/inbox": Route(chat.get_inbox),
        "/api/chat/compactions": Route(chat.get_compactions, Query.OPTIONAL),
        "/api/state": Route(tickets.get_state),
        "/api/reviews": Route(tickets.get_reviews),
        "/api/check-log": Route(tickets.get_check_log, Query.REQUIRED),
        "/api/events": Route(operations.get_events),
        "/api/metrics": Route(operations.get_metrics, Query.OPTIONAL),
        "/api/mode": Route(operations.get_mode, Query.OPTIONAL,
                           admin_error="실행 방식은 이 컴퓨터(127.0.0.1)나 TP_ADMINS만 볼 수 있어요"),
        "/api/update": Route(operations.get_update, Query.OPTIONAL),
        "/api/update/health": Route(operations.get_health, docker_probe=True),
        "/api/automations": Route(operations.get_automations),
        "/api/automation-runs": Route(operations.get_automation_runs),
        "/api/system-automations": Route(operations.get_system_automations),
        "/api/system-automations/log": Route(operations.get_system_automation_log, Query.REQUIRED),
    },
    "POST": {
        "/api/lan/login": Route(connections.post_login, public=True),  # LAN and Docker login only
        "/api/lan/login/poll": Route(connections.post_login_poll, public=True),
        "/api/lan/logout": Route(connections.post_logout),
        "/api/lan/share": Route(connections.post_lan_share, admin_error=LAN_ADMIN_403,
                                docker_admin_error=LAN_ADMIN_403 + " (Docker 모드: TP_ADMINS의 사람만)"),
        "/api/lan/sessions/delete": Route(connections.post_session_delete, admin_error=LAN_ADMIN_403,
                                          docker_admin_error=LAN_ADMIN_403 + " (Docker 모드: TP_ADMINS의 사람만)"),
        "/api/github/device": Route(connections.post_github_device),
        "/api/github/device/poll": Route(connections.post_github_device_poll),
        "/api/github/disconnect": Route(connections.post_github_disconnect),
        "/api/asana/token": Route(connections.post_asana_token),
        "/api/asana/disconnect": Route(connections.post_asana_disconnect),
        # Keep the admin check even for tool/action combinations that answer 404 after authorization.
        **{f"/api/agents/{tool}/{act}": Route(partial(connections.post_agent_login, tool=tool, act=act),
                                             admin_error=AGENT_LOGIN_403)
           for tool in ("claude", "codex", "agy")
           for act in ("login", "login/code", "login/cancel", "token", "apikey", "logout")},
        "/api/config": Route(settings.post_config),  # changes touching lan_share also require admin
        "/api/config/import": Route(settings.post_import),
        "/api/config/check-run": Route(settings.post_check_run),
        "/api/onboard/check-path": Route(settings.post_check_path),
        "/api/onboard/clone": Route(settings.post_clone),
        "/api/notion/connect": Route(settings.post_notion_connect, admin_error=NOTION_ADMIN_403),  # also checks Origin
        "/api/notion/finish": Route(settings.post_notion_finish, admin_error=NOTION_ADMIN_403),
        "/api/notion/accounts": Route(settings.post_notion_accounts),
        "/api/notion/preview": Route(settings.post_notion_preview),
        "/api/notion/sources": Route(settings.post_notion_sources),
        "/api/notion/people": Route(settings.post_notion_people),
        "/api/notion/properties": Route(settings.post_notion_properties),
        "/api/notion/rows": Route(settings.post_notion_rows),
        "/api/chat/new": Route(chat.post_new),
        "/api/chat/send": Route(chat.post_send),
        "/api/chat/rename": Route(chat.post_rename),
        "/api/chat/stop": Route(chat.post_stop),
        "/api/chat/read": Route(chat.post_read),
        "/api/chat/read-all": Route(chat.post_read_all),
        "/api/chat/delete": Route(chat.post_delete),
        "/api/notifications/read": Route(chat.post_notifications_read),
        "/api/assign": Route(tickets.post_assign),
        "/api/skip": Route(tickets.post_skip),
        "/api/reply": Route(tickets.post_reply),
        "/api/drop": Route(tickets.post_drop),
        "/api/approve": Route(tickets.post_approve),
        "/api/check-override": Route(tickets.post_check_override),
        "/api/milestone-done": Route(tickets.post_milestone_done),
        "/api/deploy": Route(tickets.post_deploy),
        "/api/deploy-cancel": Route(tickets.post_deploy_cancel),
        "/api/release-hold": Route(tickets.post_release_hold),
        "/api/refresh": Route(tickets.post_refresh),
        "/api/mode": Route(operations.post_mode, admin_error=MODE_ADMIN_403),
        "/api/mode/preview": Route(operations.post_mode_preview, admin_error=MODE_ADMIN_403),
        "/api/update": Route(operations.post_update,
                             docker_admin_error="Docker 업데이트는 TP_ADMINS에 등록된 관리자만 실행할 수 있어요"),
        "/api/update/check": Route(operations.post_update_check),
        "/api/dev/check": Route(operations.post_dev_check),
        "/api/dev/start": Route(operations.post_dev_start),
        "/api/dev/stop": Route(operations.post_dev_stop),
        "/api/metrics/mark": Route(operations.post_metric_marker),
        "/api/view": Route(operations.post_view),
    },
}

# Authentication historically examines the path before method or query matching.
PUBLIC_PATHS = frozenset(path for routes in ROUTES.values() for path, route in routes.items() if route.public)


def match(method: str, target: str) -> Route | None:
    path, has_query, _ = target.partition("?")
    if method == "GET" and path.startswith("/api/onboard/"):
        path = urlsplit(target).path  # the onboarding handlers matched the parsed URL path
    route = ROUTES[method].get(path)
    if route is None or (has_query and route.query is Query.EXACT) or (not has_query and route.query is Query.REQUIRED):
        return None
    return route
