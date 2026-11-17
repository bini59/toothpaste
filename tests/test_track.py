"""track.py's prod.release rule (TP-186): live once the latest GitHub Release's tag contains the merge commit.
Run: python3 -m unittest discover tests"""
import unittest
from unittest import mock

from ticket_pilot import track as T


class ReleaseTrackTest(unittest.TestCase):
    def gh(self, releases, status):
        def gh_json(args):
            if args[:2] == ["release", "list"]:
                return releases
            if args[1].endswith("/commits/v0.5.0"):
                return {"sha": "rel"}
            if "/compare/rel...merge" in args[1]:
                return {"status": status}
            raise AssertionError(args)
        return mock.patch.object(T, "gh_json", side_effect=gh_json)

    def live(self):
        rc = T.track_rc({"prod": {"branch": "main", "release": True}})
        sha, how = T.deployed_sha("o/r", rc)
        return sha, how, bool(sha) and T._ancestor("o/r", "merge", sha)

    def test_in_release_tag_is_live(self):
        with self.gh([{"tagName": "v0.5.0"}], "behind"):
            self.assertEqual(self.live(), ("rel", "release v0.5.0", True))

    def test_not_in_release_tag_waits(self):
        with self.gh([{"tagName": "v0.5.0"}], "ahead"):
            self.assertEqual(self.live(), ("rel", "release v0.5.0", False))
        with self.gh([], None):
            self.assertEqual(self.live(), (None, "GitHub Release 없음", False))

    def test_tracked_repos(self):
        rules = {"a": {"prod": {"branch": "main", "release": True}}, "b": {"prod": {"branch": "main"}}}
        with mock.patch.object(T, "repo_rules", return_value=rules):
            self.assertEqual(T.track_config(), {"a": {"prod_branch": "main", "release": True}})


if __name__ == "__main__":
    unittest.main()
