"""Versions and updates (TP-140). A version is a git tag vX.Y.Z (+ a GitHub Release), made after each push to main
(.github/workflows/release.yml on GitHub's runner, TP-186; the old repository's deploy.yml release job before that):
no version file is committed. `tp update` moves this
checkout to the latest release tag (or --to <tag|sha>), rebuilds what changed and restarts the launchd agents; the
deploy workflow runs the same command on the owner's Mac, and the dashboard's 업데이트 button starts it detached."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .core import ROOT, GitHubNotConnected, db, gh_env, github_token, meta_get, meta_set, now, settings
from .runner import agent_path

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FIRST = "v0.4.0"
ASSET = "web-dist.tar.gz"  # the built web/dist attached to each release (TP-142)
AUTO_STUCK_S = 1800  # update.auto: a started tp update that has not brought its tag in by then failed (alert once)


class UpdateError(Exception):
    pass


def log_path() -> Path:
    return Path(os.environ.get("TP_LOG_DIR") or Path.home() / "tp-work" / "logs") / "update.log"


def _auth_env(base=None) -> dict | None:
    """tp's GitHub token for git fetch / gh (TP-112, TP-182: the owner's login is not there in Docker mode).
    deploy.yml brings its own job-token auth (GIT_CONFIG_* extraHeader, GH_TOKEN), which gh_env would overwrite."""
    if os.environ.get("GITHUB_ACTIONS"):
        return base
    return gh_env(base, required=False)


def _git(root, *args, check=True, timeout=120) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                       env=_auth_env())
    if check and p.returncode:
        raise UpdateError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout.strip() if p.returncode == 0 else ""


def bump(last: str | None, labels) -> str:
    """Next version after the last v* tag: label major / minor, else patch. No tag yet: v0.4.0."""
    m = TAG.match(last or "")
    if not m:
        return FIRST
    x, y, z = map(int, m.groups())
    labels = set(labels)
    return f"v{x + 1}.0.0" if "major" in labels else f"v{x}.{y + 1}.0" if "minor" in labels else f"v{x}.{y}.{z + 1}"


def latest(tags) -> str | None:
    vs = [t for t in tags if TAG.match(t)]
    return max(vs, key=lambda t: tuple(map(int, TAG.match(t).groups()))) if vs else None


def latest_tag(root=ROOT) -> str | None:
    return latest(_git(root, "tag", "-l", "v*", check=False).split())


def current(root=ROOT) -> str:
    """`git describe --tags` (v0.4.1, v0.4.1-3-gabc1234), or the short sha before the first tag."""
    version = os.environ.get("TP_RELEASE_VERSION", "")
    if Path(root) == ROOT and TAG.fullmatch(version):
        return version
    return _git(root, "describe", "--tags", "--match", "v*", "--always", check=False)


def record_installed(con, root=ROOT):
    """Observe the active release on tick, after host restart or Docker update acceptance."""
    if (Path(root) / "data" / "update.pending").exists():
        return
    version = current(root)
    if TAG.fullmatch(version):
        with con:
            con.execute("""INSERT INTO metric_markers(at,label,kind)
                           SELECT ?,?,'version' WHERE ? <> COALESCE(
                             (SELECT label FROM metric_markers WHERE kind='version' ORDER BY id DESC LIMIT 1),'')""",
                        (time.time(), version, version))


def blocked(root=ROOT) -> str | None:
    """Why `tp update` would refuse to run here, or None."""
    br = _git(root, "branch", "--show-current", check=False)
    if br != "main":
        return f"checkout이 main 브랜치가 아니에요 ({br or 'detached HEAD'})"
    if _git(root, "status", "--porcelain", "--untracked-files=no", check=False):
        return "추적되는 파일에 수정 사항이 있어요 (git status)"
    return None


def newer(root=ROOT, tag: str | None = None) -> bool:
    """True when the tag is not in this checkout yet."""
    tag = tag or latest_tag(root)
    version = os.environ.get("TP_RELEASE_VERSION", "")
    if Path(root) == ROOT and TAG.fullmatch(version):
        return bool(tag and tag != version and latest([tag, version]) == tag)
    return bool(tag) and subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", tag, "HEAD"],
                                        capture_output=True).returncode == 1


def notes(root, old: str, new: str) -> list[dict]:
    """Release tags after `old` up to `new`, newest first, with the PR lines of their GitHub release notes
    (empty when gh cannot read them)."""
    tags = _git(root, "tag", "-l", "v*", "--merged", new, "--no-merged", old, check=False).split()
    out = []
    for t in sorted((t for t in tags if TAG.match(t)), key=lambda t: tuple(map(int, TAG.match(t).groups())), reverse=True):
        try:
            p = subprocess.run(["gh", "release", "view", t, "--json", "body", "--jq", ".body"], cwd=root,
                               env=_auth_env(), capture_output=True, text=True, timeout=30)
            body = p.stdout if p.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            body = ""
        # --generate-notes: "* <PR title> by @who in <url>"
        out.append({"tag": t, "items": [re.sub(r" by @\S+ in \S+$", "", ln[2:].strip())
                                        for ln in body.splitlines() if ln.startswith("* ")]})
    return out


def check_every_s() -> int:
    return int(settings().get("update", {}).get("check_min", 360)) * 60


def check_latest(con, root=ROOT, force=False):
    """tp tick: every update.check_min minutes fetch the tags and keep the latest release tag in meta update_check.
    Network errors are skipped quietly (the next tick tries again after the interval); force (지금 확인, TP-153)
    raises them as UpdateError instead and keeps the previous check."""
    prev = meta_get(con, "update_check") or {}
    if not force and prev.get("at") and (datetime.fromisoformat(now()) - datetime.fromisoformat(prev["at"])).total_seconds() < check_every_s():
        return prev
    try:
        _git(root, "fetch", "-q", "--tags", "origin", timeout=60)
    except (UpdateError, subprocess.TimeoutExpired, OSError) as e:
        if force:
            try:
                hint = "" if os.environ.get("GITHUB_ACTIONS") or github_token() else f" ({GitHubNotConnected()})"
            except RuntimeError:
                hint = ""
            raise UpdateError(f"태그를 가져오지 못했어요: {e}{hint}") from None
    cur = {"at": now(), "latest": latest_tag(root)}
    meta_set(con, "update_check", cur)
    return cur


def auto_update(con, chk: dict, start, root=ROOT) -> str | None:
    """update.auto (TP-186): tp tick starts `tp update` (start = server.start_update, the dashboard button's path) when
    the latest release tag is not in this checkout. Once per tag: a failed update is not retried every tick. When it
    cannot run (blocked, Docker) it notifies once and tries again next tick; a started run that has not brought the tag in
    after AUTO_STUCK_S notifies once (see update.log). Returns "started" / "blocked" / None."""
    from . import notifications as N
    tag = chk.get("latest")
    if not settings().get("update", {}).get("auto") or not tag or not newer(root, tag):
        return None
    last = meta_get(con, "update_auto") or {}
    if last.get("tag") == tag and last.get("state") == "started":
        if not last.get("warned") and (datetime.fromisoformat(now()) - datetime.fromisoformat(last["at"])).total_seconds() > AUTO_STUCK_S:
            N.add(con, "deploy", f"자동 업데이트 실패: {tag}", f"{AUTO_STUCK_S // 60}분이 지나도 반영되지 않았어요. update.log를 확인하세요", "#update")
            meta_set(con, "update_auto", {**last, "warned": True})
        return None
    try:
        start()
    except SystemExit as e:
        if last.get("tag") != tag:
            N.add(con, "deploy", f"자동 업데이트 건너뜀: {tag}", str(e), "#update")
        meta_set(con, "update_auto", {"tag": tag, "state": "blocked", "at": now()})
        return "blocked"
    meta_set(con, "update_auto", {"tag": tag, "state": "started", "at": now()})
    return "started"


def update(to: str | None = None, root=ROOT, npm="npm", install=None, log=None, build=False,
           gh="gh") -> tuple[str, str] | None:
    """tp update: see the module doc. Returns (old version, new version), None when already there.
    Raises UpdateError with the reason; nothing is changed when a precheck fails."""
    root = Path(root)
    say = log or print
    install = install or [str(root / "launchd" / "install.sh")]
    why = blocked(root)
    if why:
        raise UpdateError(why)
    (root / "data").mkdir(exist_ok=True)
    with open(root / "data" / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("다른 업데이트가 진행 중이에요") from None
        return _update(to, root, npm, install, say, build, gh)


def _update(to, root, npm, install, say, build, gh):
    say("git fetch --tags origin")
    _git(root, "fetch", "-q", "--tags", "origin", timeout=300)
    target = to or latest_tag(root)
    if not target:
        raise UpdateError("릴리즈 태그(v*)가 아직 없어요")
    sha = _git(root, "rev-parse", "--verify", f"{target}^{{commit}}")
    head = _git(root, "rev-parse", "HEAD")
    old_v = current(root)
    # a run that stopped after the fast-forward (build or restart failed) finishes when run again for the same target
    pending_f = root / "data" / "update.pending"
    pending = json.loads(pending_f.read_text()) if pending_f.is_file() else {}
    # resume also when HEAD moved on from the stopped run's target (e.g. a manual pull), so its web/ changes still build
    resume = bool(pending.get("to")) and subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", pending["to"], "HEAD"]).returncode == 0
    if not resume and subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", sha, "HEAD"]).returncode == 0:
        say(f"최신이에요 ({old_v})")
        return None
    frm = pending["from"] if resume else head
    pending_f.write_text(json.dumps({"from": frm, "to": sha}))
    if head != sha:
        say(f"git merge --ff-only {target}")
        _git(root, "merge", "--ff-only", sha)
    changed = _git(root, "diff", "--name-only", frm, "HEAD").splitlines()
    web = root / "web"
    # the dashboard button runs this under launchd's PATH, which lacks nvm's node
    env = {**os.environ, "CI": "1", "PATH": agent_path()}
    if any(f.startswith("web/") for f in changed) or not (web / "dist" / "index.html").is_file():
        # a sha target (deploy.yml) has no release yet, so it always builds; nor does a HEAD past the tag (resume)
        if build or not TAG.match(target) or _git(root, "rev-parse", "HEAD") != sha \
                or not _fetch_dist(root, target, gh, env, say):
            nm, lock = web / "node_modules", web / "package-lock.json"
            # mtime: an asset update skips npm ci, so a lockfile change from then is not in `changed`
            if "web/package-lock.json" in changed or not nm.is_dir() \
                    or (lock.is_file() and lock.stat().st_mtime > nm.stat().st_mtime):
                say("npm ci")
                _run([npm, "ci", "--no-audit", "--no-fund"], web, env)
            say("npm run build")
            _run([npm, "run", "build"], web, env)
    else:
        say("web/ 변경 없음: 빌드 건너뜀")
    say("launchd/install.sh (재시작)")
    _run(install, root, os.environ)
    pending_f.unlink()
    new_v = current(root)
    if root.resolve() == ROOT:
        try:
            record_installed(db(), root)
        except (OSError, sqlite3.Error) as e:
            say(f"지표의 버전 기록을 다음 tick에서 다시 시도해요 ({e})")
    say(f"완료: {old_v} -> {new_v}")
    for n in reversed(notes(root, frm, sha)):
        say(f"  {n['tag']}")
        for it in n["items"]:
            say(f"    - {it}")
    return old_v, new_v


def _fetch_dist(root, tag, gh, env, say) -> bool:
    """Swap web/dist for the release's ASSET. False (build instead) when it cannot be downloaded; a broken archive
    raises and leaves the old web/dist in place."""
    say(f"gh release download {tag} {ASSET}")
    tmp = Path(tempfile.mkdtemp(dir=root / "data", prefix="web-dist-"))  # same filesystem as web/: rename is atomic
    try:
        try:
            p = subprocess.run([gh, "release", "download", tag, "--pattern", ASSET, "--dir", str(tmp)], cwd=root,
                               env=_auth_env(env), capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            p = subprocess.CompletedProcess([], 1, "", str(e))
        if p.returncode or not (tmp / ASSET).is_file():
            say(f"{ASSET}를 받지 못했어요, 직접 빌드: {(p.stderr or p.stdout).strip()[-200:]}")
            return False
        new = tmp / "dist"
        try:
            with tarfile.open(tmp / ASSET) as t:
                t.extractall(new, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise UpdateError(f"{ASSET} 압축 해제 실패 (기존 화면 유지, --build로 직접 빌드 가능): {e}") from None
        if not (new / "index.html").is_file():
            raise UpdateError(f"{ASSET}에 index.html이 없어요 (기존 화면 유지, --build로 직접 빌드 가능)")
        dist, old = root / "web" / "dist", tmp / "old"
        if dist.exists():
            dist.rename(old)
        try:
            new.rename(dist)
        except OSError as e:
            if old.exists():
                old.rename(dist)
            raise UpdateError(f"web/dist 교체 실패 (기존 화면 유지): {e}") from None
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(cmd, cwd, env):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if p.returncode:
        raise UpdateError(f"{' '.join(map(str, cmd))} 실패 ({p.returncode}): {(p.stderr or p.stdout).strip()[-500:]}")


def main(to: str | None = None, build=False) -> int:
    """tp update: prints and appends to update.log; the last line starts with 완료 / 최신이에요 / 실패."""
    f = log_path()
    f.parent.mkdir(parents=True, exist_ok=True)

    def say(msg):
        print(msg, flush=True)
        with open(f, "a") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")

    say(f"tp update{' --to ' + to if to else ''}{' --build' if build else ''} ({ROOT})")
    try:
        update(to, log=say, build=build)
    except (UpdateError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        say(f"실패: {e}")
        return 1
    return 0


def _release_gh(*args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise UpdateError(f"gh {' '.join(args)}: {p.stderr.strip()[-300:]}")
    return p.stdout


def release_plan(sha: str, repo: str, version: str | None = None) -> tuple[str, bool, list, list]:
    """Resolve the version before publishing an image. Retries reuse the commit's existing tag."""
    if version and not TAG.fullmatch(version):
        raise UpdateError(f"버전은 vX.Y.Z 형식이어야 해요: {version}")
    pages = json.loads(_release_gh("api", "--paginate", "--slurp", f"repos/{repo}/git/matching-refs/tags/v"))
    tags = {r["ref"].removeprefix("refs/tags/"): r["object"]["sha"] for page in pages for r in page}
    have = latest([t for t, s in tags.items() if s == sha])
    if have:
        return have, True, [], []
    pulls = json.loads(_release_gh("api", f"repos/{repo}/commits/{sha}/pulls"))
    labels = [lb["name"] for lb in (pulls[0]["labels"] if pulls else [])]
    tag = version or bump(latest(tags), labels)
    if tag in tags:
        raise UpdateError(f"{tag} 태그가 다른 커밋에 이미 있어요")
    return tag, False, pulls, labels


def release(sha: str, repo: str, root=ROOT, version: str | None = None) -> str | None:
    """Create a GitHub Release and attach the web build. An already tagged commit is skipped."""
    v, exists, pulls, labels = release_plan(sha, repo, version)
    if exists:
        print(f"{sha[:7]} already has {v}, skipping")
        return None
    # checked before tagging: a re-run skips a tagged commit, so a release without the asset would stay that way
    root = Path(root)
    head = _git(root, "rev-parse", "HEAD")
    if head != sha:
        raise UpdateError(f"web/dist는 {head[:7]}에서 빌드됐어요 ({sha[:7]} 아님): 릴리즈 안 만듦")
    if not (root / "web" / "dist" / "index.html").is_file():
        raise UpdateError("web/dist/index.html이 없어요: 릴리즈 안 만듦")
    _release_gh("release", "create", v, "--repo", repo, "--target", sha, "--generate-notes")
    print(f"{v} ({'PR #' + str(pulls[0]['number']) if pulls else 'no PR'}, labels {labels or '-'})")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / ASSET
        with tarfile.open(f, "w:gz") as t:
            t.add(root / "web" / "dist", arcname=".")
        _release_gh("release", "upload", v, str(f), "--repo", repo)
    print(f"{ASSET} attached")
    return v

