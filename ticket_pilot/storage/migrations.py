"""Upgrade the schema and import settings from older installations."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

from .records import event, meta_get, meta_set, now
from .schema import PROJECT_TABLES


UNSORTED = "unsorted"


def project_slug(name, gid=None) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40].strip("-")
    return s or (f"asana-{gid}" if gid else "project")


def norm_projects(projects: list) -> list:
    """Projects in the pre-TP-91 shape (asana_gid only: old exports, the migration) get a source and a unique id.
    Deterministic, so the migration and an import of the same old export give the same ids."""
    if all(isinstance(p, dict) and p.get("id") and p.get("source") for p in projects):
        return projects
    taken = {UNSORTED} | {str(p["id"]) for p in projects if isinstance(p, dict) and p.get("id")}
    out = []
    for p in projects:
        if not isinstance(p, dict):
            out.append(p)  # validate() reports it
            continue
        p = dict(p)
        if p.get("asana_gid") is not None:
            p["asana_gid"] = str(p["asana_gid"])
        p.setdefault("source", "asana" if p.get("asana_gid") else "none")
        if not p.get("id"):
            base = i = project_slug(p.get("name"), p.get("asana_gid"))
            n = 2
            while i in taken:
                i, n = f"{base}-{n}", n + 1
            p["id"] = i
            taken.add(i)
        out.append(p)
    return out


_COLUMNS = {
    "notion_status_queue": {"next_attempt_at": "REAL NOT NULL DEFAULT 0"},
    # source: NULL = AI review, 'check' = tp check failure sent back to the agent through the same rework flow
    "reviews": {"rework_at": "TEXT", "source": "TEXT", "manual": "INTEGER", "agent_log": "TEXT"},
    # attempt: checks.py reruns a head once when tp could not run its checks (error)
    "checks": {"attempt": "INTEGER"},
    # cleanup.py: when the ticket worktree was removed after its change was seen in production
    "runs": {"removed_at": "TEXT"},
    # reply: the owner's dashboard answer to a BLOCKED agent; tp dispatch relaunches the agent with it, then clears it
    # check_override(_sha): the owner's reason for approving a head whose tp checks did not pass (only that head commit)
    "tickets": {"approve_requested_at": "TEXT", "approve_error": "TEXT", "reply": "TEXT",
                "check_override": "TEXT", "check_override_sha": "TEXT",
                # review_override(_sha): the owner's reason for approving a head the AI review did not approve (TP-99)
                "review_override": "TEXT", "review_override_sha": "TEXT",
                # milestone.py: participation and the head the AI approval auto-merged once
                "in_milestone": "INTEGER", "auto_approved_sha": "TEXT",
                # A Notion milestone page lives outside the task DB. Keep its link or a per-task relation error.
                "parent_url": "TEXT", "milestone_error": "TEXT",
                # milestone tickets: when the owner pressed 마일스톤 작업 완료 (cleared again when a subtask comes in)
                "ms_done_at": "TEXT",
                # pipeline.restage: when the stage became done/closed (NULL again once reopened); the board hides old ones
                "done_at": "TEXT"},
    # mergeable: GitHub MERGEABLE | CONFLICTING | NULL (not computed yet; GitHub answers UNKNOWN lazily)
    "prs": {"base": "TEXT", "merged_at": "TEXT", "merge_sha": "TEXT", "release_id": "INTEGER", "mergeable": "TEXT",
            # track.py: production verification for merged PRs outside the release train
            "prod_at": "TEXT", "prod_note": "TEXT", "track_sha": "TEXT",
            # review.resolve_conflict: head commit the agent was relaunched on to resolve a base-branch conflict
            "conflict_sha": "TEXT", "conflict_at": "TEXT"},
    # chat.py: JSON list of {name, file, size, type} for user and assistant files in data/chat-uploads/<conv>/
    "chat_messages": {"attachments": "TEXT", "input_tokens": "INTEGER", "cache_read_input_tokens": "INTEGER",
                      "cache_creation_input_tokens": "INTEGER", "output_tokens": "INTEGER",
                      "context_tokens": "INTEGER", "before_context_tokens": "INTEGER", "compaction_id": "INTEGER",
                      "claude_session_id": "TEXT"},
    # TP-91: conversations belong to a project (several repos), not a repo
    # TP-103: read_id = the last assistant message the owner saw (unread = finished replies after it)
    "chat_conversations": {"project_id": "TEXT", "read_id": "INTEGER"},
    # deploy.py (배포 page): mode NULL = the weekday train. state_json = resumable step state, lock_until = tick claim
    "releases": {"mode": "TEXT", "ticket_gid": "TEXT", "step": "TEXT", "state_json": "TEXT", "version": "TEXT",
                 "requested_at": "TEXT", "lock_until": "TEXT"},
}


def _cols(con, table) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


@contextmanager
def _write(con):
    # Check migration markers again once other writers are excluded. Roll back DDL and data together.
    with con:
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
        yield


def _migrate_projects(con, refresh_project_map):
    """TP-91: config_projects / config_project_repos keyed by asana_gid -> by the project's own id (norm_projects).
    project_map is a derived copy: recreated with project_id and refilled from the new tables.
    One write transaction (re-checked under the lock): a failure leaves the old tables, and a concurrent process
    (tick vs dashboard right after a deploy) migrates once."""
    if "id" in _cols(con, "config_projects"):
        return
    con.commit()
    with _write(con):
        if "id" not in _cols(con, "config_projects"):
            _rebuild_projects(con, refresh_project_map)


def _rebuild_projects(con, refresh_project_map):
    old = [dict(r) for r in con.execute("SELECT * FROM config_projects ORDER BY pos")]
    links = [dict(r) for r in con.execute("SELECT * FROM config_project_repos ORDER BY pos")]
    projects = norm_projects([{"asana_gid": p["asana_gid"], "name": p["name"]} for p in old])
    ids = {p["asana_gid"]: p["id"] for p in projects}
    con.execute("DROP TABLE config_projects")
    con.execute("DROP TABLE config_project_repos")
    con.execute("DROP TABLE IF EXISTS project_map")
    for ddl in PROJECT_TABLES.split(";"):  # not executescript: that commits, this must stay one transaction
        if ddl.strip():
            con.execute(ddl)
    for p, o in zip(projects, old):
        con.execute("INSERT INTO config_projects(id,pos,name,source,asana_gid,extra) VALUES(?,?,?,?,?,?)",
                    (p["id"], o["pos"], o["name"], p["source"], o["asana_gid"], o["extra"]))
    for r in links:
        if r["asana_gid"] in ids:
            con.execute("INSERT OR REPLACE INTO config_project_repos(project_id,repo,pos,role,is_default) VALUES(?,?,?,?,?)",
                        (ids[r["asana_gid"]], r["repo"], r["pos"], r["role"], r["is_default"]))
    refresh_project_map(con)
    if old:
        event(con, "config", f"프로젝트 {len(old)}개에 id를 붙였어요 (TP-91): "
              + ", ".join(f"{p['name']} → {p['id']}" for p in projects))


def _missing_columns(con):
    for table, cols in _COLUMNS.items():
        have = _cols(con, table)
        for col, typ in cols.items():
            if col not in have:
                yield table, col, typ


def migrate_schema(con, refresh_project_map):
    """Use the presence of columns as markers. TP-152 precedes TP-91's event, and TP-91 precedes chat backfills."""
    # TP-152 records the dashboard actor. System events leave it NULL.
    if "actor" not in _cols(con, "events"):
        with _write(con):
            if "actor" not in _cols(con, "events"):
                con.execute("ALTER TABLE events ADD COLUMN actor TEXT")
    _migrate_projects(con, refresh_project_map)
    if next(_missing_columns(con), None) is None:
        return
    with _write(con):
        added = set()
        for table, col, typ in _missing_columns(con):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            added.add((table, col))
        _backfill(con, added)


def _backfill(con, added):
    if ("chat_conversations", "project_id") in added:
        # a repo's conversations go to the first project (settings order) that has the repo, else 미분류
        con.execute(f"""UPDATE chat_conversations SET project_id=COALESCE(
                          (SELECT l.project_id FROM config_project_repos l JOIN config_projects p ON p.id=l.project_id
                           WHERE l.repo=chat_conversations.repo ORDER BY p.pos LIMIT 1), '{UNSORTED}')""")
    if ("chat_conversations", "read_id") in added:
        # existing conversations start read up to their last finished message (a turn running now still counts once done)
        con.execute("""UPDATE chat_conversations SET read_id=COALESCE((SELECT MAX(id) FROM chat_messages
                         WHERE conversation_id=chat_conversations.id AND text IS NOT NULL), 0)""")
    if ("tickets", "done_at") in added:  # prs.prod_at / merged_at may have just been added too
        # finished tickets: their PR's production / merge time, else the Asana change, else the migration time
        con.execute("""UPDATE tickets SET done_at=COALESCE(
                         (SELECT MAX(COALESCE(prod_at, merged_at)) FROM prs WHERE ticket_gid=tickets.gid),
                         asana_modified_at, ?) WHERE stage IN ('done','closed')""", (now(),))


# TP-121 moved the two defaults above under ~/tp-work. An install from before that keeps these (pin_legacy_roots).
LEGACY_ROOTS = {"worktree_root": "~/ticket-pilot/worktrees", "clone_root": "~/Desktop/07_Project"}
# TP-184 changed the default tags from orca / orca-milestone; an install from before keeps them (pin_legacy_tags)
LEGACY_TAGS = {"asana_tag": "orca", "milestone_tag": "orca-milestone"}


def migrate_settings(con, config_dir, *, write_config, backup_config, clear_cache):
    """TP-49 settings must be available before TP-121 and TP-184 pin missing defaults."""
    import_legacy_json(con, config_dir, write_config, backup_config)
    pin_legacy_roots(con, clear_cache)
    pin_legacy_tags(con, clear_cache)


def _should_import_settings(con) -> bool:
    return not (meta_get(con, "config_imported") or con.execute("SELECT 1 FROM config_global LIMIT 1").fetchone())


def import_legacy_json(con, config_dir, write_config, backup_config) -> bool:
    """First run after TP-49: copy config/settings.json + config/project_map.json into a DB that has no settings.
    Runs at most once per DB (meta config_imported); JSON files are never read after that."""
    if not _should_import_settings(con):
        return False
    files = {k: config_dir / f"{k}.json" for k in ("settings", "project_map")}
    if not any(f.is_file() for f in files.values()):
        return False  # nothing to import yet: code defaults apply, and a file that shows up later is still imported
    with _write(con):
        if not _should_import_settings(con):
            return False
        doc = {k: json.loads(f.read_text()) if f.is_file() else {} for k, f in files.items()}
        write_config(con, doc)
        meta_set(con, "config_imported", now())
        event(con, "config", "설정을 config/settings.json + config/project_map.json에서 DB로 가져왔어요 (한 번만)")
    try:
        backup_config(con)
    except Exception:  # noqa: BLE001  a backup failure must not stop the first start
        pass
    return True


def pin_legacy_roots(con, clear_cache) -> bool:
    """TP-121: a DB that was in use before the defaults moved to ~/tp-work (it has stored settings or runs) but never
    stored worktree_root / clone_root gets the old defaults written, so its worktrees and clones stay where they are.
    Stored values are never touched. Once per DB (meta roots_pinned); a new DB only gets the mark.
    Moving the files is a later `tp migrate`."""
    return _pin_old_defaults(con, "roots_pinned", LEGACY_ROOTS,
                             "기본 작업 위치가 ~/tp-work로 바뀌어서 지금 쓰던 값을 설정에 적어 뒀어요 (TP-121)", clear_cache)


def pin_legacy_tags(con, clear_cache) -> bool:
    """TP-184: the same for the Asana tags, so an install that used the orca / orca-milestone defaults keeps them."""
    return _pin_old_defaults(con, "tags_pinned", LEGACY_TAGS,
                             "기본 Asana 태그가 tp로 바뀌어서 지금 쓰던 태그를 설정에 적어 뒀어요 (TP-184)", clear_cache)


def _pin_old_defaults(con, mark: str, old: dict, note: str, clear_cache) -> bool:
    if meta_get(con, mark):
        return False
    pinned = []
    with _write(con):
        if meta_get(con, mark):
            return False
        used = (con.execute("SELECT 1 FROM config_global WHERE ns='settings' LIMIT 1").fetchone()
                or con.execute("SELECT 1 FROM runs LIMIT 1").fetchone())
        if used:
            pos = con.execute("SELECT COALESCE(MAX(pos),-1) FROM config_global WHERE ns='settings'").fetchone()[0]
            for k, v in old.items():
                pos += 1
                cur = con.execute("INSERT OR IGNORE INTO config_global(ns,k,v,pos) VALUES('settings',?,?,?)",
                                  (k, json.dumps(v), pos))
                if cur.rowcount:
                    pinned.append(f"{k}={v}")
        if pinned:
            event(con, "config", f"{note}: " + ", ".join(pinned))
        meta_set(con, mark, now())
    if pinned:
        clear_cache()
    return bool(pinned)
