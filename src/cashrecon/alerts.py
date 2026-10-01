"""Alert history: persist high/medium action items with first/last seen, for the console and reports."""

from __future__ import annotations

import hashlib
from typing import Any

from cashrecon.db import Store, now_text


def record(store: Store, day: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert alerts for ``day``; returns the ones that are new or changed since last seen."""
    now = now_text()
    fresh = []
    with store.tx():
        for item in items:
            if item["level"] not in ("high", "medium"):
                continue
            key = f"{item['key'] or item['kind']}@{day}"
            fingerprint = hashlib.sha1(f"{item['title']}|{item.get('amount')}|{item.get('count')}".encode()).hexdigest()
            row = store.one("SELECT fingerprint FROM alerts WHERE alert_key = ?", (key,))
            if row is None:
                store.execute("INSERT INTO alerts (alert_key, level, rule, biz_date, title, detail, fingerprint, "
                              "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                              (key, item["level"], item["kind"], day, item["title"], item.get("detail", ""),
                               fingerprint, now, now))
                fresh.append(item)
            else:
                store.execute("UPDATE alerts SET level=?, title=?, detail=?, fingerprint=?, last_seen=? "
                              "WHERE alert_key=?", (item["level"], item["title"], item.get("detail", ""),
                                                    fingerprint, now, key))
                if row["fingerprint"] != fingerprint:
                    fresh.append(item)
        keys = {f"{i['key'] or i['kind']}@{day}" for i in items if i["level"] in ("high", "medium")}
        for row in store.query("SELECT alert_key FROM alerts WHERE biz_date = ? AND status = 'open'", (day,)):
            if row["alert_key"] not in keys:
                store.execute("UPDATE alerts SET status='resolved', last_seen=? WHERE alert_key=?",
                              (now, row["alert_key"]))
    return fresh
