"""tp: toothpaste (치약) CLI. tp = ToothPaste."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import traceback
from pathlib import Path

from . import asana_write as AW
from . import cleanup as C
from . import chat as CH
from . import config as G
from . import checks as K
from . import deploy as D
from . import diffs as F
from . import guide as GD
from . import migrate as M
from . import mode as MO
from . import milestone as MS
from . import metrics as MET
from . import metrics_usage as MU
from . import automation, notion, notion_write, precheck, tick, ticket_write
from . import pipeline as P
from . import release as R
from . import review
from . import setup as S
from . import runner
from . import track as T
from . import update as U
from . import worktree_dev
from .core import DOCKER_OFF, db, docker, event, load_map


def _log_errors(fn):
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                con = db()
                con.rollback()
                event(con, "error", f"{getattr(fn, '__name__', 'cmd')}: {e}")
                con.commit()
            except Exception:  # noqa: BLE001
                pass
            sys.exit(2)
    return wrap


@_log_errors
def cmd_precheck_dispatch(a):
    pending = precheck.dispatch()
    print(f"triage needed: {pending}")
    sys.exit(0 if pending else 1)


@_log_errors
def cmd_precheck_review(a):
    count = precheck.review()
    print(f"review queue: {count}")
    sys.exit(0 if count else 1)


@_log_errors
def cmd_precheck_diff(a):
    sys.exit(0 if precheck.diff() else 1)


@_log_errors
def cmd_precheck_cleanup(a):
    count = precheck.cleanup_worktrees()
    print(f"cleanup queue: {count}")
    sys.exit(0 if count else 1)


def _not_docker(fn):
    """tp setup / migrate / update install on the host (launchd, systemd, the checkout): off in Docker mode."""
    def wrap(a):
        if docker():
            sys.exit(f"{DOCKER_OFF}. 업데이트는 호스트에서: git pull && (cd docker && docker compose up -d --build)")
        from . import mode
        if mode.current() == "docker":  # switched with tp mode docker: launchd would start a second tick on the 444 DB
            sys.exit("Docker로 전환돼 있어요 (tp mode docker). 업데이트: git pull && (cd docker && docker compose up -d --build); "
                     "호스트로 되돌리기: bin/tp mode host --apply")
        return fn(a)
    return wrap


def cmd_attach(a):
    """How to continue a ticket's agent session by hand (runner handle -> codex resume / claude --resume)."""
    con = db()
    t = P.find_ticket(con, a.ticket)
    r = P.latest_run(con, t["gid"])
    if not r:
        raise SystemExit(f"{t['key']} has no run")
    h = r["terminal"]
    if not runner.is_handle(h):
        raise SystemExit(f"{t['key']}: started by Orca ({h or 'no agent'}); open its worktree in Orca: {r['worktree_path']}")
    alive, code = runner.agent_state(h)
    a = runner.settings_of(h)
    how = " ".join(str(x) for x in (a.get("agent"), a.get("model"), a.get("effort"), "ultracode" if a.get("ultracode") else None) if x)
    print(f"{t['key']} {r['repo']}/{r['worktree_name']}: {how or 'agent'} {'running' if alive else f'exited ({code})'}")
    print(f"log: {h.split(' ', 1)[1]}")
    cmd = runner.resume_cmd(h)
    print(f"cd {r['worktree_path']} && {cmd}" if cmd else "no session id in the log yet")
    if alive:
        print("(stop it first: tp drop stops and skips the ticket; or kill the agent's process group "
              f"{h.split(' ', 1)[0]})")


def cmd_list(a):
    con = db()
    q = "SELECT key,stage,due_on,project_name,name FROM tickets WHERE completed=0 AND assigned=1"
    if a.stage:
        q += f" AND stage IN ({','.join('?' * len(a.stage))})"
    rows = con.execute(q + " ORDER BY due_on IS NULL, due_on", a.stage or ()).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
        return
    for r in rows:
        print(f"{r['key']:<22} {r['stage']:<12} {r['due_on'] or '-':<10} {(r['project_name'] or '-')[:26]:<26} {r['name']}")
    print(f"({len(rows)})")


def cmd_show(a):
    con = db()
    t = P.find_ticket(con, a.ticket)
    runs = [dict(r) | {"raw_json": None} for r in con.execute("SELECT * FROM runs WHERE ticket_gid=?", (t["gid"],))]
    prs = [dict(r) for r in con.execute("SELECT * FROM prs WHERE ticket_gid=?", (t["gid"],))]
    revs = [dict(r) for r in con.execute("SELECT * FROM reviews WHERE ticket_gid=?", (t["gid"],))]
    print(json.dumps({"ticket": dict(t), "runs": runs, "prs": prs, "reviews": revs}, ensure_ascii=False, indent=1))


def _print_changes(ch):
    for c in ch:
        print(f"{c['scope']}  {c['key']}: {json.dumps(c['old'], ensure_ascii=False)} -> {json.dumps(c['new'], ensure_ascii=False)}")
    print(f"({len(ch)}개 바뀜)" if ch else "바뀐 것 없음")


def cmd_config(a):
    """tp config get|set|export|import|history (settings live in data/tp.db since TP-49)."""
    try:
        if a.op == "get":
            print(json.dumps(G.get(a.path or ""), ensure_ascii=False, indent=2))
        elif a.op == "set":
            try:
                val = json.loads(a.value)
            except json.JSONDecodeError:
                raise SystemExit(f"값은 JSON이어야 해요 (문자열은 따옴표까지: '\"staging\"'): {a.value}")
            _print_changes(G.set_path(a.path, val, actor="cli", dry_run=a.dry_run))
        elif a.op == "export":
            doc = G.export(a.file)
            if not a.file:
                print(json.dumps(doc, ensure_ascii=False, indent=2))
        elif a.op == "import":
            _print_changes(G.import_file(a.file))
        elif a.op == "history":
            for h in reversed(G.history(a.scope, limit=a.limit)):
                print(f"{h['ts']} {h['actor']:<9} {h['scope']:<20} {h['key']}: "
                      f"{json.dumps(h['old'], ensure_ascii=False)} -> {json.dumps(h['new'], ensure_ascii=False)}")
    except G.ConfigError as e:
        print("저장하지 않았어요:", file=sys.stderr)
        for x in e.errors:
            print(f"  - {x}", file=sys.stderr)
        sys.exit(1)


@_log_errors
def cmd_asana(a):
    """tp asana comment|create|update|tag (asana_write.py); a failure is an error event named cmd_asana."""
    a.call(a)


def cmd_notion_app(a):
    """Provision the installation's OAuth app without passing the secret in argv."""
    secret = getpass.getpass("Notion 클라이언트 시크릿: ") if sys.stdin.isatty() else sys.stdin.readline()
    try:
        notion.save_app(a.client_id, secret.rstrip("\r\n"))
    except notion.NotionError as e:
        raise SystemExit(str(e)) from None
    print("Notion OAuth 앱을 저장했어요. 전역 설정 → Notion에서 로그인하세요.")


def cmd_dev(a):
    con = db()
    if a.op == "start":
        result = worktree_dev.start(con, a.worktree, a.port)
    elif a.op == "status":
        result = worktree_dev.status(con, a.worktree)
    else:
        worktree_dev.stop(con, a.worktree)
        result = {"status": "stopped"}
    print(json.dumps(result, ensure_ascii=False))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = ticket_write.CreateParser if argv and argv[0] in ("ticket", "milestone") else argparse.ArgumentParser
    ap = parser(prog="tp", description="toothpaste (치약), tp = ToothPaste. 사용법: tp guide [주제]")
    sp = ap.add_subparsers(dest="cmd", required=True)
    gd = sp.add_parser("guide", help="사용 설명서 docs/guide.md: 주제 목록, 또는 한 주제 (milestone, deploy, ...)")
    gd.add_argument("topic", nargs="?")
    gd.set_defaults(fn=lambda a: sys.exit(GD.main(a.topic)))
    mk = sp.add_parser("mark", help="지표에 비교 시점을 기록해요")
    mk.add_argument("label")
    mk.set_defaults(fn=lambda a: print(json.dumps(MET.create_marker(db(), a.label), ensure_ascii=False)))
    na = sp.add_parser("notion-app", help="서버 비밀 저장소에 Notion OAuth 앱 등록 (시크릿은 숨김 입력 또는 표준 입력)")
    na.add_argument("--client-id", required=True, help="Notion OAuth 클라이언트 ID")
    na.set_defaults(fn=cmd_notion_app)
    notion_write.register(sp)
    ticket_write.register(sp)
    sp.add_parser("sync").set_defaults(fn=_log_errors(lambda a: P.sync()))
    d = sp.add_parser("dispatch")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--only")
    d.set_defaults(fn=_log_errors(lambda a: P.dispatch(dry_run=a.dry_run, only=a.only)))
    sp.add_parser("scan").set_defaults(fn=_log_errors(lambda a: P.scan()))
    sp.add_parser("track").set_defaults(fn=_log_errors(lambda a: T.track_deploys()))
    sp.add_parser("precheck-dispatch").set_defaults(fn=cmd_precheck_dispatch)
    sp.add_parser("precheck-review").set_defaults(fn=cmd_precheck_review)
    sp.add_parser("precheck-diff").set_defaults(fn=cmd_precheck_diff)
    sp.add_parser("precheck-cleanup").set_defaults(fn=cmd_precheck_cleanup)
    tk = sp.add_parser("tick", help="run the automations that are due (launchd, every minute)")
    tk.add_argument("--only", nargs="+", choices=[x[0] for x in automation.AUTOMATIONS], help="run just these, due or not")
    tk.add_argument("--force", action="store_true", help="run every automation now")
    tk.set_defaults(fn=lambda a: tick.run_tick(only=a.only or (), force=a.force))
    ws = sp.add_parser("ws-set", help="agent completion signal from inside its worktree")
    ws.add_argument("--status", required=True, choices=["in-review", "todo"])
    ws.add_argument("--comment", required=True, help='"PR #<n> ready: ..." or "BLOCKED: <question for the owner>"')
    ws.add_argument("--path", default=".")
    ws.set_defaults(fn=lambda a: P.ws_set(a.status, a.comment, a.path))
    at = sp.add_parser("attach", help="print how to resume a ticket's agent session by hand")
    at.add_argument("ticket")
    at.set_defaults(fn=cmd_attach)
    chat = sp.add_parser("chat", help="프로젝트 채팅")
    attach = chat.add_subparsers(dest="op", required=True).add_parser("attach", help="현재 답변에 파일 첨부")
    attach.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    attach.set_defaults(fn=lambda a: print(f"답변에 파일 {CH.attach(a.paths)}개를 첨부했어요"))
    cl = sp.add_parser("cleanup", help="remove ticket worktrees whose change is live in production")
    cl.add_argument("--dry-run", action="store_true", help="check only, remove nothing")
    cl.set_defaults(fn=_log_errors(lambda a: C.cleanup_scan(dry_run=a.dry_run)))
    cq = sp.add_parser("cleanup-queue")
    cq.add_argument("--json", action="store_true")
    cq.set_defaults(fn=lambda a: print(json.dumps(C.cleanup_queue(), ensure_ascii=False, indent=1)))
    cv = sp.add_parser("cleanup-verdict", help="agent verdict: is this worktree's PR in production (for that deployed sha)?")
    cv.add_argument("--path", required=True)
    cv.add_argument("--prod-sha", required=True)
    cv.add_argument("--verdict", required=True, choices=["live", "not_live"])
    cv.add_argument("--note", required=True)
    cv.set_defaults(fn=lambda a: C.save_verdict(a.path, a.prod_sha, a.verdict, a.note))
    sp.add_parser("diff-scan", help="compare production vs staging per repo (read-only)").set_defaults(fn=_log_errors(lambda a: F.diff_scan()))
    dq = sp.add_parser("diff-queue")
    dq.add_argument("--json", action="store_true")
    dq.set_defaults(fn=lambda a: print(json.dumps(F.diff_queue(), ensure_ascii=False, indent=1)))
    dn = sp.add_parser("diff-note", help="save the AI summary for a repo's staging head")
    dn.add_argument("--repo", required=True)
    dn.add_argument("--head", required=True)
    dn.add_argument("--level", required=True, choices=["ok", "check", "hold"])
    dn.add_argument("--file", required=True)
    dn.set_defaults(fn=lambda a: F.save_note(a.repo, a.head, a.level, a.file))
    cf = sp.add_parser("config", help="settings in data/tp.db: get | set | export | import | history")
    cs = cf.add_subparsers(dest="op", required=True)
    x = cs.add_parser("get", help="print a path (global.max_active_runs, repo.<name>.deploy, ...) or everything")
    x.add_argument("path", nargs="?")
    x = cs.add_parser("set", help="set a path to a JSON value; validated, recorded in the history, backed up")
    x.add_argument("path")
    x.add_argument("value", help="""JSON, e.g. 4, true, '"staging"', '[{"name":"lint","cmd":"pnpm lint"}]', null""")
    x.add_argument("--dry-run", action="store_true", help="validate and show the changes only")
    x = cs.add_parser("export", help="settings as {settings, project_map} JSON (stdout or file)")
    x.add_argument("file", nargs="?")
    x = cs.add_parser("import", help="replace the settings with an export or a data/backups file")
    x.add_argument("file")
    x = cs.add_parser("history", help="change history, optionally of one scope (global, a repo name, project:<id>)")
    x.add_argument("scope", nargs="?")
    x.add_argument("--limit", type=int, default=100)
    cf.set_defaults(fn=cmd_config)
    mg = sp.add_parser("migrate", help="move this install (checkout, data, worktrees) under one folder; preview without --apply")
    mg.add_argument("--to", required=True, help="e.g. ~/tp-work: <to>/app, <to>/worktrees, <to>/repos")
    mg.add_argument("--apply", action="store_true", help="really move (default: only print the plan)")
    mg.set_defaults(fn=_not_docker(lambda a: M.migrate(a.to, a.apply)))
    md = sp.add_parser("mode", help="Docker environment check, or switch host <-> Docker (TP-165); preview without --apply")
    md.add_argument("what", nargs="?", default="check", choices=("check", "docker", "host"))
    md.add_argument("--apply", action="store_true", help="really switch (default: only print the plan)")
    md.add_argument("--admins", help="host -> Docker: TP_ADMINS, GitHub logins (default: the connected GitHub account)")
    md.set_defaults(fn=lambda a: MO.main(a.what, a.apply, a.admins))
    sp.add_parser("map-load").set_defaults(fn=lambda a: (lambda c: (load_map(c), P.restage(c), c.commit()))(db()))

    t = sp.add_parser("triage-list")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=lambda a: print(json.dumps(P.triage_list(), ensure_ascii=False, indent=1)))
    s = sp.add_parser("assign")
    s.add_argument("ticket")
    s.add_argument("repo", nargs="?")
    s.set_defaults(fn=lambda a: P.assign(a.ticket, a.repo))
    for name, val in (("skip", True), ("unskip", False)):
        x = sp.add_parser(name)
        x.add_argument("ticket")
        x.set_defaults(fn=lambda a, v=val: P.set_skip(a.ticket, v))
    dr = sp.add_parser("drop", help="stop a ticket's agent, abandon its run and skip the ticket (same as the dashboard 그만두기)")
    dr.add_argument("ticket")
    dr.set_defaults(fn=lambda a: P.drop(a.ticket))
    ad = sp.add_parser("adopt", help="link an existing worktree of a known repo to a ticket")
    ad.add_argument("ticket")
    ad.add_argument("worktree", help="path, display name or id")
    ad.set_defaults(fn=lambda a: P.adopt(a.ticket, a.worktree))

    rq = sp.add_parser("review-queue")
    rq.add_argument("--json", action="store_true")
    rq.set_defaults(fn=lambda a: print(json.dumps(P.review_queue(), ensure_ascii=False, indent=1)))
    ck = sp.add_parser("check", help="tp's own repo checks on PR heads (settings repo.<name>.checks)")
    ck.add_argument("--pr", help="run one PR's head now, in the foreground (default: start what is due, detached)")
    ck.add_argument("--sha", help="with --pr: only if this is still the head")
    ck.add_argument("--repo", help="with --ref: run the repo's checks on a ref of its host checkout, no DB writes")
    ck.add_argument("--ref", help="e.g. origin/staging")
    ck.add_argument("--record", type=int, help=argparse.SUPPRESS)  # 설정 page trial run id (config.start_check_run)
    ck.set_defaults(fn=_log_errors(lambda a: G.record_check_run(a.record, a.repo, a.ref) if a.record else
                                   K.baseline(a.repo, a.ref) if a.repo else
                                   K.run_pr(a.pr, a.sha) if a.pr else K.tick()))
    rst = sp.add_parser("review-start", help="자동 리뷰의 PR별 지표 집계를 시작해요")
    rst.add_argument("--pr", required=True)
    rst.set_defaults(fn=lambda a: MU.start_review(db(), a.pr))
    rs = sp.add_parser("review-save")
    rs.add_argument("--pr", required=True)
    rs.add_argument("--sha", required=True)
    rs.add_argument("--verdict", required=True, choices=["approve", "changes", "blocked"])
    rs.add_argument("--summary", required=True)
    rs.add_argument("--file", required=True)
    rs.add_argument("--shot", action="append", default=[], help="PNG to embed in the PR comment (repeatable)")
    rs.add_argument("--no-post", action="store_true")
    rs.set_defaults(fn=lambda a: P.review_save(a.pr, a.sha, a.verdict, a.file, a.summary, post=not a.no_post,
                                               shots=a.shot, manual=not os.environ.get("TP_AGENT")))
    dev = sp.add_parser("dev", help="start, inspect or stop a ticket worktree's dev server")
    dev.set_defaults(fn=cmd_dev)
    ops = dev.add_subparsers(dest="op", required=True)
    for op in ("start", "status", "stop"):
        sub = ops.add_parser(op)
        sub.add_argument("--worktree", type=Path, required=True, help="ticket worktree path")
        if op == "start":
            sub.add_argument("--port", type=int, help="preferred port (default: detect from the repo)")
    sc = sp.add_parser("screenshot", help="capture a URL into a PNG (the repo's Playwright; Orca's browser as fallback)")
    sc.add_argument("--url", required=True)
    sc.add_argument("--out", required=True)
    sc.add_argument("--wait-ms", type=int, default=3000)
    sc.add_argument("--device", help='Playwright device, e.g. "iPhone 12" (default: desktop)')
    sc.add_argument("--repo", default=".", help="worktree; uses its Playwright if installed, else Orca's browser")
    sc.add_argument("--js", help="JS to run before capture, e.g. scroll")
    sc.set_defaults(fn=lambda a: review.screenshot(a.url, a.out, a.wait_ms, a.device, a.js, a.repo))

    ap_ = sp.add_parser("approve", help="request a staging merge for an AI-approved #03 PR (same as the dashboard button)")
    ap_.add_argument("ticket")
    ap_.add_argument("--cancel", action="store_true")
    ap_.add_argument("--override", metavar="REASON", help="approve although tp checks of the head did not pass")
    ap_.add_argument("--override-review", metavar="REASON",
                     help="approve although the AI review of the head said blocked/changes (that head only)")
    ap_.set_defaults(fn=lambda a: R.request_approval(a.ticket, cancel=a.cancel, override=a.override,
                                                      override_review=a.override_review))
    md = sp.add_parser("milestone-done", help="마일스톤 작업 완료 (same as the 마일스톤 page button): open its milestone PRs")
    md.add_argument("parent", help="Milestone parent gid (Asana gid, notion:<page id>, gh:<owner/repo>#<number>)")
    md.add_argument("--cancel", action="store_true")
    md.set_defaults(fn=lambda a: MS.mark_done(a.parent, cancel=a.cancel))
    co = sp.add_parser("check-override", help="ignore the tp checks of a ticket's open PR head (sends it to the AI review)")
    co.add_argument("ticket")
    co.add_argument("reason")
    co.set_defaults(fn=lambda a: R.override_checks(a.ticket, a.reason))
    sp.add_parser("approvals").set_defaults(fn=_log_errors(lambda a: R.process_approvals()))
    sp.add_parser("milestones", help="milestone auto-merges, branch sync, milestone PRs (milestone.py)").set_defaults(
        fn=_log_errors(lambda a: print(MS.tick())))
    dp = sp.add_parser("deploy", help="request a deploy (same as the 배포 page): staging | direct | promote | all | preview")
    dp.add_argument("mode", choices=list(D.MODES))
    dp.add_argument("--ticket")
    dp.add_argument("--repo")
    dp.add_argument("--pr", metavar="URL", help="PR to promote (defaults to the latest run's PR)")
    dp.set_defaults(fn=lambda a: print(D.request_deploy(a.mode, a.ticket, a.repo, pr_url=a.pr) or "approval requested"))
    dc = sp.add_parser("deploy-cancel")
    dc.add_argument("id", type=int)
    dc.set_defaults(fn=lambda a: D.cancel_deploy(a.id))
    sp.add_parser("deploys", help="advance dashboard deploys one step").set_defaults(fn=_log_errors(lambda a: D.deploy_tick()))
    rt = sp.add_parser("release-tick", help="advance today's release train one step")
    rt.add_argument("--force", action="store_true", help="start even if it is not a release weekday/time or on hold")
    rt.set_defaults(fn=_log_errors(lambda a: print(R.release_tick(force=a.force))))
    sp.add_parser("release-status").set_defaults(fn=lambda a: print(json.dumps({
        "next_release_at": R.next_release_at().isoformat(),
        "staged": R.staged_pr_urls(db()),
        "today": [dict(r) for r in db().execute("SELECT * FROM releases WHERE day=? ORDER BY id", (R._today(),))],
    }, ensure_ascii=False, indent=1)))

    aw = sp.add_parser("asana", help="Asana writes for chat / ticket agents (tp holds the token): comment | create | update | tag")
    asp = aw.add_subparsers(dest="op", required=True)
    x = asp.add_parser("comment", help="add a comment to a task")
    x.add_argument("ticket", help="ticket key or Asana task gid")
    x.add_argument("text")
    x.set_defaults(fn=cmd_asana, call=lambda a: AW.comment(a.ticket, a.text))
    x = asp.add_parser("create", help="create a task in a project")
    x.add_argument("--project", required=True, help="project id (설정) or Asana project gid")
    x.add_argument("--title", required=True)
    x.add_argument("--notes")
    x.add_argument("--assignee", help="me or a user gid")
    x.add_argument("--tag", action="append", default=[], help="tag name (repeatable; created when missing)")
    x.add_argument("--parent", help="ticket key or Asana task gid: create it as that task's subtask")
    x.set_defaults(fn=cmd_asana, call=lambda a: AW.create(a.project, a.title, a.notes, a.assignee, a.tag, a.parent))
    x = asp.add_parser("update", help="change a task")
    x.add_argument("ticket", help="ticket key or Asana task gid")
    x.add_argument("--completed", choices=["true", "false"])
    x.add_argument("--assignee", help="me, a user gid, or none")
    x.add_argument("--title")
    x.add_argument("--notes")
    x.set_defaults(fn=cmd_asana, call=lambda a: AW.update(a.ticket, a.completed, a.assignee, a.title, a.notes))
    x = asp.add_parser("tag", help="add a tag by name (created in the workspace when missing)")
    x.add_argument("ticket", help="ticket key or Asana task gid")
    x.add_argument("name")
    x.set_defaults(fn=cmd_asana, call=lambda a: AW.tag(a.ticket, a.name))

    ls = sp.add_parser("list")
    ls.add_argument("--stage", nargs="*")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    sh = sp.add_parser("show")
    sh.add_argument("ticket")
    sh.set_defaults(fn=cmd_show)
    up = sp.add_parser("update", help="move this checkout to the latest release tag, rebuild what changed, restart (update.py)")
    up.add_argument("--to", help="tag or sha (default: the latest v* release tag)")
    up.add_argument("--build", action="store_true", help="build web/ with npm instead of downloading the release's web-dist.tar.gz")
    up.set_defaults(fn=_not_docker(lambda a: sys.exit(U.main(a.to, a.build))))
    sp.add_parser("setup", help="install on this Mac (prechecks, web/dist, port, launchd) and open 시작하기; safe to rerun").set_defaults(
        fn=_not_docker(lambda a: sys.exit(S.setup())))
    rl = sp.add_parser("release-tag", help=argparse.SUPPRESS)  # release.yml (and deploy.yml): tag + GitHub Release
    rl.add_argument("--sha", required=True)
    rl.add_argument("--repo", required=True)
    rl.add_argument("--version", help="vX.Y.Z instead of the PR label bump (workflow_dispatch)")
    rl.add_argument("--print-version", action="store_true", help=argparse.SUPPRESS)
    rl.set_defaults(fn=lambda a: print(U.release_plan(a.sha, a.repo, a.version)[0]) if a.print_version
                    else U.release(a.sha, a.repo, version=a.version))
    sv = sp.add_parser("serve")
    sv.add_argument("--port", type=int)
    sv.set_defaults(fn=lambda a: __import__("ticket_pilot.server", fromlist=["serve"]).serve(a.port))
    sp.add_parser("run", help="dashboard + a tick every minute in one process (the Docker image's command)").set_defaults(
        fn=lambda a: tick.run())

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
