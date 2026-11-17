"""`tp guide [topic]` and GET /api/guide: the user guide docs/guide.md (TP-150).

Each `## <key> · <title>` section is a topic; its first line is the one-line description in the topic list."""
from __future__ import annotations

import re

from .core import ROOT

GUIDE = ROOT / "docs" / "guide.md"


def text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def sections() -> dict[str, str]:
    """{key: section text from its `## ` heading to the next one}"""
    parts = re.split(r"^(?=## )", text(), flags=re.M)
    return {m.group(1): p.rstrip() + "\n" for p in parts if (m := re.match(r"## (\w+) · ", p))}


def topics() -> str:
    lines = []
    for key, body in sections().items():
        title, desc = body.split("\n")[0].split(" · ", 1)[1], body.split("\n")[1]
        lines.append(f"  {key:<10} {title}: {desc}")
    return "주제 (tp guide <주제>):\n" + "\n".join(lines)


def main(topic: str | None) -> int:
    if topic is None:
        print(topics())
        return 0
    body = sections().get(topic)
    if body is None:
        print(f"모르는 주제: {topic}\n\n{topics()}")
        return 1
    print(body, end="")
    return 0
