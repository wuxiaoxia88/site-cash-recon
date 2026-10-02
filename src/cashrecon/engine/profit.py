"""Operating profit.

Two views are produced for every day:

* **经营口径（primary）** — income/costs attributed to their business period (业务期间).
  An offline item whose period spans several days is spread evenly over those days;
  ZT settlements are daily by nature. This is the basis for profit and loss alerts.
* **现金口径（reference）** — the same items counted on the day the money moved.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.engine import categories
from cashrecon.engine.matching import PERIOD_BASIS_CN, WITHDRAW_TYPES
from cashrecon.engine.rules import Classifier
from cashrecon.sources.bill_profit import COMPONENT_CN, bill_profit_totals


def allocate(signed: int, start: date, end: date, day: date) -> int:
    """Share of ``signed`` cents falling on ``day`` when spread evenly over [start, end]."""
    n = (end - start).days + 1
    i = (day - start).days
    if n <= 0 or i < 0 or i >= n:
        return 0
    return (signed * (i + 1)) // n - (signed * i) // n


def zt_lines(store: Store, classifier: Classifier, day: str) -> list[dict[str, Any]]:
    rows = []
    for r in store.query("SELECT path, description, amount_cents FROM zt_categories WHERE biz_date = ?", (day,)):
        amount = r["amount_cents"]
        category, rule = classifier.classify("ZT_SUMMARY", "IN" if amount > 0 else "OUT", {"path": r["path"]})
        rows.append({"path": r["path"], "description": r["description"], "amount": amount, "category": category,
                     "rule": rule})
    return rows


def _lines(acc: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    result = []
    for code in sorted(acc, key=lambda c: categories.ORDER.get(c, 999)):
        item = acc[code]
        total = item["zt"] + item["offline"]
        if total or item["zt"] or item["offline"]:
            result.append({"code": code, "name": categories.name(code), "kind": categories.kind(code),
                           "zt": item["zt"], "offline": item["offline"], "total": total})
    return result


def _totals(lines: list[dict[str, Any]]) -> tuple[int, int]:
    income = sum(x["total"] for x in lines if x["kind"] == "income")
    cost = -sum(x["total"] for x in lines if x["kind"] == "cost")
    return income, cost


_OFFLINE_SQL = ("SELECT f.flow_id, f.biz_date, f.direction, f.amount_cents, f.src_category, f.summary, "
                "f.counterparty, f.account_code, a.name AS account_name, s.category, s.p_start, s.p_end, "
                "s.period_basis FROM flows f JOIN flow_states s USING (flow_id) "
                "JOIN accounts a ON a.account_code = f.account_code "
                "WHERE a.domain = 'OFFLINE' AND s.state = 'NORMAL' ")


def profit_day(store: Store, settings: Settings, classifier: Classifier, day: date) -> dict[str, Any]:
    text = day.isoformat()
    accrual: dict[str, dict[str, int]] = defaultdict(lambda: {"zt": 0, "offline": 0})
    cash: dict[str, dict[str, int]] = defaultdict(lambda: {"zt": 0, "offline": 0})
    movements: dict[str, int] = defaultdict(int)
    unclassified = {"zt": 0, "in": 0, "out": 0, "count": 0, "items": []}

    # ZT settlements: daily by nature, identical in both views.
    zt_rows = zt_lines(store, classifier, text)
    for row in zt_rows:
        cat = row["category"]
        if categories.is_pl(cat):
            accrual[cat]["zt"] += row["amount"]
            cash[cat]["zt"] += row["amount"]
        elif cat == "UNCLASSIFIED":
            unclassified["zt"] += row["amount"]
            unclassified["count"] += 1
            unclassified["items"].append({"source": "中天", "text": row["path"], "amount": row["amount"]})
        else:
            movements[cat] += row["amount"]
    # ZT withdrawals that are not labour cost (initiator mapped to an own account, or a manual category):
    # the summary counted them as labour cost, move them to their final category.
    for r in store.query("SELECT f.amount_cents, s.category FROM flows f JOIN flow_states s USING (flow_id) "
                         "WHERE f.source = 'ZT_FLOW' AND f.biz_date = ? AND f.direction = 'OUT' "
                         "AND f.src_category IN (?, ?) AND s.category <> 'COST_LABOR'", (text, *WITHDRAW_TYPES)):
        for view in (accrual, cash):
            view["COST_LABOR"]["zt"] += r["amount_cents"]
            if categories.is_pl(r["category"]):
                view[r["category"]]["zt"] -= r["amount_cents"]
        if not categories.is_pl(r["category"]):
            movements[r["category"]] -= r["amount_cents"]

    # Offline, accrual view: everything whose business period covers this day.
    allocated_in: list[dict[str, Any]] = []
    for r in store.query(_OFFLINE_SQL + "AND s.p_start <= ? AND s.p_end >= ?", (text, text)):
        if not categories.is_pl(r["category"]):
            continue
        signed = r["amount_cents"] if r["direction"] == "IN" else -r["amount_cents"]
        share = allocate(signed, date.fromisoformat(r["p_start"]), date.fromisoformat(r["p_end"]), day)
        accrual[r["category"]]["offline"] += share
        if r["p_start"] != r["p_end"] or r["biz_date"] != text:
            allocated_in.append({"account": r["account_name"], "text": r["src_category"] or r["summary"],
                                 "paid_on": r["biz_date"], "period": f"{r['p_start']}～{r['p_end']}",
                                 "amount": signed, "share": share,
                                 "basis": PERIOD_BASIS_CN.get(r["period_basis"], r["period_basis"])})

    # Offline, cash view and items paid today but attributed elsewhere.
    deferred: list[dict[str, Any]] = []
    offline_counted = 0
    for r in store.query(_OFFLINE_SQL + "AND f.biz_date = ?", (text,)):
        signed = r["amount_cents"] if r["direction"] == "IN" else -r["amount_cents"]
        cat = r["category"]
        if categories.is_pl(cat):
            cash[cat]["offline"] += signed
            offline_counted += 1
            if r["p_start"] and (r["p_start"] != text or r["p_end"] != text):
                deferred.append({"account": r["account_name"], "text": r["src_category"] or r["summary"],
                                 "amount": signed, "period": f"{r['p_start']}～{r['p_end']}",
                                 "basis": PERIOD_BASIS_CN.get(r["period_basis"], r["period_basis"])})
        elif cat == "UNCLASSIFIED":
            unclassified["in" if signed > 0 else "out"] += abs(signed)
            unclassified["count"] += 1
            unclassified["items"].append({"source": r["account_code"], "flow_id": r["flow_id"],
                                          "text": r["src_category"] or r["summary"] or r["counterparty"],
                                          "amount": signed})

    lines = _lines(accrual)
    income, cost = _totals(lines)
    cash_lines = _lines(cash)
    cash_income, cash_cost = _totals(cash_lines)
    gross = income + cost + unclassified["in"] + unclassified["out"] + abs(unclassified["zt"])
    ratio = (unclassified["in"] + unclassified["out"] + abs(unclassified["zt"])) / gross if gross else 0
    zt_present = bool(zt_rows) or store.scalar("SELECT 1 FROM balances WHERE biz_date=? AND source='ZT_SUMMARY'",
                                                (text,)) is not None
    return {
        "basis": "accrual",
        "income": income, "cost": cost, "profit": income - cost, "lines": lines,
        "cash": {"income": cash_income, "cost": cash_cost, "profit": cash_income - cash_cost, "lines": cash_lines},
        "allocated_in": sorted(allocated_in, key=lambda x: -abs(x["share"]))[:15],
        "deferred": sorted(deferred, key=lambda x: -abs(x["amount"])),
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
