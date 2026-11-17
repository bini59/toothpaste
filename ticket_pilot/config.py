"""Settings editing (TP-49): scopes and paths, validation, history, backups, export/import, base-branch check trials.

core.py stores and reads the settings (config_* tables, settings()/repo_rules()/project_map() with code defaults);
this module changes them. Every change goes through save(): validate -> write -> settings_history -> backup.

Scopes (CLI paths and POST /api/config):
  global            settings.json top level (deploy without its repos) + hidden_projects
  repo.<name>       path, github, description, setup, worktree_base, orca_repo_id (legacy), dev, depends_on, checks, ci,
                    review, deploy, links (the projects it belongs to)
  project.<id>      a project (TP-91): name, source (asana | github | none), asana_gid (source asana only), repos
                    [{repo, role, default}], confidence / note. Its repos can also be edited through repo.<name>.links
Export / import / backups use the old file shapes: {"settings": <settings.json>, "project_map": <project_map.json>}.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import core
from .core import ROOT, db, event, now

MODES = ("staging", "direct", "promote", "all")
MERGES = ("merge", "squash")
VERSIONS = ("npm-patch",)
AGENTS = ("codex", "claude")
# reasoning effort per agent CLI: claude --effort accepts these; codex models list theirs in `codex debug models`
# (this is the union; a catalog model only takes its own levels, see agent_models)
AGENT_EFFORTS = {"codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
                 "claude": ("low", "medium", "high", "xhigh", "max")}
AGENT_KEYS = {"tool", "model", "effort", "ultracode"}
AGENT_ROLES = ("work", "review", "check")  # global.agents cards (runner.resolve role)
# claude has no model list command (and tp must not use its OAuth token): the aliases `claude --model` takes
CLAUDE_MODELS = [{"id": "opus", "name": "Opus (최신)"}, {"id": "sonnet", "name": "Sonnet (최신)"},
                 {"id": "fable", "name": "Fable (최신)"}, {"id": "haiku", "name": "Haiku (최신)"}]
MODELS_TTL_S = 600
_models_cache: dict = {"at": 0.0, "codex": []}


def codex_models() -> list[dict]:
    """Models the codex CLI lists for this login (`codex debug models`, visibility "list"), best first, each with its
    reasoning levels. Cached for MODELS_TTL_S; [] when codex is missing or fails (the page falls back to typing)."""
    import time
    if time.time() - _models_cache["at"] < MODELS_TTL_S:
        return _models_cache["codex"]
    out = []
    try:
        p = subprocess.run(["codex", "debug", "models"], capture_output=True, text=True, timeout=30)
        for m in json.loads(p.stdout).get("models", []):
            if m.get("visibility") != "list" or not m.get("slug"):
                continue
            out.append({"id": m["slug"], "name": m.get("display_name") or m["slug"], "priority": m.get("priority") or 0,
                        "efforts": [e.get("effort") for e in m.get("supported_reasoning_levels") or [] if e.get("effort")],
                        "default_effort": m.get("default_reasoning_level")})
        out.sort(key=lambda m: -m["priority"])
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
        out = []
    _models_cache.update(at=time.time(), codex=out)
    return out


def agent_models() -> dict:
    """Model choices per agent tool for the 설정 page (id, name, efforts, default_effort)."""
    claude = [m | {"efforts": list(AGENT_EFFORTS["claude"]), "default_effort": None} for m in CLAUDE_MODELS]
    return {"codex": codex_models(), "claude": claude}
# need a dashboard restart: shown read-only on the 설정 page (the CLI can still change them)
READ_ONLY = ("dashboard_port", "asana_keychain_service")
# setup: shell script run in each new ticket worktree before the agent (runner.launch); worktree_base: branch new
# ticket worktrees start from when the repo has no feature base (pipeline.worktree_base)
# agent: {tool, model, effort, ultracode} for this repo's ticket agents (runner.resolve); empty = global agent / CLI config
# auto_setup: false turns off the automatic worktree prep (.env copy + pnpm/yarn/npm install) for this repo
# dev: {cmd, port} for the 마일스톤 page's dev server (devserver.py); empty = package.json script / framework default
# depends_on: repos whose production may need this repo's staging change first (diffs.diff_queue -> the diff reviewer)
REPO_INFO = ("path", "github", "orca_repo_id", "description", "setup", "worktree_base", "agent", "auto_setup", "dev",
             "depends_on")
REPO_KEYS = ("checks", "ci", "review", "deploy", "links")
DEPLOY_KEYS = {"modes", "merge", "train", "staging", "prod", "via", "version", "pr_into_staging", "preview_label",
               "retarget_since", "_note", "_comment"}
CHECK_KEYS = {"name", "cmd", "dir", "off", "reason"}
PROJECT_ID = r"[a-z0-9][a-z0-9-]{0,39}"
BACKUP_KEEP = 50


class ConfigError(SystemExit):
    """Invalid settings: nothing was saved. .errors = one line per problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("설정이 올바르지 않아요: " + "; ".join(errors))


@contextmanager
def _db():
    """One connection per operation; closed outside the main thread (db() opens a new one per call there)."""
    con = db()
    try:
        yield con
    finally:
        if threading.current_thread() is not threading.main_thread():
            con.close()


def backup_dir() -> Path:
    return core.DB_PATH.parent / "backups"


def current(con) -> dict:
    """Effective settings {"settings", "project_map"} (stored values over code defaults)."""
    return core.effective_config(core.read_config(con))


# ---------------------------------------------------------------- views (what the CLI paths and the dashboard edit)

def repo_names(doc) -> list[str]:
    pm, dep = doc["project_map"]["repos"], doc["settings"]["deploy"]["repos"]
    return list(pm) + [n for n in dep if n not in pm]


def repo_view(doc, name) -> dict:
    e = dict(doc["project_map"]["repos"].get(name) or {})
    v = {k: e.pop(k, None) for k in REPO_INFO}
    checks, ci, review = e.pop("checks", None), e.pop("ci", None), e.pop("review", None)
    v.update(e)  # any other map keys stay editable
    links = [{"project_id": p["id"], "project": p.get("name"), **({"asana_gid": p["asana_gid"]} if p.get("asana_gid") else {}),
              "role": rr.get("role"), "default": bool(rr.get("default"))}
             for p in doc["project_map"]["projects"] for rr in p.get("repos") or [] if rr.get("repo") == name]
    v.update(checks=list(checks or []), ci=bool(ci), review=dict(review or {}),
             deploy=copy.deepcopy(doc["settings"]["deploy"]["repos"].get(name)), links=links)
    return v


def global_view(doc) -> dict:
    st = doc["settings"]
    g = {k: copy.deepcopy(v) for k, v in st.items() if k != "deploy"}
    g["deploy"] = {k: v for k, v in st["deploy"].items() if k != "repos"}
    g["hidden_projects"] = dict(doc["project_map"].get("hidden_projects") or {})
    return g


def project_view(p) -> dict:
    return {"source": p.get("source"), "asana_gid": p.get("asana_gid"),
            **{k: copy.deepcopy(v) for k, v in p.items() if k != "id"}, "repos": copy.deepcopy(p.get("repos") or [])}


def view(doc) -> dict:
    return {"global": global_view(doc),
            "repo": {n: repo_view(doc, n) for n in repo_names(doc)},
            "project": {str(p["id"]): project_view(p) for p in doc["project_map"]["projects"]}}


def apply(doc: dict, scope: str, value) -> dict:
    """A copy of doc with one scope replaced by value (a view of that scope; None deletes a repo or project)."""
    doc = copy.deepcopy(doc)
    st, pm = doc["settings"], doc["project_map"]
    kind, _, name = scope.partition(".")
    if kind == "global" and not name:
        if not isinstance(value, dict):
            raise ConfigError(["global: 객체여야 해요"])
        for k, v in value.items():
            if k == "hidden_projects":
                pm["hidden_projects"] = v if v is not None else {}
            elif k == "deploy":
                st["deploy"] = {**(v or {}), "repos": st["deploy"]["repos"]}
            elif v is None:
                st.pop(k, None)  # back to the code default
            else:
                st[k] = v
        return doc
    if kind == "repo" and name:
        if value is None:
            pm["repos"].pop(name, None)
            st["deploy"]["repos"].pop(name, None)
            for p in pm["projects"]:
                p["repos"] = [r for r in p.get("repos") or [] if r.get("repo") != name]
            for e in pm["repos"].values():
                if isinstance(e, dict) and isinstance(e.get("depends_on"), list) and name in e["depends_on"]:
                    rest = [x for x in e.pop("depends_on") if x != name]
                    if rest:
                        e["depends_on"] = rest
            return doc
        if not isinstance(value, dict):
            raise ConfigError([f"repo.{name}: 객체여야 해요"])
        info = {k: v for k, v in value.items() if k not in REPO_KEYS and v is not None}
        entry = dict(info)
        if value.get("checks"):
            entry["checks"] = value["checks"]
        if value.get("ci"):
            entry["ci"] = value["ci"]
        review = {k: v for k, v in (value.get("review") or {}).items() if v is not None}
        if review:
            entry["review"] = review
        if entry:
            pm["repos"][name] = entry
        else:
            pm["repos"].pop(name, None)
        if value.get("deploy"):
            st["deploy"]["repos"][name] = value["deploy"]  # an existing rule keeps its place (train order)
        else:
            st["deploy"]["repos"].pop(name, None)
        if "links" in value:
            _set_links(pm, name, value["links"] or [])
        return doc
    if kind == "project" and name:
        ps = pm["projects"]
        i = next((i for i, p in enumerate(ps) if str(p["id"]) == name), None)
        if value is None:
            if i is not None:
                ps.pop(i)
            return doc
        if not isinstance(value, dict):
            raise ConfigError([f"project.{name}: 객체여야 해요"])
        repos = ps[i].get("repos", []) if i is not None else []
        p = {"id": name, **{k: v for k, v in value.items() if k not in ("id", "repos") and v is not None},
             "repos": value["repos"] if "repos" in value else repos}
        p.setdefault("source", "asana" if p.get("asana_gid") else "none")
        if i is None:
            ps.append(p)
        else:
            ps[i] = p
        return doc
    raise ConfigError([f"알 수 없는 범위 '{scope}' (global, repo.<이름>, project.<id>)"])


def _set_links(pm, repo, links):
    """The projects a repo belongs to: [{project_id | asana_gid, project?, role, default}]. A link by asana_gid goes to
    the project of that Asana project; an unknown one adds a new asana project (name = project). A new link's default
    only applies to a project without a default repo (the 레포 추가 wizard must not move a project's chat repo; set
    the default on the project, project.<id>.repos)."""
    want = {}
    by_gid = {str(p["asana_gid"]): p["id"] for p in pm["projects"] if p.get("asana_gid")}
    for ln in links:
        if not isinstance(ln, dict) or not (ln.get("project_id") or ln.get("asana_gid")):
            raise ConfigError([f"repo.{repo}.links: 항목마다 project_id나 asana_gid가 필요해요"])
        pid = ln.get("project_id") or by_gid.get(str(ln["asana_gid"]))
        if not pid:  # a new Asana project: norm_projects gives it an id
            pm["projects"] = core.norm_projects(pm["projects"] + [{"asana_gid": str(ln["asana_gid"]),
                                                                     "name": ln.get("project") or str(ln["asana_gid"])}])
            pid = pm["projects"][-1]["id"]
            by_gid[str(ln["asana_gid"])] = pid
            pm["projects"][-1]["repos"] = []
        want[str(pid)] = ln
    unknown = set(want) - {str(p["id"]) for p in pm["projects"]}
    if unknown:
        raise ConfigError([f"repo.{repo}.links: 없는 프로젝트예요: {', '.join(sorted(unknown))}"])
    for p in pm["projects"]:
        pid = str(p["id"])
        rows = list(p.get("repos") or [])
        mine = next((r for r in rows if r.get("repo") == repo), None)
        if pid in want:
            ln = want.pop(pid)
            dflt = ln.get("default") and (mine or not any(r.get("default") for r in rows))
            new = {"repo": repo, "role": ln.get("role"), **({"default": True} if dflt else {})}
            if mine:
                rows[rows.index(mine)] = new
            else:
                rows.append(new)
            if dflt:  # one default repo per project
                rows = [r if r.get("repo") == repo else {k: v for k, v in r.items() if k != "default"} for r in rows]
        elif mine:
            rows.remove(mine)
        p["repos"] = rows


# ---------------------------------------------------------------- validation

def _int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def _text(v):
    return isinstance(v, str) and v.strip() != ""


def validate(doc: dict, old: dict | None = None) -> list[str]:
    """Problems with doc (empty = valid). Paths are checked on disk only where they are new or changed."""
    err: list[str] = []
    st, pm = doc["settings"], doc["project_map"]
    oldrepos = (old or {}).get("project_map", {}).get("repos", {})

    def need(ok, path, msg):
        if not ok:
            err.append(f"{path}: {msg}")

    need(_int(st.get("max_active_runs"), 1, 10), "global.max_active_runs", "1~10 사이 정수여야 해요")
    need(st.get("agent") in AGENTS, "global.agent", f"{', '.join(AGENTS)} 중 하나여야 해요")

    def agent_conf(path, ag, inherit):
        """{tool, model, effort, ultracode} of a repo (repo.<name>.agent) or a role card (global.agents.<role>)."""
        if ag is not None and not isinstance(ag, dict):
            err.append(f"{path}: {{tool, model, effort, ultracode}} 객체여야 해요")
        elif ag:
            tool = ag.get("tool") or inherit
            need(not set(ag) - AGENT_KEYS, path, f"알 수 없는 키 {', '.join(sorted(set(ag) - AGENT_KEYS))}")
            need(ag.get("tool") in (None, *AGENTS), f"{path}.tool", f"{', '.join(AGENTS)} 중 하나여야 해요")
            need(ag.get("model") is None or (isinstance(ag["model"], str)
                                             and re.fullmatch(r"[A-Za-z0-9._:/\[\]-]+", ag["model"]) is not None),
                 f"{path}.model", "모델 이름은 영문, 숫자, . _ : / - [ ] 만 써요")
            known = next((m for m in agent_models().get(tool, []) if m["id"] == ag.get("model")), None)
            levels = (known or {}).get("efforts") or AGENT_EFFORTS.get(tool, ())
            need(ag.get("effort") is None or ag["effort"] in levels, f"{path}.effort",
                 f"{tool}{' ' + ag['model'] if known else ''}: {', '.join(levels)} 중 하나여야 해요")
            need(ag.get("ultracode") in (None, True, False), f"{path}.ultracode", "true/false여야 해요")
            need(not ag.get("ultracode") or tool == "claude", f"{path}.ultracode", "claude에서만 켤 수 있어요")

    ags = st.get("agents")
    if isinstance(ags, dict):
        need(not set(ags) - set(AGENT_ROLES), "global.agents", f"{', '.join(AGENT_ROLES)} 만 써요")
        for role in AGENT_ROLES:
            agent_conf(f"global.agents.{role}", ags.get(role), st.get("agent"))
    else:
        err.append("global.agents: {work, review, check} 객체여야 해요")
        ags = {}
    work = ags.get("work")
    work_tool = (work.get("tool") if isinstance(work, dict) else None) or st.get("agent")  # an empty repo tool follows it
    need(_text(st.get("asana_tag")), "global.asana_tag", "비어 있으면 안 돼요")
    need(_text(st.get("milestone_tag")), "global.milestone_tag", "비어 있으면 안 돼요")
    need(_int(st.get("dashboard_port"), 1024, 65535), "global.dashboard_port", "1024~65535 사이 정수여야 해요")
    for k in ("asana_workspace_gid", "asana_keychain_service", "git_user"):
        need(isinstance(st.get(k), str), f"global.{k}", "문자열이어야 해요")
    need(_text(st.get("worktree_root")), "global.worktree_root", "비어 있으면 안 돼요")
    need(_text(st.get("clone_root")), "global.clone_root", "비어 있으면 안 돼요")
    need(_text(st.get("owner_name")), "global.owner_name", "비어 있으면 안 돼요")
    ls = st.get("lan_share")
    need(isinstance(ls, dict) and isinstance(ls.get("enabled"), bool)
         and isinstance(ls.get("org"), str) and (re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", ls["org"]) is not None
                                                 or (ls["org"] == "" and not ls["enabled"])),
         "global.lan_share", "{enabled: true|false, org: GitHub 조직 이름 (공유를 켤 때 필요)}이어야 해요")
    need(isinstance(st.get("theme"), str) and re.fullmatch(r"\w[\w.-]*", st["theme"]) is not None,
         "global.theme", "테마 파일 이름(확장자 빼고)이나 auto여야 해요")
    need(isinstance(st.get("agent_asana_write"), bool), "global.agent_asana_write", "true 또는 false여야 해요")
    need(_int(st.get("agent_max_min"), 0, 1440), "global.agent_max_min", "0~1440분 사이 정수여야 해요 (0 = 제한 없음)")
    need(isinstance(st.get("branch_prefix"), str) and re.fullmatch(r"[A-Za-z0-9._/-]*", st["branch_prefix"]) is not None,
         "global.branch_prefix", "영문, 숫자, . _ / - 만 써요 (비워도 돼요)")
    rv, ck, rel = st.get("review"), st.get("check"), st.get("release")
    if isinstance(rv, dict):
        need(_int(rv.get("max_rework_rounds"), 0, 20), "global.review.max_rework_rounds", "0~20 사이 정수여야 해요 (0 = 제한 없음)")
    else:
        err.append("global.review: 객체여야 해요")
    if isinstance(ck, dict):
        need(_num(ck.get("timeout_min"), 1, 120), "global.check.timeout_min", "1~120분 사이여야 해요")
        need(_int(ck.get("max_parallel"), 1, 8), "global.check.max_parallel", "1~8 사이 정수여야 해요")
    else:
        err.append("global.check: 객체여야 해요")
    if isinstance(rel, dict):
        wd = rel.get("weekdays")
        need(isinstance(wd, list) and all(_int(d, 0, 6) for d in wd) and len(set(wd)) == len(wd),
             "global.release.weekdays", "0(월)~6(일) 숫자 목록이어야 하고 겹치면 안 돼요")
        need(isinstance(rel.get("at"), str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", rel["at"]) is not None,
             "global.release.at", "HH:MM 형식이어야 해요")
        need(_int(rel.get("window_min"), 10, 1440), "global.release.window_min", "10~1440분 사이 정수여야 해요")
        try:
            ZoneInfo(str(rel.get("timezone")))
        except (ZoneInfoNotFoundError, ValueError):
            err.append(f"global.release.timezone: 알 수 없는 시간대 '{rel.get('timezone')}'")
        for k in ("staging_branch", "main_branch"):
            need(_text(rel.get(k)), f"global.release.{k}", "브랜치 이름이 비어 있으면 안 돼요")
    else:
        err.append("global.release: 객체여야 해요")
    upd = st.get("update")
    if isinstance(upd, dict):
        need(isinstance(upd.get("auto"), bool), "global.update.auto", "true 또는 false여야 해요")
        need(_int(upd.get("check_min"), 10, 10080), "global.update.check_min", "10~10080분 사이 정수여야 해요")
        need(not set(upd) - {"auto", "check_min"}, "global.update", "auto, check_min만 쓸 수 있어요")
    else:
        err.append("global.update: 객체여야 해요")
    need(_int((st.get("deploy") or {}).get("deploy_timeout_min"), 5, 240), "global.deploy.deploy_timeout_min",
         "5~240분 사이 정수여야 해요")
    gi = st.get("github_issues")
    if isinstance(gi, dict):
        for repo, prefix in gi.items():
            need(repo in pm["repos"], f"global.github_issues.{repo}", "레포 목록에 없는 레포예요")
            need(isinstance(prefix, str) and re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", prefix) is not None,
                 f"global.github_issues.{repo}", "티켓 접두어는 대문자로 시작하는 영문/숫자 10자 이내여야 해요 (예: TP)")
    else:
        err.append("global.github_issues: 객체여야 해요")
    hp = pm.get("hidden_projects")
    if isinstance(hp, dict):
        for gid, name in hp.items():
            need(re.fullmatch(r"\d+", str(gid)) is not None, f"global.hidden_projects.{gid}", "Asana 프로젝트 gid는 숫자예요")
            need(isinstance(name, str), f"global.hidden_projects.{gid}", "이름은 문자열이어야 해요")
    else:
        err.append("global.hidden_projects: 객체여야 해요")

    for name, e in pm["repos"].items():
        p = f"repo.{name}"
        need(re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None, p, "레포 이름은 영문, 숫자, _ . - 만 써요")
        if not isinstance(e, dict):
            err.append(f"{p}: 객체여야 해요")
            continue
        need(_text(e.get("path")), f"{p}.path", "경로가 비어 있으면 안 돼요")
        if _text(e.get("path")):
            changed = (oldrepos.get(name) or {}).get("path") != e["path"]
            if changed and not Path(os.path.expanduser(e["path"])).is_dir():
                err.append(f"{p}.path: 폴더가 없어요 ({e['path']})")
        for k in ("orca_repo_id", "setup", "worktree_base"):
            need(e.get(k) is None or isinstance(e[k], str), f"{p}.{k}", "문자열이어야 해요")
        need(e.get("auto_setup") in (None, True, False), f"{p}.auto_setup", "true/false여야 해요")
        dv = e.get("dev")
        if dv is not None and not isinstance(dv, dict):
            err.append(f"{p}.dev: {{cmd, port}} 객체여야 해요")
        elif dv:
            need(not set(dv) - {"cmd", "port"}, f"{p}.dev", f"알 수 없는 키 {', '.join(sorted(set(dv) - {'cmd', 'port'}))}")
            need(dv.get("cmd") is None or isinstance(dv["cmd"], str), f"{p}.dev.cmd", "문자열이어야 해요")
            need(dv.get("port") is None or (isinstance(dv["port"], int) and not isinstance(dv["port"], bool)
                                            and 1024 <= dv["port"] <= 65535), f"{p}.dev.port", "1024-65535 사이 숫자여야 해요")
        agent_conf(f"{p}.agent", e.get("agent"), work_tool)
        need(e.get("github") is None or (isinstance(e["github"], str) and re.fullmatch(r"[\w.-]+/[\w.-]+", e["github"])),
             f"{p}.github", "owner/name 형식이어야 해요")
        need(e.get("description") is None or isinstance(e["description"], str), f"{p}.description", "문자열이어야 해요")
        dep = e.get("depends_on")
        if dep is not None and not (isinstance(dep, list) and all(isinstance(x, str) for x in dep)):
            err.append(f"{p}.depends_on: 레포 이름 목록이어야 해요")
        elif dep:
            known = pm["repos"]  # repos with a path and github (diff_queue reads github from there)
            for x in dep:
                need(x != name and x in known, f"{p}.depends_on", f"{x}: 자기 자신이 아닌, 레포 목록에 있는 레포여야 해요")
            need(len(set(dep)) == len(dep), f"{p}.depends_on", "같은 레포가 두 번 있어요")
        checks = e.get("checks") or []
        if not isinstance(checks, list):
            err.append(f"{p}.checks: 목록이어야 해요")
            checks = []
        names = []
        for i, c in enumerate(checks):
            cp = f"{p}.checks[{i}]"
            if not isinstance(c, dict):
                err.append(f"{cp}: {{name, cmd, dir?}} 객체여야 해요")
                continue
            extra = set(c) - CHECK_KEYS
            need(not extra, cp, f"모르는 키 {sorted(extra)} (name, cmd, dir, off, reason)")
            need(_text(c.get("name")), f"{cp}.name", "필수예요")
            need(_text(c.get("cmd")), f"{cp}.cmd", "필수예요")
            if "dir" in c:
                d = c["dir"]
                need(_text(d) and not os.path.isabs(d) and ".." not in Path(d).parts, f"{cp}.dir",
                     "레포 안의 상대 경로여야 해요 (비우려면 빼요)")
            need(isinstance(c.get("off", False), bool), f"{cp}.off", "true/false여야 해요")
            need(isinstance(c.get("reason", ""), str), f"{cp}.reason", "문자열이어야 해요")
            names.append(c.get("name"))
        dup = sorted({n for n in names if names.count(n) > 1 and n})
        need(not dup, f"{p}.checks", f"이름이 겹쳐요: {', '.join(map(str, dup))}")
        need(isinstance(e.get("ci", False), bool), f"{p}.ci", "true/false여야 해요")
        need(not (e.get("ci") and any(isinstance(c, dict) and not c.get("off") for c in checks)), f"{p}.ci",
             "GitHub 검사 읽기(ci)와 로컬 검사 명령(checks)은 같이 쓸 수 없어요. 하나를 끄세요")
        r = e.get("review")
        if r is not None:
            if not isinstance(r, dict) or set(r) - {"max_rework_rounds"}:
                err.append(f"{p}.review: {{max_rework_rounds}}만 쓸 수 있어요")
            elif r.get("max_rework_rounds") is not None:
                need(_int(r["max_rework_rounds"], 0, 20), f"{p}.review.max_rework_rounds", "0~20 사이 정수여야 해요 (비우면 전역 값)")

    for name, d in st["deploy"]["repos"].items():
        p = f"repo.{name}.deploy"
        if not isinstance(d, dict):
            err.append(f"{p}: 객체여야 해요")
            continue
        extra = set(d) - DEPLOY_KEYS
        need(not extra, p, f"모르는 키 {sorted(extra)}")
        modes = d.get("modes", ["direct"])
        bad = [m for m in modes if m not in MODES] if isinstance(modes, list) else ["?"]
        need(isinstance(modes, list) and modes and not bad and len(set(modes)) == len(modes), f"{p}.modes",
             f"{', '.join(MODES)} 중에서 하나 이상, 겹치지 않게 골라요" + (f" (허용 안 됨: {', '.join(map(str, bad))})" if bad else ""))
        need(d.get("merge", "merge") in MERGES, f"{p}.merge", f"{' 또는 '.join(MERGES)}여야 해요")
        for k in ("train", "pr_into_staging"):
            need(isinstance(d.get(k, False), bool), f"{p}.{k}", "true/false여야 해요")
        sg = d.get("staging")
        if sg is not None:
            if isinstance(sg, dict):
                need(_text(sg.get("branch")), f"{p}.staging.branch", "브랜치 이름이 비어 있으면 안 돼요")
                need("pipeline" not in sg or _text(sg["pipeline"]), f"{p}.staging.pipeline", "비어 있으면 안 돼요 (없으면 빼요)")
                need(set(sg) <= {"branch", "pipeline"}, f"{p}.staging", "branch, pipeline만 쓸 수 있어요")
            else:
                err.append(f"{p}.staging: 객체여야 해요")
        need(sg or not (d.get("train") or d.get("pr_into_staging") or set(modes if isinstance(modes, list) else []) & {"staging", "promote", "all"}),
             f"{p}.staging", "staging/promote/all 모드나 train, pr_into_staging을 쓰려면 staging 브랜치가 필요해요")
        pr = d.get("prod")
        if isinstance(pr, dict):
            need(_text(pr.get("branch")), f"{p}.prod.branch", "브랜치 이름이 비어 있으면 안 돼요")
            for k in ("pipeline", "workflow"):
                need(k not in pr or _text(pr[k]), f"{p}.prod.{k}", "비어 있으면 안 돼요 (없으면 빼요)")
            need(pr.get("release", True) is True, f"{p}.prod.release", "true만 써요 (없으면 빼요)")
            need(len({"pipeline", "workflow", "release"} & set(pr)) <= 1, f"{p}.prod", "pipeline, workflow, release 중 하나만 써요")
            if "ecs" in pr:
                ecs = pr["ecs"]
                need(isinstance(ecs, dict) and _text(ecs.get("cluster")) and _text(ecs.get("service")), f"{p}.prod.ecs",
                     "cluster와 service가 필요해요")
            sm = pr.get("smoke", [])
            need(isinstance(sm, list) and all(isinstance(u, str) and re.match(r"https?://", u) for u in sm),
                 f"{p}.prod.smoke", "http(s) 주소 목록이어야 해요")
        else:
            err.append(f"{p}.prod: prod 브랜치가 필요해요")
        need(d.get("via") is None or _text(d["via"]), f"{p}.via", "브랜치 이름이 비어 있으면 안 돼요 (없으면 빼요)")
        need(d.get("version") is None or d["version"] in VERSIONS, f"{p}.version", f"{', '.join(VERSIONS)} 또는 없음")
        need("preview_label" not in d or _text(d["preview_label"]), f"{p}.preview_label", "비어 있으면 안 돼요 (없으면 빼요)")

    seen, gids = set(), set()
    for i, pj in enumerate(pm["projects"]):
        if not isinstance(pj, dict):
            err.append(f"project[{i}]: 객체여야 해요")
            continue
        pid, gid = str(pj.get("id") or ""), pj.get("asana_gid")
        p = f"project.{pid or i}"
        need(re.fullmatch(PROJECT_ID, pid) is not None and pid != core.UNSORTED, p,
             f"id는 영문 소문자, 숫자, - 로 40자 이내예요 ('{core.UNSORTED}'는 미분류용이라 못 써요)")
        need(pid not in seen, p, "같은 id의 프로젝트가 두 번 있어요")
        seen.add(pid)
        need(pj.get("source") in core.PROJECT_SOURCES, f"{p}.source", f"{', '.join(core.PROJECT_SOURCES)} 중 하나여야 해요")
        if pj.get("source") == "asana":
            need(re.fullmatch(r"\d+", str(gid or "")) is not None, f"{p}.asana_gid", "Asana 연결 프로젝트는 Asana 프로젝트 gid(숫자)가 필요해요")
            need(str(gid) not in gids, f"{p}.asana_gid", "같은 Asana 프로젝트가 두 프로젝트에 연결돼 있어요")
            gids.add(str(gid))
        else:
            need(not gid, f"{p}.asana_gid", "Asana 연결(source asana)일 때만 써요")
        need(_text(pj.get("name")), f"{p}.name", "이름이 비어 있으면 안 돼요")
        rows = pj.get("repos") or []
        name = pj.get("name") or pid
        for rr in rows:
            need(rr.get("repo") in pm["repos"], f"repo.{rr.get('repo')}.links", f"{name}: 레포 목록에 없는 레포예요")
            need(_text(rr.get("role")), f"repo.{rr.get('repo')}.links", f"{name}: role이 비어 있으면 안 돼요")
        need(sum(1 for rr in rows if rr.get("default")) <= 1, f"{p}.repos", "기본 레포는 하나만 정할 수 있어요")
        rn = [rr.get("repo") for rr in rows]
        need(len(rn) == len(set(rn)), f"{p}.repos", "같은 레포가 두 번 연결돼 있어요")
    return err


# ---------------------------------------------------------------- history, save, backups

def _flat(v, pre="") -> dict:
    if isinstance(v, dict) and (v or pre):
        out = {}  # an empty dict adds no key: {} and null read the same in the history
        for k, x in v.items():
            out.update(_flat(x, f"{pre}.{k}" if pre else str(k)))
        return out
    return {pre: v}


def changes(old: dict, new: dict) -> list[dict]:
    """[{scope, key, old, new}] between two docs. A project's repos are recorded on the project (project:<id>)."""
    a, b = view(old), view(new)
    for v in (a, b):
        for r in v["repo"].values():
            r.pop("links", None)
    out = []

    def cmp(scope, x, y):
        fx, fy = _flat(x or {}), _flat(y or {})
        for k in list(fx) + [k for k in fy if k not in fx]:
            if fx.get(k) != fy.get(k):
                out.append({"scope": scope, "key": k, "old": fx.get(k), "new": fy.get(k)})

    cmp("global", a["global"], b["global"])
    for n in list(a["repo"]) + [n for n in b["repo"] if n not in a["repo"]]:
        cmp(n, a["repo"].get(n), b["repo"].get(n))
    for g in list(a["project"]) + [g for g in b["project"] if g not in a["project"]]:
        cmp(f"project:{g}", a["project"].get(g), b["project"].get(g))
    return [c for c in out if c["key"]]  # "" = an empty scope on one side


def save(con, new: dict, actor: str, note: str | None = None) -> list[dict]:
    """Validate and store new (a full doc); record each changed key and back up. Returns the changes.
    Running work is not touched: the next tick (and the dashboard within CONFIG_TTL_S) reads the new values."""
    old = current(con)
    new = core.effective_config(new)
    errs = validate(new, old)
    if errs:
        raise ConfigError(errs)
    ch = changes(old, new)
    if not ch:
        return []
    core.write_config(con, new)
    ts = now()
    for c in ch:
        con.execute("INSERT INTO settings_history(ts,actor,scope,key,old,new) VALUES(?,?,?,?,?,?)",
                    (ts, actor, c["scope"], c["key"], json.dumps(c["old"], ensure_ascii=False),
                     json.dumps(c["new"], ensure_ascii=False)))
    meta = sorted({c["scope"] for c in ch})
    event(con, "config", f"설정 변경 ({actor}): {', '.join(meta)} · {len(ch)}개 항목" + (f" · {note}" if note else ""))
    core.load_map(con, new["project_map"])
    con.commit()
    core.config_cache_clear()
    backup(con)
    return ch


def backup(con) -> Path:
    """data/backups/config-<UTC time>.json (export shape); keeps the newest BACKUP_KEEP."""
    doc = current(con)
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    f = d / f"config-{stamp}.json"
    f.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    for old in sorted(d.glob("config-*.json"))[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return f


# ---------------------------------------------------------------- CLI-style operations (also used by the API)

def _walk(root, parts):
    cur = root
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            raise ConfigError([f"'{'.'.join(parts)}' 경로가 없어요"])
    return cur


def get(path: str = "") -> object:
    with _db() as con:
        v = view(current(con))
    return _walk(v, [p for p in path.split(".") if p]) if path else v


def _scope_of(parts: list[str]) -> tuple[str, list[str]]:
    if not parts:
        raise ConfigError(["경로가 필요해요 (global.<키>, repo.<이름>[.<키>], project.<id>[.<키>])"])
    if parts[0] == "global":
        return "global", parts[1:]
    if parts[0] in ("repo", "project") and len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}", parts[2:]
    raise ConfigError([f"알 수 없는 경로 '{'.'.join(parts)}' (global.<키>, repo.<이름>[.<키>], project.<id>[.<키>])"])


def scope_value(doc, scope: str):
    v = view(doc)
    kind, _, name = scope.partition(".")
    if kind == "global":
        return v["global"]
    return v["repo" if kind == "repo" else "project"].get(name)


def set_path(path: str, value, actor: str = "cli", dry_run: bool = False) -> list[dict]:
    """tp config set global.max_active_runs 4 / repo.<name>.deploy.staging.branch '"staging"'."""
    parts = [p for p in path.split(".") if p]
    scope, rest = _scope_of(parts)
    with _db() as con:
        doc = current(con)
    if rest:
        sv = copy.deepcopy(scope_value(doc, scope))
        if sv is None:  # a new repo or project
            sv = {} if scope.startswith("project.") else {k: None for k in REPO_INFO} | {"checks": [], "ci": False,
                                                                                       "review": {}, "deploy": None}
        cur = sv
        for p in rest[:-1]:
            if not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        if value is None and scope == "global":
            cur[rest[-1]] = None  # apply() drops top-level None (code default); nested None is removed below
            if len(rest) > 1:
                cur.pop(rest[-1])
        elif value is None:
            cur.pop(rest[-1], None)
        else:
            cur[rest[-1]] = value
        value = sv
    return update(scope, value, actor=actor, dry_run=dry_run)


def update(scope: str, value, actor: str, dry_run: bool = False, create: bool = False, dashboard: bool = False) -> list[dict]:
    """Replace one scope (global | repo.<name> | project.<id>) with value; validated, recorded, backed up.
    dry_run: validate and return the changes only (the dashboard's confirm step).
    create: a new repo or project only (프로젝트 추가), an existing one is refused instead of replaced."""
    return update_many([{"scope": scope, "value": value, "create": create}], actor, dry_run, dashboard)


def update_many(items: list, actor: str, dry_run: bool = False, dashboard: bool = False) -> list[dict]:
    """update() for several scopes, applied in order and saved (or dry-run) as one change: the 프로젝트 추가 wizard
    creates its new repos and the project that lists them together (TP-93).
    dashboard: the 설정 page (READ_ONLY keys refused); since TP-152 its actor is a person, not the word dashboard."""
    if not isinstance(items, list) or not items:
        raise ConfigError(["updates: 하나 이상의 {scope, value}가 필요해요"])
    with _db() as con:
        cur = current(con)
        new = cur
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("scope"), str):
                raise ConfigError(["updates: 항목마다 scope가 필요해요"])
            scope, value = it["scope"], it.get("value")
            if it.get("create") and scope.startswith("repo.") and scope[5:] in repo_names(new):
                raise ConfigError([f"{scope}: 이미 있는 레포 이름이에요"])
            if it.get("create") and scope.startswith("project.") and scope[8:] in view(new)["project"]:
                raise ConfigError([f"{scope}: 이미 있는 프로젝트 id예요"])
            if (dashboard or actor == "dashboard") and scope == "global" and isinstance(value, dict):
                for k in READ_ONLY:
                    if k in value and value[k] != cur["settings"].get(k):
                        raise ConfigError([f"global.{k}: 재시작이 필요한 값이라 여기서는 바꿀 수 없어요 (tp config set)"])
            new = apply(new, scope, value)
        if dry_run:
            new = core.effective_config(new)
            errs = validate(new, cur)
            if errs:
                raise ConfigError(errs)
            return changes(cur, new)
        return save(con, new, actor)


def _read_doc(text: str) -> dict:
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ConfigError(["가져올 파일은 JSON 객체여야 해요"])
    if "settings" in doc or "project_map" in doc:
        return {"settings": doc.get("settings") or {}, "project_map": doc.get("project_map") or {}}
    if "repos" in doc and "projects" in doc:  # a bare project_map.json
        return {"project_map": doc}
    return {"settings": doc}  # a bare settings.json


def import_doc(doc: dict, actor: str = "import", dry_run: bool = False) -> list[dict]:
    """Replace the settings with an export / backup ({"settings", "project_map"}); a bare settings.json or
    project_map.json replaces only its half."""
    with _db() as con:
        cur = current(con)
        new = core.effective_config({"settings": doc["settings"] if "settings" in doc else cur["settings"],
                                     "project_map": doc["project_map"] if "project_map" in doc else cur["project_map"]})
        if dry_run:
            errs = validate(new, cur)
            if errs:
                raise ConfigError(errs)
            return changes(cur, new)
        return save(con, new, actor)


def import_file(path: str, actor: str = "import") -> list[dict]:
    return import_doc(_read_doc(Path(path).read_text()), actor=actor)


def export(path: str | None = None) -> dict:
    with _db() as con:
        doc = current(con)
    if path:
        Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return doc


def history(scope: str | None = None, limit: int = 300, con=None) -> list[dict]:
    """Newest first; scope = global, a repo name or project:<id> (project:<asana gid> before TP-91)."""
    if con is None:
        with _db() as c:
            return history(scope, limit, c)
    q, a = "SELECT * FROM settings_history", ()
    if scope:
        q, a = q + " WHERE scope=?", (scope,)
    return [dict(r) | {"old": json.loads(r["old"]) if r["old"] else None, "new": json.loads(r["new"]) if r["new"] else None}
            for r in con.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit))]


# ---------------------------------------------------------------- base-branch check trial runs (설정 page)

def base_ref(repo: str) -> str:
    """The branch ticket PRs of this repo target: its feature base, else the GitHub default branch."""
    fb = core.feature_base(repo)
    if fb:
        return f"origin/{fb}"
    path = (core.project_map()["repos"].get(repo) or {}).get("path")
    if path:
        p = subprocess.run(["git", "-C", os.path.expanduser(path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                           capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    rr = core.repo_rules(repo)
    return f"origin/{rr['target']}" if rr else "origin/main"


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def check_runs(con) -> dict:
    """Latest trial run per repo; a 'running' row whose process is gone becomes an error."""
    out = {}
    for r in con.execute("SELECT * FROM config_check_runs WHERE id IN (SELECT max(id) FROM config_check_runs GROUP BY repo)"):
        r = dict(r)
        if r["status"] == "running" and not _alive(r["pid"]):
            con.execute("UPDATE config_check_runs SET status='error', error=?, finished_at=? WHERE id=?",
                        ("검사 프로세스가 끝나버렸어요 (로그 확인)", now(), r["id"]))
            con.commit()
            r.update(status="error", error="검사 프로세스가 끝나버렸어요 (로그 확인)")
        r["results"] = json.loads(r["results"]) if r["results"] else []
        out[r["repo"]] = r
    return out


def start_check_run(repo: str) -> int:
    """Run the saved checks of repo on its base branch in the background (tp check --repo --ref --record)."""
    from . import checks as K
    cfg = K.repo_checks(repo)
    if not cfg or cfg["ci"]:
        raise ConfigError([f"{repo}: 로컬 검사 명령이 없어요 (CI를 쓰는 레포는 GitHub 검사를 읽어요)"])
    with _db() as con:
        return _start_check_run(con, repo, K)


def _start_check_run(con, repo, K) -> int:
    live = con.execute("SELECT * FROM config_check_runs WHERE repo=? AND status='running' ORDER BY id DESC LIMIT 1",
                       (repo,)).fetchone()
    if live and _alive(live["pid"]):
        raise ConfigError([f"{repo}: 시험 실행이 이미 돌고 있어요 (#{live['id']})"])
    ref = base_ref(repo)
    rid = con.execute("INSERT INTO config_check_runs(repo,ref,status,started_at) VALUES(?,?, 'running', ?)",
                      (repo, ref, now())).lastrowid
    log = K.LOG_DIR / f"config-{repo}-{rid}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as f:
        proc = subprocess.Popen([sys.executable, "-m", "ticket_pilot.cli", "check", "--repo", repo, "--ref", ref,
                                 "--record", str(rid)], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ, "PYTHONPATH": str(ROOT)})
    con.execute("UPDATE config_check_runs SET pid=?, log_path=? WHERE id=?", (proc.pid, str(log), rid))
    event(con, "config", f"{repo}: base 브랜치({ref}) 검사 시험 실행 #{rid} 시작")
    con.commit()
    return rid


def record_check_run(rid: int, repo: str, ref: str):
    """Body of the detached trial: fetch the base branch, run the checks (checks.baseline), store the result."""
    from . import checks as K
    con = db()
    try:
        path = os.path.expanduser(core.project_map()["repos"][repo]["path"])
        if ref.startswith("origin/"):
            subprocess.run(["git", "-C", path, "fetch", "origin", ref.removeprefix("origin/")], timeout=300,
                           capture_output=True, env=core.gh_env(required=False))
        res = K.baseline(repo, ref)
        ok = all(r["status"] in K.OK for r in res)
        con.execute("UPDATE config_check_runs SET status=?, sha=?, results=?, finished_at=? WHERE id=?",
                    ("pass" if ok else "fail", res[0]["sha"] if res else None, json.dumps(res, ensure_ascii=False), now(), rid))
    except BaseException as e:  # noqa: BLE001  SystemExit from baseline included
        con.execute("UPDATE config_check_runs SET status='error', error=?, finished_at=? WHERE id=?", (str(e)[:800], now(), rid))
        con.commit()
        raise
    con.commit()


def trial_log(rid: int, name: str | None = None) -> str | None:
    """Log of a trial run (one check's log, or the run's own output); only files under data/checks are served."""
    from . import checks as K
    with _db() as con:
        r = con.execute("SELECT * FROM config_check_runs WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    path = r["log_path"]
    if name:
        path = next((x.get("log") for x in json.loads(r["results"] or "[]") if x.get("name") == name), None)
    f = Path(path or "-").resolve()
    if not f.is_relative_to(K.LOG_DIR.resolve()) or not f.is_file():
        return ""
    b = f.read_bytes()
    return ("…(앞부분 생략)\n" if len(b) > 400_000 else "") + b[-400_000:].decode("utf-8", "replace")


# ---------------------------------------------------------------- dashboard payload

def api_state() -> dict:
    with _db() as con:
        return _api_state(con)


def _api_state(con) -> dict:
    doc = current(con)
    v = view(doc)
    known = {str(p["asana_gid"]): p["name"] for p in doc["project_map"]["projects"] if p.get("asana_gid")}
    for r in con.execute("SELECT DISTINCT project_gid, project_name FROM tickets WHERE project_gid IS NOT NULL"):
        known.setdefault(r["project_gid"], r["project_name"] or r["project_gid"])
    info = {}
    for n, r in v["repo"].items():
        p = Path(os.path.expanduser(r["path"])) if r.get("path") else None
        info[n] = {"path_ok": bool(p and p.is_dir()), "review_md": bool(p and (p / "REVIEW.md").is_file()),
                   "base_ref": None}
        try:
            info[n]["base_ref"] = base_ref(n) if r.get("checks") else None
        except Exception:  # noqa: BLE001
            pass
    return {"config": v, "read_only": list(READ_ONLY), "modes": list(MODES),
            "merges": list(MERGES), "versions": list(VERSIONS), "agents": list(AGENTS),
            "agent_efforts": {k: list(v) for k, v in AGENT_EFFORTS.items()}, "repo_info": info,
            "agent_models": agent_models(),
            "check_runs": check_runs(con), "history": history(limit=500, con=con), "projects": known,
            "imported_at": core.meta_get(con, "config_imported"),
            "backups": {"dir": str(backup_dir()), "count": len(list(backup_dir().glob("config-*.json"))) if backup_dir().is_dir() else 0}}

