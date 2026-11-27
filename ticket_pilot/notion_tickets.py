"""Project-scoped Notion filters, ticket projection and queue preview."""
from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field

from . import notion, notion_membership
from .core import ROOT, effective_config, event, meta_get, meta_set, read_config

FILTER_TYPES = {"people": ("people",), "tag": ("select", "multi_select"), "status": ("status", "select")}
MILESTONE_KIND_FIELDS = ("milestone_kind_property", "milestone_kind_option")
FIELDS = {"connection_id", "data_source_id", "assignee", "people_property", "tag_property", "tag_option",
          "status_property", "status_option", "date_property", "milestone_data_source_id",
          "milestone_property", *MILESTONE_KIND_FIELDS} | notion_membership.FIELDS
# Stages of an actual Notion run. Filter membership and the pre-dispatch queue are read-only.
WRITE_STAGES = {"dispatched", "in_progress", "blocked", "agent_done", "pr_open", "rework", "conflict",
                "reviewed", "approving", "staged", "merged", "deploying", "closed", "done"}
SYNC_STATE = "notion_sync"


def agent_rules(project_id):
    if not project_id:
        return ""
    return (ROOT / "prompts" / "notion.md").read_text().strip().replace("{{PROJECT_ID}}", shlex.quote(project_id))


def validate(cfg, *, con=None, previous=None):
    if (not isinstance(cfg, dict) or cfg.keys() - FIELDS - {"stage_statuses"}
            or any(not isinstance(cfg[k], str) for k in FIELDS & cfg.keys())):
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
    notion_membership.validate(cfg)
    source = cfg.get("milestone_data_source_id")
    if source == cfg["data_source_id"]:
        if not all(cfg.get(k, "").strip() for k in MILESTONE_KIND_FIELDS):
            raise notion.NotionError("작업 DB를 쓰려면 마일스톤 판별 열과 마일스톤 태그를 함께 골라 주세요")
    elif any(k in cfg for k in MILESTONE_KIND_FIELDS):
        raise notion.NotionError("마일스톤 판별 열과 태그는 작업 DB를 마일스톤 DB로 쓸 때만 설정할 수 있어요")
    if source or cfg.get("milestone_property"):
        if not notion.valid_id(source) or not cfg.get("milestone_property", "").strip():
            raise notion.NotionError("마일스톤 DB와 작업 DB의 마일스톤 관계 열을 함께 골라 주세요")
        binding = ("connection_id", "data_source_id", "milestone_data_source_id", "milestone_property", *MILESTONE_KIND_FIELDS)
        if con is not None and any(cfg.get(k) != (previous or {}).get(k) for k in binding):
            client = notion.Client(con, cfg["connection_id"])
            schema = {col["id"]: col for col in client.properties(cfg["data_source_id"])}
            notion_membership.relation_column(schema, cfg, "milestone")
            milestone_tag_filter(schema, cfg)
    stages = cfg.get("stage_statuses", {})
    if (not isinstance(stages, dict) or stages.keys() - WRITE_STAGES
            or any(not isinstance(v, str) or not v for v in stages.values())
            or (stages and not cfg.get("status_property"))):
        raise notion.NotionError("단계별로 반영할 Notion 상태 열과 선택지를 골라 주세요")


def project_config(con, pid):
    row = con.execute("SELECT extra FROM config_projects WHERE id=? AND source='notion'", (pid,)).fetchone()
    return json.loads(row["extra"])["notion"] if row else None


def milestone_kind_blocks_run(con, ticket):
    if not ticket["gid"].startswith("notion:") or not ticket["milestone_error"]:
        return False
    cfg = project_config(con, ticket["project_gid"].removeprefix("notion:"))
    return bool(cfg and cfg.get("milestone_kind_property"))


def ticket_key(page_id):
    return "NT-" + page_id.replace("-", "")


def column(schema, cfg, role):
    kinds = FILTER_TYPES.get(role, (role,))
    if selected := cfg.get(f"{role}_property"):
        col = schema.get(selected)
        return col if col and col["type"] in kinds else None
    if role in ("title", "date", "people"):
        choices = [c for c in schema.values() if c["type"] in kinds]
        return choices[0] if len(choices) == 1 else None
    return None


def _query(client, cfg):
    """Resolve persistent property/option IDs to current names for the API. Missing filters must never widen a query."""
    schema = {p["id"]: p for p in client.properties(cfg["data_source_id"])}
    who = cfg.get("assignee", "me")
    filters = []
    for role in FILTER_TYPES:
        prop = cfg.get(f"{role}_property")
        if not prop or (role == "people" and who == "all"):
            continue
        col = column(schema, cfg, role)
        if not col:
            raise notion.NotionError("선택한 Notion 열이 바뀌었어요. 프로젝트 설정에서 다시 골라 주세요")
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
    return client.items(f"/data_sources/{cfg['data_source_id']}/query", body), schema


def _options(prop):
    kind = prop.get("type")
    if kind == "multi_select":
        return [o["name"] for o in prop[kind]]
    option = prop.get(kind)
    return [option["name"]] if isinstance(option, dict) and option.get("name") else []


def _page_fields(page, cfg, schema, work_tag=None):
    props = page["properties"]

    def prop(role):
        col = column(schema, cfg, role)
        return props.get(col["name"], {}) if col else {}

    title = notion.page_title(page)
    tags = _options(prop("tag"))
    notes = [f"{k}: {notion.plain(p['rich_text'])}" for k, p in props.items() if p["type"] == "rich_text" and p["rich_text"]]
    fields = {"name": title or "제목 없음", "due_on": (prop("date").get("date") or {}).get("start"),
              "url": page["url"], "notes": "\n".join(notes), "section": next(iter(_options(prop("status"))), None),
              "tags": json.dumps(tags, ensure_ascii=False), "asana_modified_at": page["last_edited_time"]}
    if work_tag is not None:
        fields["has_tag"] = any(v.casefold() == work_tag.casefold() for v in tags)
    return fields


@dataclass
class Listing:
    rows: dict[str, dict] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)
    error: str | None = None
    parents: dict[str, dict] = field(default_factory=dict)


def milestone_tag_filter(schema, cfg):
    """Resolve the shared DB's discriminator once, before reading any rows."""
    if cfg.get("milestone_data_source_id") != cfg["data_source_id"]:
        return None
    col = schema.get(cfg["milestone_kind_property"])
    if not col or col["type"] not in FILTER_TYPES["tag"]:
        raise notion.NotionError("마일스톤 판별 열이 바뀌었어요. 프로젝트 설정에서 다시 골라 주세요")
    option = cfg["milestone_kind_option"]
    if not any(o["id"] == option for o in col["options"]):
        raise notion.NotionError(f"{col['name']} 열의 마일스톤 태그가 바뀌었어요. 프로젝트 설정에서 다시 골라 주세요")

    def tagged(page):
        kind = col["type"]
        prop = page["properties"].get(col["name"], {})
        if prop.get("type") != kind or kind not in prop or prop.get("id", col["id"]) != col["id"]:
            raise notion.NotionError("페이지의 마일스톤 판별 열을 읽을 수 없어요. 프로젝트 설정에서 다시 골라 주세요")
        value = prop[kind]
        if kind == "select":
            return value is not None and value["id"] == option
        return any(o["id"] == option for o in value)

    return tagged


def _milestone_parent(client, page, column, source, cache):
    prop = page["properties"].get(column["name"], {})
    if prop.get("type") != "relation" or not isinstance(prop.get("relation"), list):
        raise notion.NotionError("작업의 마일스톤 관계 열을 읽을 수 없어요")
    ids = set(client.relation_ids(page, column))
    if len(ids) > 1:
        raise notion.NotionError(f"마일스톤이 {len(ids)}개 연결되어 있어요. 한 개만 연결한 뒤 다시 동기화하세요")
    if ids:
        page_id = ids.pop()
        if not notion.valid_id(page_id):
            raise notion.NotionError("연결된 마일스톤 페이지 ID를 확인할 수 없어요")
        if page_id not in cache:
            cache[page_id] = client.page(page_id, source)
        return cache[page_id]


def matches(con, project, work_tag, rules, *, track_runs=False):
    cfg = project["notion"]
    validate(cfg)
    client = notion.Client(con, cfg["connection_id"])
    pages, schema = _query(client, cfg)
    milestone_col = notion_membership.relation_column(schema, cfg, "milestone")
    tagged_milestone = milestone_tag_filter(schema, cfg)
    milestones = {}
    membership = notion_membership.Membership(client, cfg["data_source_id"], schema, rules, project["id"])
    if cfg.get("project_page_id"):
        client.page(cfg["project_page_id"], cfg["project_data_source_id"])
    listing = Listing()

    def read_parent(gid, page=None):
        parent, error = None, None
        try:
            if milestone_col:
                if page is None:
                    page = client.page(gid.removeprefix("notion:"), cfg["data_source_id"])
                    if tagged_milestone and tagged_milestone(page):
                        raise notion.NotionError("진행 중인 작업에 마일스톤 태그가 붙었어요. 작업을 계속하려면 태그를 제거하세요")
                parent = _milestone_parent(client, page, milestone_col, cfg["milestone_data_source_id"], milestones)
                if parent and tagged_milestone and not tagged_milestone(parent):
                    raise notion.NotionError("연결된 행에 마일스톤 태그가 없어요. 마일스톤 태그가 붙은 행을 연결하세요")
        except notion.NotionError as e:
            parent, error = None, str(e)
        listing.parents[gid] = {"parent_gid": f"notion:{parent['id']}" if parent else None,
                                "parent_name": (notion.page_title(parent) or "제목 없음") if parent else None,
                                "parent_url": parent["url"] if parent else None, "in_milestone": int(bool(parent)),
                                "milestone_error": error}

    for page in pages:
        if not notion.active_page(page) or (tagged_milestone and tagged_milestone(page)):
            continue
        page_id = page["id"]
        if not notion.valid_id(page_id):
            raise notion.NotionError("Notion 페이지 ID를 확인할 수 없어요")
        gid = f"notion:{page_id}"
        owners, ancestors = membership.resolve(page)
        if owners is notion_membership.UNRESOLVED:
            listing.unresolved.add(gid)
            continue
        if owners is None:
            if cfg.get("project_page_id") or cfg.get("contains_property"):
                continue
        elif project["id"] not in owners:
            continue
        read_parent(gid, page)
        listing.rows[gid] = {**_page_fields(page, cfg, schema, work_tag), "gid": gid, "key": ticket_key(page_id),
                             "project_gid": f"notion:{project['id']}", "project_name": project["name"],
                             "ancestors": [f"notion:{i}" for i in ancestors], "owners": owners}
    if track_runs:
        # A stage write may exclude a running page from the import query. Its owner still needs current routing.
        running = con.execute("""SELECT t.gid FROM tickets t WHERE project_gid=? AND completed=0
                                 AND gid LIKE 'notion:%' AND (? OR in_milestone=1 OR milestone_error IS NOT NULL)
                                 AND (SELECT state FROM runs WHERE ticket_gid=t.gid ORDER BY id DESC LIMIT 1) != 'abandoned'""",
                              ("notion:" + project["id"], bool(milestone_col)))
        for gid, in running.fetchall():
            if gid not in listing.parents:
                read_parent(gid)
    return listing


def _version(project, work_tag, rules):
    cfg = {k: v for k, v in project["notion"].items() if k != "stage_statuses"}
    inputs = [cfg, work_tag]
    if peers := {k: v for k, v in rules.items() if k != project["id"]}:
        inputs.append(peers)
    data = json.dumps(inputs, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def begin_sync(con, projects):
    """Number listings in the same short write transaction as load_map, before reading any network pages."""
    if not projects and not meta_get(con, SYNC_STATE, {}).get("projects"):
        return None
    generation = meta_get(con, "notion_sync_generation", 0) + 1
    meta_set(con, "notion_sync_generation", generation)
    return generation


def page_edited(con, gid, page, source, schema):
    """Project a write response even outside the filter. Only a fresh listing may update membership."""
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    snapshot = meta_get(con, SYNC_STATE, {})
    generation = meta_get(con, "notion_sync_generation", 0)
    edited = snapshot.setdefault("edited", {})
    edited[gid] = generation
    for child, ancestors in snapshot.get("ancestors", {}).items():
        if gid in ancestors:
            edited[child] = generation
    meta_set(con, SYNC_STATE, snapshot)
    if (not page or page.get("id") != gid.removeprefix("notion:") or not page.get("properties")
            or not page.get("url") or not page.get("last_edited_time")):
        return  # partial page response
    owner = con.execute("SELECT project_gid FROM tickets WHERE gid=?", (gid,)).fetchone()
    cfg = project_config(con, owner[0].removeprefix("notion:")) if owner else None
    if cfg and cfg["data_source_id"] == source:
        fields = _page_fields(page, cfg, schema)
        con.execute("""UPDATE tickets SET name=:name, url=:url, notes=:notes, due_on=:due_on, section=:section,
                       tags=:tags, asana_modified_at=:asana_modified_at WHERE gid=:gid""",
                    {**fields, "gid": gid})


def fetch(con, projects, work_tag, *, through=None):
    """Finish each remote listing before taking the sync writer lock. Keep only complete project results."""
    listings = {}
    for p in projects:
        rules = notion_membership.settings(projects, p["notion"]["data_source_id"])
        key = (p["id"], _version(p, work_tag, rules))
        try:
            listings[key] = matches(con, p, work_tag, rules, track_runs=through is None)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
            listings[key] = Listing(error=str(e))
        if p is through:
            break
    return listings


def _run_owners(con):
    # Once dispatched, a different project's filter must not take over this page's run or status writes.
    return dict(con.execute("""SELECT t.gid, t.project_gid FROM tickets t JOIN runs r ON r.id=(
                                 SELECT max(id) FROM runs WHERE ticket_gid=t.gid)
                               WHERE t.gid LIKE 'notion:%' AND r.state != 'abandoned'"""))


def _batches(con, projects, work_tag, listings, run_owners):
    # A page has one owner, the first matching configured project. Keep previous ownership on a failed
    # listing, or just for its unresolved pages, when the settings still match.
    current, confirmed_sources, confirmed_owners = [], {}, {}
    for p in projects:
        rules = notion_membership.settings(projects, p["notion"]["data_source_id"])
        version = _version(p, work_tag, rules)
        listing = listings.get((p["id"], version), Listing(error="조회 중 설정이 바뀌었어요. 다음 동기화에서 다시 읽어요"))
        confirmed_sources.update(dict.fromkeys(listing.rows, p["notion"]["data_source_id"]))
        for gid, row in listing.rows.items():
            if row["owners"] is not None:
                confirmed_owners.setdefault(gid, set()).update(row["owners"])
        current.append((p, version, listing))
    previous = meta_get(con, SYNC_STATE, {}).get("projects", {})
    claimed = set()
    for p, version, listing in current:
        pid = p["id"]
        gids = set(listing.rows)
        if (listing.error or listing.unresolved) and previous.get(pid, {}).get("version") == version:
            held = {gid for gid, in con.execute("SELECT gid FROM tickets WHERE project_gid=? AND assigned=1",
                                                (f"notion:{pid}",))}
            if not listing.error:
                held &= listing.unresolved
            # A successful listing can confirm a move to another DB or an explicit owner in this DB.
            source = p["notion"]["data_source_id"]
            gids.update(gid for gid in held if confirmed_sources.get(gid, source) == source
                        and (gid not in confirmed_owners or pid in confirmed_owners[gid]))
        owned = {gid for gid in gids - claimed if gid not in run_owners or run_owners[gid] == f"notion:{pid}"}
        claimed.update(owned)
        yield p, version, listing, owned


def _dispatch_source(con, project_gid, gid):
    doc = effective_config(read_config(con))
    pid = project_gid.removeprefix("notion:")
    projects = [p for p in doc["project_map"]["projects"] if p["source"] == "notion"]
    project = next((p for p in projects if p["id"] == pid), None)
    snapshot = meta_get(con, SYNC_STATE, {})
    if gid in snapshot.get("edited", {}):
        raise notion.NotionError("수정한 페이지의 가져오기 조건을 다음 동기화에서 확인한 뒤 작업을 시작해요")
    state = snapshot.get("projects", {}).get(pid, {})
    rules = notion_membership.settings(projects, project["notion"]["data_source_id"]) if project else {}
    if not project or not state.get("ready") or state["version"] != _version(project, doc["settings"]["asana_tag"], rules):
        raise notion.NotionError("현재 프로젝트 조건으로 동기화한 뒤 작업을 시작해요")
    if gid in state.get("unresolved", []):
        raise notion.NotionError("공유 작업 DB의 프로젝트 소속을 확인한 뒤 작업을 시작해요")
    return project["notion"]["connection_id"], state["version"]


def queued_page(con, ticket, repo):
    from .pipeline import chosen_repo, compute_stage
    project_gid = ticket["project_gid"]
    source = _dispatch_source(con, project_gid, ticket["gid"])
    client = notion.Client(con, source[0])
    markdown = client.markdown(ticket["gid"].removeprefix("notion:"))
    # A sync, skip or project edit can finish while the body is in flight. Build the brief from the current row.
    current = con.execute("SELECT * FROM tickets WHERE gid=?", (ticket["gid"],)).fetchone()
    if (not current or current["project_gid"] != project_gid or current["parent_gid"] != ticket["parent_gid"]
            or compute_stage(con, current) != "queued"
            or chosen_repo(con, current) != repo or _dispatch_source(con, project_gid, current["gid"]) != source):
        raise notion.NotionError("페이지를 읽는 동안 대기열이나 프로젝트 설정이 바뀌었어요")
    return current, markdown


def _repo_override(old, project_gid, repos):
    # An override can name any registered repo, even for a project without mapped repos.
    if old and old["project_gid"] == project_gid and old["repo_override"] in repos:
        return old["repo_override"]
    return None


def sync(con, generation, listings, ts, new_tagged):
    from . import milestone
    if generation is None:
        return {}
    # Settings may have changed during the network reads. Apply results against the same snapshot as the write.
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    snapshot = meta_get(con, SYNC_STATE, {})
    if snapshot.get("generation", 0) >= generation:
        return {}  # a more recent dashboard refresh or tick already applied its results
    edited = {gid: gen for gid, gen in snapshot.get("edited", {}).items() if gen >= generation}
    ancestors = snapshot.get("ancestors", {})
    previous = con.execute("SELECT gid FROM tickets WHERE gid LIKE 'notion:%' AND assigned=1").fetchall()
    doc = effective_config(read_config(con))
    projects = [p for p in doc["project_map"]["projects"] if p["source"] == "notion"]
    seen, errors, state = set(edited), {}, {}
    run_owners = _run_owners(con)
    for p, version, listing, owned in _batches(con, projects, doc["settings"]["asana_tag"], listings, run_owners):
        state[p["id"]] = {"version": version, "ready": not listing.error}
        if held := owned & listing.unresolved:
            state[p["id"]]["unresolved"] = sorted(held)
        seen.update(owned)
        if listing.error:
            errors[p["id"]] = listing.error
            event(con, "error", f"Notion 동기화 ({p['name']}): {listing.error}")
            continue
        for gid, row in listing.rows.items():
            if gid not in owned:
                continue
            # The first listing of a new dependency may already have been overtaken by a parent edit.
            if changed := [edited[parent] for parent in row["ancestors"] if parent in edited]:
                edited[gid] = max(max(changed), edited.get(gid, 0))
            if gid in edited:
                continue
            if row["ancestors"]:
                ancestors[gid] = row["ancestors"]
            else:
                ancestors.pop(gid, None)
            old = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
            already_tagged = old and (old["completed"] or (old["assigned"] and old["has_tag"]))
            if row["has_tag"] and gid not in run_owners and not already_tagged:
                new_tagged.append(row["key"])
            con.execute("""INSERT INTO tickets(gid,key,name,url,project_gid,project_name,notes,section,tags,has_tag,
                                               asana_modified_at,assigned,deps_json,first_seen,last_seen,repo_override,due_on)
                           VALUES(:gid,:key,:name,:url,:project_gid,:project_name,:notes,:section,:tags,:has_tag,
                                  :asana_modified_at,1,'[]',:ts,:ts,:repo_override,:due_on)
                           ON CONFLICT(gid) DO UPDATE SET name=excluded.name,url=excluded.url,project_gid=excluded.project_gid,
                             project_name=excluded.project_name,notes=excluded.notes,section=excluded.section,tags=excluded.tags,
                             has_tag=excluded.has_tag,asana_modified_at=excluded.asana_modified_at,assigned=1,
                             last_seen=excluded.last_seen,repo_override=excluded.repo_override,due_on=excluded.due_on""",
                        {**row, "repo_override": _repo_override(old, row["project_gid"], doc["project_map"]["repos"]),
                         "project_name": p["name"], "ts": ts})
        for gid, parent in listing.parents.items():
            active = run_owners.get(gid) == "notion:" + p["id"]
            if gid in edited or (gid not in owned and not active):
                continue
            old = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
            if error := parent["milestone_error"]:
                if error != old["milestone_error"]:
                    event(con, "error", f"{old['key']}: {error}", gid)
                if active:
                    # Keep the last known branch until the owner resolves an ambiguous relation.
                    parent = {k: old[k] if k != "milestone_error" else error for k in parent}
            if any(old[k] != v for k, v in parent.items()):
                if old["parent_gid"] != parent["parent_gid"] or old["milestone_error"] != parent["milestone_error"]:
                    con.execute("UPDATE tickets SET approve_requested_at=NULL,auto_approved_sha=NULL WHERE gid=?", (gid,))
                con.execute("""UPDATE tickets SET parent_gid=:parent_gid,parent_name=:parent_name,parent_url=:parent_url,
                               in_milestone=:in_milestone,milestone_error=:milestone_error WHERE gid=:gid""", {**parent, "gid": gid})
                if parent["parent_gid"] and not parent["milestone_error"]:
                    milestone.refresh_parent(con, parent["parent_gid"], parent["parent_name"], parent["parent_url"])
    meta_set(con, SYNC_STATE, {"generation": generation, "projects": state, "edited": edited,
                               "ancestors": {gid: parents for gid, parents in ancestors.items() if gid in seen}})
    con.executemany("UPDATE tickets SET assigned=0 WHERE gid=?", [(r[0],) for r in previous if r[0] not in seen])
    return errors


def preview(con, projects, project, work_tag):
    from .pipeline import compute_stage
    cfg = project["notion"]
    validate(cfg)
    # All rules on the same source determine membership. Only earlier projects break ties.
    relevant = []
    for p in projects:
        if p["id"] == project["id"]:
            relevant.append(project)
        elif p["source"] == "notion" and p["notion"]["data_source_id"] == cfg["data_source_id"]:
            relevant.append(p)
    if project not in relevant:
        relevant.append(project)
    listings = fetch(con, relevant, work_tag, through=project)
    for p, _, listing, owned in _batches(con, relevant, work_tag, listings, _run_owners(con)):
        if p is not project:
            continue
        if listing.error:
            raise notion.NotionError(listing.error)
        items = []
        registered = {r[0] for r in con.execute("SELECT name FROM repos")}
        repos = [{"repo": name} for name in project["repos"]]
        for gid, row in listing.rows.items():
            old = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
            ticket = {"completed": 0, "skip": 0, "open_deps": 0, **(dict(old) if old else {}), **row, **listing.parents[gid],
                      "repo_override": _repo_override(old, row["project_gid"], registered), "assigned": 1}
            stage = "other_project" if gid not in owned else compute_stage(con, ticket, repos=repos)
            items.append({"gid": gid, "title": row["name"], "stage": stage, "auto_start": stage == "queued"})
        return {"count": len(items), "tickets": items}
