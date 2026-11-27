"""Creation contracts through the public CLI, with local Asana and GitHub fixtures."""
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import asana_write, cli, core, milestone, notion, pipeline, release


def command(*args, status=0):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        try:
            cli.main(list(args))
            code = 0
        except SystemExit as e:
            code = e.code
    assert code == status, (code, out.getvalue(), err.getvalue())
    result, other = (err, out) if status else (out, err)
    assert not other.getvalue() and len(result.getvalue().splitlines()) == 1
    return json.loads(result.getvalue())


class TicketWriteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "tp.db"
        self.enterContext(mock.patch.object(core, "DB_PATH", self.db))
        self.enterContext(mock.patch.object(core, "_CON", None))
        self.enterContext(mock.patch.dict(os.environ, TP_AGENT="chat-app"))
        self.con = core.db()
        self.addCleanup(self.con.close)
        self.addCleanup(core.config_cache_clear)
        self.doc = {"settings": {"asana_workspace_gid": "1", "milestone_tag": "Ship", "asana_tag": "work",
                                 "deploy": {"repos": {"app": {"pr_into_staging": True, "modes": ["staging"],
                                                              "staging": {"branch": "dev"}, "prod": {"branch": "main"}}}}},
                    "project_map": {"repos": {"app": {"github": "example/app", "path": tmp.name},
                                              "other": {"github": "example/tools", "path": tmp.name}},
                                    "projects": []}}
        self.asana = self.enterContext(mock.patch.object(asana_write, "asana"))
        self.read_asana = self.enterContext(mock.patch.object(core, "asana"))
        self.gh = self.enterContext(mock.patch.object(core, "gh_api"))
        self.notion = self.enterContext(mock.patch.object(notion, "Client"))

    def project(self, source, **kwargs):
        self.doc["project_map"]["projects"] = [{"id": "proj", "name": "Project", "source": source,
                                               "repos": [{"repo": "other"}, {"repo": "app", "default": True}], **kwargs}]
        with self.con:
            core.write_config(self.con, self.doc)

    def create(self, kind="ticket", *opts, title="Parent", status=0):
        return command(kind, "create", "--project", "proj", "--title", title, *opts, status=status)

    def test_local_failures_are_json_before_any_external_call(self):
        self.assertEqual(self.create(status=4)["code"], "project_not_found")
        for source, cfg in (("none", {}), ("notion", {"notion": {"data_source_id": "task"}}),
                            ("github", {"repos": [{"repo": "other"}]})):
            self.project(source, **cfg)
            self.assertEqual(self.create("milestone", status=4)["code"], "milestone_unsupported")
            self.assertEqual(self.create("ticket", "--milestone", "123", status=4)["code"], "milestone_unsupported")
        self.project("none")
        self.assertEqual(self.create(status=4)["code"], "source_unsupported")
        rule = self.doc["settings"]["deploy"]["repos"]["app"]
        rule["modes"] = ["direct"]  # a feature base alone cannot merge an approved milestone PR
        for source, cfg in (("asana", {"asana_gid": "77"}), ("github", {})):
            self.project(source, **cfg)
            self.assertEqual(self.create("milestone", status=4)["code"], "milestone_unsupported")
            self.assertEqual(self.create("ticket", "--milestone", "123", status=4)["code"], "milestone_unsupported")
        rule["modes"] = ["staging"]
        self.project("github")
        for args in (("--parent", "1", "--milestone", "1"), ("--tag", " "), ("--assignee", "nobody"),
                     ("--unknown",), ("--parent", "")):
            self.assertEqual(self.create("ticket", *args, status=4)["code"], "invalid_argument")
        for source, cfg in (("asana", {"asana_gid": "77"}), ("notion", {}), ("github", {})):
            self.project(source, **cfg)
            with mock.patch.dict(os.environ, TP_AGENT="auto-review"):
                self.assertEqual(self.create(status=4)["code"], "write_disabled")
        self.doc["settings"]["agent_asana_write"] = False
        self.project("github")
        with mock.patch.dict(os.environ, TP_AGENT=""):
            self.assertEqual(self.create("milestone", status=4)["code"], "write_disabled")
        for api in (self.asana, self.read_asana, self.gh, self.notion):
            api.assert_not_called()

    def test_asana_creates_tasks_and_sync_reads_their_milestone(self):
        self.project("asana", asana_gid="77")
        tasks = {}
        tags = [{"gid": str(i), "name": name} for i, name in enumerate(("Ship", "work", "bug"), 1)]

        def api(method, path, params=None, body=None):
            if path.endswith("/typeahead"):
                return {"data": tags}
            if method == "GET":
                return {"data": tasks[path.split("/")[-1]]}
            self.assertEqual((method, path, body["projects"]), ("POST", "/tasks", ["77"]))
            gid = str(100 + len(tasks))
            tasks[gid] = {**body, "gid": gid, "permalink_url": "https://example.test/" + gid,
                          "projects": [{"gid": "77", "name": "Project"}], "parent": tasks.get(body.get("parent")),
                          "tags": [t for t in tags if t["gid"] in body.get("tags", [])],
                          "custom_fields": [{"name": "ID", "display_value": "TASK-" + gid}]}
            return {"data": tasks[gid]}

        self.asana.side_effect = self.read_asana.side_effect = api
        result = self.create("ticket", "--body", "Notes", "--tag", "bug", "--assignee", "me")
        self.assertEqual(result, {"ok": True, "source": "asana", "key": "TASK-100", "gid": "100",
                                  "url": "https://example.test/100"})
        self.assertEqual((tasks["100"]["notes"], tasks["100"]["tags"]), ("Notes", [tags[2]]))
        plain = self.create("ticket", "--parent", result["key"])
        self.assertEqual(tasks[plain["gid"]]["parent"]["gid"], "100")
        self.assertEqual(self.create("ticket", "--milestone", result["key"], status=4)["code"], "parent_not_found")
        result = self.create("milestone", "--subtask", "a", "--subtask", "b")
        parent = tasks[result["gid"]]
        self.assertEqual(parent["tags"], [tags[0]])
        self.assertEqual([s["key"] for s in result["subtasks"]], ["TASK-103", "TASK-104"])
        with mock.patch.object(pipeline, "asana_all", return_value=[t for t in tasks.values() if t.get("assignee") == "me"]):
            pipeline.sync(log=lambda *_: None)
        for child in result["subtasks"]:
            row = pipeline.find_ticket(self.con, child["key"])
            self.assertEqual((row["parent_gid"], row["parent_name"], row["in_milestone"], row["stage"]),
                             (parent["gid"], "Parent", 1, "needs_repo"))

    def test_asana_event_failure_reports_a_created_task(self):
        self.project("asana", asana_gid="77")
        self.asana.return_value = {"data": {"gid": "100"}}
        with self.con:
            self.con.execute("""CREATE TRIGGER reject_event BEFORE INSERT ON events
                                WHEN NEW.kind='asana_write' BEGIN SELECT RAISE(FAIL,'fixture'); END""")
        result = self.create(status=5)
        self.assertEqual(result["code"], "upstream_error")
        self.assertEqual(result["created"], {"key": "AS-100", "gid": "100", "url": "https://app.asana.com/0/77/100"})
        self.asana.assert_called_once()

    def test_asana_ticket_key_failure_reports_a_created_subtask(self):
        self.project("asana", asana_gid="77")
        parent = {"gid": "100", "tags": [{"name": "Ship"}]}
        child = {"gid": "101", "permalink_url": "url/101",
                 "custom_fields": [{"name": "ID", "display_value": "TASK-101"}]}
        self.read_asana.return_value = {"data": parent}
        self.asana.side_effect = [{"data": [{"gid": "1", "name": "Ship"}]}, {"data": parent},
                                  {"data": [{"gid": "2", "name": "work"}]}, {"data": child}]
        with self.con:
            self.con.execute("""CREATE TRIGGER reject_ticket BEFORE INSERT ON tickets
                                WHEN NEW.gid='101' BEGIN SELECT RAISE(FAIL,'fixture'); END""")
        result = self.create("milestone", "--subtask", "created", status=5)
        self.assertEqual(result["code"], "upstream_error")
        self.assertEqual(result["subtasks"], [{"key": "TASK-101", "url": "url/101"}])
        self.assertEqual(result["failed"], [])
        self.assertEqual([(e["title"], e["created"]) for e in result["warnings"]],
                         [("created", {"key": "TASK-101", "gid": "101", "url": "url/101"})])
        self.assertEqual(self.asana.call_args.args[-1]["parent"], parent["gid"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM events WHERE ticket_gid='101'").fetchone()[0], 2)

    def test_github_subissues_sync_dispatch_base_and_verified_closure(self):
        self.project("github")
        issues = {}
        def api(method, path, body=None):
            if path == "user":
                return {"login": "owner"}
            if method == "GET":
                return issues[int(path.rsplit("/", 1)[1])]
            self.assertEqual(path, "repos/example/app/issues")
            n = len(issues) + 12
            issues[n] = {**body, "number": n, "id": n + 1000, "html_url": f"https://github.com/example/app/issues/{n}",
                         "labels": [{"name": name} for name in body["labels"]],
                         "assignees": [{"login": login} for login in body.get("assignees", [])]}
            return issues[n]
        self.gh.side_effect = api
        ms = self.create("milestone", "--body", "Release", "--assignee", "me", "--subtask", "a", "--subtask", "b")
        gid = ms["gid"]
        self.assertEqual((ms["source"], ms["key"], gid), ("github", "APP-12", "gh:example/app#12"))
        self.assertEqual(issues[12]["labels"], [{"name": "Ship"}])
        self.assertEqual((issues[12]["body"], issues[12]["assignees"]), ("Release", [{"login": "owner"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM tickets").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM events WHERE kind='ticket_write'").fetchone()[0], 3)
        for n in (13, 14):
            self.assertEqual((issues[n]["labels"], issues[n]["parent_issue_id"]), ([{"name": "tp"}], issues[12]["id"]))
        extra = self.create("ticket", "--parent", ms["key"], "--tag", "bug", "--body", "Body")
        self.assertEqual(extra["key"], "APP-15")
        self.assertEqual([l["name"] for l in issues[15]["labels"]], ["tp", "bug"])
        self.assertEqual(issues[15]["body"], "Body")
        self.assertEqual(self.create("ticket", "--milestone", extra["key"], status=4)["code"], "parent_not_found")
        issues[12]["labels"].append({"name": "tp"})  # even a dispatch label cannot queue the parent
        def listing(repo):
            return [{**i, "url": i["html_url"], "parent": {**issues[12], "url": issues[12]["html_url"],
                     "repository": {"nameWithOwner": "Example/App"}} if i.get("parent_issue_id") else None}
                    for i in issues.values()] if repo == "example/app" else []
        with mock.patch.object(pipeline, "_open_issues", side_effect=listing), mock.patch.object(pipeline, "asana_all", return_value=[]):
            self.assertEqual(pipeline.sync(log=lambda *_: None)["new_tagged"], ["APP-13", "APP-14", "APP-15"])
        self.assertEqual(pipeline.find_ticket(self.con, gid)["stage"], "open")
        child = pipeline.find_ticket(self.con, "APP-13")
        self.assertEqual((child["parent_gid"], child["parent_name"], child["in_milestone"]), (gid, "Parent", 1))
        with mock.patch.object(milestone, "_gh"):
            branch = milestone.ensure(self.con, child, "app")
        self.assertEqual(branch, "milestone/gh-example-app-12")
        msg = []
        with mock.patch.object(pipeline, "_gh_issue_json", return_value={"comments": []}):
            pipeline.dispatch(dry_run=True, only="APP-13", log=msg.append)
        self.assertIn("--base " + branch, msg[-1])
        self.assertIn("ticket create --project proj", msg[-1])
        synthetic = milestone.ms_gid(gid, "app")
        self.assertEqual(synthetic, "ms:gh:example/app#12:app")
        for n in (13, 14, 15):
            child_gid = f"gh:example/app#{n}"
            self.con.execute("INSERT INTO prs(url,ticket_gid,repo,base,state) VALUES(?,?,'app',?,'MERGED')",
                             (f"pr-{n}", child_gid, branch))
            self.con.execute("UPDATE tickets SET stage='milestone' WHERE gid=?", (child_gid,))
        with mock.patch.object(release, "_gh") as close:
            self.assertIsNone(release.complete_asana(self.con, synthetic))
        self.assertCountEqual([c.args[0][:4] for c in close.call_args_list],
                              [["issue", "close", str(n), "--repo"] for n in (12, 13, 14, 15)])

    def test_partial_failure_reports_every_child_and_keeps_parent(self):
        self.project("github")
        issue = {"id": 99, "number": 12, "html_url": "url/12", "labels": [{"name": "Ship"}]}
        child = {"number": 14, "html_url": "url/14", "labels": [{"name": "tp"}]}
        unlabeled = {"number": 15, "html_url": "url/15", "labels": []}
        self.gh.side_effect = [issue, issue, RuntimeError("HTTP 503"), issue, child, issue, unlabeled]
        result = self.create("milestone", "--subtask", "unavailable", "--subtask", "ok", "--subtask", "unlabeled",
                             "--subtask", "", status=5)
        self.assertEqual((result["ok"], result["code"], result["gid"]), (False, "upstream_error", "gh:example/app#12"))
        self.assertEqual(result["subtasks"], [{"key": "APP-14", "url": "url/14"}, {"key": "APP-15", "url": "url/15"}])
        self.assertEqual([(e["title"], e["code"]) for e in result["failed"]],
                         [("unavailable", "upstream_error"), ("", "invalid_argument")])
        self.assertEqual([(e["title"], e["created"]) for e in result["warnings"]],
                         [("unlabeled", {"key": "APP-15", "gid": "gh:example/app#15", "url": "url/15"})])

    def test_cross_repo_key_before_sync(self):
        self.doc["settings"]["github_issues"] = {"other": "tools"}
        self.project("github")
        self.gh.side_effect = [{"id": 77, "labels": [{"name": "Ship"}]},
                               {"number": 13, "html_url": "u13", "labels": [{"name": "tp"}]}]
        self.assertEqual(self.create("ticket", "--milestone", "tools-12")["key"], "APP-13")
        self.assertEqual(self.gh.call_args_list[0], mock.call("GET", "repos/example/tools/issues/12"))
        self.assertEqual(self.gh.call_args.args[2]["parent_issue_id"], 77)

    def test_github_event_failure_reports_the_created_issue(self):
        self.project("github")
        self.gh.return_value = {"number": 12, "html_url": "url/12", "labels": [{"name": "tp"}]}
        with mock.patch.object(core, "event", side_effect=sqlite3.OperationalError("disk full")):
            result = self.create(status=5)
        self.assertEqual(result["code"], "upstream_error")
        self.assertEqual(result["created"], {"key": "APP-12", "gid": "gh:example/app#12", "url": "url/12"})

    def test_bin_tp_asana_fixture_and_unsupported_project(self):
        self.project("asana", asana_gid="77")
        fixture = self.db.with_suffix(".json")
        fixture.write_text(json.dumps({"/tasks": {"data": {"gid": "91", "permalink_url": "https://example.test/91"}}}))
        env = {**os.environ, "TP_DB": str(self.db), "TP_ASANA_FIXTURE": str(fixture)}
        args = [core.ROOT / "bin/tp", "ticket", "create", "--project", "proj", "--title", "Login"]
        p = subprocess.run(args, capture_output=True, text=True, env=env, check=True)
        self.assertEqual(json.loads(p.stdout), {"ok": True, "source": "asana", "key": "AS-91", "gid": "91",
                                               "url": "https://example.test/91"})
        self.assertEqual(p.stderr, "")
        self.project("none")
        p = subprocess.run([*args, "--milestone", "91"], capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stdout, json.loads(p.stderr)["code"]), (4, "", "milestone_unsupported"))
