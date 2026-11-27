"""TP-49: settings live in SQLite. Defaults in code, a one-time import of the old JSON files, the same dict shapes
for settings()/repo_rules()/project_map()/repo_checks(), validation, history + backups, export/import round trip.
Run: python3 -m unittest discover tests"""
import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import checks as K, config as G, core, pipeline as P
from ticket_pilot.storage import migrations


def mem_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(core.SCHEMA)
    core._migrate(con)
    return con


def use_db(con):
    """Point every settings read/write at con; returns the started patches."""
    core.config_cache_clear()
    ps = [mock.patch.object(core, "_config_con", lambda: con), mock.patch.object(G, "db", lambda: con)]
    for p in ps:
        p.start()
    return ps


def legacy(tmp: Path):
    """A small settings.json + project_map.json in the pre-TP-49 shape."""
    (tmp / "shop").mkdir()
    (tmp / "site").mkdir()
    st = {"asana_workspace_gid": "1", "asana_tag": "orca", "asana_keychain_service": "ticket-pilot-asana",
          "max_active_runs": 2, "agent": "codex", "git_user": "alice", "dashboard_port": 3032,
          "review": {"max_rework_rounds": 4, "_comment": "c"}, "check": {"timeout_min": 7, "max_parallel": 1},
          "release": {"weekdays": [0, 2], "at": "10:00", "window_min": 120, "timezone": "Asia/Seoul",
                      "staging_branch": "staging", "main_branch": "main", "retarget_since": ""},
          "deploy": {"_comment": "rules", "deploy_timeout_min": 45, "repos": {
              "site": {"train": True, "merge": "merge", "modes": ["staging", "direct", "promote", "all"],
                       "staging": {"branch": "staging", "pipeline": "site-staging"},
                       "prod": {"branch": "main", "pipeline": "site-prod", "smoke": ["https://x.test/"]}},
              "shop": {"merge": "squash", "modes": ["direct"], "version": "npm-patch", "via": "main",
                       "prod": {"branch": "production", "workflow": "deploy.yml"}}}},
          "github_issues": {"shop": "ZS"}}
    pm = {"_comment": "map",
          "repos": {"shop": {"orca_repo_id": "z1", "path": str(tmp / "shop"), "github": "o/shop", "description": "Z",
                             "checks": [{"name": "typecheck", "cmd": "pnpm tsc"}, {"name": "s", "dir": "studio", "cmd": "t"}]},
                    "site": {"orca_repo_id": "n1", "path": str(tmp / "site"), "github": "o/site", "description": "N", "ci": True}},
          "projects": [{"asana_gid": "11", "name": "Web", "confidence": "high",
                        "repos": [{"repo": "site", "role": "fe", "default": True}, {"repo": "shop", "role": "be"}]}],
          "_hidden_comment": "hidden", "hidden_projects": {"99": "Old"}}
    (tmp / "settings.json").write_text(json.dumps(st))
    (tmp / "project_map.json").write_text(json.dumps(pm))
    return st, pm


def old_repo_rules(st):
    """core.repo_rules() before TP-49 (read from settings.json), to compare shapes."""
    out = {}
    for n, r in st.get("deploy", {}).get("repos", {}).items():
        r = dict(r)
        r.setdefault("staging", None)
        r.setdefault("via", None)
        r.setdefault("merge", "merge")
        r.setdefault("modes", ["direct"])
        r.setdefault("train", False)
        r["target"] = r["via"] or r["prod"]["branch"]
        out[n] = r
    return out


# settings added after the pre-TP-49 files (local runner, milestones): an imported file without them reads the code defaults
RUNNER_KEYS = ("asana_enabled", "worktree_root", "clone_root", "branch_prefix", "agent_max_min", "milestone_tag", "agent_asana_write",
               "owner_name", "lan_share", "theme", "agents", "update")


def tp91(pm):
    """The legacy project_map with the ids/sources TP-91 gives its projects."""
    return {**pm, "projects": [{"id": "web", "source": "asana", **p} for p in pm["projects"]]}


def with_runner_defaults(st):
    return {**{k: core.DEFAULT_SETTINGS[k] for k in RUNNER_KEYS}, **st}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.con = mem_db()
        self.tmp = Path(tempfile.mkdtemp())
        self.p = use_db(self.con) + [mock.patch.object(G, "backup_dir", lambda: self.tmp / "backups")]
        for p in self.p[2:]:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        core.config_cache_clear()
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def import_json(self):
        return migrations.import_legacy_json(self.con, self.tmp, core.write_config, G.backup)

    def imported(self):
        st, pm = legacy(self.tmp)
        self.assertTrue(self.import_json())
        return st, pm

    # TP-121: the default worktree / clone roots moved under ~/tp-work; an install from before keeps what it used
    def test_new_db_uses_tp_work_roots(self):
        self.assertFalse(migrations.pin_legacy_roots(self.con, core.config_cache_clear))
        self.assertEqual(G.get("global.worktree_root"), "~/tp-work/worktrees")
        self.assertEqual(G.get("global.clone_root"), "~/tp-work/repos")
        # still a new install after its first settings are saved: the mark stops a later pin
        self.con.execute("INSERT INTO config_global(ns,k,v,pos) VALUES('settings','agent','\"claude\"',0)")
        self.assertFalse(migrations.pin_legacy_roots(self.con, core.config_cache_clear))
        core.config_cache_clear()
        self.assertEqual(G.get("global.worktree_root"), "~/tp-work/worktrees")

    # TP-184: new installs use the tp / tp-milestone tags; an install from before keeps orca / orca-milestone
    def test_default_tags(self):
        self.assertFalse(migrations.pin_legacy_tags(self.con, core.config_cache_clear))  # new DB
        self.assertEqual((G.get("global.asana_tag"), G.get("global.milestone_tag")), ("tp", "tp-milestone"))

    def test_existing_db_keeps_its_tags(self):
        self.con.execute("INSERT INTO config_global(ns,k,v,pos) VALUES('settings','asana_tag','\"mine\"',0)")
        self.assertTrue(migrations.pin_legacy_tags(self.con, core.config_cache_clear))  # stored tag stays; the unstored milestone tag keeps the old default
        self.assertEqual((G.get("global.asana_tag"), G.get("global.milestone_tag")), ("mine", "orca-milestone"))
        self.assertFalse(migrations.pin_legacy_tags(self.con, core.config_cache_clear))

    def test_existing_db_keeps_stored_roots(self):
        for i, (k, v) in enumerate({"worktree_root": "~/ticket-pilot/worktrees", "clone_root": "~/somewhere"}.items()):
            self.con.execute("INSERT INTO config_global(ns,k,v,pos) VALUES('settings',?,?,?)", (k, json.dumps(v), i))
        self.assertFalse(migrations.pin_legacy_roots(self.con, core.config_cache_clear))
        self.assertEqual(G.get("global.worktree_root"), "~/ticket-pilot/worktrees")
        self.assertEqual(G.get("global.clone_root"), "~/somewhere")

    def test_existing_db_without_stored_roots_keeps_old_defaults(self):
        self.imported()  # the legacy files have neither key: this install has been using the old code defaults
        self.assertTrue(migrations.pin_legacy_roots(self.con, core.config_cache_clear))
        self.assertEqual(G.get("global.worktree_root"), "~/ticket-pilot/worktrees")
        self.assertEqual(G.get("global.clone_root"), "~/Desktop/07_Project")
        self.assertFalse(migrations.pin_legacy_roots(self.con, core.config_cache_clear))

    def history(self):
        return [dict(r) for r in self.con.execute("SELECT scope,key,old,new,actor FROM settings_history ORDER BY id")]

    # ------------------------------------------------------------ defaults and import
    def test_defaults_without_db_values_or_files(self):
        self.assertFalse(self.import_json())
        st = core.settings()
        self.assertEqual(st["max_active_runs"], 3)
        self.assertEqual(st["review"]["max_rework_rounds"], 3)
        self.assertEqual((st["check"]["timeout_min"], st["check"]["max_parallel"]), (10, 2))
        self.assertEqual(st["dashboard_port"], 3032)
        self.assertEqual(core.repo_rules(), {})
        self.assertEqual(core.project_map(), {"repos": {}, "projects": [], "hidden_projects": {}})
        self.assertIsNone(K.repo_checks("shop"))
        self.assertEqual(K.conf(), {"timeout_min": 10.0, "max_parallel": 2})
        self.assertEqual(P.max_rework_rounds(), 3)

    def test_import_once_then_json_is_not_read(self):
        st, pm = self.imported()
        self.assertEqual(G.export(), {"settings": with_runner_defaults(st), "project_map": tp91(pm)})
        self.assertEqual(self.con.execute("SELECT count(*) c FROM events WHERE kind='config'").fetchone()["c"], 1)
        self.assertEqual(len(list((self.tmp / "backups").glob("config-*.json"))), 1)
        (self.tmp / "settings.json").write_text(json.dumps({**st, "max_active_runs": 9}))
        self.assertFalse(self.import_json())
        core.config_cache_clear()
        self.assertEqual(core.settings()["max_active_runs"], 2)

    def test_missing_files_are_imported_once_they_appear(self):
        self.assertFalse(self.import_json())  # e.g. the deploy restored them a moment later
        self.imported()
        self.assertEqual(core.settings()["max_active_runs"], 2)

    def test_same_shapes_as_the_json_files(self):
        st, pm = self.imported()
        self.assertEqual(core.settings(), with_runner_defaults(st))
        self.assertEqual(core.repo_rules(), old_repo_rules(st))
        self.assertEqual(list(core.repo_rules()), ["site", "shop"])  # deploy order = train order
        self.assertEqual(core.repo_rules("shop")["target"], "main")
        self.assertEqual(core.train_repos(), ["site"])
        self.assertEqual(core.feature_base("site"), "staging")
        self.assertEqual(core.project_map(), tp91(pm))
        self.assertEqual(K.repo_checks("shop"), {"ci": False, "checks": pm["repos"]["shop"]["checks"]})
        self.assertEqual(K.repo_checks("site"), {"ci": True, "checks": []})
        self.assertIsNone(K.repo_checks("other"))
        self.assertEqual(K.conf(), {"timeout_min": 7.0, "max_parallel": 1})
        core.load_map(self.con)
        self.assertEqual({r["name"] for r in self.con.execute("SELECT name FROM repos")}, {"shop", "site"})

    def test_partial_settings_fill_in_code_defaults(self):
        core.write_config(self.con, {"settings": {"max_active_runs": 5, "check": {"timeout_min": 3}}, "project_map": {}})
        st = core.settings()
        self.assertEqual((st["max_active_runs"], st["check"]["timeout_min"], st["check"]["max_parallel"]), (5, 3, 2))
        self.assertEqual(st["review"]["max_rework_rounds"], 3)

    # ------------------------------------------------------------ validation
    def test_invalid_values_are_not_saved(self):
        self.imported()
        before = G.export()
        bad = [("global.max_active_runs", 0, "1~10"), ("global.max_active_runs", True, "1~10"),
               ("global.agent", "gpt", "agent"), ("global.release.at", "25:00", "HH:MM"),
               ("global.release.timezone", "Mars/Base", "시간대"), ("global.github_issues.ghost", "GH", "레포 목록"),
               ("repo.shop.deploy.modes", ["direct", "yolo"], "yolo"), ("repo.shop.deploy.modes", [], "modes"),
               ("repo.site.deploy.staging.branch", "", "staging.branch"), ("repo.shop.deploy.prod.workflow", " ", "workflow"),
               ("repo.shop.deploy.merge", "rebase", "merge"),
               ("repo.shop.checks", [{"name": "x"}], "cmd"), ("repo.shop.checks", [{"cmd": "x"}], "name"),
               ("repo.shop.checks", [{"name": "a", "cmd": "b", "dir": "../up"}], "dir"),
               ("repo.shop.checks", [{"name": "a", "cmd": "b", "env": "x"}], "모르는 키"),
               ("repo.shop.ci", True, "같이 쓸 수 없어요"), ("repo.shop.path", str(self.tmp / "gone"), "폴더가 없어요"),
               ("repo.shop.review.max_rework_rounds", 50, "0~20"),
               ("repo.site.depends_on", "shop", "목록"), ("repo.site.depends_on", ["ghost"], "레포 목록"),
               ("repo.site.depends_on", ["site"], "자기 자신"), ("repo.site.depends_on", ["shop", "shop"], "두 번"),
               ("global.update.check_min", 9, "10~10080"), ("global.update.auto", "yes", "true 또는 false"),
               ("repo.shop.deploy.prod.release", False, "true만"),
               ("repo.shop.deploy.staging", None, None)]  # shop has no staging: removing nothing is no change
        for path, val, why in bad:
            with self.subTest(path=path, val=val):
                if why is None:
                    self.assertEqual(G.set_path(path, val), [])
                    continue
                with self.assertRaises(G.ConfigError) as cm:
                    G.set_path(path, val)
                self.assertTrue(any(why in e for e in cm.exception.errors), cm.exception.errors)
        self.assertEqual(G.export(), before)
        self.assertEqual(self.history(), [])
        # a repo whose own checks are switched off may still read CI
        G.set_path("repo.shop.checks", [{"name": "typecheck", "cmd": "pnpm tsc", "off": True, "reason": "base fails"}])
        G.set_path("repo.shop.ci", True)
        self.assertEqual(K.repo_checks("shop"), {"ci": True, "checks": []})
        G.set_path("global.update.check_min", 10)  # TP-186: the shortest interval
        self.assertEqual(core.settings()["update"], {"auto": False, "check_min": 10})

    def test_depends_on_reaches_diff_queue(self):
        from ticket_pilot import diffs as F
        self.imported()
        G.set_path("repo.site.depends_on", ["shop"])
        self.assertEqual(G.get("repo.site")["depends_on"], ["shop"])
        con = core._config_con()
        con.execute("""INSERT INTO staging_diffs(repo, github, prod_branch, staging_branch, staging_head, ahead, verdict)
                       VALUES ('site','o/site','main','staging','abc',2,'ready'), ('shop','o/shop','production','main','d',1,'ready')""")
        q = {d["repo"]: d["depends_on"] for d in F.diff_queue(con)}
        self.assertEqual(q, {"site": [{"repo": "shop", "github": "o/shop", "prod_branch": "production"}], "shop": []})
        self.assertEqual(G.get("global.github_issues"), {"shop": "ZS"})
        G.set_path("global.github_issues", {})
        G.set_path("repo.shop", None)  # deleting a repo drops it from the others' depends_on
        self.assertIsNone(G.get("repo.site")["depends_on"])

    def test_dashboard_cannot_change_restart_values(self):
        self.imported()
        g = G.get("global")
        with self.assertRaises(G.ConfigError):
            G.update("global", {**g, "dashboard_port": 4000}, actor="dashboard")
        self.assertEqual(G.update("global", {**g, "max_active_runs": 5}, actor="dashboard")[0]["key"], "max_active_runs")

    # ------------------------------------------------------------ history, backups, next read
    def test_change_is_recorded_backed_up_and_read_next(self):
        self.imported()
        self.assertEqual(core.settings()["max_active_runs"], 2)
        ch = G.set_path("repo.shop.deploy.prod.branch", "release", actor="cli")
        self.assertEqual(ch, [{"scope": "shop", "key": "deploy.prod.branch", "old": "production", "new": "release"}])
        self.assertEqual(core.repo_rules("shop")["prod"]["branch"], "release")
        G.update("global", {"max_active_runs": 6}, actor="dashboard")
        self.assertEqual(core.settings()["max_active_runs"], 6)
        h = self.history()
        self.assertEqual([(x["actor"], x["scope"], x["key"], json.loads(x["old"]), json.loads(x["new"])) for x in h],
                         [("cli", "shop", "deploy.prod.branch", "production", "release"),
                          ("dashboard", "global", "max_active_runs", 2, 6)])
        self.assertEqual([x["key"] for x in G.history("shop")], ["deploy.prod.branch"])
        backups = sorted((self.tmp / "backups").glob("config-*.json"))
        self.assertEqual(len(backups), 3)  # import + two changes
        self.assertEqual(json.loads(backups[-1].read_text()), G.export())

    def test_backups_keep_the_newest(self):
        self.imported()
        with mock.patch.object(G, "BACKUP_KEEP", 3):
            for n in range(1, 7):
                G.set_path("global.max_active_runs", n)
        files = sorted((self.tmp / "backups").glob("config-*.json"))
        self.assertEqual(len(files), 3)
        self.assertEqual(json.loads(files[-1].read_text())["settings"]["max_active_runs"], 6)

    def test_create_refuses_an_existing_repo(self):
        # 프로젝트 추가 wizard (TP-87): a new repo under a taken name must not replace that repo's settings
        self.imported()
        (self.tmp / "new").mkdir()
        with self.assertRaisesRegex(G.ConfigError, "이미 있는 레포"):
            G.update("repo.shop", {"path": str(self.tmp / "new"), "github": "o/x"}, actor="dashboard", dry_run=True, create=True)
        ch = G.update("repo.fresh", {"path": str(self.tmp / "new"), "github": "o/x"}, actor="dashboard", create=True)
        self.assertIn("github", {c["key"] for c in ch})

    def test_dry_run_saves_nothing(self):
        self.imported()
        ch = G.set_path("global.check.max_parallel", 3, dry_run=True)
        self.assertEqual(ch, [{"scope": "global", "key": "check.max_parallel", "old": 1, "new": 3}])
        self.assertEqual(core.settings()["check"]["max_parallel"], 1)
        self.assertEqual(self.history(), [])

    # ------------------------------------------------------------ export / import
    def test_export_import_round_trip(self):
        self.imported()
        G.set_path("repo.shop.review.max_rework_rounds", 1)
        f = self.tmp / "export.json"
        G.export(str(f))
        saved = json.loads(f.read_text())
        G.set_path("global.max_active_runs", 7)
        with self.assertRaises(G.ConfigError):
            G.set_path("repo.shop", None)  # still listed in github_issues
        G.set_path("repo.site", None)
        self.assertNotIn("site", core.repo_rules())
        ch = G.import_file(str(f))
        self.assertTrue(any(c["key"] == "max_active_runs" and c["new"] == 2 for c in ch))
        self.assertEqual(G.export(), saved)
        self.assertEqual(G.import_file(str(f)), [])  # importing the same values again changes nothing
        self.assertEqual(self.history()[-1]["actor"], "import")
        bad = copy.deepcopy(saved)
        bad["settings"]["deploy"]["repos"]["shop"]["modes"] = ["nope"]
        f.write_text(json.dumps(bad))
        with self.assertRaises(G.ConfigError):
            G.import_file(str(f))
        self.assertEqual(G.export(), saved)

    # ------------------------------------------------------------ repo scope details
    def test_links_and_review_override(self):
        self.imported()
        v = G.get("repo.shop")
        v["links"] = [{"asana_gid": "11", "role": "be", "default": True}, {"asana_gid": "12", "project": "New", "role": "app"}]
        G.update("repo.shop", v, actor="dashboard")
        ps = {p["asana_gid"]: p for p in core.project_map()["projects"]}
        self.assertEqual(ps["11"]["repos"], [{"repo": "site", "role": "fe"}, {"repo": "shop", "role": "be", "default": True}])
        self.assertEqual(ps["12"], {"id": "new", "source": "asana", "asana_gid": "12", "name": "New",
                                    "repos": [{"repo": "shop", "role": "app"}]})
        self.assertEqual({(r["project_id"], r["project_gid"], r["repo"], r["is_default"])
                          for r in self.con.execute("SELECT * FROM project_map")},
                         {("web", "11", "site", 0), ("web", "11", "shop", 1), ("new", "12", "shop", 0)})  # derived table
        v["links"] = [{"project_id": "web", "role": "be"}]  # by tp project id; dropping "new" unlinks shop there
        G.update("repo.shop", v, actor="dashboard")
        ps = {p["id"]: p for p in core.project_map()["projects"]}
        site = G.get("repo.site")  # a new link does not take the default from a project that has one (the wizard)
        site["links"] = [{"project_id": "new", "role": "fe", "default": True}]
        G.update("repo.site", site, actor="dashboard")
        self.assertEqual(next(p for p in core.project_map()["projects"] if p["id"] == "new")["repos"],
                         [{"repo": "site", "role": "fe", "default": True}])  # "new" had no default: it applies
        self.assertEqual((ps["web"]["repos"][1], ps["new"]["repos"]), ({"repo": "shop", "role": "be"}, []))
        self.assertEqual(P.max_rework_rounds("shop"), 4)
        G.set_path("repo.shop.review.max_rework_rounds", 1)
        self.assertEqual((P.max_rework_rounds("shop"), P.max_rework_rounds("site")), (1, 4))
        G.set_path("repo.shop.review.max_rework_rounds", None)
        self.assertEqual(P.max_rework_rounds("shop"), 4)


    # ------------------------------------------------------------ TP-91 projects
    def test_project_without_asana_and_two_repos(self):
        self.imported()
        G.update("project.tools", {"name": "Tools", "source": "none",
                                   "repos": [{"repo": "shop", "role": "be", "default": True}, {"repo": "site", "role": "fe"}]},
                 actor="dashboard", create=True)
        p = next(p for p in core.project_map()["projects"] if p["id"] == "tools")
        self.assertEqual((p["source"], p.get("asana_gid"), [r["repo"] for r in p["repos"]]), ("none", None, ["shop", "site"]))
        self.assertEqual({(r["project_gid"], r["repo"]) for r in self.con.execute("SELECT * FROM project_map WHERE project_id='tools'")},
                         {(None, "shop"), (None, "site")})
        self.assertEqual(self.history()[-1]["scope"], "project:tools")
        with self.assertRaises(G.ConfigError):  # create refuses an existing id
            G.update("project.tools", {"name": "X", "source": "none"}, actor="dashboard", create=True)
        for value, msg in (({"name": "A", "source": "asana"}, "gid"), ({"name": "A", "source": "none", "asana_gid": "5"}, "source asana"),
                           ({"name": "A", "source": "jira"}, "source"), ({"name": "A", "source": "asana", "asana_gid": "11"}, "같은 Asana")):
            with self.assertRaises(G.ConfigError) as e:
                G.update("project.other", value, actor="dashboard")
            self.assertIn(msg, str(e.exception))
        for bad in ("Bad_ID", core.UNSORTED):
            with self.assertRaises(G.ConfigError):
                G.update(f"project.{bad}", {"name": "A", "source": "none"}, actor="dashboard")

    def test_wizard_creates_repos_and_project_in_one_change(self):
        """TP-93: two new repos + a project with no source, saved together; a bad item saves nothing."""
        self.imported()
        for n in ("a1", "b1"):
            (self.tmp / n).mkdir()
        items = [{"scope": "repo.a1", "value": {"path": str(self.tmp / "a1"), "github": "o/a1", "links": []}, "create": True},
                 {"scope": "repo.b1", "value": {"path": str(self.tmp / "b1"), "github": "o/b1", "links": []}, "create": True},
                 {"scope": "project.duo", "create": True, "value": {"name": "Duo", "source": "none", "asana_gid": None,
                  "repos": [{"repo": "a1", "role": "fe"}, {"repo": "b1", "role": "be", "default": True}]}}]
        before = G.export()
        self.assertTrue(G.update_many(items, actor="dashboard", dry_run=True))
        self.assertEqual(G.export(), before)  # dry run writes nothing
        with self.assertRaises(G.ConfigError):  # project lists a repo that is not created: whole batch refused
            G.update_many(items[:1] + items[2:], actor="dashboard")
        self.assertEqual(G.export(), before)
        G.update_many(items, actor="dashboard")
        p = next(p for p in core.project_map()["projects"] if p["id"] == "duo")
        self.assertEqual((p["source"], p["repos"]), ("none", [{"repo": "a1", "role": "fe"}, {"repo": "b1", "role": "be", "default": True}]))
        self.assertEqual({"a1", "b1"} <= set(core.project_map()["repos"]), True)
        with self.assertRaises(G.ConfigError):  # create still refuses an existing repo
            G.update_many(items[:1], actor="dashboard")

    def test_new_shape_round_trip_and_old_export_gets_the_same_ids(self):
        st, pm = self.imported()
        G.update("project.tools", {"name": "Tools", "source": "github", "repos": [{"repo": "shop", "role": "be"}]},
                 actor="dashboard", create=True)
        saved = G.export()
        f = self.tmp / "export.json"
        f.write_text(json.dumps(saved))
        G.update("project.tools", None, actor="dashboard")
        G.import_file(str(f))
        self.assertEqual(G.export(), saved)
        f.write_text(json.dumps({"settings": st, "project_map": pm}))  # a pre-TP-91 export
        G.import_file(str(f))
        self.assertEqual(G.export()["project_map"]["projects"], tp91(pm)["projects"])

    def test_migration_from_asana_keyed_tables(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("""
          CREATE TABLE config_projects (asana_gid TEXT PRIMARY KEY, pos INTEGER, name TEXT, extra TEXT);
          CREATE TABLE config_project_repos (asana_gid TEXT, repo TEXT, pos INTEGER, role TEXT, is_default INTEGER,
                                             PRIMARY KEY (asana_gid, repo));
          CREATE TABLE project_map (project_gid TEXT, project_name TEXT, repo TEXT, role TEXT, is_default INTEGER,
                                    confidence TEXT, note TEXT, PRIMARY KEY (project_gid, repo));
          CREATE TABLE chat_conversations (id TEXT PRIMARY KEY, repo TEXT, title TEXT, session TEXT, tool TEXT,
                                           created_at TEXT, updated_at TEXT);
          INSERT INTO config_projects VALUES ('11','0','Main Site','{"confidence": "high"}'),
                                             ('12','1','글로벌 웹','{}'), ('13','2','Main Site','{}');
          INSERT INTO config_project_repos VALUES ('11','site',0,'fe',1), ('11','shop',1,'be',0), ('12','shop',0,'be',1);
          INSERT INTO chat_conversations(id,repo) VALUES ('c1','shop'), ('c2','site'), ('c3','tp');
        """)
        con.executescript(core.SCHEMA)
        with mock.patch.object(core, "read_config", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            core._migrate(con)  # a failure halfway keeps the old tables, the next start migrates again
        self.assertEqual(con.execute("SELECT count(*) FROM config_project_repos WHERE asana_gid IS NOT NULL").fetchone()[0], 3)
        core._migrate(con)
        self.assertEqual([tuple(r) for r in con.execute("SELECT id,name,source,asana_gid,extra FROM config_projects ORDER BY pos")],
                         [("main-site", "Main Site", "asana", "11", '{"confidence": "high"}'),
                          ("asana-12", "글로벌 웹", "asana", "12", "{}"), ("main-site-2", "Main Site", "asana", "13", "{}")])
        self.assertEqual({tuple(r) for r in con.execute("SELECT project_id,repo,role,is_default FROM config_project_repos")},
                         {("main-site", "site", "fe", 1), ("main-site", "shop", "be", 0), ("asana-12", "shop", "be", 1)})
        self.assertEqual(dict(con.execute("SELECT id, project_id FROM chat_conversations").fetchall()),
                         {"c1": "main-site", "c2": "main-site", "c3": core.UNSORTED})
        self.assertIn("project_id", {r["name"] for r in con.execute("PRAGMA table_info(project_map)")})
        core._migrate(con)  # runs once
        self.assertEqual(con.execute("SELECT count(*) FROM config_projects").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()

