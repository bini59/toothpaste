"""TP-20: a dep whose PR merged into the waiting ticket's feature base no longer blocks dispatch.
Run: python3 -m unittest discover tests"""
import json
import sqlite3
import unittest

from unittest import mock

from ticket_pilot import core
from ticket_pilot.pipeline import compute_stage

# deploy rules these cases depend on (TP-49: settings come from the DB, not config/settings.json)
RULES = {"20_Docs": {"modes": ["staging", "direct"], "staging": {"branch": "dev"}, "pr_into_staging": True,
                      "prod": {"branch": "main", "workflow": "deploy.yml"}},
         "30_Api": {"train": True, "modes": ["staging"], "staging": {"branch": "staging"}, "prod": {"branch": "main"}},
         "31_Web": {"train": True, "modes": ["staging"], "staging": {"branch": "staging"}, "prod": {"branch": "main"}},
         "32_Ticket_pilot": {"modes": ["staging"], "staging": {"branch": "main"}, "prod": {"branch": "main"}}}


class DepsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(core.SCHEMA)
        core._migrate(self.con)
        self.con.execute("INSERT INTO repos(name) VALUES ('20_Docs'), ('32_Ticket_pilot')")
        core.write_config(self.con, {"settings": {"deploy": {"repos": RULES}}, "project_map": {}})
        self.p = mock.patch.object(core, "_config_con", lambda: self.con)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        core.config_cache_clear()
        self.con.close()

    def ticket(self, gid, deps=(), repo="20_Docs", project=None):
        dj = [{"gid": d, "name": d, "completed": False} for d in deps]
        self.con.execute("""INSERT INTO tickets(gid,key,has_tag,assigned,deps_json,open_deps,repo_override,project_gid)
                            VALUES (?,?,1,1,?,?,?,?)""", (gid, gid, json.dumps(dj), len(dj), repo, project))

    def pr(self, gid, state, base="dev", repo="20_Docs"):
        rid = self.con.execute("INSERT INTO runs(ticket_gid,repo,state) VALUES (?,?,'finished')", (gid, repo)).lastrowid
        self.con.execute("INSERT INTO prs(url,run_id,ticket_gid,repo,number,state,base) VALUES (?,?,?,?,1,?,?)",
                         (f"u{gid}", rid, gid, repo, state, base))

    def stage(self, gid):
        return compute_stage(self.con, self.con.execute("SELECT * FROM tickets WHERE gid=?", (gid,)).fetchone())

    def test_dep_merged_into_feature_base(self):
        self.ticket("A"); self.ticket("B", ["A"]); self.pr("A", "MERGED")
        self.assertEqual(self.stage("A"), "staged")
        self.assertEqual(self.stage("B"), "queued")

    def test_dep_pr_open(self):
        self.ticket("A"); self.ticket("B", ["A"]); self.pr("A", "OPEN")
        self.assertEqual(self.stage("B"), "waiting_dep")

    def test_dep_pr_closed(self):
        self.ticket("A"); self.ticket("B", ["A"]); self.pr("A", "CLOSED")
        self.assertEqual(self.stage("B"), "waiting_dep")

    def test_dep_other_repo(self):
        self.ticket("A", repo="32_Ticket_pilot"); self.ticket("B", ["A"]); self.pr("A", "MERGED", repo="32_Ticket_pilot")
        self.assertEqual(self.stage("B"), "waiting_dep")

    def test_dep_merged_into_other_branch(self):
        self.ticket("A"); self.ticket("B", ["A"]); self.pr("A", "MERGED", base="main")
        self.assertEqual(self.stage("B"), "waiting_dep")

    def test_dep_unknown_to_ticket_pilot(self):
        self.ticket("B", ["X"])
        self.assertEqual(self.stage("B"), "waiting_dep")

    def test_no_feature_base_repo_waits_for_asana(self):
        self.ticket("A", repo="32_Ticket_pilot"); self.ticket("B", ["A"], repo="32_Ticket_pilot")
        self.pr("A", "MERGED", base="main", repo="32_Ticket_pilot")
        self.assertEqual(self.stage("B"), "waiting_dep")

    def test_blocked_agent_with_unreviewed_pr(self):
        self.ticket("A"); self.pr("A", "OPEN")
        self.con.execute("UPDATE runs SET state='active', ws_status='todo', ws_comment='BLOCKED: need a Api ticket'")
        self.assertEqual(self.stage("A"), "blocked")

    def test_multi_repo_project_dep_in_staging(self):
        # Main Site: dep merged into Api staging unblocks the project's other tickets, assigned or not
        self.con.execute("INSERT INTO repos(name) VALUES ('30_Api'), ('31_Web')")
        self.con.execute("INSERT INTO project_map(project_gid,repo,is_default) VALUES ('GW','30_Api',1), "
                         "('GW','31_Web',0)")
        self.ticket("A", repo="30_Api", project="GW")
        self.ticket("B", ["A"], repo=None, project="GW"); self.ticket("C", ["A"], repo="31_Web", project="GW")
        self.pr("A", "OPEN", base="staging", repo="30_Api")
        self.assertEqual(self.stage("B"), "waiting_dep")
        self.con.execute("UPDATE prs SET state='MERGED'")
        self.assertEqual(self.stage("B"), "needs_repo")
        self.assertEqual(self.stage("C"), "queued")


if __name__ == "__main__":
    unittest.main()
