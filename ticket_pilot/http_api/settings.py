"""Dashboard projects, configuration, onboarding and Notion HTTP handlers."""
import json
from contextlib import closing

from .. import chat, config, guide, lan, notion, notion_tickets, notifications, onboard, release, themes
from ..core import db, project_map, settings
from . import Forbidden


def get_config(req):
    return req._send(200, config.api_state())


def get_config_history(req):
    return req._send(200, {"rows": config.history(limit=5000)})


def get_themes(req):
    return req._send(200, themes.api())


def get_guide(req):
    return req._send(200, {"text": guide.text()})


def get_projects(req):
    return req._send(200, {"projects": chat.projects(), "notifications_unread": notifications.unread_count()})


def get_github_repos(req):
    return req._send(200, onboard.github_repos(req.query().get("refresh", [""])[0] == "1"))


def get_asana_projects(req):
    return req._send(200, onboard.asana_projects(req.query().get("refresh", [""])[0] == "1"))


def get_clone(req):
    try:
        job = onboard.clone_status(int(req.query().get("id", ["0"])[0]))
    except ValueError:
        job = None
    return req._send(404, {"error": "not found"}) if job is None else req._send(200, job)


def get_check_log(req):
    q = req.query()
    try:
        body = config.trial_log(int(q.get("run", ["0"])[0]), q.get("name", [None])[0])
    except ValueError:
        body = None
    return req._send(404, {"error": "not found"}) if body is None else req._send(200, {"log": body})


def get_export(req):
    data = json.dumps(config.export(), ensure_ascii=False, indent=2).encode()
    return req._send(200, data, headers=[
        ("Content-Disposition", f'attachment; filename="ticket-pilot-config-{release._today()}.json"')])


def _change_config(req, fn, body, **kwargs):
    """Dry-run member changes first so any change to lan_share, including removal by import, is refused."""
    dry = bool(body.get("dry_run"))
    if not req.admin and any(c["scope"] == "global" and c["key"].split(".")[0] == "lan_share"
                             for c in fn(actor=req.actor, dry_run=True, **kwargs)):
        raise Forbidden("로컬망 공유 설정은 이 맥(127.0.0.1)에서만 바꿀 수 있어요")
    changes = fn(actor=req.actor, dry_run=dry, **kwargs)
    return req._send(200, {"ok": True, "changes": changes})


def post_config(req, body):
    if "updates" in body:  # several scopes as one change (프로젝트 추가)
        return _change_config(req, config.update_many, body, items=body["updates"], dashboard=True)
    return _change_config(req, config.update, body, scope=body["scope"], value=body.get("value"),
                          create=bool(body.get("create")), dashboard=True)


def post_import(req, body):
    return _change_config(req, config.import_doc, body, doc=config._read_doc(json.dumps(body.get("doc"))))


def post_check_run(req, body):
    return req._send(200, {"ok": True, "id": config.start_check_run(body["repo"])})


def post_check_path(req, body):
    return req._send(200, onboard.check_path(body.get("path"), body.get("github")))


def post_clone(req, body):
    return req._send(200, onboard.start_clone(body.get("github"), body.get("name")))


def post_notion_connect(req, _body):
    origin = req.headers.get("Origin")
    if not lan.origin_ok(origin, req.headers.get("Host")):
        raise Forbidden("대시보드에서 연결을 시작하세요")
    url, state, proof = notion.start(req.actor, origin + notion.CALLBACK)
    attrs = "HttpOnly; SameSite=Strict; Path=/api/notion/finish" + ("; Secure" if origin.startswith("https:") else "")
    return req._send(200, {"url": url}, headers=[
        ("Set-Cookie", f"tp_notion_{state}={proof}; Max-Age={notion.TTL}; {attrs}")])


def post_notion_finish(req, body):
    with closing(db()) as con:
        state = body.get("state")
        result = notion.finish(con, req.actor, state, req._cookie(f"tp_notion_{state}"), body.get("code"))
    return req._send(200, result)


def post_notion_accounts(req, _body):
    with closing(db()) as con:
        result = {**notion.accounts(con), "admin": req.admin}
    return req._send(200, result)


def post_notion_preview(req, body):
    with closing(db()) as con:
        repos = body.get("repos")
        if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
            raise notion.NotionError("프로젝트 레포 목록을 확인하세요")
        draft = {"id": body.get("project_id"), "name": "", "notion": body.get("notion"), "repos": repos}
        result = notion_tickets.preview(con, project_map()["projects"], draft, settings()["asana_tag"])
    return req._send(200, result)


def post_notion_sources(req, body):
    with closing(db()) as con:
        result = notion.Client(con, body.get("connection_id")).sources()
    return req._send(200, result)


def post_notion_people(req, body):
    with closing(db()) as con:
        result = notion.Client(con, body.get("connection_id")).people()
    return req._send(200, result)


def post_notion_properties(req, body):
    with closing(db()) as con:
        result = notion.Client(con, body.get("connection_id")).properties(body.get("data_source_id"))
    return req._send(200, result)


def post_notion_rows(req, body):
    with closing(db()) as con:
        result = notion.Client(con, body.get("connection_id")).rows(body.get("data_source_id"))
    return req._send(200, result)
