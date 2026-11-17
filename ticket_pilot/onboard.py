"""Project onboarding (TP-87, 프로젝트 추가 wizard): pick a GitHub repo and Asana projects from lists, check or clone
the local checkout. Saving goes through the normal POST /api/config (config.update), not through here.

  github_repos / asana_projects   lists for the pickers, cached in memory for CACHE_S (refresh=True refetches)
  check_path(path, github)         is it a git checkout whose origin is that GitHub repo (https/ssh spelling ignored)
  start_clone(github, name)        `gh repo clone` into <clone_root>/<name> in a background thread; clone_status polls it
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import core

CACHE_S = 300
GITHUB_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")  # same rule as config.validate for repo names
_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()
_jobs: dict[int, dict] = {}  # ponytail: in-memory clone jobs, lost on a dashboard restart (the clone itself still lands)
_ids = itertools.count(1)


def _cached(key: str, fetch, refresh: bool) -> list:
    hit = _cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    data = fetch()
    _cache[key] = (time.monotonic(), data)
    return data


def _fetch_github() -> list[dict]:
    p = subprocess.run(["gh", "api", "user/repos?affiliation=owner,collaborator,organization_member&per_page=100",
                        "--paginate", "--jq", ".[] | select(.archived | not) | {full_name, description, default_branch}"],
                       capture_output=True, text=True, timeout=120, env=core.gh_env())
    if p.returncode != 0:
        raise RuntimeError(f"gh api user/repos 실패 (대시보드 설정의 GitHub 연결 확인): {p.stderr.strip()[-400:]}")
    return sorted((json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()), key=lambda r: r["full_name"].lower())


def _fetch_asana() -> list[dict]:
    ps = core.asana_all("/projects", {"workspace": core.asana_workspace(), "archived": False, "opt_fields": "name"})
    return sorted(({"gid": str(p["gid"]), "name": p.get("name") or ""} for p in ps), key=lambda p: p["name"].lower())


def github_repos(refresh: bool = False) -> dict:
    """{repos: [{full_name, description, default_branch}]} without the already registered ones, or {repos: [], error}."""
    try:
        repos = _cached("github", _fetch_github, refresh)
    except Exception as e:  # noqa: BLE001  shown as a hint, the wizard still works with a typed path
        return {"repos": [], "error": str(e)}
    taken = {norm_github(r.get("github") or "") for r in core.project_map()["repos"].values()}
    return {"repos": [r for r in repos if r["full_name"].lower() not in taken]}


def asana_projects(refresh: bool = False) -> dict:
    """{projects: [{gid, name}]} of the workspace without hidden_projects, or {projects: [], error}."""
    try:
        ps = _cached("asana:" + core.asana_workspace(), _fetch_asana, refresh)  # a newly picked workspace refetches
    except Exception as e:  # noqa: BLE001  AsanaError (no PAT, API) / no workspace
        return {"projects": [], "error": str(e)}
    hidden = {str(g) for g in core.project_map().get("hidden_projects") or {}}
    return {"projects": [p for p in ps if p["gid"] not in hidden]}


def norm_github(url: str) -> str:
    """owner/repo (lowercase) of a GitHub remote in any spelling: https, ssh, git@host:, with or without .git."""
    u = url.strip().removesuffix("/").removesuffix(".git")
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+)$", u)
    return (m.group(1) if m else u).lower()


def _valid_github(github) -> str:
    if not isinstance(github, str) or not GITHUB_RE.fullmatch(github):
        raise SystemExit("github은 owner/repo 형식이어야 해요")
    return github


def check_path(path, github) -> dict:
    """{exists, is_git, origin, matches}: path is a folder, the root of a git checkout, and its origin is the github repo."""
    _valid_github(github)
    if not isinstance(path, str) or not path.strip():
        raise SystemExit("경로가 비어 있어요")
    d = Path(os.path.expanduser(path.strip()))
    if not d.is_absolute():
        raise SystemExit("절대 경로를 넣어 주세요 (~/ 가능)")
    out = {"exists": d.is_dir(), "is_git": False, "origin": None, "matches": False}
    if not out["exists"]:
        return out
    top = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    # the checkout root itself, not a subfolder of one (worktrees, checks and dev commands run from path)
    out["is_git"] = top.returncode == 0 and os.path.realpath(top.stdout.strip()) == os.path.realpath(d)
    if out["is_git"]:
        o = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True)
        out["origin"] = (o.stdout.strip() or None) if o.returncode == 0 else None
        out["matches"] = bool(out["origin"]) and norm_github(out["origin"]) == github.lower()
    return out


def clone_root() -> Path:
    return Path(os.path.realpath(os.path.expanduser(core.settings()["clone_root"])))


def clone_target(name) -> Path:
    """<clone_root>/<name>; a name that would leave clone_root ('..', '.') is refused."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SystemExit("레포 이름은 영문, 숫자, _ . - 만 써요")
    root = clone_root()
    t = Path(os.path.realpath(root / name))
    if t.parent != root:
        raise SystemExit(f"클론 위치가 clone_root({root}) 밖이에요: {name}")
    return t


def start_clone(github, name) -> dict:
    """{id, path, status}. An existing target is not touched: status 'exists' (the wizard checks it as a typed path)."""
    _valid_github(github)
    t = clone_target(name)
    with _lock:
        if any(j["path"] == str(t) and j["status"] == "running" for j in _jobs.values()):
            raise SystemExit(f"{t}에 이미 클론하는 중이에요")
        jid = next(_ids)
        job = _jobs[jid] = {"id": jid, "github": github, "path": str(t), "status": "running", "error": None}
        if t.exists():
            job["status"] = "exists"
            return dict(job)
    t.parent.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_clone, args=(job,), daemon=True).start()
    return dict(job)


def _clone(job: dict):
    try:
        p = subprocess.run(["gh", "repo", "clone", job["github"], job["path"]], capture_output=True, text=True,
                           timeout=1800, stdin=subprocess.DEVNULL, env=core.gh_env())
        job.update(status="done" if p.returncode == 0 else "error",
                   error=None if p.returncode == 0 else (p.stderr.strip() or p.stdout.strip())[-2000:])
    except Exception as e:  # noqa: BLE001  timeout, gh missing
        job.update(status="error", error=str(e))


def clone_status(jid: int) -> dict | None:
    j = _jobs.get(jid)
    return dict(j) if j else None
