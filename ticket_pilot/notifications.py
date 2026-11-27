"""수신함 alerts (TP-105): pipeline things the owner has to look at, next to the unread chat replies (chat.inbox).
  review    tp review save of an ordinary ticket (milestone subtasks and milestone PRs left out)
  ms_ready  every subtask of a milestone is in: the owner has to press 마일스톤 작업 완료 (milestone.notify_ready)
  deploy    a release row finished done/failed (release._finish: train + 배포 page), or a PR merged elsewhere is live (track)
  blocked   an agent stopped with BLOCKED (ws-set, rework cap, exit without ready): the owner has to answer (TP-138)"""
from __future__ import annotations

import json
from urllib.parse import quote

from . import chat as CH
from . import milestone as M
from .core import db, now


def add(con, kind: str, title: str, body: str, href: str, ticket_gid: str | None = None) -> None:
    con.execute("INSERT INTO notifications(ts,kind,ticket_gid,title,body,href) VALUES(?,?,?,?,?,?)",
                (now(), kind, ticket_gid, title, body, href))


def milestone_href(parent_gid: str) -> str:
    return f"#milestones/{quote(parent_gid, safe=':')}"


def read_ms_ready(con, parent_gid: str) -> None:
    """A milestone's 작업 완료 alert stops being true (pressed, or a subtask in progress again): no stale one stays unread."""
    con.execute("UPDATE notifications SET read_at=? WHERE kind='ms_ready' AND href=? AND read_at IS NULL",
                (now(), milestone_href(parent_gid)))


def deploy(con, rel, status: str, reason: str | None) -> None:
    """A release row just finished (TP-138); the 배포 page shows it. Previews are the only non-production deploys."""
    where = "프리뷰" if rel["mode"] == "preview" else "프로덕션"
    if status == "done":
        keys = [p.get("key") for p in json.loads(rel["prs_json"] or "[]") if p.get("key")]
        what = ", ".join(keys[:5]) + (f" 외 {len(keys) - 5}개" if len(keys) > 5 else "") if keys else "티켓 없음"
        add(con, "deploy", f"{rel['repo']} {where} 배포 완료: {what}", reason or "", "#release")
    else:
        add(con, "deploy", f"{rel['repo']} {where} 배포 실패: {(reason or '')[:80]}", reason or "", "#release")


def blocked(con, ticket_gid: str, question: str, repo: str | None) -> None:
    """An agent waits for the owner's answer: one unread alert per ticket (a newer question replaces the older one)."""
    t = con.execute("SELECT * FROM tickets WHERE gid=?", (ticket_gid,)).fetchone()
    if not t:
        return
    read_blocked(con, ticket_gid)
    q = question.removeprefix("BLOCKED:").strip()
    head = q.splitlines()[0][:80] if q else ""
    try:
        href = review_href(con, t, repo, CH._layout()) or "#board"
    except Exception:  # noqa: BLE001  (a bad project setting must not keep the BLOCKED status from being saved)
        href = "#board"
    add(con, "blocked", f"{t['key']} 답변 필요: {head}", q, href, ticket_gid)


def read_blocked(con, ticket_gid: str) -> None:
    """The owner answered (or dropped the ticket): the question is no longer waiting."""
    con.execute("UPDATE notifications SET read_at=? WHERE kind='blocked' AND ticket_gid=? AND read_at IS NULL",
                (now(), ticket_gid))


def unread_count() -> int:
    return db().execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]


def review_href(con, t, repo: str | None, layout: list[dict]) -> str | None:
    """Where a review alert opens (TP-119): the milestone page for milestone work, else the board that shows the ticket,
    in web lib.ts inProject's order: the tp project linked to its Asana project, else the first one (미분류 last) with
    the PR's repo. None = no board shows it (keep the PR)."""
    if M.is_ms(t["gid"]):
        return milestone_href(M.parse(t["gid"])[0])
    if M.in_milestone(t):
        return milestone_href(t["parent_gid"])
    own = t["project_gid"] and con.execute("SELECT project_id FROM project_map WHERE project_gid=? AND project_id IS NOT NULL",
                                           (t["project_gid"],)).fetchone()
    pid = own["project_id"] if own else next((p["id"] for p in layout if any(r["name"] == repo for r in p["repos"])), None)
    return pid and f"#projects/{quote(pid, safe='')}/board"


def unread() -> list[dict]:
    """Unread alerts, newest first; ticket_key / ticket_url from the ticket they are about. A review alert's href is
    worked out here (stored href = the PR, kept as pr_url) so alerts saved before TP-119 follow the same rule."""
    con = db()
    out, layout = [], None
    for r in con.execute(
            """SELECT n.id, n.ts, n.kind, n.ticket_gid, n.title, n.body, n.href, t.key AS ticket_key, t.url AS ticket_url
               FROM notifications n LEFT JOIN tickets t ON t.gid=n.ticket_gid WHERE n.read_at IS NULL ORDER BY n.id DESC"""):
        a = dict(r)
        if a["kind"] == "review":
            a["pr_url"] = a["href"]
            t = con.execute("SELECT * FROM tickets WHERE gid=?", (a["ticket_gid"],)).fetchone()
            pr = con.execute("SELECT repo FROM prs WHERE url=?", (a["href"],)).fetchone()
            if t:
                layout = CH._layout() if layout is None else layout
                a["href"] = review_href(con, t, pr and pr["repo"], layout) or a["href"]
        out.append(a)
    return out


def check_id(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise SystemExit("잘못된 알림 id예요")
    return v


def read(ident) -> None:
    con = db()
    con.execute("UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL", (now(), check_id(ident)))
    con.commit()


def read_all(upto) -> None:
    """수신함 모두 읽음: only the alerts it listed (ids up to the newest shown), not one that came in after."""
    con = db()
    con.execute("UPDATE notifications SET read_at=? WHERE id<=? AND read_at IS NULL", (now(), check_id(upto)))
    con.commit()
