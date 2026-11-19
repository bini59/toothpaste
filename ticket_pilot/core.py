"""Shared plumbing: paths, settings, SQLite, Asana, gh helpers (and Orca for legacy runs)."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.environ.get("TP_DB", DATA / "tp.db"))
CONFIG = ROOT / "config"
DOCKER_OFF = "Docker 모드에서는 쓰지 않아요"


def docker() -> bool:
    """Docker mode (TP-164): the image sets TP_MODE=docker. Every visitor logs in (lan.py), host install steps are off."""
    return os.environ.get("TP_MODE") == "docker"


# launchd (tick, dashboard) starts with a thin PATH.
os.environ["PATH"] = ":".join(
    ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", os.environ.get("PATH", "")]
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Settings live in SQLite (config_* tables, TP-49). Code holds the defaults: a key the DB does not have uses these.
# config/settings.json + config/project_map.json are read once, when a DB has no settings yet (import_legacy_json);
# git keeps only config/*.example.json. Edit with `tp config` or the dashboard 설정 page (ticket_pilot/config.py).
DEFAULT_SETTINGS = {
    "asana_workspace_gid": "",
    "asana_enabled": True,
    "asana_tag": "tp",
    # an Asana parent task with this tag is a milestone: its subtasks merge into a milestone branch (milestone.py)
    "milestone_tag": "tp-milestone",
    "asana_keychain_service": "ticket-pilot-asana",
    "max_active_runs": 3,
    "agent": "codex",
    # per-role agent cards (TP-168): {work|review|check: {tool, model, effort, ultracode}}; empty = "agent" above
    "agents": {},
    # local runner (runner.py): new ticket worktrees go to <worktree_root>/<repo>/<ticket>, on <branch_prefix><ticket>
    "worktree_root": "~/tp-work/worktrees",
    "branch_prefix": "tp/",
    "clone_root": "~/tp-work/repos",  # 프로젝트 추가 wizard clones a new repo into <clone_root>/<name> (onboard.py)
    "agent_max_min": 240,  # a ticket agent still running after this is stopped and the ticket blocked (0 = no limit)
    "agent_asana_write": True,  # chat / ticket agents may write to Asana through `tp asana` (asana_write.py, TP-97)
    "git_user": "",
    "owner_name": "the user",  # who the agent prompts address ({{OWNER}}, runner.launch)
    "dashboard_port": 3032,
    "theme": "light",  # dashboard theme id (themes.py): a built-in, a ~/tp-work/themes file stem, or auto
    "review": {"max_rework_rounds": 3},
    "check": {"timeout_min": 10, "max_parallel": 2},
    "release": {"weekdays": [0, 1, 2, 3, 4], "at": "10:00", "window_min": 120, "timezone": "Asia/Seoul",
                "staging_branch": "staging", "main_branch": "main", "retarget_since": ""},
    "deploy": {"deploy_timeout_min": 45, "repos": {}},
    "github_issues": {},
    # auto: tp tick runs tp update when it sees a new release; check_min: minutes between its tag fetches (TP-186)
    "update": {"auto": False, "check_min": 360},
    # 로컬망 공유 (TP-152): enabled = launchd/install.sh binds the dashboard to 0.0.0.0; LAN users log in with GitHub
    # and must be active members of org. Only a 127.0.0.1 request changes it (server.H._config)
    "lan_share": {"enabled": False, "org": ""},  # org: asked for when sharing is turned on
}
# TP-121 moved the two defaults above under ~/tp-work. An install from before that keeps these (pin_legacy_roots).
LEGACY_ROOTS = {"worktree_root": "~/ticket-pilot/worktrees", "clone_root": "~/Desktop/07_Project"}
# TP-184 changed the default tags from orca / orca-milestone; an install from before keeps them (pin_legacy_tags)
LEGACY_TAGS = {"asana_tag": "orca", "milestone_tag": "orca-milestone"}
DEFAULT_MAP = {"repos": {}, "projects": [], "hidden_projects": {}}
CONFIG_TTL_S = 2.0  # long-running processes (dashboard) see a change made elsewhere within this many seconds
_CFG_CACHE: dict = {}


def _merge(base, over):
    """over (stored) on top of base (code defaults): dicts merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = {k: _merge(base[k], v) if k in base else v for k, v in over.items()}
        out.update({k: json.loads(json.dumps(v)) for k, v in base.items() if k not in out})
        return out
    return over


def read_config(con) -> dict | None:
    """The stored settings as {"settings": <settings.json shape>, "project_map": <project_map.json shape>},
    without defaults; None when the DB has no settings at all."""
    g = con.execute("SELECT ns,k,v FROM config_global ORDER BY ns, pos").fetchall()
    repos = con.execute("SELECT * FROM config_repos").fetchall()
    projs = con.execute("SELECT * FROM config_projects ORDER BY pos").fetchall()
    hidden = con.execute("SELECT * FROM config_hidden ORDER BY pos").fetchall()
    if not (g or repos or projs or hidden):
        return None
    st = {r["k"]: json.loads(r["v"]) for r in g if r["ns"] == "settings"}
    pm_extra = {r["k"]: json.loads(r["v"]) for r in g if r["ns"] == "project_map"}
    dep = dict(st.get("deploy") or {})
    dep["repos"] = {r["name"]: json.loads(r["deploy"])
                    for r in sorted((r for r in repos if r["deploy"]), key=lambda r: r["deploy_pos"] or 0)}
    st["deploy"] = dep
    mrepos = {}
    for r in sorted((r for r in repos if r["info"]), key=lambda r: r["map_pos"] or 0):
        e = json.loads(r["info"])
        if r["checks"] is not None:
            e["checks"] = json.loads(r["checks"])
        if r["ci"] is not None:
            e["ci"] = bool(r["ci"])
        if r["review"]:
            e["review"] = json.loads(r["review"])
        mrepos[r["name"]] = e
    links: dict = {}
    for r in con.execute("SELECT * FROM config_project_repos ORDER BY pos"):
        links.setdefault(r["project_id"], []).append(
            {"repo": r["repo"], "role": r["role"], **({"default": True} if r["is_default"] else {})})
    projects = [{"id": p["id"], "name": p["name"], "source": p["source"],
                 **({"asana_gid": p["asana_gid"]} if p["asana_gid"] else {}), **json.loads(p["extra"] or "{}"),
                 "repos": links.get(p["id"], [])} for p in projs]
    pm = {**pm_extra, "repos": mrepos, "projects": projects,
          "hidden_projects": {h["asana_gid"]: h["name"] for h in hidden}}
    return {"settings": st, "project_map": pm}


def write_config(con, doc: dict):
    """Replace the stored settings with doc ({"settings", "project_map"}, the export shape). No commit."""
    st, pm = doc.get("settings") or {}, doc.get("project_map") or {}
    for t in ("config_global", "config_repos", "config_projects", "config_project_repos", "config_hidden"):
        con.execute(f"DELETE FROM {t}")
    for i, (k, v) in enumerate(st.items()):
        if k == "deploy":
            v = {dk: dv for dk, dv in (v or {}).items() if dk != "repos"}
        con.execute("INSERT INTO config_global(ns,k,v,pos) VALUES('settings',?,?,?)", (k, json.dumps(v, ensure_ascii=False), i))
    for i, (k, v) in enumerate(pm.items()):
        if k not in ("repos", "projects", "hidden_projects"):
            con.execute("INSERT INTO config_global(ns,k,v,pos) VALUES('project_map',?,?,?)",
                        (k, json.dumps(v, ensure_ascii=False), i))
    rows: dict = {}
    for i, (name, e) in enumerate((pm.get("repos") or {}).items()):
        e = dict(e)
        checks, ci, review = e.pop("checks", None), e.pop("ci", None), e.pop("review", None)
        rows[name] = {"map_pos": i, "info": json.dumps(e, ensure_ascii=False),
                      "checks": None if checks is None else json.dumps(checks, ensure_ascii=False),
                      "ci": None if ci is None else int(bool(ci)),
                      "review": json.dumps(review, ensure_ascii=False) if review else None}
    for i, (name, rule) in enumerate(((st.get("deploy") or {}).get("repos") or {}).items()):
        rows.setdefault(name, {})
        rows[name].update(deploy_pos=i, deploy=json.dumps(rule, ensure_ascii=False))
    for name, r in rows.items():
        con.execute("""INSERT INTO config_repos(name,map_pos,info,checks,ci,review,deploy_pos,deploy,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (name, r.get("map_pos"), r.get("info"), r.get("checks"), r.get("ci"), r.get("review"),
                     r.get("deploy_pos"), r.get("deploy"), now()))
    for i, p in enumerate(norm_projects(pm.get("projects") or [])):
        extra = {k: v for k, v in p.items() if k not in PROJECT_COLS}
        con.execute("INSERT INTO config_projects(id,pos,name,source,asana_gid,extra) VALUES(?,?,?,?,?,?)",
                    (p["id"], i, p.get("name"), p["source"], p.get("asana_gid"), json.dumps(extra, ensure_ascii=False)))
        for j, rr in enumerate(p.get("repos") or []):
            con.execute("INSERT OR REPLACE INTO config_project_repos(project_id,repo,pos,role,is_default) VALUES(?,?,?,?,?)",
                        (p["id"], rr.get("repo"), j, rr.get("role"), 1 if rr.get("default") else 0))
    for i, (gid, name) in enumerate((pm.get("hidden_projects") or {}).items()):
        con.execute("INSERT INTO config_hidden(asana_gid,pos,name) VALUES(?,?,?)", (str(gid), i, name))
    config_cache_clear()


def effective_config(stored: dict | None) -> dict:
    """Stored settings merged over the code defaults (what settings()/project_map() return)."""
    stored = stored or {}
    pm = _merge(DEFAULT_MAP, stored.get("project_map") or {})
    if isinstance(pm.get("projects"), list):
        pm["projects"] = norm_projects(pm["projects"])
    return {"settings": _merge(DEFAULT_SETTINGS, stored.get("settings") or {}), "project_map": pm}


# Projects (TP-91): a project has its own id (slug), a name and 1..N repos; source says where its tickets come from
# (asana = an Asana project, asana_gid; github = issues of its repos; none). Repos no project links show under the
# virtual project UNSORTED (미분류) on the 프로젝트 page.
PROJECT_SOURCES = ("asana", "github", "none")
PROJECT_COLS = ("id", "name", "source", "asana_gid", "repos")
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


def config_cache_clear():
    _CFG_CACHE.clear()


def _config_con():
    return db()


def config() -> dict:
    """Effective settings ({"settings", "project_map"}), cached for CONFIG_TTL_S."""
    import threading
    import time
    t = time.monotonic()
    if _CFG_CACHE.get("db") == str(DB_PATH) and t - _CFG_CACHE.get("at", -1e9) < CONFIG_TTL_S:
        return _CFG_CACHE["val"]
    con = _config_con()
    try:
        val = effective_config(read_config(con))
    finally:
        if threading.current_thread() is not threading.main_thread():
            con.close()  # db() opens a fresh connection per call outside the main thread
    _CFG_CACHE.update(db=str(DB_PATH), at=t, val=val)
    return val


def settings() -> dict:
    return json.loads(json.dumps(config()["settings"]))  # callers may mutate their copy


def repo_rules(name: str | None = None):
    """Per-repo deploy rules (settings -> deploy.repos, see `tp config get global`), normalized.

    staging: {branch, pipeline} or None   prod: {branch, pipeline|workflow|release, ecs?, smoke?}
    via: branch between staging and prod (e.g. main) or None; target = via or prod.branch (where ticket code lands first)
    """
    out = {}
    for n, r in settings().get("deploy", {}).get("repos", {}).items():
        r = dict(r)
        r.setdefault("staging", None)
        r.setdefault("via", None)
        r.setdefault("merge", "merge")
        r.setdefault("modes", ["direct"])
        r.setdefault("train", False)
        r["target"] = r["via"] or r["prod"]["branch"]
        out[n] = r
    return out.get(name) if name is not None else out


def train_repos() -> list[str]:
    return [n for n, r in repo_rules().items() if r["train"]]


def feature_base(repo: str | None) -> str | None:
    """Branch ticket worktrees branch from and ticket PRs target, or None for the GitHub default branch.
    Train repos always use their staging branch; other repos opt in with `pr_into_staging: true` (e.g. main + dev)."""
    rr = repo_rules(repo) if repo else None
    if not rr or not rr["staging"] or not (rr["train"] or rr.get("pr_into_staging")):
        return None
    return rr["staging"]["branch"]


def project_map() -> dict:
    """{"repos": {name: {orca_repo_id, path, github, description, checks?, ci?, review?}}, "projects": [...],
    "hidden_projects": {gid: name}}: the old config/project_map.json shape, read from the DB."""
    return json.loads(json.dumps(config()["project_map"]))


def import_legacy_json(con) -> bool:
    """First run after TP-49: copy config/settings.json + config/project_map.json into a DB that has no settings.
    Runs at most once per DB (meta config_imported); JSON files are never read after that."""
    if meta_get(con, "config_imported") or con.execute("SELECT 1 FROM config_global LIMIT 1").fetchone():
        return False
    files = {k: CONFIG / f"{k}.json" for k in ("settings", "project_map")}
    if not any(f.is_file() for f in files.values()):
        return False  # nothing to import yet: code defaults apply, and a file that shows up later is still imported
    doc = {k: json.loads(f.read_text()) if f.is_file() else {} for k, f in files.items()}
    write_config(con, doc)
    meta_set(con, "config_imported", now())
    event(con, "config", "설정을 config/settings.json + config/project_map.json에서 DB로 가져왔어요 (한 번만)")
    con.commit()
    try:
        from . import config as _c
        _c.backup(con)
    except Exception:  # noqa: BLE001  a backup failure must not stop the first start
        pass
    return True


def pin_legacy_roots(con) -> bool:
    """TP-121: a DB that was in use before the defaults moved to ~/tp-work (it has stored settings or runs) but never
    stored worktree_root / clone_root gets the old defaults written, so its worktrees and clones stay where they are.
    Stored values are never touched. Once per DB (meta roots_pinned); a new DB only gets the mark.
    Moving the files is a later `tp migrate`."""
    return _pin_old_defaults(con, "roots_pinned", LEGACY_ROOTS, "기본 작업 위치가 ~/tp-work로 바뀌어서 지금 쓰던 값을 설정에 적어 뒀어요 (TP-121)")


def pin_legacy_tags(con) -> bool:
    """TP-184: the same for the Asana tags, so an install that used the orca / orca-milestone defaults keeps them."""
    return _pin_old_defaults(con, "tags_pinned", LEGACY_TAGS, "기본 Asana 태그가 tp로 바뀌어서 지금 쓰던 태그를 설정에 적어 뒀어요 (TP-184)")


def _pin_old_defaults(con, mark: str, old: dict, note: str) -> bool:
    if meta_get(con, mark):
        return False
    used = (con.execute("SELECT 1 FROM config_global WHERE ns='settings' LIMIT 1").fetchone()
            or con.execute("SELECT 1 FROM runs LIMIT 1").fetchone())
    pinned = []
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
        config_cache_clear()
    meta_set(con, mark, now())
    con.commit()
    return bool(pinned)


# ---------------------------------------------------------------- SQLite

# TP-91 project tables (also what _migrate_projects recreates). project_map: derived from config_projects (load_map);
# project_gid = its Asana project (NULL without one). config_projects: id = tp's own slug, source asana | github | none,
# asana_gid only for source asana.
PROJECT_TABLES = """
CREATE TABLE IF NOT EXISTS project_map (
  project_gid TEXT, project_name TEXT, repo TEXT, role TEXT, is_default INTEGER,
  confidence TEXT, note TEXT, project_id TEXT,
  PRIMARY KEY (project_id, repo)
);
CREATE TABLE IF NOT EXISTS config_projects (
  id TEXT PRIMARY KEY, pos INTEGER, name TEXT, source TEXT, asana_gid TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS config_project_repos (
  project_id TEXT, repo TEXT, pos INTEGER, role TEXT, is_default INTEGER, PRIMARY KEY (project_id, repo)
);
"""

SCHEMA = PROJECT_TABLES + """
CREATE TABLE IF NOT EXISTS tickets (
  gid TEXT PRIMARY KEY,
  key TEXT,                -- Asana "ID" custom field (CRSC-340) or AS-<gid>
  name TEXT,
  url TEXT,
  project_gid TEXT,
  project_name TEXT,
  section TEXT,
  parent_gid TEXT,
  parent_name TEXT,
  due_on TEXT,
  notes TEXT,
  tags TEXT,
  has_tag INTEGER DEFAULT 0,
  deps_json TEXT,          -- [{gid,name,completed}]
  open_deps INTEGER DEFAULT 0,
  completed INTEGER DEFAULT 0,
  assigned INTEGER DEFAULT 1,
  asana_modified_at TEXT,
  first_seen TEXT,
  last_seen TEXT,
  stage TEXT,
  repo_override TEXT,
  skip INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS repos (
  name TEXT PRIMARY KEY, orca_repo_id TEXT, path TEXT, github TEXT, description TEXT
);
-- settings (TP-49): the source of truth for what config/settings.json + config/project_map.json used to hold.
-- config_global: top-level keys; ns 'settings' (deploy without its repos) or 'project_map' (comments). v = JSON
CREATE TABLE IF NOT EXISTS config_global (ns TEXT, k TEXT, v TEXT, pos INTEGER, PRIMARY KEY (ns, k));
-- one row per repo: map entry (info = orca_repo_id/path/github/description JSON; NULL = deploy rule only), checks
-- ([{name, cmd, dir?, off?, reason?}]; off = kept with a reason but not run), ci, review overrides, deploy rule JSON.
-- map_pos / deploy_pos keep the JSON order (deploy order = release train order)
CREATE TABLE IF NOT EXISTS config_repos (
  name TEXT PRIMARY KEY, map_pos INTEGER, info TEXT, checks TEXT, ci INTEGER, review TEXT,
  deploy_pos INTEGER, deploy TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS config_hidden (asana_gid TEXT PRIMARY KEY, pos INTEGER, name TEXT);
-- every settings change: actor dashboard | cli | import, scope global | <repo> | project:<id>, key = dotted path
CREATE TABLE IF NOT EXISTS settings_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, scope TEXT, key TEXT, old TEXT, new TEXT
);
-- 설정 page "base 브랜치에서 시험 실행": detached `tp check --repo --ref --record <id>`
CREATE TABLE IF NOT EXISTS config_check_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, ref TEXT, sha TEXT, status TEXT, results TEXT, log_path TEXT,
  pid INTEGER, error TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_gid TEXT,
  repo TEXT,
  worktree_id TEXT,
  worktree_name TEXT,
  worktree_path TEXT,
  branch TEXT,
  terminal TEXT,
  ws_status TEXT,
  ws_comment TEXT,
  state TEXT,              -- active | finished | abandoned
  origin TEXT,             -- dispatch | adopt
  created_at TEXT,
  updated_at TEXT,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS prs (
  url TEXT PRIMARY KEY,
  run_id INTEGER,
  ticket_gid TEXT,
  repo TEXT,
  github TEXT,
  number INTEGER,
  title TEXT,
  state TEXT,
  is_draft INTEGER,
  head_sha TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_url TEXT,
  ticket_gid TEXT,
  head_sha TEXT,
  verdict TEXT,            -- approve | changes | blocked
  summary TEXT,
  body TEXT,
  comment_url TEXT,
  posted INTEGER DEFAULT 0,
  created_at TEXT,
  rework_at TEXT           -- when a 'changes' verdict was sent back to the agent (tp dispatch)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, kind TEXT, ticket_gid TEXT, message TEXT
);
-- TP-105 수신함 alerts the owner has to look at (events is the log): kind review | ms_ready; read_at NULL = unread
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, kind TEXT, ticket_gid TEXT, title TEXT, body TEXT, href TEXT, read_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- TP-152 로컬망 공유 logins (lan.py): id_hash = sha256 of the cookie (the cookie itself is never stored), org = the
-- org checked at login, checked_at = last org membership check
CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY, login TEXT, org TEXT, created_at TEXT, last_seen TEXT, checked_at TEXT, ip TEXT
);
CREATE TABLE IF NOT EXISTS staging_diffs (
  repo TEXT PRIMARY KEY,
  github TEXT,
  prod_branch TEXT,
  staging_branch TEXT,
  staging_head TEXT,
  ahead INTEGER,
  hotfixes INTEGER,          -- non-merge commits in prod that staging lacks (staging -> prod may conflict)
  via_ahead INTEGER,         -- via repos: commits in main that production lacks
  pipeline TEXT,             -- staging pipeline status for staging_head (Succeeded/InProgress/Failed/...) or NULL
  verdict TEXT,              -- ready | building | broken | none | error
  oldest_at TEXT,            -- committer date of the oldest commit not in prod
  items_json TEXT,           -- [{kind: ticket|pr|commit|merge, ...}]
  error TEXT,
  checked_at TEXT,
  note_md TEXT, note_level TEXT, note_head TEXT, note_at TEXT
);
-- ^ diffs.py: production vs staging per repo (Orca automation "ticket-pilot: staging diff")
CREATE TABLE IF NOT EXISTS cleanups (
  path TEXT PRIMARY KEY,     -- ticket worktree path (runs.worktree_path)
  ticket_gid TEXT, key TEXT, repo TEXT, branch TEXT,
  heads TEXT,                -- sorted head shas of the merged PRs that were checked
  prs_json TEXT,             -- [{number, url, merge_sha, head_sha, live: merge|cherry|patch|agent|null, signals}]
  prod_sha TEXT, prod_note TEXT,
  status TEXT,               -- waiting | ambiguous | kept | removing | removed | error
  reason TEXT,
  agent_verdict TEXT, agent_note TEXT, agent_sha TEXT,
  checked_at TEXT, removed_at TEXT
);
-- ^ cleanup.py: remove ticket worktrees once production has their change (Orca automation "ticket-pilot: worktree cleanup")
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_url TEXT, ticket_gid TEXT, head_sha TEXT,
  name TEXT,               -- project_map.json repos.<name>.checks[].name, or the GitHub check name for "ci" repos
  cmd TEXT,
  status TEXT,             -- running | pass | fail | flaky (failed, passed on the rerun) | timeout | error | ci_pending
  exit_code INTEGER,
  duration_s REAL,
  log_path TEXT,           -- data/checks/<repo>-<n>-<sha7>-<name>.log, or the GitHub details URL
  created_at TEXT,
  finished_at TEXT,
  attempt INTEGER DEFAULT 1,   -- 2 = rerun after tp could not run the checks (error)
  UNIQUE (pr_url, head_sha, name)   -- also the lock: one run per PR head
);
-- ^ checks.py: tp's own checks on the PR head (tp check); gate the review queue and the dashboard approval
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,                -- KST date of the release train
  repo TEXT,               -- repos.name ('' for a "nothing to release" marker)
  github TEXT,
  status TEXT,             -- pending | deploying | done | aborted | failed | empty
  reason TEXT,
  prs_json TEXT,           -- [{url, number, key, title}]
  release_pr_url TEXT,     -- the staging -> main PR
  merge_sha TEXT,
  started_at TEXT,
  merged_at TEXT,
  finished_at TEXT
);
-- chat.py (프로젝트 page 채팅): the owner's conversations with a headless agent in a repo's checkout. session = the
-- conversation ID the next turn resumes; tool = the agent it belongs to (a tool change starts over).
-- project_id (TP-91, added by _migrate) = the project it belongs to; repo = the checkout its session runs in
CREATE TABLE IF NOT EXISTS chat_conversations (
  id TEXT PRIMARY KEY, repo TEXT, title TEXT, session TEXT, tool TEXT, created_at TEXT, updated_at TEXT
);
-- one row per message or compaction. A NULL text marks a running process (handle = runner handle).
-- A finished compaction stores the handoff in text, which chat.messages displays as a divider.
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, role TEXT, text TEXT, ts TEXT,
  handle TEXT, failed INTEGER DEFAULT 0, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS chat_messages_conv ON chat_messages(conversation_id, id);
"""


_CON: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    """One connection per process (the dashboard server opens its own per request thread)."""
    global _CON
    import threading
    if _CON is not None and threading.current_thread() is threading.main_thread():
        return _CON
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # Every writer (automations, dashboard via launchd) must run on this Mac's kernel: SQLite locks do not cross
    # a Docker Desktop bind mount, and two writers there corrupted the DB (TP-8). DELETE journal is enough at
    # this load (busy_timeout waits) and keeps tp.db a single file that cp/backup/VACUUM INTO handle as-is.
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    _migrate(con)
    import_legacy_json(con)
    pin_legacy_roots(con)
    pin_legacy_tags(con)
    if threading.current_thread() is threading.main_thread():
        _CON = con
    return con


_COLUMNS = {
    # source: NULL = AI review, 'check' = tp check failure sent back to the agent through the same rework flow
    "reviews": {"rework_at": "TEXT", "source": "TEXT"},
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
                # milestone.py: the Asana parent has the milestone tag; head the AI approval auto-merged once
                "in_milestone": "INTEGER", "auto_approved_sha": "TEXT",
                # milestone tickets: when the owner pressed 마일스톤 작업 완료 (cleared again when a subtask comes in)
                "ms_done_at": "TEXT",
                # pipeline.restage: when the stage became done/closed (NULL again once reopened); the board hides old ones
                "done_at": "TEXT"},
    # mergeable: GitHub MERGEABLE | CONFLICTING | NULL (not computed yet; GitHub answers UNKNOWN lazily)
    "prs": {"base": "TEXT", "merged_at": "TEXT", "merge_sha": "TEXT", "release_id": "INTEGER", "mergeable": "TEXT",
            # track.py: production verification for merged PRs outside the release train
            "prod_at": "TEXT", "prod_note": "TEXT", "track_sha": "TEXT",
            # pipeline.conflict_rework: head commit the agent was relaunched on to resolve a base-branch conflict
            "conflict_sha": "TEXT", "conflict_at": "TEXT"},
    # chat.py (TP-72): JSON list of {name, file, size, type} of a user message's files in data/chat-uploads/<conv>/
    "chat_messages": {"attachments": "TEXT", "input_tokens": "INTEGER", "cache_read_input_tokens": "INTEGER",
                      "cache_creation_input_tokens": "INTEGER", "output_tokens": "INTEGER",
                      "context_tokens": "INTEGER", "before_context_tokens": "INTEGER", "compaction_id": "INTEGER",
                      "claude_session_id": "TEXT"},
    # TP-152: who did it (dashboard request: GitHub login of a LAN user, owner_name on 127.0.0.1; NULL = tp itself)
    "events": {"actor": "TEXT"},
    # TP-91: conversations belong to a project (several repos), not a repo
    # TP-103: read_id = the last assistant message the owner saw (unread = finished replies after it)
    "chat_conversations": {"project_id": "TEXT", "read_id": "INTEGER"},
    # deploy.py (배포 page): mode NULL = the weekday train. state_json = resumable step state, lock_until = tick claim
    "releases": {"mode": "TEXT", "ticket_gid": "TEXT", "step": "TEXT", "state_json": "TEXT", "version": "TEXT",
                 "requested_at": "TEXT", "lock_until": "TEXT"},
}


def _cols(con, table) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate_projects(con) -> bool:
    """TP-91: config_projects / config_project_repos keyed by asana_gid -> by the project's own id (norm_projects).
    project_map is a derived copy: recreated with project_id and refilled from the new tables.
    One write transaction (re-checked under the lock): a failure leaves the old tables, and a concurrent process
    (tick vs dashboard right after a deploy) migrates once."""
    if "id" in _cols(con, "config_projects"):
        return False
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        if "id" in _cols(con, "config_projects"):
            con.rollback()
            return False
        _rebuild_projects(con)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return True


def _rebuild_projects(con):
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
    stored = read_config(con)
    if stored:
        load_map(con, effective_config(stored)["project_map"])
    if old:
        event(con, "config", f"프로젝트 {len(old)}개에 id를 붙였어요 (TP-91): "
              + ", ".join(f"{p['name']} → {p['id']}" for p in projects))


def _migrate(con):
    if "actor" not in _cols(con, "events"):  # first: _migrate_projects logs an event (TP-152)
        con.execute("ALTER TABLE events ADD COLUMN actor TEXT")
    changed = _migrate_projects(con)
    backfill_done = backfill_conv = backfill_read = False
    for table, cols in _COLUMNS.items():
        have = _cols(con, table)
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                changed = True
                backfill_done = backfill_done or (table, col) == ("tickets", "done_at")
                backfill_conv = backfill_conv or (table, col) == ("chat_conversations", "project_id")
                backfill_read = backfill_read or (table, col) == ("chat_conversations", "read_id")
    if backfill_conv:
        # a repo's conversations go to the first project (settings order) that has the repo, else 미분류
        con.execute(f"""UPDATE chat_conversations SET project_id=COALESCE(
                          (SELECT l.project_id FROM config_project_repos l JOIN config_projects p ON p.id=l.project_id
                           WHERE l.repo=chat_conversations.repo ORDER BY p.pos LIMIT 1), '{UNSORTED}')""")
    if backfill_read:
        # existing conversations start read up to their last finished message (a turn running now still counts once done)
        con.execute("""UPDATE chat_conversations SET read_id=COALESCE((SELECT MAX(id) FROM chat_messages
                         WHERE conversation_id=chat_conversations.id AND text IS NOT NULL), 0)""")
    if backfill_done:  # after the loop: prs.prod_at / merged_at may have just been added too
        # finished tickets: their PR's production / merge time, else the Asana change, else the migration time
        con.execute("""UPDATE tickets SET done_at=COALESCE(
                         (SELECT MAX(COALESCE(prod_at, merged_at)) FROM prs WHERE ticket_gid=tickets.gid),
                         asana_modified_at, ?) WHERE stage IN ('done','closed')""", (now(),))
    if changed:
        con.commit()


_ACTOR = threading.local()


def set_actor(name: str | None):
    """The dashboard sets this per request thread (server.H._gate); event() records it."""
    _ACTOR.name = name


def current_actor() -> str | None:
    return getattr(_ACTOR, "name", None)


def event(con, kind: str, message: str, ticket_gid: str | None = None):
    con.execute("INSERT INTO events(ts,kind,ticket_gid,message,actor) VALUES(?,?,?,?,?)",
                (now(), kind, ticket_gid, message, current_actor()))


def meta_set(con, k: str, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def meta_get(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def load_map(con, m: dict | None = None):
    """Copy the repo map into the repos / project_map tables (derived copies the queries join on)."""
    m = m or project_map()
    con.execute("DELETE FROM repos")
    for name, r in m["repos"].items():
        con.execute(
            "INSERT INTO repos(name,orca_repo_id,path,github,description) VALUES(?,?,?,?,?)",
            (name, r.get("orca_repo_id"), os.path.expanduser(r["path"]), r.get("github"), r.get("description")),
        )
    con.execute("DELETE FROM project_map")
    for p in m["projects"]:
        rows = p.get("repos") or [{"repo": None, "role": None}]
        for rr in rows:
            con.execute(
                "INSERT OR REPLACE INTO project_map(project_gid,project_name,repo,role,is_default,confidence,note,"
                "project_id) VALUES(?,?,?,?,?,?,?,?)",
                (p.get("asana_gid"), p["name"], rr.get("repo"), rr.get("role"), 1 if rr.get("default") else 0,
                 p.get("confidence"), p.get("note"), p["id"]),
            )
    # completed tickets are not re-synced; keep their project names in line with the map too
    con.execute("UPDATE tickets SET project_name=(SELECT project_name FROM project_map pm WHERE pm.project_gid=tickets.project_gid LIMIT 1) "
                "WHERE project_gid IN (SELECT project_gid FROM project_map)")


def repos_for_project(con, project_gid: str | None):
    if not project_gid:
        return []
    return [dict(r) for r in con.execute(
        "SELECT pm.*, r.orca_repo_id, r.path, r.github, r.description FROM project_map pm "
        "JOIN repos r ON r.name = pm.repo WHERE pm.project_gid=? ORDER BY pm.is_default DESC", (project_gid,))]


# ---------------------------------------------------------------- Asana

class AsanaError(RuntimeError):
    pass


NO_WORKSPACE = "Asana 워크스페이스가 비어 있어요. 설정 → Asana 연결에서 워크스페이스를 고르세요"


def asana_workspace() -> str:
    """The chosen Asana workspace gid (TP-148: picked on 설정 → Asana 연결), read fresh on every call."""
    if not settings().get("asana_enabled", True):
        raise AsanaError("Asana 사용이 꺼져 있어요. 설정 → Asana 연결에서 켜세요")
    ws = settings().get("asana_workspace_gid")
    if not ws:
        raise AsanaError(NO_WORKSPACE)
    return ws


def asana_token() -> str:
    tok = os.environ.get("ASANA_PAT")
    if tok:
        return tok.strip()
    svc = settings()["asana_keychain_service"]
    try:
        tok = secret_get(svc)
    except RuntimeError as e:  # secrets.json unreadable (mode, JSON): callers handle AsanaError
        raise AsanaError(str(e)) from None
    if not tok:
        raise AsanaError("Asana PAT가 없어요. 대시보드 설정에서 Asana PAT를 등록하세요 " + (
            f"(터미널: security add-generic-password -U -s {svc} -a asana -w)" if KEYCHAIN else "(또는 ASANA_PAT 환경변수)"))
    return tok


_FIXTURE: dict | None = None


def _fixture(path: str):
    """Offline testing: TP_ASANA_FIXTURE=<json file mapping API path -> response>."""
    global _FIXTURE
    if _FIXTURE is None:
        _FIXTURE = json.loads(Path(os.environ["TP_ASANA_FIXTURE"]).read_text())
    if path in _FIXTURE:
        return _FIXTURE[path]
    if path.endswith("/stories"):
        return {"data": []}
    raise AsanaError(f"fixture miss: {path}")


def asana(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    if not settings().get("asana_enabled", True):
        raise AsanaError("Asana 사용이 꺼져 있어요. 설정 → Asana 연결에서 켜세요")
    if os.environ.get("TP_ASANA_FIXTURE"):
        return _fixture(path)
    url = "https://app.asana.com/api/1.0" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps({"data": body}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {asana_token()}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        raise AsanaError(f"{method} {path} -> {e.code}: {e.read()[:400]!r}") from None


def asana_all(path: str, params: dict) -> list[dict]:
    out, params = [], dict(params, limit=100)
    while True:
        j = asana("GET", path, params)
        out += j.get("data", [])
        nxt = (j.get("next_page") or {}).get("offset")
        if not nxt:
            return out
        params["offset"] = nxt


TASK_FIELDS = ",".join([
    "name", "notes", "due_on", "completed", "permalink_url", "modified_at",
    "projects.name", "tags.name", "memberships.project.gid", "memberships.section.name",
    "parent.name", "parent.projects.name", "parent.custom_fields.name", "parent.custom_fields.display_value", "parent.tags.name",
    "custom_fields.name", "custom_fields.display_value", "dependencies", "assignee",
])


def asana_comments(gid: str) -> list[dict]:
    st = asana_all(f"/tasks/{gid}/stories", {"opt_fields": "type,resource_subtype,text,created_by.name,created_at"})
    return [s for s in st if s.get("type") == "comment" or s.get("resource_subtype") == "comment_added"]


# ---------------------------------------------------------------- GitHub token (TP-112)
# tp and its agents use their own OAuth token (dashboard 설정 > GitHub 연결, server.py), never the owner's `gh` login:
# GH_TOKEN carries the token and GH_CONFIG_DIR points gh at an empty tp-only dir instead of ~/.config/gh.

GITHUB_KEYCHAIN = "ticket-pilot-github"
GH_CONFIG_DIR = DATA / "gh"
# https git uses the same token: drop every configured credential helper (the owner's gh login, osxkeychain), then gh's.
# Without a token gh's helper is left out too: it falls back to the owner's keychain entry even with an empty GH_CONFIG_DIR.
GIT_NO_CREDENTIAL_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                         "GIT_TERMINAL_PROMPT": "0"}
GIT_CREDENTIAL_ENV = {**GIT_NO_CREDENTIAL_ENV, "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "credential.helper",
                      "GIT_CONFIG_VALUE_1": "!gh auth git-credential"}


class GitHubNotConnected(RuntimeError):
    def __init__(self):
        super().__init__("GitHub가 연결되지 않았어요. 대시보드 설정에서 GitHub를 연결하세요")


# ---------------------------------------------------------------- secrets (TP-162)
# macOS: the login keychain (`security`). Elsewhere: SECRETS_FILE, {service: {account: value}}, mode 600 in a 700 folder.
KEYCHAIN = sys.platform == "darwin"
# Docker mode: TP_SECRETS_FILE points into the data volume
SECRETS_FILE = Path(os.environ.get("TP_SECRETS_FILE") or Path.home() / ".config" / "ticket-pilot" / "secrets.json")
_SECRET_RE = re.compile(r"[\w./:+=-]+")  # names and values go into a `security -i` command line on macOS
_secrets_lock = threading.Lock()  # dashboard threads: two saves must not drop each other's entry


def secret_store() -> str:
    """Where secrets live, for messages."""
    return "keychain" if KEYCHAIN else str(SECRETS_FILE)


def _secrets_read() -> dict:
    try:
        mode = SECRETS_FILE.stat().st_mode & 0o777
    except FileNotFoundError:
        return {}
    if mode & 0o077:  # another user could have read it: refuse rather than keep using a leaked token
        raise RuntimeError(f"{SECRETS_FILE} 권한이 {mode:o}이라 읽지 않았어요. 600이어야 해요: chmod 600 {SECRETS_FILE}")
    try:
        d = json.loads(SECRETS_FILE.read_text())
    except ValueError as e:
        raise RuntimeError(f"{SECRETS_FILE}이 JSON이 아니에요: {e}") from None
    if not isinstance(d, dict):
        raise RuntimeError(f"{SECRETS_FILE}이 JSON 객체가 아니에요")
    return d


def _secrets_write(d: dict):
    folder = SECRETS_FILE.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".secrets-")  # mode 600; same folder, so the rename is atomic
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SECRETS_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def secret_get(service: str, account: str | None = None) -> str | None:
    """The stored value, None when there is none. account None: any account of service."""
    if KEYCHAIN:
        p = subprocess.run(["security", "find-generic-password", "-s", service, *(["-a", account] if account else []),
                            "-w"], capture_output=True, text=True)
        return (p.stdout.strip() or None) if p.returncode == 0 else None
    accts = _secrets_read().get(service) or {}
    v = accts.get(account) if account else next(iter(accts.values()), None)
    return v if isinstance(v, str) and v else None


def secret_set(service: str, account: str, value: str):
    """Store value (replacing the old one) and read it back; RuntimeError when that fails. Never logs the value."""
    if not all(isinstance(x, str) and _SECRET_RE.fullmatch(x) for x in (service, account, value)):
        raise RuntimeError("저장할 수 없는 형식이에요")
    if KEYCHAIN:
        # through stdin (`security -i`) so the value never shows up in the process list
        subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {service} -a {account} -w {value}\n",
                       capture_output=True, text=True, timeout=30)
    else:
        with _secrets_lock:
            d = _secrets_read()
            d.setdefault(service, {})[account] = value
            _secrets_write(d)
    if secret_get(service, account) != value:
        raise RuntimeError(f"{secret_store()}에 저장하지 못했어요")


def secret_delete(service: str, account: str):
    """Remove the value; RuntimeError when it is still there afterwards. Missing is fine."""
    if KEYCHAIN:
        subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account], capture_output=True,
                       timeout=30)
    else:
        with _secrets_lock:
            d = _secrets_read()
            if account in (d.get(service) or {}):
                del d[service][account]
                if not d[service]:
                    del d[service]
                _secrets_write(d)
    if secret_get(service, account):
        raise RuntimeError(f"{secret_store()}에서 지우지 못했어요")


def github_token() -> str | None:
    return secret_get(GITHUB_KEYCHAIN, "github")  # ponytail: one `security` call per gh/git run, cache if it ever shows up


def gh_env(base: dict | None = None, required: bool = True) -> dict:
    """base (default os.environ) with tp's GitHub token for gh and https git. required: raise GitHubNotConnected
    when no token is stored; otherwise gh/git just fail to authenticate (never with the owner's login)."""
    try:
        tok = github_token()
    except RuntimeError:  # secrets.json unreadable: gh says why; local git works without a token
        if required:
            raise
        tok = None
    if not tok and required:
        raise GitHubNotConnected()
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")}
    GH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env.update(GIT_CREDENTIAL_ENV if tok else GIT_NO_CREDENTIAL_ENV, GH_CONFIG_DIR=str(GH_CONFIG_DIR))
    if tok:
        env["GH_TOKEN"] = tok
    return env


# ---------------------------------------------------------------- Orca / gh / shell

def run(cmd: list[str], cwd: str | None = None, timeout: int = 300, check: bool = True,
        env: dict | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    # every gh call needs tp's token; git only for https remotes, and local git commands must work without one
    if cmd and cmd[0] in ("gh", "git"):
        env = gh_env(env, required=cmd[0] == "gh")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed ({p.returncode}): {p.stderr.strip()[-800:] or p.stdout.strip()[-800:]}")
    return p


def orca_json(args: list[str], timeout: int = 300) -> dict:
    p = run(["orca", *args, "--json"], timeout=timeout, check=False)
    try:
        j = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"orca {' '.join(args[:3])}: non-JSON output: {p.stdout[-400:]} {p.stderr[-400:]}")
    if not j.get("ok", False):
        raise RuntimeError(f"orca {' '.join(args[:3])}: {json.dumps(j.get('error') or j)[:800]}")
    return j["result"]


def gh_json(args: list[str], cwd: str | None = None):
    p = run(["gh", *args], cwd=cwd, timeout=120)
    return json.loads(p.stdout or "null")
