"""TP-32/TP-56: Claude usage token pick and response parsing. Run: python3 -m unittest discover tests"""
import io
import json
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ticket_pilot import usage

LIVE = [{"accessToken": "secret-tok", "expiresAt": (time.time() + 3600) * 1000}]


def opener(status=200, body=None):
    def f(req, timeout):
        assert req.get_header("Authorization") == "Bearer secret-tok" and timeout == 5
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(body).encode() if not isinstance(body, bytes) else body)
    return f


class UsageTest(unittest.TestCase):
    def test_ok(self):
        r = usage.fetch(LIVE, opener(body={"five_hour": {"utilization": 42, "resets_at": "2026-10-22T05:20:00Z"},
                                           "seven_day": {"utilization": 18.5, "resets_at": None},
                                           "seven_day_opus": None, "new_thing": [1]}))
        self.assertEqual(r, {"five_hour": {"utilization": 42.0, "resets_at": "2026-10-22T05:20:00Z"},
                             "seven_day": {"utilization": 18.5, "resets_at": None}})

    def test_401(self):
        self.assertEqual(usage.fetch(LIVE, opener(401)), {"error": "unauthorized"})

    def test_odd_response(self):
        self.assertEqual(usage.fetch(LIVE, opener(body={"five_hour": "?"})), {"error": "bad_response"})
        self.assertEqual(usage.fetch(LIVE, opener(body=b"<html>")), {"error": "bad_response"})

    def test_expired_or_missing_token_skips_request(self):
        old = [{"accessToken": "t", "expiresAt": 1000}]
        self.assertEqual(usage.fetch(old, opener(500)), {"error": "token_expired"})
        self.assertEqual(usage.fetch([], opener(500)), {"error": "no_token"})

    def test_picks_the_live_account_when_one_service_name_has_several(self):
        """TP-56: `-s <svc>` alone returns a stale `root` entry; the current user's entry is read too."""
        def tok(expires, access):
            return json.dumps({"claudeAiOauth": {"accessToken": access, "expiresAt": expires}})
        live, stale = (time.time() + 3600) * 1000, 1000
        calls = []

        def security(cmd, **kw):
            calls.append(cmd)
            svc, acct = cmd[3], (cmd[5] if "-a" in cmd else None)
            if svc != "Claude Code-credentials":
                return mock.Mock(returncode=1, stdout="")
            return mock.Mock(returncode=0, stdout=tok(*((live, "secret-tok") if acct == "alice" else (stale, "root-tok"))))

        with mock.patch.dict(usage.os.environ, {"USER": "alice"}), \
             mock.patch.object(usage.subprocess, "run", security), \
             mock.patch.object(usage, "FILE", Path("/nonexistent/.credentials.json")):
            creds = usage._creds()
        self.assertIn(["security", "find-generic-password", "-s", "Claude Code-credentials", "-a", "alice", "-w"], calls)
        self.assertEqual(sorted(c["expiresAt"] for c in creds), [stale, live])
        # opener asserts the request carries the live account's token, not the stale `root` one
        self.assertEqual(usage.fetch(creds, opener(body={"five_hour": {"utilization": 3}}))["five_hour"]["utilization"], 3)

    def test_zero_expiry_credentials_file_does_not_win(self):
        """~/.claude/.credentials.json carries expiresAt 0; a valid keychain token must still be used."""
        file_cred = {"accessToken": "file-tok", "expiresAt": 0}
        r = usage.fetch([file_cred, *LIVE], opener(body={"five_hour": {"utilization": 7}}))
        self.assertEqual(r["five_hour"]["utilization"], 7)  # opener asserts the Bearer token is the live one
        self.assertEqual(usage.fetch([file_cred], opener(500)), {"error": "token_expired"})

    def test_rejected_token_falls_through_to_the_next_candidate(self):
        """TP-56: a revoked token with a far expiry must not shadow a working one."""
        creds = [{"accessToken": "revoked", "expiresAt": (time.time() + 9999) * 1000}, *LIVE]
        tried = []

        def f(req, timeout):
            tok = req.get_header("Authorization").removeprefix("Bearer ")
            tried.append(tok)
            if tok != "secret-tok":
                raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b""))
            return io.BytesIO(json.dumps({"five_hour": {"utilization": 5}}).encode())

        self.assertEqual(usage.fetch(creds, f)["five_hour"]["utilization"], 5)
        self.assertEqual(tried, ["revoked", "secret-tok"])
        # all rejected -> unauthorized; a non-auth HTTP error stops right away
        self.assertEqual(usage.fetch([creds[0]], f), {"error": "unauthorized"})
        self.assertEqual(usage.fetch(LIVE, opener(500)), {"error": "http_500"})

    def test_partial_response_keeps_the_window_it_left_out(self):
        """TP-56: values are never erased, so a body with only five_hour must not blank the 7일 bar."""
        both = usage.merge(None, {"five_hour": {"utilization": 1.0, "resets_at": None},
                                  "seven_day": {"utilization": 2.0, "resets_at": None}})
        partial = usage.merge(both, {"five_hour": {"utilization": 3.0, "resets_at": None}})
        self.assertEqual(partial["five_hour"]["utilization"], 3.0)
        self.assertEqual(partial["seven_day"]["utilization"], 2.0)
        self.assertIsNone(partial["error"])

    def test_failure_keeps_last_values_without_token(self):
        good = usage.merge(None, {"five_hour": {"utilization": 1.0, "resets_at": None}})
        bad = usage.merge(good, {"error": "unauthorized"})
        self.assertEqual((bad["five_hour"], bad["fetched_at"], bad["error"]), (good["five_hour"], good["fetched_at"], "unauthorized"))
        self.assertNotIn("secret", json.dumps(bad))

    def test_cached_returns_the_stored_value_offline(self):
        """TP-56: /api/state never waits on a collection; offline mode never touches the keychain."""
        con = mock.Mock()
        stored = usage.merge(None, {"five_hour": {"utilization": 9.0, "resets_at": None}})
        with mock.patch.dict(usage.os.environ, {"TP_ASANA_FIXTURE": "x"}), \
             mock.patch.object(usage, "meta_get", return_value=stored), \
             mock.patch.object(usage.subprocess, "run", side_effect=AssertionError("no keychain offline")):
            self.assertEqual(usage.cached(con), stored)


if __name__ == "__main__":
    unittest.main()
