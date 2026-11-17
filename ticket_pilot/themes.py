"""Dashboard themes (TP-157): one JSON file per theme, like Claude Code's ~/.claude/themes.

  {"name": "...", "base": "light|dark", "tokens": {"color-primary": "#4e60c0", ...}}

Built-in themes are web/themes/*.json; user themes are <checkout>/../themes/*.json (~/tp-work/themes, not a setting).
Token names are the variables of web/src/theme/tokens.css (with or without the leading --); a missing token takes the
base theme's value. Unknown names and values that are not a plain color or font list are dropped with a warning, so a
theme file cannot put arbitrary CSS on the page. GET /api/themes lists them; the choice is the global setting `theme`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .core import ROOT, settings

BUILTIN_DIR = ROOT / "web" / "themes"
USER_DIR = ROOT.parent / "themes"
TOKENS_CSS = ROOT / "web" / "src" / "theme" / "tokens.css"
BASES = ("light", "dark")
AUTO = "auto"  # follows prefers-color-scheme between the built-in light and dark (resolved in the browser)
HEX = re.compile(r"#[0-9a-fA-F]{6}")  # the 작업실 canvas parses colors as 6-digit hex (views/factory/pixel.ts)
ID = re.compile(r"\w[\w.-]*")  # a theme id the `theme` setting accepts (config.validate)
FONT = re.compile(r"[\w \-,.'\"]+")  # a font list: names, quotes and commas only


def token_names() -> set[str]:
    return set(re.findall(r"^\s*--([\w-]+)\s*:", TOKENS_CSS.read_text(encoding="utf-8"), re.M))


def _clean(tokens, names: set[str]) -> tuple[dict, list[str]]:
    out, warn = {}, []
    if not isinstance(tokens, dict):
        return out, ["tokens: 객체여야 해요"]
    for k, v in tokens.items():
        name = str(k).removeprefix("--")
        if name not in names:
            warn.append(f"모르는 토큰 '{k}'을 무시했어요")
        elif not isinstance(v, str) or not (HEX if name.startswith("color-") else FONT).fullmatch(v.strip()):
            warn.append(f"'{k}' 값을 무시했어요: " + ("#rrggbb 색이어야 해요" if name.startswith("color-") else "글꼴 이름 목록이어야 해요"))
        else:
            out[name] = v.strip()
    return out, warn


def _read(path: Path, names: set[str]) -> dict:
    t = {"id": path.stem, "file": path.name, "name": path.stem, "base": "light", "tokens": {}, "warnings": [], "ok": True}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("JSON 객체가 아니에요")
    except (OSError, ValueError) as e:
        return {**t, "ok": False, "warnings": [f"읽지 못해서 무시했어요: {e}"]}
    if isinstance(doc.get("name"), str) and doc["name"].strip():
        t["name"] = doc["name"].strip()
    if doc.get("base") in BASES:
        t["base"] = doc["base"]
    else:
        t["warnings"].append(f"base는 {' 또는 '.join(BASES)}여야 해요: light로 봐요")
    t["tokens"], w = _clean(doc.get("tokens", {}), names)
    t["warnings"] += w
    return t


def themes() -> list[dict]:
    """Built-in then user themes; each {id, file, name, base, builtin, tokens (resolved over its base), warnings, ok}."""
    names = token_names()
    out = [{**_read(p, names), "builtin": True} for p in sorted(BUILTIN_DIR.glob("*.json"))]
    base = {t["id"]: t["tokens"] for t in out if t["id"] in BASES}
    for p in sorted(USER_DIR.glob("*.json")) if USER_DIR.is_dir() else []:
        t = {**_read(p, names), "builtin": False}
        if t["id"] in (*(x["id"] for x in out), AUTO):
            t.update(ok=False, warnings=["내장 테마와 이름이 같아서 무시했어요: 파일 이름을 바꿔 주세요"])
        elif not ID.fullmatch(t["id"]):
            t.update(ok=False, warnings=["파일 이름은 영문, 숫자, _ . - 만 써요 (.으로 시작하지 않게): 파일 이름을 바꿔 주세요"])
        out.append(t)
    for t in out:
        if not t["builtin"]:
            t["tokens"] = {**base.get(t["base"], {}), **t["tokens"]}
    return out


def selected(ts: list[dict]) -> str:
    """The saved choice, or light when it is missing or unusable."""
    want = settings().get("theme") or "light"
    return want if want == AUTO or any(t["id"] == want and t["ok"] for t in ts) else "light"


def api() -> dict:
    ts = themes()
    return {"themes": ts, "selected": selected(ts), "user_dir": str(USER_DIR)}
