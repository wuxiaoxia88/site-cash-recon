"""Cash-basis operating profit (现金口径经营利润) and fund movements."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.engine import categories
from cashrecon.engine.rules import Classifier
from cashrecon.sources.bill_profit import COMPONENT_CN, bill_profit_totals


def zt_lines(store: Store, classifier: Classifier, day: str) -> list[dict[str, Any]]:
    rows = []
    for r in store.query("SELECT path, description, amount_cents FROM zt_categories WHERE biz_date = ?", (day,)):
        amount = r["amount_cents"]
        category, rule = classifier.classify("ZT_SUMMARY", "IN" if amount > 0 else "OUT", {"path": r["path"]})
        rows.append({"path": r["path"], "description": r["description"], "amount": amount, "category": category,
                     "rule": rule})
    return rows


def profit_day(store: Store, settings: Settings, classifier: Classifier, day: date) -> dict[str, Any]:
    text = day.isoformat()
    lines: dict[str, dict[str, int]] = defaultdict(lambda: {"zt": 0, "offline": 0})
    movements: dict[str, int] = defaultdict(int)
    unclassified = {"zt": 0, "in": 0, "out": 0, "count": 0, "items": []}
    zt_rows = zt_lines(store, classifier, text)
    for row in zt_rows:
        cat = row["category"]
        if categories.is_pl(cat):
            lines[cat]["zt"] += row["amount"]
        elif cat == "UNCLASSIFIED":
            unclassified["zt"] += row["amount"]
            unclassified["count"] += 1
            unclassified["items"].append({"source": "中天", "text": row["path"], "amount": row["amount"]})
        else:
            movements[cat] += row["amount"]
    # ZT withdrawals whose initiator is configured as a cost: move from movement to P&L.
    for r in store.query("SELECT f.amount_cents, s.category FROM flows f JOIN flow_states s USING (flow_id) "
                         "WHERE f.source = 'ZT_FLOW' AND f.biz_date = ? AND f.direction = 'OUT' "
                         "AND s.category NOT IN ('XFER_ZT_WITHDRAW','XFER_ZT_TOPUP','XFER_INTERNAL','PASS_THROUGH',"
                         "'COST_CLAIM','UNCLASSIFIED') AND f.src_category IN ('线下提现','中天余额提现')", (text,)):
        if categories.is_pl(r["category"]):
            lines[r["category"]]["zt"] -= r["amount_cents"]
            movements["XFER_ZT_WITHDRAW"] += r["amount_cents"]
    offline_counted = 0
    for r in store.query("SELECT f.flow_id, f.direction, f.amount_cents, f.src_category, f.summary, f.counterparty, "
                         "f.account_code, s.state, s.category FROM flows f JOIN flow_states s USING (flow_id) "
                         "JOIN accounts a ON a.account_code = f.account_code "
                         "WHERE f.biz_date = ? AND a.domain = 'OFFLINE' AND s.state = 'NORMAL'", (text,)):
        signed = r["amount_cents"] if r["direction"] == "IN" else -r["amount_cents"]
        cat = r["category"]
        if categories.is_pl(cat):
            lines[cat]["offline"] += signed
            offline_counted += 1
        elif cat == "UNCLASSIFIED":
            unclassified["in" if signed > 0 else "out"] += abs(signed)
            unclassified["count"] += 1
            unclassified["items"].append({"source": r["account_code"], "flow_id": r["flow_id"],
                                          "text": r["src_category"] or r["summary"] or r["counterparty"],
                                          "amount": signed})
    result_lines = []
    for code in sorted(lines, key=lambda c: categories.ORDER.get(c, 999)):
        item = lines[code]
        total = item["zt"] + item["offline"]
        if total or item["zt"] or item["offline"]:
            result_lines.append({"code": code, "name": categories.name(code), "kind": categories.kind(code),
                                 "zt": item["zt"], "offline": item["offline"], "total": total})
    income = sum(x["total"] for x in result_lines if x["kind"] == "income")
    cost = -sum(x["total"] for x in result_lines if x["kind"] == "cost")
    gross_activity = income + cost + unclassified["in"] + unclassified["out"] + abs(unclassified["zt"])
    ratio = (unclassified["in"] + unclassified["out"] + abs(unclassified["zt"])) / gross_activity if gross_activity else 0
    zt_present = bool(zt_rows) or store.scalar("SELECT 1 FROM balances WHERE biz_date=? AND source='ZT_SUMMARY'",
                                                (text,)) is not None
    return {
        "income": income, "cost": cost, "profit": income - cost, "lines": result_lines,
        "unclassified": {**unclassified, "ratio": round(ratio, 4)},
        "movements": dict(movements), "zt_included": zt_present, "offline_items": offline_counted,
        "complete": zt_present,
    }


def bill_profit_day(store: Store, day: date) -> dict[str, Any] | None:
    rows = store.query("SELECT component, amount_cents FROM bill_profit WHERE biz_date = ?", (day.isoformat(),))
    if not rows:
        return None
    components = {r["component"]: r["amount_cents"] for r in rows}
    totals = bill_profit_totals(components)
    totals["components"] = [{"code": k, "name": COMPONENT_CN.get(k, k), "amount": v} for k, v in components.items()]
    totals["missing_cn"] = [COMPONENT_CN.get(m, m) for m in totals["missing"]]
    return totals
