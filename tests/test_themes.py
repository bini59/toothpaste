"""TP-157: dashboard themes. Built-in and user theme files, dropping bad values, falling back to light, GET /api/themes.
Run: python3 -m unittest discover tests"""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ticket_pilot import config as G, core, themes as TH
from ticket_pilot import server as S


class ThemesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(TH, "USER_DIR", self.dir),
                  mock.patch.object(TH, "settings", lambda: {"theme": self.theme})):
            p.start()
            self.addCleanup(p.stop)
        self.theme = "light"

    def put(self, name, doc):
        (self.dir / name).write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")

    def by_id(self):
        return {t["id"]: t for t in TH.themes()}

    def test_builtin(self):
        ts = self.by_id()
        self.assertEqual(list(ts), ["dark", "light"])
        self.assertTrue(all(t["ok"] and t["builtin"] and not t["warnings"] for t in ts.values()))
        # dark covers every token of tokens.css, so nothing of the light default shows through
        self.assertEqual(set(ts["dark"]["tokens"]) | {"font-sans"}, TH.token_names())

    def test_user_theme_over_its_base(self):
        self.put("corp.json", {"name": "Corp", "base": "dark", "tokens": {"--color-primary": "#112233", "font-sans": "\"Pretendard\", sans-serif"}})
        t = self.by_id()["corp"]
        self.assertEqual((t["name"], t["file"], t["base"], t["builtin"], t["ok"], t["warnings"]), ("Corp", "corp.json", "dark", False, True, []))
        self.assertEqual(t["tokens"]["color-primary"], "#112233")
        self.assertEqual(t["tokens"]["font-sans"], "\"Pretendard\", sans-serif")
        self.assertEqual(t["tokens"]["color-grey-900"], self.by_id()["dark"]["tokens"]["color-grey-900"])  # from base dark

    def test_dangerous_and_unknown_values_dropped(self):
        self.put("bad.json", {"name": "Bad", "base": "light", "tokens": {
            "color-primary": "red; } body { display:none", "color-surface": "url(http://x/)", "font-sans": "a} b{",
            "color-accent": "#abcdef", "nope": "#000000", "color-border": 5}})
        t = self.by_id()["bad"]
        self.assertTrue(t["ok"])
        self.assertEqual(t["tokens"], {"color-accent": "#abcdef"})
        self.assertEqual(len(t["warnings"]), 5)
        self.assertTrue(any("nope" in w for w in t["warnings"]))

    def test_broken_json_and_name_clash(self):
        self.put("broken.json", "{not json")
        self.put("dark.json", {"name": "X", "base": "dark", "tokens": {}})
        self.put("auto.json", {"name": "X", "base": "dark", "tokens": {}})
        self.put("my theme.json", {"name": "X", "base": "dark", "tokens": {}})
        self.put("..json", {"name": "X", "base": "dark", "tokens": {}})
        ts = [t for t in TH.themes() if not t["builtin"]]
        self.assertEqual(sorted((t["file"], t["ok"]) for t in ts),
                         [("..json", False), ("auto.json", False), ("broken.json", False), ("dark.json", False), ("my theme.json", False)])
        self.assertTrue(all(t["warnings"] for t in ts))
        self.theme = "broken"
        self.assertEqual(TH.api()["selected"], "light")

    def test_selected_fallback(self):
        self.put("corp.json", {"name": "Corp", "base": "light", "tokens": {}})
        for want, got in (("corp", "corp"), ("dark", "dark"), ("auto", "auto"), ("gone", "light"), ("", "light")):
            self.theme = want
            self.assertEqual(TH.api()["selected"], got)

    def test_setting_validation(self):
        doc = core.effective_config({})
        self.assertEqual(doc["settings"]["theme"], "light")
        self.assertEqual(G.validate(doc), [])
        for bad in ("../x", ".", "a b"):
            doc["settings"]["theme"] = bad
            self.assertTrue(any(e.startswith("global.theme") for e in G.validate(doc)), bad)

    def test_api_themes(self):
        self.put("corp.json", {"name": "Corp", "base": "light", "tokens": {"color-primary": "#112233"}})
        self.theme = "corp"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
        c.request("GET", "/api/themes")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        j = json.loads(r.read())
        self.assertEqual((j["selected"], j["user_dir"]), ("corp", str(self.dir)))
        self.assertEqual([(t["id"], t["builtin"]) for t in j["themes"]], [("dark", True), ("light", True), ("corp", False)])


if __name__ == "__main__":
    unittest.main()
