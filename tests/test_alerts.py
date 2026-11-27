"""수신함 deploy / blocked alerts (TP-138). Run: python3 -m unittest discover tests"""
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from ticket_pilot import core
from ticket_pilot import notifications as N
from ticket_pilot import pipeline as P
from ticket_pilot import pr_tracking as PT
from ticket_pilot import release as R


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO tickets(gid,key,name,has_tag,assigned,repo_override) VALUES ('A','TP-1','t',1,1,'r')")
        self.con.execute("INSERT INTO runs(ticket_gid,repo,worktree_path,ws_status,state,terminal) "
                         "VALUES ('A','r','/tmp','in-progress','active','1 /tmp/x.log')")
        for x in (mock.patch.object(P, "db", lambda: self.con), mock.patch.object(PT, "db", lambda: self.con), mock.patch.object(N.CH, "_layout", return_value=[])):
            x.start()
            self.addCleanup(x.stop)

    def alerts(self, kind):
        return self.con.execute("SELECT * FROM notifications WHERE kind=? AND read_at IS NULL", (kind,)).fetchall()

    def release(self, mode=None):
        rid = self.con.execute("INSERT INTO releases(day,repo,status,mode,prs_json) VALUES ('d','r','deploying',?,?)",
                               (mode, json.dumps([{"key": "TP-1"}, {"key": "TP-2"}]))).lastrowid
        return self.con.execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()

    def test_deploy_done_once(self):
        rel = self.release()
        R._finish(self.con, rel, "done", None)
        R._finish(self.con, rel, "failed", "late error")  # a later step failing on a finished release: no second alert
        [a] = self.alerts("deploy")
        self.assertEqual((a["title"], a["href"]), ("r 프로덕션 배포 완료: TP-1, TP-2", "#release"))

    def test_deploy_failed_and_preview(self):
        R._finish(self.con, self.release(), "failed", "스모크 체크 실패: x 500")
        R._finish(self.con, self.release("preview"), "done", "label")
        R._finish(self.con, self.release(), "aborted", "no diff")  # not a deploy that ran
        self.assertEqual([a["title"] for a in self.alerts("deploy")],
                         ["r 프로덕션 배포 실패: 스모크 체크 실패: x 500", "r 프리뷰 배포 완료: TP-1, TP-2"])

    def test_ws_set_blocked_then_reply_reads_it(self):
        with mock.patch.object(PT, "run", return_value=SimpleNamespace(stdout="/tmp\n")), \
             mock.patch.object(PT, "refresh_prs"):
            P.ws_set("todo", "BLOCKED: which table?\nmore detail")
            P.ws_set("todo", "BLOCKED: still which table?")  # a newer question replaces the older alert
        [a] = self.alerts("blocked")
        self.assertEqual((a["title"], a["ticket_gid"], a["href"]), ("TP-1 답변 필요: still which table?", "A", "#board"))
        with mock.patch.object(N.CH, "_layout", side_effect=ValueError("bad settings")), \
             mock.patch.object(PT, "run", return_value=SimpleNamespace(stdout="/tmp\n")), mock.patch.object(PT, "refresh_prs"):
            P.ws_set("todo", "BLOCKED: third")  # the status is saved and the alert still made
        [a] = self.alerts("blocked")
        self.assertEqual(a["title"], "TP-1 답변 필요: third")
        self.con.execute("UPDATE tickets SET stage='blocked'")
        P.request_reply("A", "the users table")
        self.assertEqual(self.alerts("blocked"), [])

    def test_ws_set_in_review_reads_it(self):
        with mock.patch.object(PT, "run", return_value=SimpleNamespace(stdout="/tmp\n")), mock.patch.object(PT, "refresh_prs"):
            P.ws_set("todo", "BLOCKED: q")
            P.ws_set("in-review", "PR #1 ready")
        self.assertEqual(self.alerts("blocked"), [])

    def test_agent_exit_without_ready(self):
        with mock.patch.object(P.R, "is_handle", return_value=True), \
             mock.patch.object(P.R, "agent_state", return_value=(False, 1)), \
             mock.patch.object(P.R, "last_message", return_value="need a key"), \
             mock.patch.object(PT, "settings", return_value={}):
            PT.agent_exits(self.con)
        [a] = self.alerts("blocked")
        self.assertEqual(a["title"], "TP-1 답변 필요: need a key")


if __name__ == "__main__":
    unittest.main()
