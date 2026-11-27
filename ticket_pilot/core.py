"""Shared plumbing: paths, settings, Asana, gh helpers (and Orca for legacy runs)."""
from __future__ import annotations

import fcntl
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
from contextlib import contextmanager
from pathlib import Path

from .storage import connection, migrations, settings_backup
from .storage.connection import close_db
from .storage.migrations import UNSORTED, norm_projects
from .storage.records import current_actor, event, meta_get, meta_set, now, set_actor
from .storage.schema import SCHEMA

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
# (Asana project, GitHub issues, Notion data source or none). Repos no project links show under the
# virtual project UNSORTED (미분류) on the 프로젝트 page.
PROJECT_SOURCES = ("asana", "github", "notion", "none")
PROJECT_COLS = ("id", "name", "source", "asana_gid", "repos")


def config_cache_clear():
    _CFG_CACHE.clear()


def _config_con():
    return db()


def config() -> dict:
    """Effective settings ({"settings", "project_map"}), cached for CONFIG_TTL_S."""
    import time
    t = time.monotonic()
    db_key = (os.getpid(), str(DB_PATH))
    if _CFG_CACHE.get("db") == db_key and t - _CFG_CACHE.get("at", -1e9) < CONFIG_TTL_S:
        return _CFG_CACHE["val"]
    con = _config_con()
    try:
        val = effective_config(read_config(con))
    finally:
        if threading.current_thread() is not threading.main_thread():
            con.close()  # db() opens a fresh connection per call outside the main thread
    _CFG_CACHE.update(db=db_key, at=t, val=val)
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


def default_repo(project: dict) -> str | None:
    links = project.get("repos") or []
    return next((r["repo"] for r in links if r.get("default")), links[0]["repo"] if links else None)


def db() -> sqlite3.Connection:
    """Open the configured DB with ticket-pilot's settings migration hooks."""
    return connection.open_db(DB_PATH, _migrate, _migrate_settings)


def _migrate(con):
    migrations.migrate_schema(con, _refresh_migrated_map)


def _refresh_migrated_map(con):
    stored = read_config(con)
    if stored:
        load_map(con, effective_config(stored)["project_map"])


def _migrate_settings(con):
    migrations.migrate_settings(con, CONFIG, write_config=write_config,
                                backup_config=_backup_legacy_config, clear_cache=config_cache_clear)


def _backup_legacy_config(con):
    settings_backup.write_backup(effective_config(read_config(con)), settings_backup.backup_dir(DB_PATH))


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
                (f"notion:{p['id']}" if p.get("source") == "notion" else p.get("asana_gid"),
                 p["name"], rr.get("repo"), rr.get("role"), 1 if rr.get("default") else 0,
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
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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
        raise AsanaError(f"{method} {path} -> {e.code}: {e.read()[:400]!r}", e.code) from None


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


@contextmanager
def _secret_update():
    if KEYCHAIN:
        yield
        return
    # Both the dashboard and tick update this JSON file. The lock stays in place across atomic replacements.
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with SECRETS_FILE.with_name(SECRETS_FILE.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
    with _secret_update():
        if KEYCHAIN:
            # through stdin (`security -i`) so the value never shows up in the process list
            subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {service} -a {account} -w {value}\n",
                           capture_output=True, text=True, timeout=30)
        else:
            d = _secrets_read()
            d.setdefault(service, {})[account] = value
            _secrets_write(d)
        if secret_get(service, account) != value:
            raise RuntimeError(f"{secret_store()}에 저장하지 못했어요")


def secret_delete(service: str, account: str):
    """Remove the value; RuntimeError when it is still there afterwards. Missing is fine."""
    with _secret_update():
        if KEYCHAIN:
            subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account], capture_output=True,
                           timeout=30)
        else:
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


def gh_api(method: str, path: str, body: dict | None = None) -> dict:
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    return json.loads(run(cmd, timeout=120, input=json.dumps(body) if body is not None else None).stdout or "{}")


def gh_json(args: list[str], cwd: str | None = None):
    p = run(["gh", *args], cwd=cwd, timeout=120)
    return json.loads(p.stdout or "null")
