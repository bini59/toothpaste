"""Production vs staging diff per repo (Orca automation "ticket-pilot: staging diff").

Precheck (`tp precheck-diff`, deterministic, read-only on GitHub/AWS):
  for every repo whose deploy rule has a staging branch: compare <prod>...<staging>, group the commits into
  ticket-pilot tickets / other PRs / direct commits, read the staging pipeline for the staging head, count hotfixes
  that are in prod but not in staging, and store a verdict in `staging_diffs`:
    ready    staging is ahead and its pipeline is green (or it has none): worth deploying
    building staging pipeline still running for the head
    broken   staging pipeline failed for the head
    none     nothing in staging that prod lacks
  Exit 0 only when a repo is ahead and its staging head has no AI note yet, so the agent runs once per new head.
Agent (prompts/automation_diff.md): reads the diff and saves a Korean summary + recommendation
  (`tp diff-note --repo R --head SHA --level ok|check|hold --file note.md`). It never merges or deploys.
The 배포 page shows both; deploying stays a button press.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import os

from .core import db, event, gh_json, meta_set, now, project_map, repo_rules, run

_PR_MERGE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
_SQUASH = re.compile(r"\(#(\d+)\)\s*$")


def _con():
    return db()


def _commits(github: str, base: str, head: str) -> tuple[int, list[dict]]:
    j = gh_json(["api", f"repos/{github}/compare/{base}...{head}", "--jq",
                 "{ahead: .ahead_by, commits: [.commits[] | {sha, parents: [.parents[].sha], "
                 "msg: (.commit.message | split(\"\\n\")[0]), author: .commit.author.name, date: .commit.committer.date}]}"])
    return j["ahead"], j["commits"]


def _group(con, github: str, commits: list[dict], titles: dict[int, str]) -> list[dict]:
    """First-parent walk from the staging head: PR merges, squash-merged PRs, other merges and direct commits."""
    by = {c["sha"]: c for c in commits}
    head = commits[-1]["sha"] if commits else None
    chain, sha = [], head
    while sha in by:  # first-parent history inside the compare range
        chain.append(by[sha])
        sha = by[sha]["parents"][0] if by[sha]["parents"] else None
    items = []
    for c in reversed(chain):
        m = _PR_MERGE.match(c["msg"]) or (_SQUASH.search(c["msg"]) if len(c["parents"]) == 1 else None)
        if m:
            n = int(m.group(1))
            t = con.execute("""SELECT t.key, t.name, t.url, t.stage FROM prs p JOIN tickets t ON t.gid=p.ticket_gid
                               WHERE p.github=? AND p.number=?""", (github, n)).fetchone()
            base = {"number": n, "sha": c["sha"], "date": c["date"], "author": c["author"]}
            if t:
                items.append({"kind": "ticket", **base, "key": t["key"], "title": t["name"], "url": t["url"], "stage": t["stage"]})
            else:
                items.append({"kind": "pr", **base, "title": titles.get(n) or c["msg"]})
        elif len(c["parents"]) > 1:
            items.append({"kind": "merge", "sha": c["sha"], "msg": c["msg"], "date": c["date"], "author": c["author"]})
        else:
            items.append({"kind": "commit", "sha": c["sha"], "msg": c["msg"], "date": c["date"], "author": c["author"]})
    return items


def _git(repo_path, *args, timeout=180, check=True, input=None):
    return run(["git", "-C", repo_path, *args], timeout=timeout, check=check, input=input).stdout


def _patch_ids(repo_path, *args) -> set[str]:
    return {ln.split()[0] for ln in _git(repo_path, "patch-id", "--stable", input=_git(repo_path, *args)).splitlines()}


def _mark_live(repo_path: str | None, prod_sha: str, stg_sha: str, items: list[dict]) -> int:
    """Cherry-pick releases (hotfixes cherry-picked onto production) put a change in prod under a different sha. Mark items whose own commits
    all have a patch-equivalent in prod as `live`. Returns prod-only non-merge commits that staging lacks.
    Objects only: the local clone gets FETCH_HEAD updated, no branch or working-tree change."""
    if not repo_path or not os.path.isdir(repo_path):
        return -1
    _git(repo_path, "fetch", "-q", "--no-tags", "origin", prod_sha, stg_sha, timeout=300)
    minus = {ln[2:].strip() for ln in _git(repo_path, "cherry", prod_sha, stg_sha).splitlines() if ln.startswith("- ")}
    prod_ids = None
    for it in items:
        if it["kind"] == "merge":
            continue
        # no second parent: rev-parse -q --verify exits 1 with empty output -> squash commit
        if it["kind"] == "commit" or not _git(repo_path, "rev-parse", "-q", "--verify", f"{it['sha']}^2^{{commit}}", check=False).strip():
            own = [it["sha"]]  # direct or squash commit
        else:
            own = _git(repo_path, "rev-list", "--no-merges", f"{it['sha']}^1..{it['sha']}^2").split()
        it["live"] = bool(own) and all(s in minus for s in own)
        if not it["live"] and len(own) > 1:  # a promote PR squash-merged into prod lands all its commits as one
            if prod_ids is None:
                prod_ids = _patch_ids(repo_path, "log", "-p", "--no-merges", prod_sha, f"^{stg_sha}")
            it["live"] = bool(_patch_ids(repo_path, "diff", f"{own[-1]}^", own[0]) & prod_ids)
    return sum(1 for ln in _git(repo_path, "cherry", stg_sha, prod_sha).splitlines() if ln.startswith("+ "))


def scan_repo(con, name: str, rr: dict, github: str) -> dict:
    from .release import _execution_for

    prod, stg = rr["prod"]["branch"], rr["staging"]["branch"]
    ahead, commits = _commits(github, prod, stg)
    row = {"repo": name, "github": github, "prod_branch": prod, "staging_branch": stg, "ahead": ahead,
           "staging_head": None, "hotfixes": 0, "via_ahead": None, "pipeline": None, "oldest_at": None,
           "items_json": "[]", "error": None}
    row["staging_head"] = gh_json(["api", f"repos/{github}/commits/{stg}", "--jq", "{sha: .sha}"])["sha"]
    prod_sha = gh_json(["api", f"repos/{github}/commits/{prod}", "--jq", "{sha: .sha}"])["sha"]
    if rr.get("via"):
        row["via_ahead"] = gh_json(["api", f"repos/{github}/compare/{prod}...{rr['via']}", "--jq", "{a: .ahead_by}"])["a"]
    rp = con.execute("SELECT path FROM repos WHERE name=?", (name,)).fetchone()
    titles = {p["number"]: p["title"] for p in gh_json(["pr", "list", "--repo", github, "--state", "merged", "--base", stg,
                                                          "-L", "100", "--json", "number,title"]) or []} if ahead else {}
    items = _group(con, github, commits, titles)
    row["hotfixes"] = _mark_live(rp and rp["path"], prod_sha, row["staging_head"], items)
    if not any(not it.get("live") and it["kind"] != "merge" for it in items):
        return {**row, "items_json": json.dumps(items, ensure_ascii=False), "verdict": "none"}  # nothing new (or only cherry-picked)
    row["items_json"] = json.dumps(items, ensure_ascii=False)
    row["oldest_at"] = min((c["date"] for c in commits), default=None)
    verdict = "ready"
    if rr["staging"].get("pipeline"):
        ex = _execution_for(rr["staging"]["pipeline"], row["staging_head"])
        row["pipeline"] = ex["status"] if ex else "NotFound"
        if not ex or ex["status"] in ("InProgress", "Stopping"):
            verdict = "building"
        elif ex["status"] not in ("Succeeded",):
            verdict = "broken"
    return {**row, "verdict": verdict}


def diff_scan(log=print) -> list[dict]:
    con = _con()
    out = []
    rules = repo_rules()
    # repos dropped from deploy.repos must leave the 배포 page too
    con.execute(f"DELETE FROM staging_diffs WHERE repo NOT IN ({','.join('?' * len(rules))})", list(rules))
    for name, rr in rules.items():
        if not rr["staging"] or rr["staging"]["branch"] == rr["target"]:
            continue  # repos with only main, or whose staging branch is production
        gh = con.execute("SELECT github FROM repos WHERE name=?", (name,)).fetchone()
        if not gh or not gh["github"]:
            continue
        try:
            row = scan_repo(con, name, rr, gh["github"])
        except Exception as e:  # noqa: BLE001
            row = {"repo": name, "github": gh["github"], "verdict": "error", "error": str(e)[:300]}
        row["checked_at"] = now()
        cols = [k for k in row if k != "repo"]
        con.execute(f"""INSERT INTO staging_diffs(repo,{','.join(cols)}) VALUES(?,{','.join('?' * len(cols))})
                        ON CONFLICT(repo) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols)}""",
                    (name, *[row[c] for c in cols]))
        prev = con.execute("SELECT 1 FROM events WHERE kind='diff' AND message LIKE ?",
                           (f"%{name}%{(row.get('staging_head') or '')[:7]}%",)).fetchone()
        if row["verdict"] == "ready" and not prev:
            event(con, "diff", f"{name}: staging {row['staging_head'][:7]} has {row['ahead']} commit(s) not in "
                               f"{row['prod_branch']} and is green; ready to deploy")
        out.append(row)
        log(f"diff {name}: {row['verdict']} ahead={row.get('ahead')} hotfixes={row.get('hotfixes')} pipeline={row.get('pipeline')}"
            + (f" error={row['error']}" if row.get("error") else ""))
    meta_set(con, "last_diff_scan", now())
    con.commit()
    return out


def diff_queue(con=None) -> list[dict]:
    """Repos the agent should summarize: ahead of prod and no note for the current staging head."""
    con = con or _con()
    rows = con.execute("""SELECT * FROM staging_diffs WHERE ahead>0 AND verdict IN ('ready','building','broken')
                          AND (note_head IS NULL OR note_head<>staging_head) ORDER BY repo""").fetchall()
    rules, repos = repo_rules(), project_map().get("repos") or {}
    out = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d.pop("items_json") or "[]")
        d.pop("note_md", None)
        rp = con.execute("SELECT path FROM repos WHERE name=?", (r["repo"],)).fetchone()
        d["repo_path"] = rp and rp["path"]
        d["compare_url"] = f"https://github.com/{r['github']}/compare/{r['prod_branch']}...{r['staging_branch']}"
        d["train"] = rules.get(r["repo"], {}).get("train", False)
        # repo.<name>.depends_on: repos whose production this staging change may need first (prompts/automation_diff.md)
        d["depends_on"] = [{"repo": n, "github": (repos.get(n) or {}).get("github"),
                            "prod_branch": ((rules.get(n) or {}).get("prod") or {}).get("branch")}
                           for n in (repos.get(r["repo"]) or {}).get("depends_on") or []]
        out.append(d)
    return out


def save_note(repo: str, head: str, level: str, file: str):
    if level not in ("ok", "check", "hold"):
        raise SystemExit("level must be ok | check | hold")
    con = _con()
    r = con.execute("SELECT staging_head FROM staging_diffs WHERE repo=?", (repo,)).fetchone()
    if not r:
        raise SystemExit(f"no diff row for {repo}")
    if not r["staging_head"] or not r["staging_head"].startswith(head):
        raise SystemExit(f"staging head moved ({(r['staging_head'] or '')[:7]}); re-run tp diff-queue")
    con.execute("UPDATE staging_diffs SET note_md=?, note_level=?, note_head=?, note_at=? WHERE repo=?",
                (Path(file).read_text(), level, r["staging_head"], now(), repo))
    event(con, "diff", f"{repo}: AI note on staging {r['staging_head'][:7]} -> {level}")
    con.commit()


def precheck(log=print) -> bool:
    diff_scan(log)
    q = diff_queue()
    log(f"diff notes needed: {len(q)}")
    return bool(q)

