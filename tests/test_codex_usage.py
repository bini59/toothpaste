"""TP-56: Codex App Server usage adapter (ported from 330_ticket_pilot) and its meta.codex_usage cache.
Run: python3 -m unittest discover tests"""
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import codex_usage, core
from ticket_pilot import usage as U
from ticket_pilot.codex_usage import UsageError, collect, normalize


class NormalizeTest(unittest.TestCase):
    def test_windows_and_allowlist(self):
        for used, expected in [(0, 100), (100, 0), (27.25, 72.75), (-1, 100), (101, 0),
                               (None, None), ('5', None), (True, None), (float('nan'), None), (float('inf'), None), (10**400, None)]:
            result = normalize({'rateLimits': {'primary': {'usedPercent': used, 'windowDurationMins': 300,
                                 'resetsAt': 1792600000, 'secret': 'never'}, 'secondary': None}, 'email': 'never'})
            self.assertEqual(result[0]['windows'][0], {'window': 'primary', 'remainingPercent': expected,
                             'windowDurationMins': 300, 'resetAt': 1792600000})
            self.assertNotIn('never', json.dumps(result))

    def test_buckets(self):
        bucket = {'limitId': 'codex', 'primary': {}, 'secondary': {'usedPercent': 20}}
        result = normalize({'rateLimits': bucket, 'rateLimitsByLimitId': {'codex': bucket, 'other': {'primary': {}}}})
        self.assertEqual([b['id'] for b in result], ['codex', 'other'])
        self.assertEqual(len(result[0]['windows']), 2)
        result = normalize({'rateLimitsByLimitId': {'invalid': None, 'other': {'primary': {'resetsAt': 'bad', 'windowDurationMins': -1}}, 'codex': bucket}})
        self.assertEqual(result[0]['id'], 'codex')
        self.assertIsNone(result[1]['windows'][0]['resetAt'])
        self.assertIsNone(result[1]['windows'][0]['windowDurationMins'])
        for value in ({}, {'rateLimits': None}, {'rateLimits': []}):
            self.assertEqual(normalize(value), [])


class AdapterTest(unittest.TestCase):
    def fake(self, script, timeout=1):
        return collect(timeout, [sys.executable, '-u', '-c', script])

    def test_protocol_and_matching(self):
        buckets, account = self.fake('''
import sys,json
assert json.loads(input())['method']=='initialize'
print(json.dumps({'method':'notification'}))
print(json.dumps({'method':'account/updated','params':{'authMode':'chatgpt'}}))
print(json.dumps({'id':99,'result':{}}))
print(json.dumps({'id':1,'result':{}}))
assert json.loads(input())['method']=='initialized'
assert json.loads(input())['method']=='account/rateLimits/read'
print(json.dumps({'id':2,'result':{'accountId':'private','rateLimits':{'primary':{'usedPercent':28}}}}))
input()
''')
        self.assertEqual(account, 'private')
        self.assertEqual(buckets[0]['windows'][0]['remainingPercent'], 72)

    def test_account_change_notification(self):
        with self.assertRaises(UsageError) as caught:
            self.fake('import json; input(); print(json.dumps({"method":"account/updated","params":{"authMode":"chatgpt"}})); print(json.dumps({"method":"account/updated","params":{"authMode":"agentIdentity"}})); input()')
        self.assertEqual(caught.exception.code, 'account_changed')

    def test_timeout_and_bounded_output(self):
        for script, code in [('import time; time.sleep(10)', 'timeout'),
                             ('print("x"*1100000); input()', 'invalid_response')]:
            start = time.monotonic()
            with self.assertRaises(UsageError) as caught:
                self.fake(script, .15)
            self.assertEqual(caught.exception.code, code)
            self.assertLess(time.monotonic()-start, 2)

    def test_stray_non_json_line_is_skipped(self):
        buckets, _ = self.fake('''
import json
input()
print("codex 0.165.0 update available")
print(json.dumps({"id":1,"result":{}}))
input(); input()
print(json.dumps({"id":2,"result":{"rateLimits":{"primary":{"usedPercent":10}}}}))
input()
''')
        self.assertEqual(buckets[0]['windows'][0]['remainingPercent'], 90)

    def test_errors(self):
        for message, code in [('authentication required', 'login_required'), ('API key unsupported', 'unsupported_auth'), ('chatgpt authentication required to read rate limits', 'unsupported_auth'), ('no method', 'unsupported')]:
            with self.assertRaises(UsageError) as caught:
                self.fake('import json; input(); print(json.dumps({"id":1,"error":{"code":-32601,"message":'+repr(message)+'}})); input()')
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(UsageError) as caught:
            collect(command=['/nonexistent/codex'])
        self.assertEqual(caught.exception.code, 'cli_missing')


class FetchTest(unittest.TestCase):
    def test_fetch_drops_the_account_id_and_maps_errors(self):
        with mock.patch.object(codex_usage, "collect", return_value=([{"id": "codex", "windows": []}], "private")):
            self.assertEqual(codex_usage.fetch(), {"buckets": [{"id": "codex", "windows": []}]})
            self.assertNotIn("private", json.dumps(codex_usage.fetch()))
        with mock.patch.object(codex_usage, "collect", side_effect=UsageError("timeout")):
            self.assertEqual(codex_usage.fetch(), {"error": "timeout"})
        with mock.patch.object(codex_usage, "collect", side_effect=RuntimeError("boom")):
            self.assertEqual(codex_usage.fetch(), {"error": "unavailable"})

    def test_a_read_with_no_window_is_an_error_so_the_last_value_survives(self):
        """TP-56: renamed/empty rateLimits must not count as a successful empty reading."""
        with mock.patch.object(codex_usage, "collect", return_value=([], None)):
            self.assertEqual(codex_usage.fetch(), {"error": "invalid_response"})
            prev = U.merge(None, {"buckets": [{"id": "codex", "windows": [{"window": "primary"}]}]})
            self.assertEqual(U.merge(prev, codex_usage.fetch())["buckets"], prev["buckets"])


class CacheTest(unittest.TestCase):
    """TP-56: meta.codex_usage keeps the last good buckets through failures and restarts (same rule as Claude)."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(U._locks.pop, "codex_usage", None)
        U._locks.pop("codex_usage", None)
        self.con = self.open()

    def open(self):
        """A fresh connection to the same file: reopening is what a dashboard restart does."""
        con = sqlite3.connect(self.tmp / "tp.db")
        con.row_factory = sqlite3.Row
        con.executescript(core.SCHEMA)
        core._migrate(con)
        return con

    def test_last_value_survives_failure_and_restart(self):
        ok = {"buckets": [{"id": "codex", "name": "codex", "windows": [{"window": "primary", "remainingPercent": 40,
                                                                        "windowDurationMins": 300, "resetAt": 1}]}]}
        core.meta_set(self.con, "codex_usage", U.merge(None, ok))
        stored = core.meta_get(self.con, "codex_usage")
        self.assertEqual(stored["buckets"], ok["buckets"])
        self.assertIsNone(stored["error"])

        after = U.merge(stored, {"error": "timeout"})
        self.assertEqual(after["buckets"], ok["buckets"])
        self.assertEqual(after["error"], "timeout")
        self.assertEqual(after["fetched_at"], stored["fetched_at"])
        self.assertGreaterEqual(after["checked_at"], stored["checked_at"])

        # a restart only has the DB: cached() returns the stored value without waiting for a collection
        core.meta_set(self.con, "codex_usage", after)
        self.con.commit()
        self.con.close()
        with mock.patch.object(codex_usage, "collect", side_effect=AssertionError("must not block the request")):
            self.assertEqual(codex_usage.cached(self.open())["buckets"], ok["buckets"])

    def test_refresh_stores_the_new_value(self):
        with mock.patch.object(U, "db", self.open), \
             mock.patch.object(codex_usage, "collect", return_value=([{"id": "codex", "windows": []}], "private")):
            U._locks.setdefault("codex_usage", threading.Lock()).acquire()
            U._refresh("codex_usage", codex_usage.fetch)
        v = core.meta_get(self.open(), "codex_usage")
        self.assertEqual(v["buckets"], [{"id": "codex", "windows": []}])
        self.assertIsNone(v["error"])
        self.assertNotIn("private", json.dumps(v))


if __name__ == "__main__":
    unittest.main()
