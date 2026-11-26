"""An Asana parent tagged with milestone_tag or a related Notion milestone page collects its
subtasks on a branch per repo and ships to staging once, as one ticket.

  subtask (tagged asana_tag, parent tagged milestone_tag)
      worktree from origin/milestone/<parent gid>, PR base milestone/<parent gid> (open PRs on staging are retargeted)
      AI review approve (+ tp checks) -> tick() requests the merge itself -> process_approvals merges it into the
      milestone branch (no dashboard button)                                  stage: reviewed -> approving -> milestone
  milestone ticket, one per (parent, repo): gid ms:<parent gid>:<repo>, created together with the branch
      while subtasks are open: stage collecting; the feature base is merged into the branch (GitHub merges API) so
      it does not drift; a conflict there is left for the milestone PR's conflict flow
      every tagged subtask finished (merged into the milestone, or done / closed / skipped): the host checks the
      branch out in a worktree and opens milestone/<parent gid> -> staging. From here it is an ordinary ticket:
      AI review, rework / conflict agents in that worktree, the owner's staging approval, the weekday train
      live in production: complete() completes the milestone ticket, the subtasks merged into it and, once every
      repo of the milestone is live, the parent

Milestones apply in every repo that merges approved PRs into a staging branch: feature-base repos (train repos,
pr_into_staging repos) and staging-mode repos with a via branch (staging -> main -> production via the 배포 page).
"""
from __future__ import annotations

import json
import os
import re

from .core import db, event, feature_base, meta_get, meta_set, now, repo_rules, run, settings
from . import notifications as N
from . import runner as R
from . import notion_stages, notion_tickets

PREFIX = "milestone/"
# subtask stages that no longer hold the milestone back
FINISHED = {"milestone", "done", "closed", "skipped", "staged", "merged", "deploying"}
# A merged Notion PR travels with its actual branch even if its task's relation changes later.
_MERGED_SUBTASKS = """ FROM prs p JOIN tickets s ON s.gid=p.ticket_gid
    WHERE p.repo=:repo AND p.base=:base AND p.state='MERGED'
      AND (s.parent_gid=:parent OR (:parent LIKE 'notion:%' AND s.gid LIKE 'notion:%')) """
# The current run's landed PR fixes its release destination even if the editable relation moves.
LANDED_NOTION = """SELECT p.ticket_gid, ms.parent_gid, ms.parent_name, ms.url FROM prs p
    JOIN tickets ms ON ms.gid='ms:notion:' || substr(p.base, length('milestone/')+1) || ':' || p.repo
    JOIN runs r ON r.id=p.run_id
    WHERE p.ticket_gid LIKE 'notion:%' AND p.state='MERGED' AND p.base LIKE 'milestone/%'
      AND r.state!='abandoned' AND r.id=(SELECT max(id) FROM runs WHERE ticket_gid=p.ticket_gid)
      AND p.number=(SELECT max(number) FROM prs WHERE run_id=r.id)"""


def tag() -> str:
    return (settings().get("milestone_tag") or "tp-milestone").lower()


def has_tag(tags: list[str]) -> bool:
    return tag() in [x.lower() for x in tags or []]


def is_ms(gid: str | None) -> bool:
    return bool(gid) and gid.startswith("ms:")


def ms_gid(parent_gid: str, repo: str) -> str:
    return f"ms:{parent_gid}:{repo}"


def parse(gid: str) -> tuple[str, str]:
    parent, repo = gid.removeprefix("ms:").rsplit(":", 1)
    return parent, repo


def branch(parent_gid: str) -> str:
    return PREFIX + parent_gid.removeprefix("notion:")


def short(repo: str) -> str:
    """30_Shop_Api -> Api"""
    return repo.rsplit("_", 1)[-1]


def refresh_parent(con, parent: str, name: str, url: str):
    for gid, repo in con.execute("""SELECT gid, repo_override FROM tickets WHERE parent_gid=? AND gid LIKE 'ms:notion:%'
                                    AND (parent_name IS NOT ? OR url IS NOT ?)""", (parent, name, url)).fetchall():
        con.execute("UPDATE tickets SET parent_name=?,name=?,url=? WHERE gid=?",
                    (name, f"{name} · {short(repo)} 마일스톤", url, gid))


def in_milestone(t) -> bool:
    return bool(t["parent_gid"]) and "in_milestone" in t.keys() and bool(t["in_milestone"])


def staging_of(repo: str | None, *, notion: bool = False) -> str | None:
    """Notion milestones need an approvable staging branch before production. Asana also uses the feature base."""
    if not notion and (fb := feature_base(repo)):
        return fb
    rr = repo_rules(repo) if repo else None
    if rr and rr["staging"] and "staging" in rr["modes"]:
        stg = rr["staging"]["branch"]
        if stg != rr["target"] and (not notion or stg != rr["prod"]["branch"]):
            return stg
    return None


def notion_milestone(t, base: str | None = None) -> bool:
    # A relation and its open PR base can change on different ticks.
    return t["gid"].startswith("ms:notion:") or (
        t["gid"].startswith("notion:") and (in_milestone(t) or (base or "").startswith(PREFIX)))


def staging_config_error(t, repo: str, base: str | None = None) -> str | None:
    if notion_milestone(t, base) and not staging_of(repo, notion=True):
        return (f"Notion 마일스톤에는 staging 머지가 필요해요. 프로젝트 → 설정 → 레포별 설정 → {repo} → 배포에서 "
                "‘staging 브랜치 사용’을 켜고 중간·prod 브랜치와 다른 staging 브랜치를 지정한 뒤, ‘배포 방식’에서 staging을 선택하세요.")
    return None


def base_for(t, repo: str | None) -> str | None:
    """Branch t's PR targets (and its worktree starts from): its milestone branch, else the repo's feature base."""
    # A Notion relation fixes the required base even if the repo loses its staging settings before launch.
    if t is not None and in_milestone(t) and (t["parent_gid"].startswith("notion:") or staging_of(repo)):
        return branch(t["parent_gid"])
    return feature_base(repo)


def _gh(args: list[str], check: bool = True, timeout: int = 120):
    return run(["gh", *args], timeout=timeout, check=check)


def _ahead(gh: str, base: str, head: str) -> int:
    """Commits head has that base lacks."""
    return int(_gh(["api", f"repos/{gh}/compare/{base}...{head}", "--jq", ".ahead_by"]).stdout.strip() or 0)


def check_retarget(gh, old_base, new_base, head):
    """Changing milestones must not take the old milestone's other work along on the feature PR."""
    fork = _gh(["api", f"repos/{gh}/compare/{old_base}...{head}", "--jq", ".merge_base_commit.sha"]).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", fork) or _ahead(gh, new_base, fork):
        raise RuntimeError(f"{old_base}에서 가져온 커밋이 있어요. 이 작업의 커밋만 {new_base} 위로 옮겨 주세요")


def _github(con, repo: str) -> str | None:
    r = con.execute("SELECT github FROM repos WHERE name=?", (repo,)).fetchone()
    return r["github"] if r else None


def ensure(con, t, repo: str) -> str:
    """Create the milestone branch of t's parent in repo (from the feature base) and its milestone ticket if missing.
    Returns the branch."""
    parent = t["parent_gid"]
    b, fb, gh = branch(parent), staging_of(repo, notion=parent.startswith("notion:")), _github(con, repo)
    if not (fb and gh):
        raise RuntimeError(f"{repo}: a milestone needs a staging branch and a GitHub repo")
    gid = ms_gid(parent, repo)
    if con.execute("SELECT 1 FROM tickets WHERE gid=?", (gid,)).fetchone():
        return b
    if _gh(["api", f"repos/{gh}/git/ref/heads/{b}"], check=False).returncode:
        sha = _gh(["api", f"repos/{gh}/commits/{fb}", "--jq", ".sha"]).stdout.strip()
        _gh(["api", "-X", "POST", f"repos/{gh}/git/refs", "-f", f"ref=refs/heads/{b}", "-f", f"sha={sha}"])
        event(con, "milestone", f"{t['parent_name'] or t['parent_gid']}: {repo} {b} created from {fb} ({sha[:7]})", gid)
    p = con.execute("SELECT * FROM tickets WHERE gid=?", (parent,)).fetchone()
    if p:
        pkey = p["key"]
    elif parent.startswith("notion:"):
        pkey = notion_tickets.ticket_key(parent.removeprefix("notion:"))
    else:
        pkey = f"AS-{parent}"
    suffix = repo if parent.startswith("notion:") else short(repo)
    ts = now()
    con.execute("""INSERT INTO tickets(gid,key,name,url,project_gid,project_name,parent_gid,parent_name,notes,tags,
                   has_tag,deps_json,open_deps,completed,assigned,first_seen,last_seen,repo_override)
                   VALUES(?,?,?,?,?,?,?,?,?,'[]',1,'[]',0,0,1,?,?,?)""",
                (gid, f"{pkey}-{suffix}", f"{t['parent_name'] or pkey} · {short(repo)} 마일스톤",
                 p["url"] if p else t["parent_url"], t["project_gid"], t["project_name"], parent, t["parent_name"],
                 f"{repo}의 {b} 브랜치를 {fb}로 보내는 마일스톤 PR", ts, ts, repo))
    return b


def subtasks(con, parent_gid: str) -> list:
    """The parent's subtasks ticket-pilot works on (tagged), milestone tickets excluded."""
    if not parent_gid:
        return []
    if parent_gid.startswith("notion:"):
        # A Notion stage write can move a running subtask out of its import filter before its PR lands.
        return con.execute(f"""SELECT t.* FROM tickets t LEFT JOIN ({LANDED_NOTION}) landed ON landed.ticket_gid=t.gid
                               WHERE t.gid LIKE 'notion:%' AND COALESCE(landed.parent_gid,t.parent_gid)=?
                                 AND ((has_tag=1 AND assigned=1) OR
                                      (SELECT state FROM runs WHERE ticket_gid=t.gid ORDER BY id DESC LIMIT 1) != 'abandoned')""",
                           (parent_gid,)).fetchall()
    return con.execute("SELECT * FROM tickets WHERE parent_gid=? AND has_tag=1 AND assigned=1 AND gid NOT LIKE 'ms:%'",
                       (parent_gid,)).fetchall()


def ready(con, parent_gid: str) -> bool:
    if parent_gid.startswith("notion:"):
        # Relations remain editable after a PR lands. Its actual branch determines what still needs shipping.
        return unshipped(con, parent_gid) and not unfinished(con, parent_gid)
    subs = subtasks(con, parent_gid)
    return bool(subs) and all(s["stage"] in FINISHED for s in subs) and any(s["stage"] == "milestone" for s in subs)


def unfinished(con, parent_gid: str) -> list:
    """Tagged subtasks still being worked on (not yet in the milestone branch, done, closed or skipped)."""
    return [s for s in subtasks(con, parent_gid) if s["stage"] not in FINISHED and not s["completed"]]


def last_pr(con, gid: str):
    return con.execute("SELECT * FROM prs WHERE ticket_gid=? ORDER BY number DESC LIMIT 1", (gid,)).fetchone()


def unshipped(con, parent_gid: str) -> bool:
    return bool(con.execute("""SELECT 1 FROM prs WHERE ticket_gid LIKE 'notion:%' AND base=?
                               AND state='MERGED' AND prod_at IS NULL LIMIT 1""", (branch(parent_gid),)).fetchone())


def merged_since(con, gid: str, since: str | None) -> list:
    """Subtask PRs merged into this milestone ticket's branch after `since` (the merge of its last milestone PR)."""
    parent, repo = parse(gid)
    return con.execute("SELECT p.*" + _MERGED_SUBTASKS + "AND p.merged_at > :since",
                       {"parent": parent, "repo": repo, "base": branch(parent), "since": since or ""}).fetchall()


def stage(con, t, pr) -> str | None:
    """A milestone ticket's own stage, or None for the ordinary PR flow (pipeline.compute_stage).
    collecting (하위작업 진행 중): a subtask is still being worked on (also while the milestone PR is open: it goes back
      to 작업 중, 2026-10-30), or the owner pressed 마일스톤 작업 완료 and tick is about to open the PR
    ms_hold (작업 완료 대기, 대기 column): every subtask is in, but the owner has not pressed 마일스톤 작업 완료. Nothing
      moves on mechanically: no milestone PR, no AI review, no staging approval (2026-10-30)
    New subtasks after a merged milestone PR start the next round the same way: on staging right away, after it went
    live once tick reopens the ticket."""
    busy = unfinished(con, t["parent_gid"])
    done = t["ms_done_at"] if "ms_done_at" in t.keys() else None
    if not pr:
        if t["skip"]:
            return "skipped"
        return "collecting" if busy or done else "ms_hold"
    if pr["state"] == "OPEN":
        return "collecting" if busy else None if done else "ms_hold"
    # a subtask merged after the milestone PR (live or only on staging) starts the next round (2026-11-10).
    if pr["state"] == "MERGED" and (busy or merged_since(con, t["gid"], pr["merged_at"])):
        return "collecting" if busy or done else "ms_hold"
    return None


def awaiting_next_round(con, gid: str | None) -> bool:
    """The train waits for a milestone's next round to merge before shipping its staging commits."""
    if not is_ms(gid):
        return False
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
    pr = last_pr(con, gid)
    return bool(t and pr and (pr["state"] != "MERGED" or stage(con, t, pr)))


def mark_done(parent_gid: str, cancel: bool = False) -> None:
    """Dashboard 마일스톤 작업 완료 (or its 취소): only records it; tick opens the milestone PRs. Refused while a
    subtask is still in progress, and cleared by tick as soon as one comes in again (reset_done)."""
    from .pipeline import restage
    con = db()
    like = f"ms:{parent_gid}:%"
    rows = con.execute("SELECT * FROM tickets WHERE gid LIKE ? AND completed=0", (like,)).fetchall()
    if not rows:
        raise SystemExit("진행할 마일스톤 티켓이 없어요 (아직 투입된 하위작업이 없거나 이미 배포됐어요)")
    busy = unfinished(con, parent_gid)
    if busy and not cancel:
        raise SystemExit(f"하위작업 {len(busy)}개가 아직 진행 중이에요: " + ", ".join(s["key"] for s in busy[:3]))
    con.execute("UPDATE tickets SET ms_done_at=? WHERE gid LIKE ? AND completed=0", (None if cancel else now(), like))
    if not cancel:
        N.read_ms_ready(con, parent_gid)
    name = rows[0]["parent_name"] or parent_gid
    event(con, "milestone", f"{name}: 마일스톤 작업 {'완료 취소' if cancel else '완료'} (dashboard)", rows[0]["gid"])
    restage(con)
    con.commit()


def reset_done(con, log=print) -> int:
    """Safety net: a subtask in progress again (new, reopened, sent back) undoes 마일스톤 작업 완료, so the milestone
    waits in 대기 for the owner's next press instead of shipping without it."""
    n = 0
    for p in con.execute("SELECT parent_gid, parent_name, min(gid) gid FROM tickets WHERE gid LIKE 'ms:%' AND completed=0 "
                         "AND ms_done_at IS NOT NULL GROUP BY parent_gid").fetchall():
        busy = unfinished(con, p["parent_gid"])
        if not busy:
            continue
        con.execute("UPDATE tickets SET ms_done_at=NULL WHERE gid LIKE ?", (f"ms:{p['parent_gid']}:%",))
        event(con, "milestone", f"{p['parent_name'] or p['parent_gid']}: 하위작업이 다시 진행 중이라 마일스톤 작업 완료를 "
                                f"취소했어요 ({', '.join(s['key'] for s in busy[:3])})", p["gid"])
        log(f"milestone {p['parent_gid']}: done mark cleared")
        n += 1
    return n


def notify_ready(con) -> int:
    """수신함 alert (TP-105), once per milestone: every subtask is in and the owner has not pressed 마일스톤 작업 완료.
    meta ms_notified = parents already alerted; a subtask in progress again (reset_done's condition) or a round that
    went live takes the parent out, so the next time it is ready alerts again."""
    notified = set(meta_get(con, "ms_notified", []))
    n = 0
    for p in con.execute("""SELECT parent_gid, max(parent_name) name, min(gid) gid,
                                   max(completed=0 AND skip=0) open, max(completed=0 AND skip=0 AND ms_done_at IS NULL) waiting
                            FROM tickets WHERE gid LIKE 'ms:%' GROUP BY parent_gid""").fetchall():
        pg = p["parent_gid"]
        if not p["open"] or unfinished(con, pg):
            if pg in notified:
                N.read_ms_ready(con, pg)
                notified.discard(pg)
        elif p["waiting"] and pg not in notified and ready(con, pg):
            N.add(con, "ms_ready", f"{p['name'] or pg}: 마일스톤 작업 완료 처리 필요",
                  f"하위작업 {len(subtasks(con, pg))}개가 모두 끝났어요. 마일스톤 페이지에서 마일스톤 작업 완료를 눌러 주세요.",
                  f"#milestones/{pg}", p["gid"])
            notified.add(pg)
            n += 1
    meta_set(con, "ms_notified", sorted(notified))
    return n


def _notion_repo_has_work(con, parent, repo, gid):
    from .pipeline import chosen_repo, latest_run
    # An existing PR follows its actual branch until it is retargeted, closed or verified live.
    if con.execute("""SELECT 1 FROM prs WHERE repo=? AND (ticket_gid=? OR (ticket_gid LIKE 'notion:%' AND base=?))
                      AND (state='OPEN' OR (state='MERGED' AND prod_at IS NULL AND release_id IS NULL)) LIMIT 1""",
                   (repo, gid, branch(parent))).fetchone():
        return True
    for t in unfinished(con, parent):
        run = latest_run(con, t["gid"])
        owner = run if run and run["state"] != "abandoned" else chosen_repo(con, t)
        if owner and owner["repo"] == repo:
            return True
    return False


def reopen(con, log=print) -> int:
    """Reopen milestones with new work. A Notion relation can also move all work out of a repo."""
    n = 0
    for t in con.execute("SELECT * FROM tickets WHERE gid LIKE 'ms:%' AND (completed=1 OR gid LIKE 'ms:notion:%')").fetchall():
        if t["gid"].startswith("ms:notion:"):
            parent, repo = parse(t["gid"])
            needed = _notion_repo_has_work(con, parent, repo, t["gid"])
            if needed == (not t["completed"]):
                continue
            if not needed:
                con.execute("UPDATE tickets SET completed=1,ms_done_at=NULL WHERE gid=?", (t["gid"],))
                event(con, "milestone", f"{t['key']}: {repo}에 남은 하위 작업이나 배포할 PR이 없어요", t["gid"])
                # Another repo may have gone live before this last, empty repo dropped out.
                if all_repos_live(con, parent) and con.execute("""SELECT 1 FROM prs WHERE ticket_gid LIKE ?
                        AND state='MERGED' AND (prod_at IS NOT NULL OR release_id IS NOT NULL) LIMIT 1""",
                        (f"ms:{parent}:%",)).fetchone():
                    notion_stages.enqueue(con, t, notion_stages.MILESTONE_DONE)
                continue
        else:
            pr = last_pr(con, t["gid"])
            if pr and pr["state"] == "MERGED":
                more = unfinished(con, t["parent_gid"]) or merged_since(con, t["gid"], pr["merged_at"])
            else:  # completed without a PR (nothing to ship): only a subtask in progress brings it back
                more = not pr and unfinished(con, t["parent_gid"])
            if not more:
                continue
        con.execute("UPDATE tickets SET completed=0, ms_done_at=NULL WHERE gid=?", (t["gid"],))
        event(con, "milestone", f"{t['key']}: new subtasks after the last milestone PR; collecting the next round",
              t["gid"])
        log(f"milestone {t['key']}: next round")
        n += 1
    return n


# ---------------------------------------------------------------- tick (host, before process_approvals)

def tick(log=print) -> dict:
    from .pipeline import restage
    con = db()
    res = {"auto_merge": request_merges(con, log), "reopened": reopen(con, log), "reset": reset_done(con, log),
           "synced": 0, "opened": 0}
    restage(con)
    res["notified"] = notify_ready(con)
    con.commit()
    for t in con.execute("SELECT * FROM tickets WHERE gid LIKE 'ms:%' AND completed=0 "
                         "AND stage IN ('collecting', 'ms_hold')").fetchall():
        try:
            if t["ms_done_at"] and ready(con, t["parent_gid"]):
                res["opened"] += open_pr(con, t, log)
            else:
                res["synced"] += sync_base(con, t, log)
        except Exception as e:  # noqa: BLE001  (GitHub trouble: the next tick retries)
            con.rollback()
            event(con, "error", f"{t['key']}: milestone: {str(e)[:300]}", t["gid"])
        con.commit()
    restage(con)
    con.commit()
    return res


def request_merges(con, log=print) -> int:
    """Since 2026-10-30, subtask PRs merge into their milestone branch once the AI review approves (no button).
    Once per head: a failed merge (approve_error) waits for a new commit or the owner's own approval."""
    from .release import approval_target
    n = 0
    for t in con.execute("SELECT * FROM tickets WHERE in_milestone=1 AND stage='reviewed' "
                         "AND approve_requested_at IS NULL").fetchall():
        pr, err = approval_target(con, t)
        if err or not (pr["base"] or "").startswith(PREFIX) or t["auto_approved_sha"] == pr["head_sha"]:
            continue
        con.execute("UPDATE tickets SET approve_requested_at=?, approve_error=NULL, auto_approved_sha=? WHERE gid=?",
                    (now(), pr["head_sha"], t["gid"]))
        event(con, "approve", f"{t['key']}: AI approved, merging PR #{pr['number']} into {pr['base']} (milestone)",
              t["gid"])
        log(f"milestone {t['key']}: auto-merge PR #{pr['number']} into {pr['base']}")
        n += 1
    con.commit()
    return n


def sync_base(con, t, log=print) -> int:
    """Merge the feature base into the milestone branch while subtasks still work on it (GitHub merges API).
    Returns 1 when it merged. A conflict is logged once per base head and left for the milestone PR."""
    parent, repo = parse(t["gid"])
    gh, fb, b = _github(con, repo), staging_of(repo, notion=parent.startswith("notion:")), branch(parent)
    if not (gh and fb) or not _ahead(gh, b, fb):
        return 0
    p = _gh(["api", "-X", "POST", f"repos/{gh}/merges", "-f", f"base={b}", "-f", f"head={fb}",
             "-f", f"commit_message=Merge {fb} into {b} (ticket-pilot milestone sync)"], check=False)
    if p.returncode:
        head = _gh(["api", f"repos/{gh}/commits/{fb}", "--jq", ".sha"]).stdout.strip()[:7]
        msg = f"{t['key']}: {fb} {head} conflicts with {b}"
        if not con.execute("SELECT 1 FROM events WHERE kind='milestone' AND ticket_gid=? AND message LIKE ?",
                           (t["gid"], msg + "%")).fetchone():
            event(con, "milestone", msg + "; the milestone PR's conflict flow resolves it once the subtasks are done",
                  t["gid"])
        return 0
    event(con, "milestone", f"{t['key']}: merged {fb} into {b}", t["gid"])
    log(f"milestone {t['key']}: merged {fb} into {b}")
    return 1


def merged_subtasks(con, t) -> list:
    parent, repo = parse(t["gid"])
    return con.execute("SELECT s.key, s.name, p.number, p.url" + _MERGED_SUBTASKS + "ORDER BY p.merged_at",
                       {"parent": parent, "repo": repo, "base": branch(parent)}).fetchall()


def _brief(t, b: str, fb: str, subs: str) -> str:
    return (f"# {t['key']}: {t['parent_name']} (milestone)\n\n"
            f"Milestone {t['parent_name']} ({t['url'] or t['parent_gid']}) in "
            f"{t['repo_override']}. Each subtask below was reviewed on its own PR and merged into `{b}`; this PR ships "
            f"them to `{fb}` together. Review it as an integration: do the subtasks work together, and did merging "
            f"them (or `{fb}`) break anything.\n"
            f"The PR head is `{b}` (this worktree); commit fixes there with the subject prefix [{t['key']}].\n\n"
            f"## Subtasks\n{subs}\n")


def open_pr(con, t, log=print) -> int:
    """Every subtask is finished: check the milestone branch out in a worktree, open its PR into the feature base and
    give the milestone ticket a run (in review). A branch with nothing to ship completes the ticket."""
    from .pipeline import ROOT, _insert_run, latest_run, refresh_prs, run_pr, slug
    parent, repo = parse(t["gid"])
    r0 = latest_run(con, t["gid"])
    r0 = r0 if r0 and r0["state"] != "abandoned" else None
    p0 = run_pr(con, r0["id"]) if r0 else None
    if p0 and p0["state"] == "OPEN":
        return 0  # the milestone PR is already open (its head moves with each subtask merge)
    rp = con.execute("SELECT * FROM repos WHERE name=?", (repo,)).fetchone()
    fb, b = staging_of(repo, notion=parent.startswith("notion:")), branch(parent)
    if not (rp and rp["github"] and rp["path"] and fb):
        return 0
    gh = rp["github"]
    if not _ahead(gh, fb, b):
        con.execute("UPDATE tickets SET completed=1 WHERE gid=?", (t["gid"],))
        event(con, "milestone", f"{t['key']}: {b} has nothing {fb} lacks; nothing to ship", t["gid"])
        return 0
    name = slug(t["key"])
    subs = "\n".join(f"- [{s['key']}] {s['name']} (#{s['number']} {s['url']})" for s in merged_subtasks(con, t)) or "(none)"
    (ROOT / "data" / "briefs").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "briefs" / f"{name}.md").write_text(_brief(t, b, fb, subs))
    path = R.root() / repo / name
    if path.is_dir():  # next round, or a retry: the worktree of an earlier milestone PR is still there
        run(["git", "-C", str(path), "pull", "-q", "--ff-only"], timeout=180, check=False)
        wt = {"id": str(path), "path": str(path), "repo": repo, "displayName": name, "branch": b}
    else:
        wt = R.checkout(repo, rp["path"], name, b)
    if not json.loads(_gh(["pr", "list", "--repo", gh, "--head", b, "--base", fb, "--state", "open",
                           "--json", "url"]).stdout or "[]"):
        body = (f"Milestone {t['parent_name']} ({t['url'] or ''}) for {repo}, opened by ticket-pilot once every "
                f"subtask was merged into `{b}`.\n\n## Subtasks\n{subs}\n\nTickets: {t['key']}\n")
        _gh(["pr", "create", "--repo", gh, "--base", fb, "--head", b, "--title",
             f"[{t['key']}] {t['parent_name'] or t['key']}"[:200], "--body", body], timeout=180)
    if not (r0 and not p0):  # a run without a PR row (refresh failed last time) is reused
        _insert_run(con, t, {"repo": repo}, wt | {"workspaceStatus": "in-review",
                                                "comment": f"milestone PR {b} -> {fb}"}, None, "milestone")
    r = con.execute("SELECT r.*, rp.github FROM runs r LEFT JOIN repos rp ON rp.name=r.repo WHERE r.ticket_gid=? "
                    "ORDER BY r.id DESC LIMIT 1", (t["gid"],)).fetchone()
    refresh_prs(con, r, b)
    event(con, "milestone", f"{t['key']}: every subtask is in {b}; PR {b} -> {fb} opened for review", t["gid"])
    log(f"milestone {t['key']}: opened {b} -> {fb}")
    return 1


# ---------------------------------------------------------------- production

def all_repos_live(con, parent):
    return (not con.execute("SELECT 1 FROM tickets WHERE gid LIKE ? AND completed=0", (f"ms:{parent}:%",)).fetchone()
            and not unfinished(con, parent) and not (parent.startswith("notion:") and unshipped(con, parent)))


def complete(con, gid: str, *, pr_url: str | None = None) -> str | None:
    """The milestone PR is live in production: complete the milestone ticket, the subtasks merged into it, and the
    parent once every milestone ticket of it is complete. Returns errors or None."""
    from .release import complete_asana
    parent, repo = parse(gid)
    b = branch(parent)
    # only the subtasks this milestone PR carried; later ones ship with the next round
    latest = last_pr(con, gid)
    pr = latest
    if pr_url is not None and (not latest or latest["url"] != pr_url):
        pr = con.execute("SELECT * FROM prs WHERE url=? AND ticket_gid=?", (pr_url, gid)).fetchone()
        if not pr:
            return f"마일스톤 PR을 찾을 수 없어요 ({pr_url})"
    cut = (pr["merged_at"] if pr and pr["merged_at"] else None) or "9999"
    if not pr or (pr["url"] == latest["url"] and not merged_since(con, gid, pr["merged_at"])):
        con.execute("UPDATE tickets SET completed=1 WHERE gid=?", (gid,))
    errs = []
    shipped = con.execute("SELECT DISTINCT s.gid, s.key, s.completed" + _MERGED_SUBTASKS +
                          """AND (s.completed=0 OR (s.gid LIKE 'notion:%' AND p.prod_at IS NULL))
                             AND (p.merged_at IS NULL OR p.merged_at <= :cut)""",
                          {"parent": parent, "repo": repo, "base": b, "cut": cut}).fetchall()
    for s in shipped:
        con.execute("UPDATE prs SET prod_at=COALESCE(prod_at, ?), prod_note=COALESCE(prod_note, ?) "
                    "WHERE ticket_gid=? AND state='MERGED' AND repo=? AND base=? AND (merged_at IS NULL OR merged_at <= ?)",
                    (now(), f"마일스톤 {gid}로 배포", s["gid"], repo, b, cut))
        err = None if s["completed"] else complete_asana(con, s["gid"])
        if err:
            errs.append(f"{s['key']}: {err}")
    if all_repos_live(con, parent):
        if parent.startswith("notion:"):
            ticket = con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone()
            notion_stages.enqueue(con, ticket, notion_stages.MILESTONE_DONE)
        elif not con.execute("SELECT 1 FROM tickets WHERE gid=? AND completed=1", (parent,)).fetchone():
            err = complete_asana(con, parent)
            if err:
                errs.append(f"parent: {err}")
            else:
                event(con, "release", f"milestone {parent}: every repo is live; Asana parent completed", parent)
    return "; ".join(errs) or None


def commits(con, repo: str, prod: str, sha: str) -> set[str]:
    """Commits the milestone merge commit sha brought into staging that prod lacks (GitHub's PR commit list stops at
    250). ^sha^1 (staging before the merge) keeps out what the branch only got from syncing staging, so unapproved
    staging commits still stop the train."""
    rp = con.execute("SELECT path FROM repos WHERE name=?", (repo,)).fetchone()
    if not (rp and rp["path"] and os.path.isdir(rp["path"])):
        return set()
    run(["git", "-C", rp["path"], "fetch", "-q", "--no-tags", "origin", f"+refs/heads/{prod}:refs/remotes/origin/{prod}",
         sha], timeout=180)
    out = run(["git", "-C", rp["path"], "rev-list", sha, f"^{sha}^1", f"^refs/remotes/origin/{prod}"], timeout=60).stdout
    return {ln.strip() for ln in out.splitlines() if re.fullmatch(r"[0-9a-f]{40}", ln.strip())}
