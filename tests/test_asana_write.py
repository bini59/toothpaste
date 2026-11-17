"""tp asana (TP-97): agent Asana writes go through tp, are logged without text, and stop with agent_asana_write off.
Run: python3 -m unittest discover tests"""
import os
import sqlite3
import unittest
from unittest import mock

from ticket_pilot import asana_write as AW, core


class AsanaWriteTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO tickets(gid,key) VALUES ('111','PROJ-1'), ('gh:o/r#5','R-5')")
        self.con.execute("INSERT INTO config_projects(id,pos,name,source,asana_gid,extra) VALUES ('web',0,'Web','asana','999','{}')")
        self.st = {"asana_tag": "orca", "agent_asana_write": True, "asana_workspace_gid": "WS"}
        self.calls = []

        def fake(method, path, params=None, body=None):
            self.calls.append((method, path, body))
            if path.endswith("/typeahead"):
                return {"data": [{"gid": "T1", "name": "Orca"}]}
            if path in ("/tasks", "/tags"):
                return {"data": {"gid": "NEW", "permalink_url": "https://app.asana.com/x"}}
            return {"data": {}}
        for p in (mock.patch.object(AW, "db", lambda: self.con), mock.patch.object(AW, "asana", fake),
                  mock.patch.object(AW, "settings", lambda: dict(self.st)),
                  mock.patch.dict(os.environ, {"TP_AGENT": "chat-web-1"})):
            p.start()
            self.addCleanup(p.stop)

    def events(self):
        return [r["message"] for r in self.con.execute("SELECT message FROM events WHERE kind='asana_write' ORDER BY id")]

    def test_comment_by_key_logs_without_text(self):
        AW.comment("proj-1", "secret plan details")
        self.assertEqual(self.calls, [("POST", "/tasks/111/stories", {"text": "secret plan details"})])
        self.assertEqual(self.events(), ["agent chat-web-1: comment on PROJ-1 (111) (19 chars)"])

    def test_create_with_dispatch_tag(self):
        AW.create("web", "New task", notes="body text", assignee="me", tags=["orca"])
        self.assertEqual(self.calls[0], ("POST", "/tasks", {"name": "New task", "projects": ["999"], "notes": "body text",
                                                            "assignee": "me"}))
        self.assertEqual(self.calls[-1], ("POST", "/tasks/NEW/addTag", {"tag": "T1"}))
        ev = self.events()
        self.assertNotIn("body text", " ".join(ev))
        self.assertIn("(auto-dispatch tag)", ev[-1])

    def test_create_subtask_of_ticket(self):
        AW.create("web", "Sub", parent="PROJ-1")
        self.assertEqual(self.calls, [("POST", "/tasks", {"name": "Sub", "projects": ["999"], "parent": "111"})])
        self.assertIn("under 111", self.events()[0])

    def test_tag_created_when_missing(self):
        AW.tag("111", "new-tag")
        self.assertIn(("POST", "/tags", {"name": "new-tag", "workspace": "WS"}), self.calls)
        self.assertEqual(self.calls[-1], ("POST", "/tasks/111/addTag", {"tag": "NEW"}))

    def test_update_and_unassign(self):
        AW.update("PROJ-1", completed="false", assignee="none")
        self.assertEqual(self.calls, [("PUT", "/tasks/111", {"completed": False, "assignee": None})])

    def test_complete_refused_while_run_active(self):
        self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES ('111','r','active')")
        with self.assertRaises(SystemExit):
            AW.update("PROJ-1", completed="true")
        self.assertEqual(self.calls, [])

    def test_merged_not_released_keeps_completion_and_assignee(self):
        self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES ('111','r','finished')")
        for kw in ({"completed": "true"}, {"assignee": "none"}):
            with self.assertRaises(SystemExit):
                AW.update("PROJ-1", **kw)
        AW.update("PROJ-1", title="Renamed")
        self.assertEqual(self.calls, [("PUT", "/tasks/111", {"name": "Renamed"})])

    def test_off_refuses_agents_not_cli(self):
        self.st["agent_asana_write"] = False
        with self.assertRaises(SystemExit):
            AW.comment("PROJ-1", "x")
        self.assertEqual(self.calls, [])
        self.assertIn("read only", AW.rules())
        with mock.patch.dict(os.environ, {"TP_AGENT": ""}):
            AW.comment("PROJ-1", "x")
        self.assertEqual(self.events(), ["cli: comment on PROJ-1 (111) (1 chars)"])

    def test_automation_and_issue_refused(self):
        with mock.patch.dict(os.environ, {"TP_AGENT": "auto-review"}), self.assertRaises(SystemExit):
            AW.comment("PROJ-1", "x")
        with self.assertRaises(SystemExit):
            AW.comment("R-5", "x")
        self.assertEqual(self.calls, [])

    def test_rules_on(self):
        self.assertIn("asana comment", AW.rules())
        self.assertIn("`orca`", AW.rules())


if __name__ == "__main__":
    unittest.main()
