"""Resolve project ownership within a shared Notion task data source."""
from __future__ import annotations

from . import notion

FIELDS = {"parent_property", "contains_property", "contains_text", "project_data_source_id", "project_page_id",
          "project_property"}
TEXT_TYPES = ("title", "rich_text", "formula", "unique_id")


def validate(cfg):
    if bool(cfg.get("contains_property")) != bool(cfg.get("contains_text", "").strip()):
        raise notion.NotionError("문자열을 검사할 열과 포함할 문자열을 함께 입력하세요")
    source, row = cfg.get("project_data_source_id"), cfg.get("project_page_id")
    if source or row or cfg.get("project_property"):
        if not notion.valid_id(source) or not notion.valid_id(row):
            raise notion.NotionError("프로젝트 행이 있는 DB와 프로젝트 행을 함께 골라 주세요")
        if source != cfg["data_source_id"] and not cfg.get("project_property"):
            raise notion.NotionError("프로젝트 행이 있는 DB를 가리키는 관계형 열을 골라 주세요")
    if cfg.get("parent_property") and cfg["parent_property"] == cfg.get("project_property"):
        raise notion.NotionError("상위 항목 열과 프로젝트 관계 열을 각각 골라 주세요")


def relation_column(schema, cfg, role):
    if prop := cfg.get(f"{role}_property"):
        target = cfg["data_source_id"] if role == "parent" else cfg.get("project_data_source_id")
        col = schema.get(prop)
        if not col or col["type"] != "relation" or col["data_source_id"] != target:
            label = "상위 항목" if role == "parent" else "프로젝트"
            raise notion.NotionError(f"{label} 관계 열과 연결 대상 DB를 다시 확인하세요")
        return col


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
        if cfg["data_source_id"] == source and (cfg.get("project_page_id") or cfg.get("contains_property")):
            rules[project["id"]] = {k: cfg[k] for k in FIELDS if cfg.get(k)}
    return rules


class Rule:
    def __init__(self, schema, cfg):
        self.parent = relation_column(schema, cfg, "parent")
        self.project = relation_column(schema, cfg, "project")
        self.row = cfg.get("project_page_id")
        self.parent_row = self.row if cfg.get("project_data_source_id") == cfg["data_source_id"] else None
        self.text = None
        if prop := cfg.get("contains_property"):
            col = schema.get(prop)
            if not col or col["type"] not in TEXT_TYPES:
                raise notion.NotionError("문자열을 검사할 Notion 열이 바뀌었어요. 다시 골라 주세요")
            self.text = col["name"]
        self.needle = cfg.get("contains_text")


class Membership:
    def __init__(self, client, source, schema, settings):
        self.client, self.source = client, source
        self.rules = {pid: Rule(schema, {**cfg, "data_source_id": source}) for pid, cfg in settings.items()}
        self.pages = {}
        self.relations = {}

    def links(self, page, column):
        if not column:
            return ()
        key = (page["id"], column["id"])
        if key not in self.relations:
            self.relations[key] = self.client.relation_ids(page, column)
        return self.relations[key]

    def includes(self, page, project_id):
        if not self.rules:
            return True
        root = page["id"]
        self.pages[root] = page
        level = {(pid, root) for pid in self.rules}
        seen = set(level)
        text_matches = set()
        while level:
            # At each depth a project row (including someone else's row) settles ownership.
            linked, has_link = set(), False
            for pid, page_id in level:
                rule = self.rules[pid]
                if page_id != root and page_id == rule.parent_row:
                    linked.add(pid)
                    has_link = True
                    continue
                if page_id not in self.pages:
                    self.pages[page_id] = self.client.page(page_id, self.source)
                if rows := self.links(self.pages[page_id], rule.project):
                    has_link = True
                    if rule.row in rows:
                        linked.add(pid)
            if has_link:
                return project_id in linked

            # An ancestor's resolved project takes precedence over text on its descendants.
            matches, parents = set(), set()
            for pid, page_id in level:
                rule, current = self.rules[pid], self.pages[page_id]
                if rule.text and rule.needle in readable_text(current["properties"].get(rule.text, {})):
                    matches.add(pid)
                parents.update((pid, parent) for parent in self.links(current, rule.parent))
            if matches:
                text_matches = matches
            level = parents - seen
            seen.update(level)
        return project_id in text_matches if text_matches else project_id not in self.rules
