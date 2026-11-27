"""Project-routed ticket and milestone creation. Sync and dispatch own the local milestone lifecycle."""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sqlite3
import subprocess
import sys
import uuid

from . import asana_write, core, milestone, notion, notion_write, pipeline


class CreateError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.result = {"code": code, "message": message, **details}

    def exit(self):
        print(json.dumps({"ok": False, **self.result}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(5 if self.result["code"] == "upstream_error" else 4)


class CreateParser(argparse.ArgumentParser):
    def error(self, message):
        CreateError("invalid_argument", message).exit()


def _parent_gid(con, ref, source, *, for_milestone):
    """Board keys (including ms: tickets) and native IDs both work before the next sync."""
    rows = con.execute("SELECT gid FROM tickets WHERE gid=? OR key=? COLLATE NOCASE", (ref, ref)).fetchall()
    matches = set()
    for gid, in rows or [(ref,)]:
        if for_milestone and milestone.is_ms(gid) and ":" in gid[3:]:
            gid, _ = milestone.parse(gid)
        if ((source == "asana" and gid.isdigit()) or (source == "github" and gid.startswith("gh:"))
                or (source == "notion" and gid.startswith("notion:"))):
            matches.add(gid)
    if len(matches) > 1:
        raise CreateError("parent_not_found", f"티켓 키가 여러 개 일치해요. 부모 gid를 지정하세요: {ref}")
    return next(iter(matches), ref)


def _asana_result(task, project_gid):
    gid = task["gid"]
    return {"key": pipeline.field(task, "ID") or f"AS-{gid}", "gid": gid,
            "url": task.get("permalink_url") or f"https://app.asana.com/0/{project_gid}/{gid}"}


def _asana_create(con, project, args, parent, is_milestone):
    st = core.settings()
    pg = project.get("asana_gid")
    if not pg or not st.get("asana_enabled", True):
        raise CreateError("source_unsupported", "프로젝트의 Asana 연결을 확인하세요")
    tags, assignee = list(args.tag or []), args.assignee
    if parent:
        parent = re.sub(r"^AS-", "", parent, flags=re.I)
        if not parent.isdigit():
            raise CreateError("parent_not_found", f"Asana 부모 티켓을 찾을 수 없어요: {parent}")
        try:
            task = core.asana("GET", f"/tasks/{parent}", {"opt_fields": "tags.name"})["data"]
        except core.AsanaError as e:
            if e.status == 404:
                raise CreateError("parent_not_found", str(e)) from e
            raise
        if args.milestone and not milestone.has_tag([t["name"] for t in task["tags"]]):
            raise CreateError("parent_not_found", f"Asana 태스크 {parent}에 마일스톤 태그가 없어요")
    if is_milestone:
        tags = [st["milestone_tag"]]
    elif args.milestone:
        tags.append(st["asana_tag"])
        assignee = assignee or "me"
    if tags and not st.get("asana_workspace_gid"):
        raise CreateError("invalid_argument", core.NO_WORKSPACE)
    try:
        task = asana_write.create(pg, args.title, args.body, assignee, tags, parent, log=None)
    except asana_write.CreateRecordError as e:
        raise CreateError("upstream_error", str(e), created=_asana_result(e.task, pg)) from e
    result = _asana_result(task, pg)
    key, gid, url = result["key"], result["gid"], result["url"]
    # A custom Asana key cannot be decoded. Keep it resolvable for a second create before sync.
    if key != f"AS-{gid}":
        ts = core.now()
        try:
            with con:
                con.execute("""INSERT INTO tickets(gid,key,name,url,project_gid,assigned,first_seen,last_seen)
                               VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(gid) DO NOTHING""", (gid, key, args.title, url, pg, ts, ts))
        except sqlite3.Error as e:
            raise CreateError("upstream_error", "Asana 태스크는 생성됐어요. 로컬 DB에 티켓 키를 저장할 수 없어요",
                              created=result) from e
    return result


def _notion_create(project, args, parent, is_milestone, *, with_subtasks):
    cfg = project.get("notion") or {}
    if not cfg.get("data_source_id") or not cfg.get("connection_id"):
        raise CreateError("source_unsupported", "프로젝트에 Notion 작업 DB를 연결하세요")
    if args.parent and not cfg.get("parent_property"):
        raise CreateError("invalid_argument", "프로젝트 설정에서 상위 항목 관계 열을 먼저 골라 주세요")
    if parent:
        try:
            parent = str(uuid.UUID(parent[3:] if parent[:3].upper() == "NT-" else parent.removeprefix("notion:")))
        except ValueError:
            raise CreateError("parent_not_found", f"Notion 부모 티켓을 찾을 수 없어요: {parent}") from None
    assignee = args.assignee
    if args.milestone:
        if not assignee and cfg.get("people_property") and cfg.get("assignee", "me") != "all":
            assignee = cfg.get("assignee", "me")
    values = argparse.Namespace(op="create", project=project["id"], title=args.title, body=args.body, assignee=assignee,
                                tag=args.tag, status=None, due=None, parent=parent if args.parent else None)
    return notion_write.run(values, milestone=parent if args.milestone else None, milestone_parent=is_milestone,
                            with_subtasks=with_subtasks)


def _github_parent(ref, gh, for_milestone):
    if match := re.fullmatch(r"gh:([\w.-]+/[\w.-]+)#([1-9]\d*)", ref):
        parent_repo, number = match.groups()
    else:
        prefix, _, number = ref.upper().rpartition("-")
        repos = core.project_map()["repos"]
        targets = {repos[r]["github"] for r, (p, _) in pipeline.issue_repos().items()
                   if p.upper() == prefix and repos.get(r, {}).get("github")}
        if len(targets) != 1 or not re.fullmatch(r"[1-9]\d*", number):
            raise CreateError("parent_not_found", f"GitHub 부모 이슈를 특정할 수 없어요. 이슈 gid를 지정하세요: {ref}")
        parent_repo, = targets
    if parent_repo.split("/")[0].lower() != gh.split("/")[0].lower():
        raise CreateError("invalid_argument", "GitHub 하위 이슈는 부모와 같은 소유자의 레포에 만들어야 해요")
    try:
        issue = pipeline.gh_api("GET", f"repos/{parent_repo}/issues/{number}")
    except RuntimeError as e:
        if "(HTTP 404)" in str(e):
            raise CreateError("parent_not_found", f"GitHub 부모 이슈를 찾을 수 없어요: {ref}") from e
        raise
    if "pull_request" in issue or (for_milestone and not milestone.has_tag([lb["name"] for lb in issue["labels"]])):
        raise CreateError("parent_not_found", f"요청한 GitHub 부모 이슈를 찾을 수 없어요: {ref}")
    return issue["id"]


def _github_create(con, project, args, parent, is_milestone, actor):
    repo = core.default_repo(project)
    gh = core.project_map()["repos"].get(repo, {}).get("github")
    if not gh:
        raise CreateError("source_unsupported", "프로젝트의 기본 레포에 GitHub 저장소를 설정하세요")
    prefix = pipeline.issue_repos()[repo][0]
    tags = [core.settings()["milestone_tag"]] if is_milestone else [pipeline.ISSUE_LABEL, *(args.tag or [])]
    body = {"title": args.title, "body": args.body or "", "labels": list(dict.fromkeys(tags))}
    if parent:
        body["parent_issue_id"] = _github_parent(parent, gh, args.milestone)
    if args.assignee:
        body["assignees"] = [pipeline.gh_api("GET", "user")["login"]]
    issue = pipeline.gh_api("POST", f"repos/{gh}/issues", body)
    number = issue["number"]
    result = {"key": f"{prefix}-{number}", "gid": f"gh:{gh}#{number}", "url": issue["html_url"]}
    try:
        with con:
            core.event(con, "ticket_write", f"{actor} · {project['id']} · {result['key']} · create", result["gid"])
    except sqlite3.Error as e:
        raise CreateError("upstream_error", "이슈는 생성됐어요. 로컬 DB에 이벤트를 기록할 수 없어요", created=result) from e
    # GitHub can silently drop labels or assignees when the caller has insufficient repository permissions.
    if (not {t.lower() for t in tags} <= {lb["name"].lower() for lb in issue["labels"]}
            or not set(body.get("assignees", [])) <= {u["login"] for u in issue.get("assignees", [])}):
        raise CreateError("upstream_error", "이슈는 생성됐어요. GitHub에서 라벨과 담당자 권한을 확인하세요", created=result)
    return result


def create(project, args, *, is_milestone=False):
    try:
        actor = asana_write.write_actor(agents_only=False)
        with_subtasks = is_milestone and bool(args.subtask)
        if is_milestone or args.milestone is not None:
            supported, reason = milestone.supported(project, for_subtasks=with_subtasks or args.milestone is not None)
            if not supported:
                raise CreateError("milestone_unsupported", reason)
        ref = args.parent if args.parent is not None else args.milestone
        if not args.title.strip() or any(not t.strip() for t in args.tag or []) or (ref is not None and not ref.strip()):
            raise CreateError("invalid_argument", "제목, 태그와 부모 티켓 값은 비워 둘 수 없어요")
        source = project["source"]
        if source not in ("asana", "notion", "github"):
            raise CreateError("source_unsupported", "이 프로젝트에 티켓 출처를 설정하세요")
        con = core.db()
        parent = _parent_gid(con, ref, source, for_milestone=bool(args.milestone)) if ref else None
        if source == "asana":
            return _asana_create(con, project, args, parent, is_milestone)
        if source == "notion":
            return _notion_create(project, args, parent, is_milestone, with_subtasks=with_subtasks)
        return _github_create(con, project, args, parent, is_milestone, actor)
    except notion.PageNotFound as e:
        raise CreateError("parent_not_found", str(e)) from e
    except notion_write.MilestoneUnsupported as e:
        raise CreateError("milestone_unsupported", str(e)) from e
    except notion_write.CreateRecordError as e:
        raise CreateError("upstream_error", str(e), created=e.created) from e
    except notion_write.InvalidFields as e:
        raise CreateError("invalid_argument", str(e)) from e
    except asana_write.WriteDisabled as e:
        raise CreateError("write_disabled", str(e)) from e
    except SystemExit as e:
        raise CreateError("invalid_argument", str(e)) from e
    except (OSError, RuntimeError, http.client.HTTPException, subprocess.SubprocessError, sqlite3.Error, KeyError, TypeError, ValueError) as e:
        raise CreateError("upstream_error", str(e)) from e


def run(args):
    project = next((p for p in core.project_map()["projects"] if p["id"] == args.project), None)
    if not project:
        raise CreateError("project_not_found", f"프로젝트를 찾을 수 없어요: {args.project}")
    is_milestone = args.cmd == "milestone"
    result = {"source": project["source"], **create(project, args, is_milestone=is_milestone)}
    if is_milestone:
        result["subtasks"] = []
        failed, warnings = [], []
        for title in args.subtask or []:
            child = argparse.Namespace(**{**vars(args), "title": title, "body": None, "assignee": None, "milestone": result["gid"]})
            try:
                created = create(project, child)
            except CreateError as e:
                detail = {"title": title, **e.result}
                created = e.result.get("created")
                if created is None:
                    failed.append(detail)
                    continue
                warnings.append(detail)
            result["subtasks"].append({"key": created["key"], "url": created["url"]})
        if failed or warnings:
            code = "upstream_error" if warnings or any(e["code"] == "upstream_error" for e in failed) else failed[0]["code"]
            if warnings:
                result["warnings"] = warnings
            message = f"하위 티켓 {len(failed)}개 생성 실패, 생성 후 오류 {len(warnings)}개"
            raise CreateError(code, message, **result, failed=failed)
    return {"ok": True, **result}


def command(args):
    try:
        print(json.dumps(run(args), ensure_ascii=False))
    except CreateError as e:
        e.exit()


def register(parsers):
    for name in ("ticket", "milestone"):
        group = parsers.add_parser(name, help="프로젝트 설정에 따라 티켓 또는 마일스톤 생성")
        p = group.add_subparsers(required=True).add_parser("create")
        p.set_defaults(fn=command, parent=None, milestone=None, tag=None)
        p.add_argument("--project", required=True, help="프로젝트 설정의 ID")
        p.add_argument("--title", required=True)
        p.add_argument("--body")
        p.add_argument("--assignee", choices=["me"])
        if name == "milestone":
            p.add_argument("--subtask", action="append", help="생성할 하위 티켓 제목 (반복 가능)")
        else:
            p.add_argument("--tag", action="append")
            parent = p.add_mutually_exclusive_group()
            parent.add_argument("--parent", help="부모 티켓 키 또는 gid")
            parent.add_argument("--milestone", help="마일스톤 키 또는 gid")
