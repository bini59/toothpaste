"""Shared ticket worktree names and agent brief substitutions."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import milestone as M
from . import notion_tickets
from . import runner as R
from .core import ROOT, project_map, settings


# briefs call ticket-pilot's CLI by absolute path from inside the target repo's worktree
TP_BIN = str(ROOT / "bin" / "tp")


def slug(s: str) -> str:
    if s.startswith("ms:gh:"):
        _, repo = M.parse(s)
        return f"ms-{slug(repo)[:14]}-{hashlib.sha256(s.encode()).hexdigest()[:16]}"
    if match := re.fullmatch(r"NT-[0-9a-f]{32}-([a-zA-Z0-9_.-]+)", s):
        # Notion milestone keys share a 35-character parent prefix. Keep worktree names distinct at the 40-char limit.
        return f"ms-{match[1][:14].lower()}-{hashlib.sha256(s.encode()).hexdigest()[:16]}"
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _brief_dir(root: Path, gid: str) -> Path:
    return root / "data" / "briefs" / "by-ticket" / hashlib.sha256(gid.encode()).hexdigest()


def write_brief(root: Path, gid: str, text: str, *, name: str = "work") -> None:
    folder = _brief_dir(root, gid)
    folder.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(text)
            f.close()
            tmp.replace(folder / f"{name}.md")
        finally:
            tmp.unlink(missing_ok=True)


def find_brief(root: Path, gid: str, key: str, url: str | None) -> Path | None:
    brief = _brief_dir(root, gid) / "work.md"
    if brief.is_file():
        return brief
    # The old slug could belong to several tickets. Check the stored key and source link before using it.
    if not url:
        return None
    legacy = root / "data" / "briefs" / f"{slug(key)}.md"
    try:
        text = legacy.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith(f"# {key}: "):
        return None
    if gid.startswith("ms:"):
        summary = text.partition("\n\n")[2].partition("\n")[0]
        return legacy if summary.startswith("Milestone ") and f" ({url}) in " in summary else None
    ticket = text.partition("\n## Ticket\n")[2]
    return legacy if ticket.startswith(f"- Link: {url}\n") else None


def render_agent_brief(template, ticket):
    gid = ticket["project_gid"] if "project_gid" in ticket.keys() else None
    pid = gid.removeprefix("notion:") if gid and gid.startswith("notion:") else None
    template = template.replace("{{NOTION}}", notion_tickets.agent_rules(pid))
    if not pid and "{{PROJECT_ID}}" in template:
        repo = ticket["repo_override"] if "repo_override" in ticket.keys() else None
        projects = [p["id"] for p in project_map()["projects"] if (gid and p.get("asana_gid") == gid)
                    or (not gid and repo and p["source"] == "github" and any(r["repo"] == repo for r in p.get("repos") or []))]
        pid = projects[0] if len(projects) == 1 else None
    return (template.replace("{{PROJECT_ID}}", pid or "PROJECT_ID").replace("{{ASANA}}", R.asana_rules())
            .replace("{{TP}}", TP_BIN).replace("{{OWNER}}", settings().get("owner_name", "the user")))
