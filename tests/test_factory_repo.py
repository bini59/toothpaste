"""작업실 factory (TP-127): /api/state repos carry the drawn repo settings, never check commands.
Run: python3 -m unittest discover tests"""
import unittest
from unittest import mock

from ticket_pilot import checks as K, review, runner, server as S

PM = {"repos": {
    "a": {"agent": {"tool": "claude", "model": "opus", "effort": "high"},
          "checks": [{"name": "lint", "cmd": "secret-ish cmd"}, {"name": "old", "cmd": "x", "off": True, "reason": "r"}]},
    "b": {"ci": True, "checks": [{"name": "lint", "cmd": "x"}]},
    "c": {},
}}
RULES = {"a": {"train": True, "modes": ["staging", "all"], "merge": "merge", "prod": {"branch": "main"}}}


class FactoryRepoTest(unittest.TestCase):
    def setUp(self):
        for x in (mock.patch.object(S, "project_map", return_value=PM), mock.patch.object(K, "project_map", return_value=PM),
                  mock.patch.object(review, "project_map", return_value=PM),
                  mock.patch.object(S, "repo_rules", side_effect=lambda n: RULES.get(n)),
                  mock.patch.object(runner, "settings", return_value={"agent": "codex"}),
                  mock.patch.object(review, "settings", return_value={"review": {"max_rework_rounds": 2}})):
            x.start()
            self.addCleanup(x.stop)

    def test_repo_settings(self):
        a = S.factory_repo("a")
        self.assertEqual(a, {"agent": {"tool": "claude", "model": "opus"}, "checks": ["lint"], "ci": False,
                             "deploy": {"train": True, "modes": ["staging", "all"], "merge": "merge"}, "max_rework": 2})
        self.assertNotIn("secret-ish", str(a))
        b = S.factory_repo("b")
        self.assertEqual((b["checks"], b["ci"]), ([], True))  # a CI repo runs no local checks (checks.repo_checks)
        c = S.factory_repo("c")
        self.assertEqual((c["agent"], c["deploy"], c["checks"]), ({"tool": "codex", "model": None}, None, []))


if __name__ == "__main__":
    unittest.main()
