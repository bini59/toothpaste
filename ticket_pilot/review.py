"""AI review queue, saved verdicts and retries in an existing ticket worktree."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import metrics_usage as MU
from . import milestone as M
from . import notifications as N
from . import notion, notion_tickets
from . import runner as R
from .briefs import render_agent_brief, slug
from .core import ROOT, db, event, gh_api, now, orca_json, project_map, run, settings
from .pr_tracking import latest_run, mirror_orca, run_pr


def review_overridden(t, pr) -> bool:
    """The owner approved this head despite an AI 'blocked'/'changes' verdict, with a reason (TP-99; that commit only)."""
    return bool("review_override" in t.keys() and t["review_override"] and t["review_override_sha"] == pr["head_sha"])


def review_queue(con=None) -> list[dict]:
    """PRs waiting for an AI review of their head. Repos with tp checks (checks.py) only enter once every check of
    the head passed (or tp could not run it: 'error'); a failed head goes back to the agent instead, unless the owner
    overrode its checks with a reason (check_override)."""
    from . import checks as K
    con = con or db()
    rows = con.execute("""
      SELECT p.*, t.key, t.name tname, t.url turl, t.check_override, t.check_override_sha,
             r.worktree_path, r.ws_status, r.branch, rp.path repo_path
      FROM prs p JOIN runs r ON r.id=p.run_id JOIN tickets t ON t.gid=p.ticket_gid LEFT JOIN repos rp ON rp.name=p.repo
      WHERE p.state='OPEN' AND r.ws_status IN ('in-review','completed') AND r.state != 'abandoned'
        -- a milestone PR whose subtasks are still coming in is reviewed once they are all in
        AND COALESCE(t.stage, '') NOT IN ('collecting', 'ms_hold')
        AND NOT EXISTS (SELECT 1 FROM reviews v WHERE v.pr_url=p.url AND v.head_sha=p.head_sha
                        -- a reworked review whose agent marked in-review again without pushing needs a new pass
                        AND NOT (v.rework_at IS NOT NULL
                                 AND v.id=(SELECT max(id) FROM reviews WHERE pr_url=p.url)))
    """).fetchall()
    out = []
    for p in rows:
        cs = K.check_state(con, p)
        over = K.overridden(con, p)
        if cs["state"] in ("pending", "fail") and not over:
            continue
        # rounds and the previous review count AI reviews only; a tp check failure is not a review of the code
        prev = con.execute("SELECT verdict, summary, head_sha, body FROM reviews WHERE pr_url=? AND source IS NULL "
                           "ORDER BY id DESC LIMIT 1", (p["url"],)).fetchone()
        rounds = con.execute("SELECT count(*) c FROM reviews WHERE pr_url=? AND source IS NULL", (p["url"],)).fetchone()["c"]
        brief = ROOT / "data" / "briefs" / f"{slug(p['key'])}.md"
        out.append({
            "pr_url": p["url"], "github": p["github"], "number": p["number"], "title": p["title"],
            "head_sha": p["head_sha"], "ticket": p["key"], "ticket_title": p["tname"], "asana_url": p["turl"],
            "worktree_path": p["worktree_path"], "repo_path": p["repo_path"],
            "brief_file": str(brief) if brief.exists() else None,
            "round": rounds + 1,  # 1 = first review; later rounds review the diff since previous_review.head_sha
            "previous_review": dict(prev) if prev else None,
            # tp's own results for this head ([] = the repo has no checks: run its tests yourself as before)
            "checks": [{k: c[k] for k in ("name", "status", "cmd", "log_path")} for c in cs["rows"]],
            # The owner's reason for ignoring the checks of this head (they did not pass); null otherwise
            "check_override": p["check_override"] if over else None,
        })
    return out


VERDICT_LABEL = {"approve": "Looks good", "changes": "Changes requested", "blocked": "Blocked"}
VERDICT_KO = {"approve": "승인", "changes": "수정 요청", "blocked": "막힘"}
SHOT_BRANCH = "ticket-pilot-screenshots"


def upload_shots(github: str, prefix: str, files: list[str]) -> list[tuple[str, str]]:
    """Commit images to an orphan branch (PR branch untouched) and return (name, url) for Markdown embeds."""
    if run(["gh", "api", f"repos/{github}/branches/{SHOT_BRANCH}"], check=False).returncode != 0:
        tree = gh_api("POST", f"repos/{github}/git/trees", {"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
                      "content": "Screenshots attached to ticket-pilot AI reviews.\n"}]})
        commit = gh_api("POST", f"repos/{github}/git/commits",
                        {"message": "ticket-pilot screenshots [skip ci]", "tree": tree["sha"], "parents": []})
        gh_api("POST", f"repos/{github}/git/refs", {"ref": f"refs/heads/{SHOT_BRANCH}", "sha": commit["sha"]})
    out = []
    for f in files:
        path = f"{prefix}/{Path(f).name}"
        gh_api("PUT", f"repos/{github}/contents/{path}", {"message": f"screenshot {path} [skip ci]", "branch": SHOT_BRANCH,
               "content": base64.b64encode(Path(f).read_bytes()).decode()})
        out.append((Path(f).stem, f"https://github.com/{github}/blob/{SHOT_BRANCH}/{path}?raw=true"))
    return out


def screenshot(url: str, out: str, wait_ms: int = 3000, device: str | None = None, js: str | None = None,
               repo: str = "."):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    mods = Path(repo).resolve() / "node_modules"
    if (mods / "playwright").is_dir():
        p = subprocess.run(["node", str(Path(__file__).with_name("shot.cjs")), url, out, device or "Desktop Chrome HiDPI",
                            str(wait_ms), js or ""],
                           env={**os.environ, "NODE_PATH": str(mods)}, capture_output=True, text=True, timeout=180)
        if p.returncode:
            raise SystemExit(f"screenshot failed: {p.stderr.strip()[-800:]}")
        print(json.dumps({"saved": out}))
        return
    if device:
        raise SystemExit(f"--device needs Playwright installed in {mods} (pass --repo <worktree>)")
    if not shutil.which("orca"):
        raise SystemExit(f"no Playwright in {mods} (pass --repo <worktree with Playwright installed>)")
    # shortcut: Orca fallback is desktop-only and subject to CORS; install Playwright in the repo for more
    page = orca_json(["tab", "create", "--url", url])["browserPageId"]
    try:
        orca_json(["wait", "--page", page, "--timeout", str(wait_ms)])
        if js:  # e.g. scroll to the area under review
            orca_json(["eval", "--expression", js, "--page", page])
            orca_json(["wait", "--page", page, "--timeout", "1000"])
        data = orca_json(["screenshot", "--format", "png", "--page", page])["data"]
        Path(out).write_bytes(base64.b64decode(data))
    finally:
        orca_json(["tab", "close", "--page", page])
    print(json.dumps({"saved": out}))


def review_save(pr_url: str, sha: str, verdict: str, file: str, summary: str, post: bool = True, shots: list[str] = (),
                *, manual=False, restage):
    if verdict not in VERDICT_LABEL:
        raise SystemExit("verdict must be approve|changes|blocked")
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):  # a bad sha (e.g. "undefined") hides the approve button
        raise SystemExit(f"--sha must be the full 40-char head sha from review-queue, got {sha!r}")
    con = db()
    p = con.execute("SELECT p.*, t.key FROM prs p JOIN tickets t ON t.gid=p.ticket_gid WHERE p.url=?", (pr_url,)).fetchone()
    if not p:
        raise SystemExit(f"unknown PR {pr_url}; run `tp scan` first")
    body = Path(file).read_text().strip()
    for f in shots:
        if not Path(f).is_file():
            raise SystemExit(f"screenshot not found: {f}")
    comment_url = None
    if post:
        if shots:  # upload before posting so a failure leaves the PR unreviewed and the next run retries
            links = upload_shots(p["github"], f"pr-{p['number']}/{sha[:7]}-{int(time.time())}", list(shots))
            body += "\n\n## 스크린샷\n\n" + "\n\n".join(f"**{n}**\n\n![{n}]({u})" for n, u in links)
        text = (f"## AI review (ticket-pilot): {VERDICT_LABEL[verdict]}\n\n**Summary:** {summary}\n\n{body}\n\n---\n"
                f"<sub>Automated review for {p['key']} at `{sha[:7]}`. Not a merge approval; a human decides.</sub>\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
        out = run(["gh", "pr", "comment", pr_url, "--body-file", f.name], timeout=120)
        comment_url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else None
    review_log = os.environ.get("TP_REVIEW_LOG") if os.environ.get("TP_AGENT") == "auto-review" else None
    con.execute("""INSERT INTO reviews(pr_url,ticket_gid,head_sha,verdict,summary,body,comment_url,posted,created_at,
                                       manual,agent_log) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pr_url, p["ticket_gid"], sha, verdict, summary, body, comment_url, int(bool(comment_url)), now(),
                 int(manual), review_log))
    event(con, "review", f"{p['key']} PR #{p['number']}: {verdict}", p["ticket_gid"])
    if manual and verdict == "changes":
        event(con, "manual_rework", f"{p['key']} PR #{p['number']}: 수정 요청", p["ticket_gid"])
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (p["ticket_gid"],)).fetchone()
    if not (M.is_ms(t["gid"]) or M.in_milestone(t)):  # milestone work merges / waits on its own (TP-105)
        N.add(con, "review", f"{p['key']} PR #{p['number']} AI 리뷰: {VERDICT_KO[verdict]}", summary, pr_url, t["gid"])
    restage(con)
    con.commit()
    print(json.dumps({"saved": True, "comment_url": comment_url}))


# ---------------------------------------------------------------- retries

def max_rework_rounds(repo: str | None = None) -> int:
    """Fix rounds the AI reviewer may send one PR back to the agent before the owner decides (0 = no cap).
    A repo's own review.max_rework_rounds (설정 page) overrides the global value."""
    own = ((project_map().get("repos") or {}).get(repo or "") or {}).get("review") or {}
    if own.get("max_rework_rounds") is not None:
        return int(own["max_rework_rounds"])
    return int((settings().get("review") or {}).get("max_rework_rounds", 3))


def rework_rounds(con, t, pr) -> int:
    """Fix rounds already sent back to the agent on this PR since the owner last answered a BLOCKED question."""
    since = con.execute("SELECT max(ts) m FROM events WHERE ticket_gid=? AND kind='unblock'", (t["gid"],)).fetchone()["m"]
    return con.execute("SELECT count(*) c FROM reviews WHERE pr_url=? AND rework_at IS NOT NULL AND rework_at > ?",
                       (pr["url"], since or "")).fetchone()["c"]


def escalate_rework(con, t, cap, dry_run=False, log=print) -> bool:
    """The review still wants changes after `cap` fix rounds: stop the agent/reviewer loop and ask the owner.

    An agent author and an agent reviewer can cycle forever, mostly re-finding old code. The workspace gets a
    BLOCKED comment, so the ticket shows as blocked; the owner's dashboard reply relaunches the agent (unblock())
    and restarts the count."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not (r and pr and r["worktree_path"]) or rework_rounds(con, t, pr) < cap:
        return False
    rv = con.execute("SELECT * FROM reviews WHERE pr_url=? AND head_sha=? ORDER BY id DESC LIMIT 1",
                     (pr["url"], pr["head_sha"])).fetchone()
    if not rv:
        return False
    what = (f"{cap}번 고친 뒤에도 tp 검사 실패" if rv["source"] == "check" else f"AI 리뷰가 {cap}번 고친 뒤에도 수정 요청")
    q = (f"BLOCKED: {what} (PR #{pr['number']}). "
         f"계속 / 방향 변경 / 그만두기? 최근 {'검사' if rv['source'] == 'check' else '리뷰'}: {rv['summary'] or ''}")[:240]
    if dry_run:
        log(f"--- would ask the owner instead of rework round {cap + 1}: {q}")
        return False
    mirror_orca(r, "todo", q, unread=True)
    ts = now()
    # rework_at marks this review handled, so the reply relaunches only one agent (unblock), not a rework too
    con.execute("UPDATE reviews SET rework_at=? WHERE id=?", (ts, rv["id"]))
    con.execute("UPDATE runs SET ws_status='todo', ws_comment=?, updated_at=? WHERE id=?", (q, ts, r["id"]))
    event(con, "rework_cap", f"{t['key']}: PR #{pr['number']} still needs changes after {cap} rounds -> asked the owner",
          t["gid"])
    N.blocked(con, t["gid"], q, pr["repo"])
    con.commit()
    log(f"rework cap {t['key']}: asked the owner")
    return True


def build_rework_brief(t, run, pr, rv, round_no=1) -> str:
    tpl = (ROOT / "prompts" / "rework_brief.md").read_text()
    brief = ROOT / "data" / "briefs" / f"{slug(t['key'])}.md"
    sha7 = (pr["head_sha"] or "")[:7]
    vals = {
        "OPENING": (f"ticket-pilot's own repo checks (`tp check`, run by the host on head `{sha7}`) failed on your draft "
                    f"PR {pr['url']} (#{pr['number']})." if rv["source"] == "check" else
                    f"The ticket-pilot AI review of your draft PR {pr['url']} (#{pr['number']}, head `{sha7}`) "
                    "requested changes."),
        "KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "PR_URL": pr["url"], "PR_NUMBER": pr["number"],
        "HEAD_SHA": (pr["head_sha"] or "")[:7], "BRANCH": run["branch"] or "", "SUMMARY": rv["summary"] or "",
        "REVIEW": (rv["body"] or "").strip(), "BRIEF_FILE": str(brief) if brief.exists() else "(not available)",
        "ROUND": round_no, "MAX_ROUNDS": max_rework_rounds(pr["repo"]) or "unlimited",
    }
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return render_agent_brief(tpl, t)


def rework(con, t, dry_run=False, log=print) -> bool:
    """'changes' verdict -> relaunch the agent in the same worktree/branch with the review as its brief."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    rv = pr and con.execute("SELECT * FROM reviews WHERE pr_url=? AND head_sha=? ORDER BY id DESC LIMIT 1",
                            (pr["url"], pr["head_sha"])).fetchone()
    if not (r and pr and rv and r["worktree_path"]):
        log(f"rework {t['key']}: missing run/PR/review, skipped")
        return False
    brief = build_rework_brief(t, r, pr, rv, rework_rounds(con, t, pr) + 1)
    if dry_run:
        log(f"--- would relaunch {st_agent()} in {r['worktree_path']}\n{brief}")
        return False
    (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "briefs" / f"{slug(t['key'])}-rework-{(pr['head_sha'] or '')[:7]}.md").write_text(brief)
    why = "tp checks failed" if rv["source"] == "check" else "AI review requested changes"
    comment = f"rework: {why} on PR #{pr['number']}"[:120]
    try:
        handle = relaunch(con, r, brief, f"{slug(t['key'])}-rework", comment, ticket=t)
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"{t['key']}: rework launch failed: {e}", t["gid"])
        con.commit()
        log(f"ERROR rework {t['key']}: {e}")
        return False
    ts = now()
    con.execute("UPDATE reviews SET rework_at=? WHERE id=?", (ts, rv["id"]))
    # back to in-progress so the review automation waits for the agent's in-review signal
    con.execute("UPDATE runs SET ws_status='in-progress', ws_comment=?, terminal=?, updated_at=? WHERE id=?",
                (comment, handle, ts, r["id"]))
    event(con, "rework", f"{t['key']}: PR #{pr['number']} {rv['summary'] if rv['source'] == 'check' else 'changes requested'}"
                         f" -> agent relaunched in {r['worktree_name']}", t["gid"])
    con.commit()
    log(f"rework {t['key']} -> {r['repo']}/{r['worktree_name']} terminal={handle}")
    return True


def build_conflict_brief(t, run, pr) -> str:
    tpl = (ROOT / "prompts" / "conflict_brief.md").read_text()
    vals = {
        "KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "PR_URL": pr["url"], "PR_NUMBER": pr["number"],
        "HEAD_SHA": (pr["head_sha"] or "")[:7], "BRANCH": run["branch"] or "", "BASE": pr["base"] or "",
    }
    for k, v in vals.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return render_agent_brief(tpl, t)


def resolve_conflict(con, t, dry_run=False, log=print) -> bool:
    """충돌 대기 -> relaunch the agent in the same worktree to merge the PR's base branch in (prompts/conflict_brief.md).
    Stage 'conflict' is set mechanically by compute_stage for any open PR GitHub reports CONFLICTING (any repo/base);
    tp dispatch picks it up like rework (ahead of new tickets, within max_active_runs). Once per head commit."""
    r = latest_run(con, t["gid"])
    pr = r and run_pr(con, r["id"])
    if not (r and pr and r["worktree_path"] and pr["base"]):
        log(f"conflict {t['key']}: missing run/PR/base, skipped")
        return False
    brief = build_conflict_brief(t, r, pr)
    if dry_run:
        log(f"--- would relaunch {st_agent()} in {r['worktree_path']}\n{brief}")
        return False
    (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "briefs" / f"{slug(t['key'])}-conflict-{(pr['head_sha'] or '')[:7]}.md").write_text(brief)
    comment = f"conflict: PR #{pr['number']} conflicts with {pr['base']}, merging it in"[:120]
    try:
        handle = relaunch(con, r, brief, f"{slug(t['key'])}-conflict", comment, ticket=t)
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"{t['key']}: conflict relaunch failed: {e}", t["gid"])
        con.commit()
        log(f"ERROR conflict {t['key']}: {e}")
        return False
    ts = now()
    con.execute("UPDATE prs SET conflict_sha=?, conflict_at=? WHERE url=?", (pr["head_sha"], ts, pr["url"]))
    con.execute("UPDATE runs SET ws_status='in-progress', ws_comment=?, terminal=?, updated_at=? WHERE id=?",
                (comment, handle, ts, r["id"]))
    # a pending dashboard approval cannot merge a conflicting head; the new head needs a fresh review + approval
    con.execute("UPDATE tickets SET approve_requested_at=NULL, approve_error=NULL WHERE gid=?", (t["gid"],))
    event(con, "conflict_rework", f"{t['key']}: PR #{pr['number']} ({(pr['head_sha'] or '')[:7]}) conflicts with "
                                  f"{pr['base']} -> agent relaunched in {r['worktree_name']}", t["gid"])
    con.commit()
    log(f"conflict {t['key']} -> {r['repo']}/{r['worktree_name']} terminal={handle}")
    return True


def unblock(con, t, dry_run=False, log=print) -> bool:
    """BLOCKED + the owner's dashboard reply -> relaunch the agent in the same worktree with their answer."""
    r = latest_run(con, t["gid"])
    if not (r and r["worktree_path"]):
        log(f"unblock {t['key']}: missing run, skipped")
        return False
    brief = (ROOT / "prompts" / "unblock_brief.md").read_text()
    orig = ROOT / "data" / "briefs" / f"{slug(t['key'])}.md"
    for k, v in {"KEY": t["key"], "TITLE": t["name"], "URL": t["url"], "BRANCH": r["branch"] or "",
                 "BRIEF_FILE": str(orig) if orig.exists() else "(not available)",
                 "QUESTION": r["ws_comment"] or "", "REPLY": t["reply"]}.items():
        brief = brief.replace("{{" + k + "}}", str(v))
    brief = render_agent_brief(brief, t)
    if dry_run:
        log(f"--- would relaunch {st_agent()} in {r['worktree_path']}\n{brief}")
        return False
    comment = f"answered: {t['reply']}"[:120]
    try:
        handle = relaunch(con, r, brief, f"{slug(t['key'])}-unblock", comment, ticket=t)
    except Exception as e:  # noqa: BLE001
        event(con, "error", f"{t['key']}: unblock launch failed: {e}", t["gid"])
        con.commit()
        log(f"ERROR unblock {t['key']}: {e}")
        return False
    ts = now()
    con.execute("UPDATE runs SET ws_status='in-progress', ws_comment=?, terminal=?, updated_at=? WHERE id=?",
                (comment, handle, ts, r["id"]))
    con.execute("UPDATE tickets SET reply=NULL WHERE gid=?", (t["gid"],))
    event(con, "unblock", f"{t['key']}: the owner answered -> agent relaunched in {r['worktree_name']}", t["gid"])
    con.commit()
    log(f"unblock {t['key']} -> {r['repo']}/{r['worktree_name']} terminal={handle}")
    return True


def st_agent() -> str:
    return R.resolve(None)["tool"]


def relaunch(con, r, brief: str, tag: str, comment: str, *, ticket=None) -> str:
    """Start the agent again in a run's worktree (rework / conflict / unblock); returns the new handle.
    An agent still running there is stopped first: two agents on one branch fight over it."""
    if R.running(r["terminal"]):
        R.stop(r["terminal"])
    if (r["branch"] or "").startswith(M.PREFIX) and r["worktree_path"]:
        # milestone PR worktree: subtask merges may have moved the branch on GitHub since the checkout
        run(["git", "-C", r["worktree_path"], "pull", "-q", "--ff-only"], timeout=180, check=False)
    mirror_orca(r, "in-progress", comment)
    rp = con.execute("SELECT path FROM repos WHERE name=?", (r["repo"],)).fetchone()
    if ticket is not None and (err := notion_tickets.retry_error(con, ticket, lock=True)):
        raise notion.NotionError(err)
    handle = R.launch(r["worktree_path"], brief, tag, repo_path=rp["path"] if rp else None,
                      agent=R.repo_extra(r["repo"]).get("agent"))
    MU.register(con, handle, "work", r["ticket_gid"], track_ws_set=True)
    return handle
