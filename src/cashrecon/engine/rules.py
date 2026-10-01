"""Category rules: seeding defaults and classifying records."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import Any

from cashrecon.db import Store, now_text
from cashrecon.engine.categories import BY_CODE

FIELDS = {"path", "src_category", "counterparty", "summary"}
OPS = {"contains", "equals", "prefix", "regex"}


class RuleError(ValueError):
    pass


def load_default_rules() -> tuple[int, list[dict[str, Any]]]:
    text = resources.files("cashrecon.engine").joinpath("default_rules.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    return int(data.get("version", 1)), list(data.get("rules", []))


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    field = rule.get("field")
    op = rule.get("op", "contains")
    category = rule.get("category")
    pattern = str(rule.get("pattern", ""))
    if field not in FIELDS:
        raise RuleError(f"未知字段：{field}")
    if op not in OPS:
        raise RuleError(f"未知匹配方式：{op}")
    if category not in BY_CODE:
        raise RuleError(f"未知科目：{category}")
    if not pattern:
        raise RuleError("匹配内容不能为空")
    if op == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuleError(f"正则表达式错误：{exc}") from None
    direction = str(rule.get("direction", "*")).upper()
    if direction not in {"IN", "OUT", "*"}:
        raise RuleError("方向只能是 IN / OUT / *")
    return {"source": str(rule.get("source", "*")) or "*", "field": field, "op": op, "pattern": pattern,
            "direction": direction, "category": category, "priority": int(rule.get("priority", 100)),
            "note": str(rule.get("note", ""))}


def ensure_default_rules(store: Store) -> None:
    version, rules = load_default_rules()
    current = int(store.get_meta("default_rules_version", "0") or 0)
    if current >= version:
        return
    now = now_text()
    with store.tx():
        store.execute("DELETE FROM category_rules WHERE origin = 'default'")
        for raw in rules:
            rule = validate_rule(raw)
            store.execute("INSERT INTO category_rules (source, field, op, pattern, direction, category, priority, "
                          "enabled, origin, note, updated_at) VALUES (?,?,?,?,?,?,?,1,'default',?,?)",
                          (rule["source"], rule["field"], rule["op"], rule["pattern"], rule["direction"],
                           rule["category"], rule["priority"], rule["note"], now))
        store.set_meta("default_rules_version", str(version))


def add_user_rule(store: Store, rule: dict[str, Any]) -> int:
    clean = validate_rule(rule)
    cursor = store.execute(
        "INSERT INTO category_rules (source, field, op, pattern, direction, category, priority, enabled, origin, "
        "note, updated_at) VALUES (?,?,?,?,?,?,?,1,'user',?,?)",
        (clean["source"], clean["field"], clean["op"], clean["pattern"], clean["direction"], clean["category"],
         clean["priority"], clean["note"], now_text()))
    return int(cursor.lastrowid)


@dataclass
class CompiledRule:
    id: int
    source: str
    field: str
    op: str
    pattern: str
    direction: str
    category: str
    regex: re.Pattern[str] | None

    def matches(self, source: str, direction: str, values: dict[str, str]) -> bool:
        if self.source not in ("*", source):
            return False
        if self.direction != "*" and self.direction != direction:
            return False
        value = values.get(self.field)
        if not value:
            return False
        if self.op == "contains":
            return self.pattern in value
        if self.op == "equals":
            return value == self.pattern
        if self.op == "prefix":
            return value.startswith(self.pattern)
        return bool(self.regex and self.regex.search(value))


class Classifier:
    def __init__(self, store: Store) -> None:
        rows = store.query("SELECT * FROM category_rules WHERE enabled = 1 ORDER BY priority, id")
        self.rules = [CompiledRule(r["id"], r["source"], r["field"], r["op"], r["pattern"], r["direction"],
                                   r["category"], re.compile(r["pattern"]) if r["op"] == "regex" else None)
                      for r in rows]
        self.version = ",".join(str(r["id"]) + ":" + r["category"] for r in rows)

    def classify(self, source: str, direction: str, values: dict[str, str]) -> tuple[str, int | None]:
        for rule in self.rules:
            if rule.matches(source, direction, values):
                return rule.category, rule.id
        return "UNCLASSIFIED", None
