"""View models for daily and period reports (pure data, no HTML)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from cashrecon import dates
from cashrecon.analysis import ActionItem, action_items, analysis, headline, load_history, month_to_date
from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.engine import categories
from cashrecon.reports import charts

TITLES = {"daily": "网点资金日报", "weekly": "网点资金周报", "monthly": "网点资金月报",
          "monthly_final": "网点资金月报（定稿）"}


class ReportError(RuntimeError):
    pass


def _payloads(store: Store, start: date, end: date) -> dict[str, dict[str, Any]]:
    rows = store.query("SELECT biz_date, payload FROM daily_results WHERE biz_date BETWEEN ? AND ? ORDER BY biz_date",
                       (start.isoformat(), end.isoformat()))
    return {r["biz_date"]: json.loads(r["payload"]) for r in rows}


def _kb_link(settings: Settings, cadence: str, period: str) -> str | None:
    kb = settings.delivery.get("kb", {})
    base = kb.get("public_base_url")
    if not kb.get("enabled") or not base:
        return None
    prefix = kb.get("destination_prefix", "internal/reports/cash-recon").strip("/")
    return f"{base.rstrip('/')}/{prefix}/{settings.site_slug}/{cadence}/{period}/"


def daily_view(store: Store, settings: Settings, day: date) -> dict[str, Any]:
    payload = _payloads(store, day, day).get(day.isoformat())
    if payload is None:
        raise ReportError(f"{day} 没有日结果，请先运行 cashrecon recon --date {day}")
    history = load_history(store, day)
    items = action_items(payload, settings, history)
    previous = history[-1] if history and history[-1]["day"] == (day - timedelta(days=1)).isoformat() else None
    series = (history + [payload])[-30:]
    labels = [p["day"][5:] for p in series]
    profit = payload["profit"]
    income_lines = [x for x in profit["lines"] if x["kind"] == "income"]
    cost_lines = [x for x in profit["lines"] if x["kind"] == "cost"]
    view = {
        "cadence": "daily",
        "title": TITLES["daily"],
        "site": settings.site_name,
        "period_label": f"{payload['day']}（{payload['weekday']}）",
        "period_key": payload["day"],
        "generated_at": payload["generated_at"],  # data computation time: same data => identical report
        "p": payload,
        "headline": headline(payload, items, history),
        "mtd": month_to_date(payload, history),
        "items": [i.to_dict() for i in items],
        "analysis": analysis(payload, history, settings),
        "previous": previous,
        "delta": {
            "profit": None if previous is None else profit["profit"] - previous["profit"]["profit"],
            "position": None if previous is None else payload["position"]["total"] - previous["position"]["total"],
        },
        "income_lines": income_lines,
        "cost_lines": cost_lines,
        "chart_profit": charts.bar_chart(labels, [p["profit"]["profit"] for p in series], title="近 30 日经营利润"),
        "chart_position": charts.line_chart(labels, [p["position"]["total"] for p in series], title="近 30 日现金头寸"),
        "kb_link": _kb_link(settings, "daily", payload["day"]),
    }
    return view


def _aggregate(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    lines: dict[str, dict[str, int]] = defaultdict(lambda: {"zt": 0, "offline": 0, "total": 0})
    movements: dict[str, int] = defaultdict(int)
    unclassified = {"in": 0, "out": 0, "zt": 0, "count": 0}
    bill = {"income_cents": 0, "expense_cents": 0, "profit_cents": 0, "days": 0}
    initiators: dict[str, dict[str, Any]] = {}
    duplicates = transfers = 0
    for p in payloads:
        for x in p["profit"]["lines"]:
            for k in ("zt", "offline", "total"):
                lines[x["code"]][k] += x[k]
        for k, v in p["profit"]["movements"].items():
            movements[k] += v
        for k in unclassified:
            unclassified[k] += p["profit"]["unclassified"].get(k, 0)
        if p.get("bill_profit"):
            bill["days"] += 1
            for k in ("income_cents", "expense_cents", "profit_cents"):
                bill[k] += p["bill_profit"][k]
        for w in p["movements"].get("withdrawals_by_initiator") or []:
            entry = initiators.setdefault(w["initiator"], {"initiator": w["initiator"], "count": 0, "amount": 0,
                                                          "landed": 0, "mapped": w.get("mapped", ""),
                                                          "label": w.get("label") or w["initiator"]})
            for k in ("count", "amount", "landed"):
                entry[k] += w[k]
        duplicates += p["recon"].get("duplicates") or 0
        transfers += len(p["movements"].get("transfers") or [])
    result_lines = [{"code": c, "name": categories.name(c), "kind": categories.kind(c), **v}
                    for c, v in sorted(lines.items(), key=lambda kv: categories.ORDER.get(kv[0], 999))]
    income = sum(x["total"] for x in result_lines if x["kind"] == "income")
    cost = -sum(x["total"] for x in result_lines if x["kind"] == "cost")
    return {"lines": result_lines, "income": income, "cost": cost, "profit": income - cost,
            "movements": dict(movements), "movements_cn": {k: categories.name(k) for k in movements},
            "unclassified": unclassified, "bill": bill if bill["days"] else None,
            "withdrawals": sorted(initiators.values(), key=lambda x: -x["amount"]),
            "duplicates": duplicates, "transfers": transfers}


def _accounts_over(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for p in payloads:
        for a in p["accounts"]:
            entry = by_code.setdefault(a["code"], {"code": a["code"], "name": a["name"], "type": a["type"],
                                                   "collection": a["collection"], "personal_funds": a["personal_funds"],
                                                   "opening": a["opening"], "inflow": 0, "outflow": 0,
                                                   "closing": None, "min": None, "max": None,
                                                   "status_days": defaultdict(int)})
            entry["inflow"] += a["inflow"] or 0
            entry["outflow"] += a["outflow"] or 0
            if a["closing"] is not None:
                entry["closing"] = a["closing"]
                entry["min"] = a["closing"] if entry["min"] is None else min(entry["min"], a["closing"])
                entry["max"] = a["closing"] if entry["max"] is None else max(entry["max"], a["closing"])
            if entry["opening"] is None:
                entry["opening"] = a["opening"]
            entry["status_days"][a["status_cn"]] += 1
    for entry in by_code.values():
        entry["status_days"] = dict(entry["status_days"])
    return list(by_code.values())


def period_view(store: Store, settings: Settings, cadence: str, start: date, end: date) -> dict[str, Any]:
    payloads_by_day = _payloads(store, start, end)
    all_days = [d.isoformat() for d in dates.day_range(start, end)]
    present = [payloads_by_day[d] for d in all_days if d in payloads_by_day]
    if not present:
        raise ReportError(f"{start}～{end} 没有任何日结果")
    agg = _aggregate(present)
    agg["cash_profit"] = sum(p["profit"].get("cash", {}).get("profit", p["profit"]["profit"]) for p in present)
    length = (end - start).days + 1
    prev_start = start - timedelta(days=length) if cadence == "weekly" else (start - timedelta(days=1)).replace(day=1)
    prev_end = start - timedelta(days=1)
    prev_payloads = list(_payloads(store, prev_start, prev_end).values())
    prev = _aggregate(prev_payloads) if prev_payloads else None
    last = present[-1]
    history = load_history(store, date.fromisoformat(last["day"]))
    items: list[ActionItem] = action_items(last, settings, history)
    loss_days = [p["day"] for p in present if p["profit"]["complete"] and p["profit"]["profit"] < 0]
    labels = [p["day"][5:] for p in present]
    prev_lines = {x["code"]: x["total"] for x in prev["lines"]} if prev else {}
    for x in agg["lines"]:
        x["prev"] = prev_lines.get(x["code"])
        x["change"] = None if x["prev"] is None else x["total"] - x["prev"]
    status_days = defaultdict(int)
    for p in present:
        status_days[p["data_status"]] += 1
    label = (f"{start.isoformat()} 至 {end.isoformat()}" if cadence == "weekly" else f"{start.year} 年 {start.month} 月")
    period_key = f"{start.isoformat()}_{end.isoformat()}" if cadence == "weekly" else start.strftime("%Y-%m")
    if cadence in ("monthly", "monthly_final") and agg["profit"] < 0:
        items.insert(0, ActionItem("high", "month_loss", f"{label}经营亏损 {abs(agg['profit']) / 100:,.2f} 元",
                                   amount=agg["profit"], key="month_loss",
                                   action="对照利润表逐项核实成本，重点看人工、面单物料与派费"))
    note = ""
    if cadence == "monthly":
        note = ("本报告为初版：直营员工工资、驿站派费通常在次月 20 日左右登记并计入本月，"
                "次月 25 日将发送定稿版。")
    elif cadence == "monthly_final":
        note = "定稿版：已包含次月 20 日前后登记的本月工资与派费。之后补登的款项仍会更新系统数据。"
    view = {
        "cadence": cadence,
        "title": TITLES[cadence],
        "site": settings.site_name,
        "period_label": label,
        "period_key": period_key,
        "generated_at": max(p["generated_at"] for p in present),
        "start": start.isoformat(), "end": end.isoformat(),
        "days_total": len(all_days),
        "days_present": len(present),
        "missing_days": [d for d in all_days if d not in payloads_by_day],
        "status_days": dict(status_days),
        "agg": agg, "prev": prev,
        "delta_profit": None if prev is None else agg["profit"] - prev["profit"],
        "loss_days": loss_days,
        "best_day": max(present, key=lambda p: p["profit"]["profit"])["day"],
        "worst_day": min(present, key=lambda p: p["profit"]["profit"])["day"],
        "daily": [{"day": p["day"], "weekday": p["weekday"], "profit": p["profit"]["profit"],
                   "income": p["profit"]["income"], "cost": p["profit"]["cost"],
                   "position": p["position"]["total"], "status": p["data_status"],
                   "review": len(p["recon"]["review"])} for p in present],
        "accounts": _accounts_over(present),
        "position_end": last["position"],
        "items": [i.to_dict() for i in items],
        "chart_profit": charts.bar_chart(labels, [p["profit"]["profit"] for p in present], title="每日经营利润"),
        "chart_position": charts.line_chart(labels, [p["position"]["total"] for p in present], title="现金头寸"),
        "kb_link": _kb_link(settings, cadence, period_key),
        "note": note,
    }
    days = view["days_present"]
    word = "盈利" if agg["profit"] >= 0 else "亏损"
    view["headline"] = (f"{label}经营{word} {abs(agg['profit']) / 100:,.2f} 元（{days} 天数据），"
                        f"亏损 {len(loss_days)} 天；期末现金头寸 {last['position']['total'] / 100:,.2f} 元。")
    if view["missing_days"]:
        view["headline"] += f" 缺少 {len(view['missing_days'])} 天日结果。"
    return view
