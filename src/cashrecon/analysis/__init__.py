"""Insights derived from daily results: action items, headline and rule-based analysis.

Everything here only quotes numbers present in the daily result payloads, so the
text can be traced back to data (no invented figures).
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.money import fmt_yuan, to_cents

LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}
LEVEL_CN = {"high": "紧急", "medium": "重要", "low": "提示"}
KIND_ORDER = {"source_missing": 0, "loss": 1, "balance_diff": 2, "low_balance": 3, "bill_loss": 4, "withdraw_unknown": 4,
              "topup_unmatched": 5, "journal_unmatched": 6, "unmapped_account": 7}


@dataclass
class ActionItem:
    level: str
    kind: str
    title: str
    detail: str = ""
    action: str = ""
    amount: int | None = None
    count: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)
    key: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["level_cn"] = LEVEL_CN[self.level]
        return data


def month_to_date(payload: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    """Operating (business-period) totals from the 1st of the month up to this payload's day."""
    month = payload["day"][:7]
    days = [p for p in history if p["day"][:7] == month and p["day"] < payload["day"]] + [payload]
    income = sum(p["profit"]["income"] for p in days)
    cost = sum(p["profit"]["cost"] for p in days)
    return {"income": income, "cost": cost, "profit": income - cost, "days": len(days)}


def load_history(store: Store, day: date, days: int = 35) -> list[dict[str, Any]]:
    start = (day - timedelta(days=days)).isoformat()
    rows = store.query("SELECT payload FROM daily_results WHERE biz_date >= ? AND biz_date < ? ORDER BY biz_date",
                       (start, day.isoformat()))
    return [json.loads(r["payload"]) for r in rows]


def _sample(row: dict[str, Any]) -> dict[str, Any]:
    return {"date": row["biz_date"], "time": row["biz_time"][11:16], "account": row["account_name"],
            "direction": "收" if row["direction"] == "IN" else "付", "amount": row["amount_cents"],
            "text": row["src_category"] or row["summary"] or row["counterparty"], "reason": row["reason"],
            "flow_id": row["flow_id"], "initiator": row["initiator"]}


def action_items(payload: dict[str, Any], settings: Settings, history: list[dict[str, Any]]) -> list[ActionItem]:
    day = payload["day"]
    items: list[ActionItem] = []
    alerts = settings.alerts
    # data completeness
    for src in payload["sources"]:
        if src["status"] in ("failed", "never"):
            items.append(ActionItem(
                "high" if src["required"] else "medium", "source_missing", f"数据来源缺失：{src['name']}",
                detail=src["note"] or "", key=f"source:{src['code']}",
                action="检查 zto-cli 与门户登录状态、上游采集任务后，在控制台对该日期重新采集"))
        elif src.get("incomplete") or src.get("anomaly"):
            items.append(ActionItem("low", "source_partial", f"数据可能不完整：{src['name']}",
                                    detail=src.get("anomaly") or src["note"], key=f"partial:{src['code']}",
                                    action="上游采集完成后重跑当日（18:00 补跑会自动处理）"))
    # loss: (a) bill-basis daily streak, (b) month-to-date operating loss once payroll is registered
    profit = payload["profit"]
    if alerts.get("loss", True):
        streak_days = int(alerts.get("loss_streak_days", 3))
        bills = [p.get("bill_profit") for p in history + [payload]]
        streak = 0
        for bill in reversed(bills):
            if bill and bill.get("profit_cents") is not None and bill["profit_cents"] < 0:
                streak += 1
            else:
                break
        if streak >= streak_days:
            items.append(ActionItem(
                "medium", "bill_loss", f"账单口径已连续 {streak} 天亏损",
                detail=f"今日账单口径利润 {fmt_yuan(bills[-1]['profit_cents'])} 元（进港/出港/返利账单，"
                       f"未含线下工资房租等）", amount=bills[-1]["profit_cents"], key="bill_loss",
                action="查看进港派费收入与出港账单费用的变化，确认是否有异常扣费或单价变化"))
        mtd = month_to_date(payload, history)
        check_day = int(alerts.get("mtd_loss_from_day", 25))
        if mtd["profit"] < 0 and date.fromisoformat(day).day >= check_day:
            items.append(ActionItem(
                "high", "loss", f"本月至今经营亏损 {fmt_yuan(-mtd['profit'])} 元",
                detail=f"本月 {mtd['days']} 天：收入 {fmt_yuan(mtd['income'])}，成本 {fmt_yuan(mtd['cost'])}",
                amount=mtd["profit"], key="loss",
                action="对照利润表核实大额成本是否合理、收入是否漏记；月结客户回款请登记业务发生时间"))
    # balances
    diff_floor = to_cents(alerts.get("balance_diff_yuan", 100))
    for acc in payload["accounts"]:
        if acc["status"] == "DIFF":
            diffs = [c for c in acc["checks"] if c["diff"] and c["kind"] != "info"]
            biggest = max((abs(c["diff"]) for c in diffs), default=0)
            text = "；".join(f"{c['label']} 差 {fmt_yuan(c['diff'], sign=True)}" for c in diffs)
            items.append(ActionItem(
                "high" if biggest >= diff_floor else "medium", "balance_diff", f"{acc['name']} 余额核对有差额",
                detail=text, amount=biggest, key=f"balance:{acc['code']}",
                action="常见原因：短信/账单漏采、日记账漏记或重复；可在控制台查看当日逐笔流水"))
        if acc["closing"] is not None and acc.get("below_low"):
            severe = acc["domain"] == "ONLINE" and acc["closing"] < acc["low_balance"] // 2
            items.append(ActionItem(
                "high" if severe else "medium", "low_balance", f"{acc['name']} 余额 {fmt_yuan(acc['closing'])} 元，低于 {fmt_yuan(acc['low_balance'])} 元",
                amount=acc["closing"], key=f"low:{acc['code']}",
                action="中天主账户请及时充值，避免结算扣款失败" if acc["domain"] == "ONLINE" else "请关注资金安排"))
    # review groups
    review = payload["recon"]["review"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in review:
        group_key = "*" if row.get("kind") == "withdraw_unknown" else row["account_code"]
        groups[(row.get("kind") or "other", group_key)].append(row)
    spec = {
        "withdraw_unknown": ("medium", "中天提现去向待确认", "在控制台把该发起人绑定到收款账户，或标记为工资/承包区结算等用途"),
        "topup_unmatched": ("medium", "付款给中通总部但未见中天充值", "确认这笔付款的用途（面单/物料/保证金等），在控制台归类"),
        "journal_unmatched": ("medium", "日记账登记未在银行/支付宝流水中找到", "核实是否登记错误、测试数据，或银行短信漏采"),
        "contradiction": ("low", "日记账科目与收付方向矛盾", "在门户更正登记，或在控制台指定正确科目"),
        "adjust": ("low", "日记账“余额修改”记录", "确认修改原因"),
        "transfer_out_unknown": ("low", "转出至未纳入系统的账户", "如属店主提取或个人转账，可在控制台批量标记"),
        "transfer_in_unknown": ("low", "转入来源不明", "确认资金来源后在控制台标记"),
        "unmapped_account": ("medium", "日记账出现未配置的账户", "在配置中登记该账户"),
        "manual_broken": ("low", "人工结论失效", "重新处理该记录"),
        "other": ("low", "其他待核记录", "在控制台逐条处理"),
    }
    single_floor = to_cents(alerts.get("unmatched_single_yuan", 500))
    for (kind, group_key), rows in groups.items():
        level, title, action = spec.get(kind, spec["other"])
        total = sum(r["amount_cents"] for r in rows)
        if kind in ("journal_unmatched", "transfer_out_unknown", "transfer_in_unknown") and \
                any(r["amount_cents"] >= single_floor for r in rows) and level == "low":
            level = "medium"
        detail = f"共 {len(rows)} 笔，合计 {fmt_yuan(total)} 元"
        if kind == "withdraw_unknown":
            by_who: dict[str, int] = defaultdict(int)
            for r in rows:
                by_who[r["initiator"] or "未知"] += r["amount_cents"]
            detail += "；按发起人：" + "，".join(f"{who} {fmt_yuan(v)}" for who, v in
                                              sorted(by_who.items(), key=lambda kv: -kv[1]))
            label_title = title
        else:
            label_title = f"{title}（{rows[0]['account_name']}）"
        items.append(ActionItem(level, kind, label_title, amount=total, count=len(rows), action=action,
                                detail=detail, samples=[_sample(r) for r in rows[:8]],
                                key=f"review:{kind}:{group_key}"))
    # unclassified share
    ratio = profit["unclassified"]["ratio"]
    if ratio >= float(alerts.get("unclassified_ratio", 0.05)):
        items.append(ActionItem("low", "unclassified", f"待分类金额占比 {ratio:.0%}",
                                detail=f"{profit['unclassified']['count']} 项未能归入利润科目", key="unclassified",
                                action="在控制台为这些记录补充分类规则，利润会更准确"))
    # weekly manual balance check
    weekday = date.fromisoformat(day).weekday()
    for acc in payload["accounts"]:
        if acc["collection"] == "系统推算" and acc["closing"] is None:
            items.append(ActionItem("low", "fund_anchor", f"请录入一次{acc['name']}的实际余额",
                                    detail="系统按转入转出推算余额，需要一个起点", key=f"anchor:{acc['code']}",
                                    action="在控制台“余额录入”选择该账户，填写当前余额和时间"))
    manual_accounts = [a for a in payload["accounts"] if a["collection"] in ("人工登记", "系统推算")
                       and a["closing"] is not None]
    stale = []
    for acc in manual_accounts:
        last = acc.get("last_manual_check")
        if not last or (date.fromisoformat(day) - date.fromisoformat(last["as_of"][:10])).days > 7:
            stale.append(acc["name"])
    if stale and weekday >= 4:
        items.append(ActionItem("low", "manual_check", f"本周末请核对人工账户实际余额（{len(stale)} 个）",
                                detail="、".join(stale), key="manual_check",
                                action="在控制台“余额录入”填写实际余额和时间，系统会自动比对"))
    items.sort(key=lambda x: (LEVEL_ORDER[x.level], KIND_ORDER.get(x.kind, 50), -(abs(x.amount or 0))))
    return items


def headline(payload: dict[str, Any], items: list[ActionItem], history: list[dict[str, Any]] | None = None) -> str:
    profit = payload["profit"]
    parts = []
    if profit["complete"]:
        mtd = month_to_date(payload, history or [])
        word = "盈利" if mtd["profit"] >= 0 else "亏损"
        parts.append(f"本月至今经营{word} {fmt_yuan(abs(mtd['profit']))} 元")
        parts.append(f"当日 {fmt_yuan(profit['profit'], sign=True)} 元")
    else:
        parts.append("中天数据缺失，利润不完整")
    parts.append(f"现金头寸 {fmt_yuan(payload['position']['total'])} 元")
    urgent = sum(1 for i in items if i.level == "high")
    important = sum(1 for i in items if i.level == "medium")
    if urgent or important:
        parts.append(f"{urgent} 件紧急、{important} 件重要事项待处理" if urgent else f"{important} 件重要事项待处理")
    else:
        parts.append("无紧急事项")
    if payload["data_status"] != "OK":
        parts.append("部分数据不完整")
    return "，".join(parts) + "。"


def _avg(values: list[int]) -> float | None:
    return statistics.mean(values) if values else None


def analysis(payload: dict[str, Any], history: list[dict[str, Any]], settings: Settings) -> list[dict[str, str]]:
    """Rule-based financial commentary (财务负责人视角)."""
    out: list[dict[str, str]] = []
    profit = payload["profit"]
    recent = [h for h in history[-7:] if h["profit"]["complete"]]
    if profit["complete"]:
        avg = _avg([h["profit"]["profit"] for h in recent])
        mtd = month_to_date(payload, history)
        text = f"本月至今经营利润 {fmt_yuan(mtd['profit'], sign=True)} 元（{mtd['days']} 天）。" \
               f"当日按业务期间计经营利润 {fmt_yuan(profit['profit'], sign=True)} 元（收入 {fmt_yuan(profit['income'])}，" \
               f"成本 {fmt_yuan(profit['cost'])}），按收付日计 {fmt_yuan(profit['cash']['profit'], sign=True)} 元。"
        if avg is not None:
            delta = profit["profit"] - avg
            text += f"近 7 日日均 {fmt_yuan(round(avg), sign=True)} 元，今日{'高' if delta >= 0 else '低'}于均值 " \
                    f"{fmt_yuan(round(abs(delta)))} 元。"
        if profit["income"]:
            text += f"成本收入比 {profit['cost'] / profit['income']:.0%}。"
        out.append({"title": "经营概况", "text": text})
        # drivers vs 7-day average per category
        if recent:
            base: dict[str, list[int]] = defaultdict(list)
            for h in recent:
                seen = {x["code"]: x["total"] for x in h["profit"]["lines"]}
                for code in {x["code"] for x in profit["lines"]} | set(seen):
                    base[code].append(seen.get(code, 0))
            names = {x["code"]: x["name"] for h in recent for x in h["profit"]["lines"]}
            names.update({x["code"]: x["name"] for x in profit["lines"]})
            today = {x["code"]: x["total"] for x in profit["lines"]}
            changes = sorted(((code, today.get(code, 0) - statistics.mean(vals)) for code, vals in base.items()),
                             key=lambda x: -abs(x[1]))[:3]
            parts = [f"{names.get(code, code)}使利润{'增加' if d > 0 else '减少'} {fmt_yuan(round(abs(d)))} 元"
                     for code, d in changes if abs(d) >= 100]
            if parts:
                out.append({"title": "主要变化（较近 7 日均值）", "text": "；".join(parts) + "。"})
    # cash and ZT runway
    zt = next((a for a in payload["accounts"] if a["domain"] == "ONLINE"), None)
    if zt and zt["closing"] is not None:
        nets = [ (h_zt["closing"] - h_zt["opening"]) for h in history[-7:]
                 for h_zt in [next((a for a in h["accounts"] if a["domain"] == "ONLINE"), None)]
                 if h_zt and h_zt["closing"] is not None and h_zt["opening"] is not None]
        topups = [h["profit"]["movements"].get("XFER_ZT_TOPUP", 0) for h in history[-7:]]
        if nets:
            burn = statistics.mean(n - t for n, t in zip(nets, topups, strict=False))
            text = f"中天主账户期末 {fmt_yuan(zt['closing'])} 元。"
            if burn < 0:
                days = zt["closing"] / -burn
                text += f"剔除充值后近 7 日日均净消耗 {fmt_yuan(round(-burn))} 元，按此估算余额约可支撑 {days:.1f} 天。"
            out.append({"title": "资金状况", "text": text + f"全部账户现金头寸 {fmt_yuan(payload['position']['total'])} 元，"
                        f"其中已独立核对 {fmt_yuan(payload['position']['verified'])} 元。"})
    # large manual entries and concentration
    big = payload["recon"].get("large_items") or []
    if big:
        out.append({"title": "大额单笔收付", "text": "；".join(
            f"{r['account']}{'收' if r['direction'] == 'IN' else '付'} {fmt_yuan(r['amount'])} 元（{r['text']}）"
            for r in big[:4]) + "。现金口径按实际收付计入，大额回款或集中付款会让单日利润明显波动，"
                                "请结合周报、月报判断经营趋势；人工登记的大额记录请确认确为当日实际收付。"})
    withdrawals = payload["movements"].get("withdrawals_by_initiator") or []
    if withdrawals:
        total = sum(w["amount"] for w in withdrawals)
        names = "、".join(f"{w.get('label') or w['initiator']} {fmt_yuan(w['amount'])}" for w in withdrawals[:5])
        out.append({"title": "承包区/业务员提现", "text": f"当日通过中天预付款账户提现 {fmt_yuan(total)} 元，"
                    f"计入人工成本：{names}。"})
    deferred = profit.get("deferred") or []
    if deferred:
        out.append({"title": "计入其他期间的收付", "text": "；".join(
            f"{d['account']}{'收' if d['amount'] > 0 else '付'} {fmt_yuan(abs(d['amount']))} 元（{d['text']}）计入 {d['period']}"
            for d in deferred[:4]) + "。这些款项按业务发生期间计入利润，不影响当日。"})
    if profit["complete"] and date.fromisoformat(payload["day"]).day < 25:
        out.append({"title": "说明", "text": "直营员工工资、驿站派费通常在次月 20 日左右登记并计入本月，"
                    "届时本月利润会相应下调；月结客户回款请在日记账登记业务发生时间，系统会计入对应月份。"})
    return out
