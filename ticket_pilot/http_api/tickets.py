"""Dashboard ticket, review, milestone and deploy HTTP handlers."""
import json
import threading
from contextlib import closing

from .. import checks as K
from .. import deploy as D
from .. import devserver as DS
from .. import milestone as MS
from .. import notion_tickets
from .. import pipeline as P
from .. import release as R
from .. import runner
from .. import update as UP
from ..core import ROOT, db, meta_get, meta_set, project_map, repo_rules, settings

_sync_lock = threading.Lock()


def state() -> dict:
    con = db()
    try:
        return _state(con)
    finally:
        con.close()


def _state(con) -> dict:
    rows = lambda q, *a: [dict(r) for r in con.execute(q, a)]  # noqa: E731
    rules = repo_rules()
    staged_pr_urls = R.staged_pr_urls(con, list(rules))
    staged_urls = {url for urls in staged_pr_urls.values() for url in urls}
    # The board uses the head's AI verdict, while the deploy screen uses the last verdict from any source.
    # The office uses the newest summary by timestamp and counts every review sent back for rework.
    prs = rows("""WITH ranked AS (
            SELECT r.id, r.pr_url, r.source, r.rework_at, r.head_sha IS p.head_sha AS matches_head,
                   ROW_NUMBER() OVER (PARTITION BY r.pr_url ORDER BY r.created_at DESC, r.id DESC) AS summary_rank
            FROM reviews r JOIN prs p ON p.url = r.pr_url
        ), review_ids AS (
            SELECT pr_url,
                   MAX(CASE WHEN matches_head THEN id END) AS head_id,
                   MAX(CASE WHEN matches_head AND NULLIF(source, '') IS NULL THEN id END) AS ai_id,
                   MAX(CASE WHEN summary_rank = 1 THEN id END) AS summary_id,
                   COUNT(NULLIF(rework_at, '')) AS rework_count
            FROM ranked GROUP BY pr_url
        )
        SELECT p.*, head.verdict AS head_verdict, ai.verdict AS head_ai_verdict,
               latest.summary AS latest_review_summary, COALESCE(v.rework_count, 0) AS rework_count
        FROM prs p LEFT JOIN review_ids v ON v.pr_url = p.url
        LEFT JOIN reviews head ON head.id = v.head_id
        LEFT JOIN reviews ai ON ai.id = v.ai_id
        LEFT JOIN reviews latest ON latest.id = v.summary_id""")
    staged_gids = {p["ticket_gid"] for p in prs if p["url"] in staged_urls}
    tickets = rows(f"""SELECT gid,key,name,url,project_gid,project_name,section,parent_gid,parent_name,parent_url,milestone_error,due_on,tags,has_tag,
                      deps_json,open_deps,completed,assigned,stage,repo_override,skip,first_seen,last_seen,
                      approve_requested_at,approve_error,reply,check_override,check_override_sha,in_milestone,ms_done_at,
                      review_override,review_override_sha,done_at,asana_modified_at
                      FROM tickets WHERE (completed=0 AND assigned=1) OR gid IN (SELECT ticket_gid FROM runs)
                      OR gid IN ({','.join('?' for _ in staged_gids)})""", *staged_gids)
    landed = MS.landed_parents(con)
    for t in tickets:
        if t["stage"] == "needs_staging" and (repo := P.chosen_repo(con, t)):
            t["staging_config_error"] = MS.staging_config_error(t, repo["repo"])
        elif t["stage"] == "blocked":
            if t["milestone_error"]:
                t["milestone_kind_blocked"] = notion_tickets.uses_milestone_kind(con, t)
            else:
                t["staging_config_error"] = P.resume_staging_error(con, t)
        if (ms := landed.get(t["gid"])) and (t["parent_gid"] != ms["parent_gid"] or not t["in_milestone"]):
            t.update(parent_gid=ms["parent_gid"], parent_name=ms["parent_name"], parent_url=ms["url"], in_milestone=1)
        if t["open_deps"]:  # deps merged into the feature base no longer block (TP-20)
            deps = P.deps_status(con, t)
            t["deps_json"] = json.dumps(deps, ensure_ascii=False)
            t["open_deps"] = sum(1 for d in deps if not d["completed"] and not d["landed"])
    tickets_by_gid = {t["gid"]: t for t in tickets}
    for p in prs:
        # tp checks of the open head (checks.py): state none|pending|pass|fail|error, rows, and the approval gate text
        if p["state"] == "OPEN":
            cs = K.check_state(con, p)
            p["checks"] = {"state": cs["state"], "rows": cs["rows"], "gate": K.gate_error(con, p)}
            if t := tickets_by_gid.get(p["ticket_gid"]):
                errors = D.milestone_deploy_errors(t, p["repo"], p["base"])
                p["staging_deploy_error"] = errors.get("staging")
                p["direct_deploy_error"] = errors.get("direct")
        elif p["url"] in staged_urls and MS.awaiting_next_round(con, p["ticket_gid"]):
            p["awaiting_milestone_round"] = True
    return {
        "meta": {"last_sync": meta_get(con, "last_sync"), "last_scan": meta_get(con, "last_scan"),
                 "settings": settings(),
                 "release_hold": bool(meta_get(con, "release_hold", False)),
                 "next_release_at": R.next_release_at().isoformat(),
                 "rules": rules, "modes": D.MODES, "last_diff_scan": meta_get(con, "last_diff_scan"),
                 "version": version(con)},
        "staging_diffs": rows("SELECT * FROM staging_diffs ORDER BY repo"),
        "tickets": tickets,
        "runs": rows("SELECT id,ticket_gid,repo,worktree_name,worktree_path,branch,terminal,ws_status,ws_comment,state,origin,created_at,updated_at FROM runs"),
        "prs": prs,
        "staged_pr_urls": staged_pr_urls,
        "repos": [{**r, **factory_repo(r["name"])} for r in rows("SELECT * FROM repos")],
        "project_map": rows("SELECT * FROM project_map"),
        "releases": rows("SELECT * FROM releases ORDER BY id DESC LIMIT 100"),
        "dev_servers": DS.status(con),
    }


def version(con) -> dict:
    """Sidebar version + 업데이트 item: `update` is the latest release tag tp tick saw (every 6 h) when not in HEAD."""
    tag = (meta_get(con, "update_check") or {}).get("latest")
    return {"current": UP.current(), "update": tag if tag and UP.newer(ROOT, tag) else None}


def factory_repo(name: str) -> dict:
    """The repo settings the 작업실 factory draws (TP-127), read-only: agent tool/model, check names (no commands),
    CI, the deploy rule's train/modes/merge and the rework cap."""
    a = runner.resolve(((project_map().get("repos") or {}).get(name) or {}).get("agent"))
    rc = K.repo_checks(name) or {"ci": False, "checks": []}
    rule = repo_rules(name)
    return {"agent": {"tool": a["tool"], "model": a["model"]}, "checks": [c["name"] for c in rc["checks"]],
            "ci": rc["ci"], "deploy": rule and {k: rule[k] for k in ("train", "modes", "merge")},
            "max_rework": P.max_rework_rounds(name)}


def get_state(req):
    return req._send(200, state())


def get_reviews(req):
    with closing(db()) as con:
        return req._send(200, {"reviews": [dict(r) for r in con.execute("SELECT * FROM reviews ORDER BY id DESC")]})


def get_check_log(req):
    con = db()
    try:
        got = K.read_log(con, int((req.query().get("id") or ["0"])[0]))
    except ValueError:
        got = None
    finally:
        con.close()
    if not got:
        return req._send(404, {"error": "not found"})
    row, body = got
    return req._send(200, {"check": row, "log": body})


def post_assign(req, body):
    P.assign(body["ticket"], body.get("repo") or None)
    return req._send(200, {"ok": True})


def post_skip(req, body):
    P.set_skip(body["ticket"], bool(body.get("skip", True)))
    return req._send(200, {"ok": True})


def post_reply(req, body):
    # only records the answer; the host prechecks (every 5 min) relaunch the agent with it
    P.request_reply(body["ticket"], body.get("text") or "")
    return req._send(200, {"ok": True})


def post_drop(req, body):
    P.drop(body["ticket"])
    return req._send(200, {"ok": True})


def post_milestone_done(req, body):
    # only records it; the host tick opens the milestone PRs (milestone.py)
    MS.mark_done(body["parent"], cancel=bool(body.get("cancel")))
    return req._send(200, {"ok": True})


def post_approve(req, body):
    # only records the request; the host prechecks (every 5 min) do the actual staging merge
    # override: the owner's reason to merge although tp checks did not pass (stored + logged as check_override)
    # override_review: their reason to merge although the AI review said blocked/changes (TP-99, that head only)
    R.request_approval(body["ticket"], cancel=bool(body.get("cancel")), override=body.get("override") or None,
                       override_review=body.get("override_review") or None)
    return req._send(200, {"ok": True})


def post_check_override(req, body):
    # The owner ignores the tp checks of the open head with a reason: it goes to the AI review, not back to the agent
    R.override_checks(body["ticket"], body.get("reason") or "")
    return req._send(200, {"ok": True})


def post_deploy(req, body):
    # only records the request; the host prechecks (every 5 min) run it
    D.request_deploy(body["mode"], body.get("ticket") or None, body.get("repo") or None, pr_url=body.get("pr_url"))
    return req._send(200, {"ok": True})


def post_deploy_cancel(req, body):
    D.cancel_deploy(int(body["id"]))
    return req._send(200, {"ok": True})


def post_release_hold(req, body):
    con = db()
    meta_set(con, "release_hold", bool(body.get("hold")))
    con.commit()
    return req._send(200, {"ok": True})


def post_refresh(req, _body):
    if not _sync_lock.acquire(blocking=False):
        return req._send(409, {"error": "already running"})
    try:
        P.sync(log=lambda *_: None)
        P.scan(log=lambda *_: None)
    finally:
        _sync_lock.release()
    return req._send(200, {"ok": True})
