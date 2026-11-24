"""Project-scoped Notion filters, ticket projection and queue preview."""
from __future__ import annotations

import hashlib
import json

from . import notion
from .core import effective_config, event, meta_get, meta_set, read_config

FILTER_TYPES = {"people": ("people",), "tag": ("select", "multi_select"), "status": ("status", "select")}
FIELDS = {"connection_id", "data_source_id", "assignee", "people_property", "tag_property", "tag_option",
          "status_property", "status_option"}
SYNC_STATE = "notion_sync"


def validate(cfg):
    if not isinstance(cfg, dict) or set(cfg) - FIELDS or any(not isinstance(v, str) for v in cfg.values()):
        raise notion.NotionError("Notion 연결과 가져오기 조건을 확인하세요")
    account = cfg.get("connection_id", "").split(":")
    if len(account) != 2 or not all(notion.valid_id(v) for v in account):
        raise notion.NotionError("Notion 연결 계정을 골라 주세요")
    if not notion.valid_id(cfg.get("data_source_id")):
        raise notion.NotionError("공유한 데이터베이스를 골라 주세요")
    who = cfg.get("assignee", "me")
    if who not in ("me", "all") and not notion.valid_id(who):
        raise notion.NotionError("담당자를 골라 주세요")
    if who != "all" and not cfg.get("people_property"):
        raise notion.NotionError("담당자 열을 고르거나 담당자를 전체로 바꿔 주세요")
    for role in ("tag", "status"):
        if cfg.get(f"{role}_option") and not cfg.get(f"{role}_property"):
            raise notion.NotionError("선택한 값에 해당하는 열을 골라 주세요")


def _query(client, cfg):
    """Resolve persistent property/option IDs to current names for the API. Missing filters must never widen a query."""
    schema = {p["id"]: p for p in client.properties(cfg["data_source_id"])}
    who = cfg.get("assignee", "me")
    filters, columns = [], {}
    for role, types in FILTER_TYPES.items():
        prop = cfg.get(f"{role}_property")
        if not prop or (role == "people" and who == "all"):
            continue
        col = schema.get(prop)
        if not col or col["type"] not in types:
            raise notion.NotionError("선택한 Notion 열이 바뀌었어요. 프로젝트 설정에서 다시 골라 주세요")
        columns[role] = col["name"]
        if role == "people":
            condition = {"contains": client.account["user_id"] if who == "me" else who}
        elif option := cfg.get(f"{role}_option"):
            name = next((o["name"] for o in col["options"] if o["id"] == option), None)
            if name is None:
                raise notion.NotionError(f"{col['name']} 열의 선택지가 바뀌었어요. 값을 다시 골라 주세요")
            condition = {"contains" if col["type"] == "multi_select" else "equals": name}
        else:
            continue
        filters.append({"property": col["name"], col["type"]: condition})
    body = {"result_type": "page"}
    if filters:
        body["filter"] = filters[0] if len(filters) == 1 else {"and": filters}
    return client.items(f"/data_sources/{cfg['data_source_id']}/query", body), columns


def _options(prop):
    kind = prop.get("type")
    if kind == "multi_select":
        return [o["name"] for o in prop[kind]]
    option = prop.get(kind)
    return [option["name"]] if isinstance(option, dict) and option.get("name") else []


def matches(con, project, work_tag):
    cfg = project["notion"]
    validate(cfg)
    pages, columns = _query(notion.Client(con, cfg["connection_id"]), cfg)
    found = {}
    for page in pages:
        if page["object"] != "page" or page.get("in_trash") or page.get("is_archived") or page.get("archived"):
            continue
        page_id, props = page["id"], page["properties"]
        if not notion.valid_id(page_id):
            raise notion.NotionError("Notion 페이지 ID를 확인할 수 없어요")
        title = next((notion.plain(p["title"]) for p in props.values() if p["type"] == "title"), "")
        tags = _options(props.get(columns.get("tag"), {}))
        status = _options(props.get(columns.get("status"), {}))
        notes = [f"{k}: {notion.plain(p['rich_text'])}" for k, p in props.items() if p["type"] == "rich_text" and p["rich_text"]]
        gid = f"notion:{page_id}"
        found[gid] = {"gid": gid, "key": "NT-" + page_id.replace("-", ""), "name": title or "제목 없음",
                      "url": page["url"], "project_gid": f"notion:{project['id']}", "project_name": project["name"],
                      "notes": "\n".join(notes), "section": next(iter(status), None), "tags": json.dumps(tags, ensure_ascii=False),
                      "has_tag": work_tag.casefold() in [v.casefold() for v in tags], "asana_modified_at": page["last_edited_time"]}
    return found


def _version(project, work_tag):
    data = json.dumps([project.get("notion"), work_tag], sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def begin_sync(con, projects):
    """Number listings in the same short write transaction as load_map, before reading any network pages."""
    if not projects and not meta_get(con, SYNC_STATE, {}).get("projects"):
        return None
    generation = meta_get(con, "notion_sync_generation", 0) + 1
    meta_set(con, "notion_sync_generation", generation)
    return generation


def fetch(con, projects, work_tag):
    """Finish each remote listing before taking the sync writer lock. Keep only complete project results."""
    listings = {}
    for p in projects:
        key = (p["id"], _version(p, work_tag))
        try:
            listings[key] = matches(con, p, work_tag), None
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
            listings[key] = {}, str(e)
    return listings


def _batches(con, projects, work_tag, listings):
    # A page has one owner, the first matching configured project. A failed query only reserves pages
    # previously confirmed under the same settings. A concurrent edit invalidates the in-flight listing.
    current, confirmed_sources = [], {}
    for p in projects:
        version = _version(p, work_tag)
        rows, error = listings.get((p["id"], version), ({}, "조회 중 설정이 바뀌었어요. 다음 동기화에서 다시 읽어요"))
        if rows:
            confirmed_sources.update(dict.fromkeys(rows, p["notion"]["data_source_id"]))
        current.append((p, version, rows, error))
    previous = meta_get(con, SYNC_STATE, {}).get("projects", {})
    claimed = set()
    for p, version, rows, error in current:
        pid = p["id"]
        gids = rows.keys()
        if error and previous.get(pid, {}).get("version") == version:
            # A successful listing from another database confirms the page has moved there.
            source = p["notion"]["data_source_id"]
            gids = {r[0] for r in con.execute("SELECT gid FROM tickets WHERE project_gid=? AND assigned=1",
                                              (f"notion:{pid}",)) if confirmed_sources.get(r[0], source) == source}
        owned = gids - claimed
        claimed.update(gids)
        yield p, version, rows, owned, error


def _dispatch_source(con, project_gid):
    doc = effective_config(read_config(con))
    pid = project_gid.removeprefix("notion:")
    project = next((p for p in doc["project_map"]["projects"] if p["id"] == pid and p["source"] == "notion"), None)
    state = meta_get(con, SYNC_STATE, {}).get("projects", {}).get(pid, {})
    if not project or not state.get("ready") or state["version"] != _version(project, doc["settings"]["asana_tag"]):
        raise notion.NotionError("현재 프로젝트 조건으로 동기화한 뒤 작업을 시작해요")
    return project["notion"]["connection_id"], state["version"]


def queued_page(con, ticket, repo):
    from .pipeline import chosen_repo, compute_stage
    project_gid = ticket["project_gid"]
    source = _dispatch_source(con, project_gid)
    client = notion.Client(con, source[0])
    markdown = client.read(f"/pages/{ticket['gid'].removeprefix('notion:')}/markdown").get("markdown")
    if not isinstance(markdown, str):
        raise notion.NotionError("Notion 페이지의 본문을 읽을 수 없어요")
    # A sync, skip or project edit can finish while the body is in flight. Build the brief from the current row.
    current = con.execute("SELECT * FROM tickets WHERE gid=?", (ticket["gid"],)).fetchone()
    if (not current or current["project_gid"] != project_gid or compute_stage(con, current) != "queued"
            or chosen_repo(con, current) != repo or _dispatch_source(con, project_gid) != source):
        raise notion.NotionError("페이지를 읽는 동안 대기열이나 프로젝트 설정이 바뀌었어요")
    return current, markdown


def _local_fields(old, row):
    # A manual repo choice belongs to the project where it was made.
    return {"repo_override": old["repo_override"] if old and old["project_gid"] == row["project_gid"] else None}


def sync(con, generation, listings, ts, new_tagged):
    if generation is None:
        return {}
    # Settings may have changed during the network reads. Apply results against the same snapshot as the write.
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    if meta_get(con, SYNC_STATE, {}).get("generation", 0) >= generation:
        return {}  # a more recent dashboard refresh or tick already applied its results
    previous = con.execute("SELECT gid FROM tickets WHERE gid LIKE 'notion:%' AND assigned=1").fetchall()
    doc = effective_config(read_config(con))
    projects = [p for p in doc["project_map"]["projects"] if p["source"] == "notion"]
    seen, errors, state = set(), {}, {}
    for p, version, rows, owned, error in _batches(con, projects, doc["settings"]["asana_tag"], listings):
        state[p["id"]] = {"version": version, "ready": not error}
        seen.update(owned)
        if error:
            errors[p["id"]] = error
            event(con, "error", f"Notion 동기화 ({p['name']}): {error}")
            continue
        for gid, row in rows.items():
            if gid not in owned:
                continue
            old = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
            if row["has_tag"] and not (old and (old["completed"] or (old["assigned"] and old["has_tag"]))):
                new_tagged.append(row["key"])
            con.execute("""INSERT INTO tickets(gid,key,name,url,project_gid,project_name,notes,section,tags,has_tag,
                                               asana_modified_at,assigned,deps_json,first_seen,last_seen,repo_override)
                           VALUES(:gid,:key,:name,:url,:project_gid,:project_name,:notes,:section,:tags,:has_tag,
                                  :asana_modified_at,1,'[]',:ts,:ts,:repo_override)
                           ON CONFLICT(gid) DO UPDATE SET name=excluded.name,url=excluded.url,project_gid=excluded.project_gid,
                             project_name=excluded.project_name,notes=excluded.notes,section=excluded.section,tags=excluded.tags,
                             has_tag=excluded.has_tag,asana_modified_at=excluded.asana_modified_at,assigned=1,
                             last_seen=excluded.last_seen,repo_override=excluded.repo_override""",
                        {**row, **_local_fields(old, row), "project_name": p["name"], "ts": ts})
    meta_set(con, SYNC_STATE, {"generation": generation, "projects": state})
    con.executemany("UPDATE tickets SET assigned=0 WHERE gid=?", [(r[0],) for r in previous if r[0] not in seen])
    return errors


def preview(con, projects, project, work_tag):
    from .pipeline import compute_stage
    cfg = project["notion"]
    validate(cfg)
    # Earlier projects can claim pages only from this same data source.
    relevant = []
    for p in projects:
        if p["id"] == project["id"]:
            break
        if p["source"] == "notion" and p["notion"]["data_source_id"] == cfg["data_source_id"]:
            relevant.append(p)
    relevant.append(project)
    listings = fetch(con, relevant, work_tag)
    for p, _, rows, owned, error in _batches(con, relevant, work_tag, listings):
        if p is not project:
            continue
        if error:
            raise notion.NotionError(error)
        items = []
        for gid, row in rows.items():
            old = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
            ticket = {"completed": 0, "skip": 0, "open_deps": 0, **(dict(old) if old else {}), **row,
                      **_local_fields(old, row), "assigned": 1}
            stage = "other_project" if gid not in owned else compute_stage(con, ticket, repo_count=len(project["repos"]))
            items.append({"gid": gid, "title": row["name"], "stage": stage, "auto_start": stage == "queued"})
        return {"count": len(items), "tickets": items}
