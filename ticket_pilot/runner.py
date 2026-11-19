"""Local runner (replaces Orca): ticket worktrees via `git worktree`, agents as detached headless processes.

An agent is `codex exec`, `claude -p`, or `agy` (settings.agent) started by a small /bin/sh wrapper in its own session, so it
outlives the tick that launched it. Its handle "<pid> <log path>" is stored in runs.terminal (and in meta.automations
for the automations' AI steps). The wrapper writes "<log>.exit" with the exit code, so a finished agent is visible even
after its process is gone; "<log>.json" keeps the agent name and session id for `tp attach` (resume by hand).

The agent signals completion itself with `tp ws-set` (pipeline.ws_set); an agent that exits without it is handled by
pipeline.agent_exits. A repo's settings `setup` script (what Orca's worktree setup ran: install deps, copy .env) runs
first, in the same wrapper and log, with TP_/ORCA_ROOT_PATH (main checkout) and TP_/ORCA_WORKTREE_PATH set.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import antigravity, chat_usage
from .asana_write import rules as asana_rules
from .core import DATA, ROOT, gh_env, run, settings

LOGS = DATA / "agents"
# Full access like the owner's interactive sessions: agents push branches, open PRs and call bin/tp (writes data/tp.db).
# Hook trust: an untrusted hook used to stop every Orca codex session on a "Hooks need review" screen (2026-10-29);
# The owner answered "Trust all" there, and headless runs have nobody to answer.
CODEX = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
CLAUDE = ["claude", "-p", "--dangerously-skip-permissions"]
HANDLE = re.compile(r"^(\d+) (/.+\.log)$")
SESSION = re.compile(r"session id: ([0-9a-f-]{36})", re.I)
# folders never searched for .env files to copy into a new worktree
ENV_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".turbo", ".venv", "venv", "vendor", "coverage", ".cache"}
ENV_MAX_DEPTH = 3


def node_bin() -> str | None:
    """bin dir of nvm's default node. nvm loads only in interactive zsh (~/.zshrc), so launchd processes and the
    agents' `zsh -lc` commands would not find node, pnpm or yarn from it (Orca handed its interactive env over)."""
    nvm = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    try:
        want = (nvm / "alias" / "default").read_text().strip().lstrip("v")
    except OSError:
        return None
    vers = sorted((p for p in (nvm / "versions" / "node").glob("v*") if p.name[1:].startswith(want)),
                  key=lambda p: [int(x) if x.isdigit() else 0 for x in p.name[1:].split(".")])
    return str(vers[-1] / "bin") if vers else None


def agent_path() -> str:
    nb = node_bin()
    paths = os.environ.get("PATH", "").split(os.pathsep)
    local = str(Path.home() / ".local" / "bin")  # the Antigravity installer uses this on host and Docker
    if local not in paths:
        paths.append(local)
    if nb and nb not in paths:
        paths.insert(0, nb)
    return os.pathsep.join(paths)


def install_cmd(path: str) -> str | None:
    """Dependency install for a new worktree: by lockfile, pnpm before yarn before npm (2026-10-29); a bare
    package.json uses the first of those that is installed. None without a package.json."""
    p = Path(path)
    if not (p / "package.json").is_file():
        return None
    for lock, tool in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm")):
        if (p / lock).is_file():
            return f"{tool} install"
    for tool in ("pnpm", "yarn", "npm"):
        if shutil.which(tool, path=agent_path()):
            return f"{tool} install"
    return None


def copy_env(src: str, dst: str) -> list[str]:
    """Copy every .env / .env.* of the main checkout into the worktree at the same relative path (up to
    ENV_MAX_DEPTH folders deep, build/dependency folders skipped). Files the worktree already has stay."""
    copied = []
    src_p = Path(src)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src_p)
        dirs[:] = [d for d in dirs if d not in ENV_SKIP_DIRS and len(rel.parts) < ENV_MAX_DEPTH]
        for f in files:
            if f != ".env" and not f.startswith(".env."):
                continue
            target = Path(dst) / rel / f
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / f, target)
            copied.append(str(rel / f))
    return sorted(copied)


def root() -> Path:
    """Where new ticket worktrees go: <root>/<repo>/<ticket slug>. Not under a dot dir (dev servers that serve
    static files refuse paths with dot segments)."""
    return Path(os.path.realpath(os.path.expanduser(settings().get("worktree_root") or "~/tp-work/worktrees")))


def _git(cwd, *args, check=True, timeout=300):
    return run(["git", "-C", str(cwd), *args], check=check, timeout=timeout)


def worktrees(repos) -> list[dict]:
    """Linked worktrees (main checkouts excluded) of the given repos [(name, path)], Orca-made ones included."""
    out = []
    for name, path in repos:
        if not path or not os.path.isdir(path):
            continue
        p = _git(path, "worktree", "list", "--porcelain", check=False)
        if p.returncode:
            continue
        for i, block in enumerate(p.stdout.strip().split("\n\n")):
            f = dict((ln.split(" ", 1) + [""])[:2] for ln in block.splitlines())
            if i and "worktree" in f:  # the first entry is the main checkout
                wt = os.path.realpath(f["worktree"])
                out.append({"id": wt, "path": wt, "repo": name, "displayName": Path(wt).name,
                            "branch": f.get("branch", "").replace("refs/heads/", "")})
    return out


def create(repo: str, repo_path: str, name: str, base: str | None) -> dict:
    """New branch <branch_prefix><name> from origin/<base> (or origin's default branch) in <root>/<repo>/<name>."""
    path = root() / repo / name
    if path.exists():
        raise RuntimeError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "fetch", "-q", "origin")
    start = f"origin/{base}" if base else (
        _git(repo_path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False).stdout.strip() or "HEAD")
    branch = f"{settings().get('branch_prefix', 'tp/')}{name}"
    # --no-track: the agent pushes its own upstream (`git push -u`); tracking origin/<base> would make a bare
    # `git push` target the base branch
    _git(repo_path, "worktree", "add", "-q", "--no-track", "-b", branch, str(path), start)
    return {"id": str(path), "path": str(path), "repo": repo, "displayName": name, "branch": branch}


def checkout(repo: str, repo_path: str, name: str, branch: str) -> dict:
    """Worktree <root>/<repo>/<name> on an existing remote branch (a milestone branch, milestone.py): local branch
    <branch> at origin/<branch>, tracking it, so the conflict / rework agent pushes the milestone PR's head."""
    path = root() / repo / name
    if path.exists():
        raise RuntimeError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    _git(repo_path, "worktree", "add", "-q", "--track", "-B", branch, str(path), f"origin/{branch}")
    return {"id": str(path), "path": str(path), "repo": repo, "displayName": name, "branch": branch}


# role of each automation's AI step (cli.AUTOMATIONS ids) for the agents.<role> setting (TP-168)
AUTOMATION_ROLES = {"dispatch": "check", "review": "review", "diff": "check", "cleanup": "check"}


def resolve(conf: dict | None, role: str = "work") -> dict:
    """Agent settings {tool, model, effort, ultracode}: a repo's agent settings (repo.<name>.agent, ticket work only)
    > the role card (global agents.<role>: work | review | check) > the global agent tool. The first non-empty one
    gives model/effort/ultracode as a whole; an empty tool there falls back to the role card's tool, then the global
    agent (the 설정 page offers the models of that tool). Empty model/effort = the CLI's own config."""
    st = settings()
    card = (st.get("agents") or {}).get(role) or {}
    conf = conf if conf and any(conf.values()) else card
    tool = conf.get("tool") or card.get("tool") or st["agent"]
    return {"tool": tool, "model": conf.get("model") or None, "effort": conf.get("effort") or None,
            "ultracode": bool(conf.get("ultracode")) and tool == "claude"}


def describe(conf: dict | None, role: str = "work") -> str:
    """e.g. "claude opus/high ultracode", "codex" (for events and logs)."""
    a = resolve(conf, role)
    extra = "/".join(x for x in (a["model"], a["effort"]) if x)
    return " ".join(x for x in (a["tool"], extra, "ultracode" if a["ultracode"] else "") if x)


def command(a: dict, log: Path) -> list[str]:
    """The agent command line for resolved settings a (resolve()). Claude's session id is a["session"];
    a["resume"] = a session to continue (chat.py) instead of a new one. a["add_dirs"] = more folders the agent
    works in (a project's other repos, chat.py): claude --add-dir; codex exec --add-dir, but `codex exec resume`
    has no such flag (0.166), so a resumed codex turn relies on the paths from the first prompt (its sandbox is off)."""
    dirs = a.get("add_dirs") or ()
    if a["tool"] == "agy":
        cmd = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
               "--dangerously-skip-permissions", "--disable-slash-commands"]
        if a.get("resume"):
            cmd += ["--conversation", a["resume"]]
        if a["model"]:
            cmd += ["--model", a["model"]]
        if a["effort"]:
            cmd += ["--effort", a["effort"]]
        for directory in dirs:
            cmd += ["--add-dir", directory]
        return cmd
    if a["tool"] == "claude":
        cmd = [*CLAUDE, "--resume" if a.get("resume") else "--session-id", a["session"]]
        if a.get("compact"):
            # Claude's PostCompact hook supplies the actual summary. Stdout may contain status text.
            save = ("import json, pathlib, sys; "
                    "pathlib.Path(sys.argv[1]).write_text(json.load(sys.stdin)['compact_summary'], encoding='utf-8')")
            hook = {"type": "command", "command": shlex.join([sys.executable, "-c", save, f"{log}.summary"])}
            config = {"hooks": {"PostCompact": [{"matcher": "manual", "hooks": [hook]}]}}
            if a["ultracode"]:
                config["ultracode"] = True
            cmd = ["claude", "-p", "--resume", a["session"], "--no-session-persistence",
                   "--output-format", "json", "--setting-sources", "", "--strict-mcp-config", "--no-chrome",
                   "--settings", json.dumps(config)]
        cmd += [f"--add-dir={d}" for d in dirs]
        if a["model"]:
            cmd += ["--model", a["model"]]
        if a["effort"]:
            cmd += ["--effort", a["effort"]]
        if a["ultracode"] and not a.get("compact"):
            # the toggle, not --effort ultracode (that would pin the effort to xhigh)
            cmd += ["--settings", json.dumps({"ultracode": True})]
        return cmd
    cmd = [*CODEX[:2], "resume", *CODEX[2:]] if a.get("resume") else list(CODEX)
    if a["model"]:
        cmd += ["-m", a["model"]]
    if a["effort"]:
        cmd += ["-c", f'model_reasoning_effort="{a["effort"]}"']
    # --image=<path> per file: the spaced form takes several values and would swallow the "-" prompt argument
    cmd += [f"--image={i}" for i in a.get("images") or ()]
    if not a.get("resume"):
        cmd += [f"--add-dir={d}" for d in dirs]
    return [*cmd, "-o", f"{log}.last", *([a["resume"]] if a.get("resume") else []), "-"]


def launch(cwd: str, prompt: str, tag: str, setup: str | None = None, repo_path: str | None = None,
           agent: dict | None = None, resume: str | None = None, split_stderr: bool = False,
           images: list[str] | None = None, add_dirs: list[str] | None = None, role: str = "work",
           compact: bool = False) -> str:
    """Start the agent (agent = a repo's agent settings, see resolve()) in cwd with the prompt on stdin, detached;
    returns the handle "<pid> <log>". setup (a repo's worktree setup script) runs first in the same wrapper; its
    failure is logged, the agent still runs. resume = a session id of the same tool to continue. split_stderr
    writes stderr (codex progress, CLI warnings) to <log>.err so the log holds only the answer (chat.py).
    images = image files codex gets with --image (claude reads them itself from the paths in the prompt).
    role = whose agent card applies when the repo sets none (resolve()). compact = Claude /compact of the
    resumed history, with a PostCompact hook and session persistence disabled."""
    a = resolve(agent, role)
    a["resume"] = resume
    a["images"] = images
    a["add_dirs"] = add_dirs
    a["compact"] = compact
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', tag)}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    prompt_file = log.with_suffix(".prompt.md")
    # briefs call ticket-pilot's CLI from inside the target repo's worktree; {{ASANA}} = the agent's Asana rules
    prompt_file.write_text(prompt.replace("{{ASANA}}", asana_rules()).replace("{{TP}}", str(ROOT / "bin" / "tp"))
                           .replace("{{OWNER}}", settings().get("owner_name", "the user")))
    if a["tool"] == "claude":
        a["session"] = resume or str(uuid.uuid4())
    cmd = command(a, log)
    meta = {"agent": a["tool"], "model": a["model"], "effort": a["effort"], "ultracode": a["ultracode"],
            "session": a.get("session") or resume, "cmd": cmd, "cwd": cwd, "started": time.time()}
    Path(f"{log}.json").write_text(json.dumps(meta))
    if compact:
        cmd = [sys.executable, chat_usage.__file__, str(log), *cmd]
    elif a["tool"] == "agy":
        cmd = [sys.executable, "-I", antigravity.__file__, str(log), *cmd]
    script = ('if [ -n "$TP_SETUP" ]; then echo "--- worktree setup"; sh -c "$TP_SETUP" < /dev/null '
              '|| echo "--- worktree setup failed ($?), starting the agent anyway"; echo "--- agent"; fi; '
              '"$@" < "$TP_PROMPT"; echo $? > "$TP_EXIT"')
    # TP_AGENT marks the agent's processes for `tp asana` (asana_write.py); the PAT stays with tp (keychain).
    # gh_env: gh and https git push use tp's GitHub token (TP-112); without one they fail instead of using the owner's login
    env = gh_env({**{k: v for k, v in os.environ.items() if k != "ASANA_PAT"}, "TP_PROMPT": str(prompt_file),
                  "TP_EXIT": f"{log}.exit", "TP_SETUP": setup or "", "TP_BIN": str(ROOT / "bin" / "tp"),
                  "PATH": agent_path(), "TP_AGENT": tag}, required=False)
    from .accounts import agent_env  # accounts imports this module
    env.update(agent_env(a["tool"]))  # tp's Claude token / API key, only without the CLI's own login (TP-179)
    if repo_path:
        for k in ("TP_ROOT_PATH", "ORCA_ROOT_PATH"):
            env[k] = repo_path
        for k in ("TP_WORKTREE_PATH", "ORCA_WORKTREE_PATH"):
            env[k] = cwd
    with open(log, "w") as out, open(f"{log}.err" if split_stderr else os.devnull, "w") as err:
        p = subprocess.Popen(["/bin/sh", "-c", script, "sh", *cmd], cwd=cwd, stdout=out,
                             stderr=err if split_stderr else subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    return f"{p.pid} {log}"


def is_handle(handle: str | None) -> bool:
    """A handle from launch() (Orca terminal handles of runs started before the local runner are not)."""
    return bool(HANDLE.match(handle or ""))


def _parts(handle):
    m = HANDLE.match(handle or "")
    return (int(m.group(1)), Path(m.group(2))) if m else (None, None)


def _alive(pid: int) -> bool:
    """True while the process exists. Reaps it first when it is our own finished child (a zombie still answers
    kill 0): the process that launched an agent may check it again (relaunch, stop)."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def agent_state(handle: str | None) -> tuple[bool, int | None]:
    """(running, exit code). Exit code None = killed before the wrapper could record it, or not a runner handle."""
    pid, log = _parts(handle)
    if not pid:
        return False, None
    ex = Path(f"{log}.exit")
    if ex.exists():
        try:
            return False, int(ex.read_text().strip() or -1)
        except ValueError:
            return False, -1
    return _alive(pid), None


def running(handle: str | None) -> bool:
    return agent_state(handle)[0]


def started_at(handle: str | None) -> float | None:
    _, log = _parts(handle)
    try:
        return json.loads(Path(f"{log}.json").read_text()).get("started")
    except (OSError, ValueError, TypeError):
        return None


def window(handle: str | None) -> tuple[float | None, float | None]:
    """(start, end) of an agent as unix times; end None while it runs (the exit file's mtime once it ended)."""
    _, log = _parts(handle)
    if not log:
        return None, None
    try:
        end = Path(f"{log}.exit").stat().st_mtime
    except OSError:
        end = None
    return started_at(handle), end


def stop(handle: str | None, wait_s: float = 5) -> bool:
    """SIGTERM the agent's whole process group (wrapper, agent, its children), SIGKILL after wait_s."""
    pid, _ = _parts(handle)
    if not pid or not running(handle):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        for _ in range(int(wait_s * 10)):
            if not _alive(pid):
                return True
            time.sleep(0.1)
    return True


def session(handle: str | None) -> tuple[str | None, str | None]:
    """(agent, session id) of a launched agent, for resuming it by hand."""
    _, log = _parts(handle)
    try:
        meta = json.loads(Path(f"{log}.json").read_text())
    except (OSError, ValueError, TypeError):
        return None, None
    sid = meta.get("session")
    if meta.get("agent") == "agy":
        try:
            sid = Path(f"{log}.conversation").read_text().strip()
        except OSError:
            pass
        return "agy", sid if sid and antigravity.CONVERSATION.fullmatch(sid) else None
    for f in ((log, Path(f"{log}.err")) if not sid and log else ()):  # codex prints it in its header (stderr)
        try:
            with open(f, errors="replace") as fh:
                m = SESSION.search(fh.read(20000))
        except OSError:
            continue
        if m:
            sid = m.group(1)
            break
    return meta.get("agent"), sid


def resume_cmd(handle: str | None) -> str | None:
    agent, sid = session(handle)
    if not sid:
        return None
    if agent == "agy":
        return f"agy --conversation {sid}"
    return f"claude --resume {sid}" if agent == "claude" else f"codex resume {sid}"


def settings_of(handle: str | None) -> dict:
    """tool/model/effort/ultracode a launched agent ran with (from <log>.json)."""
    _, log = _parts(handle)
    try:
        meta = json.loads(Path(f"{log}.json").read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return {k: meta.get(k) for k in ("agent", "model", "effort", "ultracode")}


def compacted_summary(handle: str | None, limit: int) -> str:
    _, log = _parts(handle)
    if not log:
        return ""
    try:
        with Path(f"{log}.summary").open(encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return ""


def compacted_usage(handle: str | None) -> tuple:
    _, log = _parts(handle)
    return chat_usage.compaction(log) if log else (None,) * 5


def claude_turn_usage(handle: str | None, end: float | None) -> tuple:
    _, log = _parts(handle)
    if log:
        try:
            meta = json.loads(Path(f"{log}.json").read_text())
            if meta["agent"] == "claude" and meta["session"]:
                return chat_usage.transcript(meta["cwd"], meta["session"], meta["started"], end)
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return (None,) * 5


def last_message(handle: str | None, limit: int = 4000) -> str:
    """The agent's final message (often its question for the owner): codex writes it to <log>.last (-o), claude -p
    prints nothing else. Keeps the end, where the question usually is."""
    _, log = _parts(handle)
    if not log:
        return ""
    for f in (Path(f"{log}.last"), log):
        try:
            text = f.read_text(errors="replace").strip()
        except OSError:
            continue
        if f == log:
            text = text.rpartition("--- agent\n")[2].strip()
        if text:
            return text if len(text) <= limit else "…" + text[-limit:]
    return ""


def remove(repo_path: str, path: str, force: bool = False) -> None:
    """git worktree remove (refuses dirty worktrees unless force); cleanup.safety() decided that already."""
    _git(repo_path, "worktree", "remove", *(["--force"] if force else []), path)
