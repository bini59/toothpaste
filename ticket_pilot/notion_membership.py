"""Resolve project ownership within a shared Notion task data source."""
from __future__ import annotations

from . import notion

MILESTONE_KIND_FIELDS = ("milestone_kind_property", "milestone_kind_option")
FIELDS = {"parent_property", "contains_property", "contains_text", "project_data_source_id", "project_page_id",
          "project_property", *MILESTONE_KIND_FIELDS}
TEXT_TYPES = ("title", "rich_text", "formula", "unique_id")
UNRESOLVED = object()


def validate(cfg):
    needle = cfg.get("contains_text", "")
    if bool(cfg.get("contains_property")) != bool(needle) or (needle and not needle.strip()):
        raise notion.NotionError("문자열을 검사할 열과 포함할 문자열을 함께 입력하세요")
    source, row = cfg.get("project_data_source_id"), cfg.get("project_page_id")
    if source or row or cfg.get("project_property"):
        if not notion.valid_id(source) or not notion.valid_id(row):
            raise notion.NotionError("프로젝트 행이 있는 DB와 프로젝트 행을 함께 골라 주세요")
    if cfg.get("parent_property") and cfg["parent_property"] == cfg.get("project_property"):
        raise notion.NotionError("상위 항목 열과 프로젝트 관계 열을 각각 골라 주세요")


def relation_column(schema, cfg, role, *, strict=True):
    if prop := cfg.get(f"{role}_property"):
        target = cfg["data_source_id"] if role == "parent" else cfg.get(f"{role}_data_source_id")
        col = schema.get(prop)
        if col and col["type"] == "relation" and col["data_source_id"] == target:
            return col
        if strict:
            label = {"parent": "상위 항목", "project": "프로젝트", "milestone": "마일스톤"}[role]
            raise notion.NotionError(f"{label} 관계 열과 연결 대상 DB를 다시 확인하세요")


def readable_text(prop):
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return notion.plain(value)
    if kind == "formula" and value and value.get("type") == "string":
        return value.get("string") or ""
    if kind == "unique_id" and value and value.get("number") is not None:
        prefix, number = value.get("prefix"), value["number"]
        return f"{prefix}-{number}" if prefix else str(number)
    return ""


def settings(projects, source):
    """Only membership rules on this task DB can change another project's ownership."""
    rules = {}
    for project in projects:
        cfg = project["notion"]
        if cfg["data_source_id"] == source and any(cfg.get(k) for k in FIELDS):
            rules[project["id"]] = {k: cfg[k] for k in FIELDS if cfg.get(k)}
    return rules


class Rule:
    def __init__(self, schema, cfg, *, strict):
        self.parent = relation_column(schema, cfg, "parent", strict=strict)
        self.project = relation_column(schema, cfg, "project", strict=strict)
        self.row = cfg.get("project_page_id")
        self.parent_row = self.row if cfg.get("project_data_source_id") == cfg["data_source_id"] else None
        self.needle = cfg.get("contains_text")
        col = schema.get(cfg.get("contains_property"))
        self.text = col["name"] if col and col["type"] in TEXT_TYPES else None
        if strict and self.needle and not self.text:
            raise notion.NotionError("문자열을 검사할 Notion 열이 바뀌었어요. 다시 골라 주세요")
        self.missing = {role for role, column in (("parent", self.parent), ("project", self.project), ("contains", self.text))
                        if cfg.get(f"{role}_property") and not column}


class Membership:
    def __init__(self, client, source, schema, settings, project_id):
        self.client, self.source = client, source
        # Report this project's stale settings even if its import filters return no rows.
        self.rules = {pid: Rule(schema, {**cfg, "data_source_id": source}, strict=pid == project_id)
                      for pid, cfg in settings.items()}
        self.missing = set().union(*(rule.missing for rule in self.rules.values()))
        self.scoped = any(rule.row or rule.needle for rule in self.rules.values())
        self.parent_pages = {}
        self.relations = {}

    def links(self, page, column):
        if not column:
            return ()
        key = (page["id"], column["id"])
        if key not in self.relations:
            self.relations[key] = self.client.relation_ids(page, column)
        return self.relations[key]

    def resolve(self, page):
        """Return owners and ancestors. None permits unscoped defaults. UNRESOLVED holds the page."""
        if not self.scoped:
            return None, []
        root = page["id"]
        level, seen, levels = {root: page}, {root}, []
        incomplete_level = False
        while level or incomplete_level:
            levels.append(level)
            linked, has_link = set(), False
            for page_id, current in level.items():
                for pid, rule in self.rules.items():
                    if page_id != root and page_id == rule.parent_row:
                        linked.add(pid)
                        has_link = True
                    if rows := self.links(current, rule.project):
                        has_link = True
                        if rule.row in rows:
                            linked.add(pid)
            if has_link:
                return linked, sorted(seen - {root})
            # A known link at this level wins. An unreadable link here could outrank any deeper match.
            if "project" in self.missing or incomplete_level:
                return UNRESOLVED, []
            # The parent may belong to a peer that only defines a project row or a text rule.
            parents = {parent for current in level.values() for rule in self.rules.values()
                       for parent in self.links(current, rule.parent)}
            incomplete_level = "parent" in self.missing
            level = {}
            for parent in parents - seen:
                if parent not in self.parent_pages:
                    self.parent_pages[parent] = self.client.page(parent, self.source)
                level[parent] = self.parent_pages[parent]
            seen.update(level)

        ancestors = sorted(seen - {root})
        # Resolve the topmost parent's text before falling back to its descendants' text.
        for level in reversed(levels):
            matches = {pid for pid, rule in self.rules.items() if rule.text and any(
                rule.needle in readable_text(current["properties"].get(rule.text, {})) for current in level.values())}
            if matches:
                return matches, ancestors
        # A peer's missing text column matters only if the whole chain has no known match.
        if "contains" in self.missing:
            return UNRESOLVED, []
        return None, ancestors
