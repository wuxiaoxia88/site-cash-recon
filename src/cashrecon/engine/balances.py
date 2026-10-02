"""Per-account daily balance views and checks."""

from __future__ import annotations

from datetime import date
from typing import Any

from cashrecon.config import Account, Settings
from cashrecon.db import Store

STATUS_CN = {
    "MATCH": "已核对",
    "DIFF": "有差额",
    "BOOK_ONLY": "仅账面（无独立对照）",
    "MISSING": "数据缺失",
}

AUTO_SOURCE_OF = {"CORP_BANK": "BANK_SMS", "BANK_CARD": "ICBC", "ALIPAY": "ALIPAY"}


def _balance(store: Store, day: str, account: str, source: str) -> dict[str, Any] | None:
    row = store.one("SELECT * FROM balances WHERE biz_date = ? AND account_code = ? AND source = ?",
                    (day, account, source))
    return dict(row) if row else None


def _last_before(store: Store, day: str, account: str, source: str) -> dict[str, Any] | None:
    row = store.one("SELECT * FROM balances WHERE biz_date < ? AND account_code = ? AND source = ? "
                    "AND closing_cents IS NOT NULL ORDER BY biz_date DESC LIMIT 1", (day, account, source))
    return dict(row) if row else None


def _primary_source(account: Account, settings: Settings) -> str:
    if account.type == "ZT_PREPAY":
        return "ZT_SUMMARY"
    if account.is_auto:
        ledger = settings.source("BANK_LEDGER")
        if ledger.get("corp_account") == account.code:
            return "BANK_SMS"
        if ledger.get("icbc_account") == account.code:
            return "ICBC"
        if ledger.get("alipay_account") == account.code:
            return "ALIPAY"
        return AUTO_SOURCE_OF.get(account.type, "JOURNAL")
    return "JOURNAL"


def _base_view(settings: Settings, account: Account, source: str) -> dict[str, Any]:
    return {
        "code": account.code, "name": account.name, "type": account.type_cn, "domain": account.domain,
        "collection": {"auto": "自动采集", "manual": "人工登记", "derived": "系统推算"}[account.collection],
        "personal_funds": account.personal_funds, "source": source, "opening": None, "inflow": None,
        "outflow": None, "closing": None, "calc_closing": None, "checks": [], "status": "MISSING",
        "status_cn": STATUS_CN["MISSING"], "note": "", "low_balance": settings.low_balance_cents(account),
        "below_low": False, "carried": False, "diff_reason": "", "last_manual_check": None,
    }


def derived_view(store: Store, settings: Settings, account: Account, day: date) -> dict[str, Any]:
    """Virtual account (e.g. 余额宝): balance = first manual balance + derived movements since then."""
    view = _base_view(settings, account, "DERIVED")
    text = day.isoformat()
    day_end = f"{text} 23:59:59"
    moves = store.query("SELECT biz_time, direction, amount_cents FROM flows WHERE account_code = ? "
                        "AND source = 'DERIVED' AND removed = 0 ORDER BY biz_time", (account.code,))
    today = [m for m in moves if m["biz_time"][:10] == text]
    view["inflow"] = sum(m["amount_cents"] for m in today if m["direction"] == "IN")
    view["outflow"] = sum(m["amount_cents"] for m in today if m["direction"] == "OUT")
    entries = store.query("SELECT as_of, balance_cents FROM manual_balances WHERE account_code = ? ORDER BY as_of",
                          (account.code,))
    if not entries or entries[0]["as_of"] > day_end:
        view["note"] = "尚无起点余额：请在控制台“余额录入”填写一次实际余额，之后系统按转入转出自动推算"
        return view
    anchor = entries[0]

    def book(at: str) -> int:
        net = sum((m["amount_cents"] if m["direction"] == "IN" else -m["amount_cents"]) for m in moves
                  if anchor["as_of"] < m["biz_time"][:16] and m["biz_time"][:16] <= at[:16])
        return anchor["balance_cents"] + net

    closing = book(day_end)
    view.update(closing=closing, opening=closing - view["inflow"] + view["outflow"],
                calc_closing=closing, note=f"以 {anchor['as_of']} 录入余额为起点推算（收益未计入，会体现为差额）")
    checks = [e for e in entries[1:] if e["as_of"][:10] == text]
    for e in checks:
        expected = book(e["as_of"])
        view["checks"].append({"label": f"人工核对实际余额（{e['as_of']}）", "value": e["balance_cents"],
                               "diff": e["balance_cents"] - expected, "kind": "independent"})
    if any(c["diff"] for c in view["checks"]):
        view["status"], view["diff_reason"] = "DIFF", "实际余额与推算不一致（常见原因：余额宝收益）"
    elif view["checks"] or anchor["as_of"][:10] == text:
        view["status"] = "MATCH"
    else:
        view["status"] = "BOOK_ONLY"
    view["status_cn"] = STATUS_CN[view["status"]]
    view["last_manual_check"] = dict(entries[-1]) if entries[-1]["as_of"] <= day_end else dict(anchor)
    view["below_low"] = False
    return view


def account_view(store: Store, settings: Settings, account: Account, day: date) -> dict[str, Any]:
    if account.is_derived:
        return derived_view(store, settings, account, day)
    text = day.isoformat()
    source = _primary_source(account, settings)
    primary = _balance(store, text, account.code, source)
    carried = False
    if primary is None and source == "JOURNAL":
        previous = _last_before(store, text, account.code, source)
        if previous is not None:
            primary = {"opening_cents": previous["closing_cents"], "closing_cents": previous["closing_cents"],
                       "inflow_cents": 0, "outflow_cents": 0, "note": "当日无登记，沿用前日余额"}
            carried = True
    view = _base_view(settings, account, source)
    if primary is None:
        view["note"] = "该账户当日没有数据"
        return view
    opening, closing = primary.get("opening_cents"), primary.get("closing_cents")
    inflow, outflow = primary.get("inflow_cents") or 0, primary.get("outflow_cents") or 0
    view.update(opening=opening, inflow=inflow, outflow=outflow, closing=closing, note=primary.get("note") or "",
                carried=carried)
    if opening is not None:
        view["calc_closing"] = opening + inflow - outflow
    diffs = []
    independent = 0
    if view["calc_closing"] is not None and closing is not None:
        ok = view["calc_closing"] == closing
        label = {"ZT_SUMMARY": "门户期初+发生额=期末", "JOURNAL": "日记账期初+收支=期末"}.get(
            source, "逐笔余额链连续")
        view["checks"].append({"label": label, "value": view["calc_closing"], "diff": closing - view["calc_closing"],
                               "kind": "equation"})
        if not ok:
            diffs.append("计算期末与来源期末不一致")
        elif source in ("ZT_SUMMARY", "BANK_SMS", "ICBC", "ALIPAY") and not primary.get("note"):
            independent += 1
    # independent / informational references
    refs: list[tuple[str, str, str]] = []
    if account.is_auto and source != "ZT_SUMMARY":
        refs.append(("BANK_RECON", "银行采集日对账期末", "independent"))
        if account.portal_code:
            refs.append(("JOURNAL", "门户日记账账户余额", "info"))
    for ref_source, label, kind in refs:
        ref = _balance(store, text, account.code, ref_source)
        if ref is None or ref.get("closing_cents") is None or closing is None:
            continue
        diff = ref["closing_cents"] - closing
        view["checks"].append({"label": label, "value": ref["closing_cents"], "diff": diff, "kind": kind})
        if kind == "independent":
            if diff:
                diffs.append(f"{label}相差")
            else:
                independent += 1
    if source == "ZT_SUMMARY":
        flow_totals = _balance(store, text, account.code, "ZT_FLOW")
        if flow_totals and flow_totals.get("inflow_cents") is not None:
            net_flow = flow_totals["inflow_cents"] - flow_totals["outflow_cents"]
            view["checks"].append({"label": "逐笔流水净额（含下级/时差，仅参考）", "value": net_flow,
                                   "diff": net_flow - (inflow - outflow), "kind": "info"})
    manual = store.one("SELECT * FROM manual_balances WHERE account_code = ? AND substr(as_of, 1, 10) = ? "
                       "ORDER BY as_of DESC LIMIT 1", (account.code, text))
    if manual is not None and closing is not None:
        diff = manual["balance_cents"] - closing
        view["checks"].append({"label": f"人工核对实际余额（{manual['as_of']}）", "value": manual["balance_cents"],
                               "diff": diff, "kind": "independent"})
        if diff:
            diffs.append("人工核对余额相差")
        else:
            independent += 1
    latest_manual = store.one("SELECT as_of, balance_cents FROM manual_balances WHERE account_code = ? "
                              "AND substr(as_of, 1, 10) <= ? ORDER BY as_of DESC LIMIT 1", (account.code, text))
    view["last_manual_check"] = dict(latest_manual) if latest_manual else None
    if diffs:
        view["status"], view["diff_reason"] = "DIFF", "；".join(diffs)
    elif independent:
        view["status"] = "MATCH"
    else:
        view["status"] = "BOOK_ONLY"
    view["status_cn"] = STATUS_CN[view["status"]]
    view["below_low"] = closing is not None and closing < view["low_balance"]
    return view


def account_views(store: Store, settings: Settings, day: date) -> list[dict[str, Any]]:
    views = []
    for account in settings.accounts:
        if not account.active:
            continue
        view = account_view(store, settings, account, day)
        view["status_cn"] = STATUS_CN[view["status"]]
        views.append(view)
    return views


def position(views: list[dict[str, Any]]) -> dict[str, Any]:
    known = [v for v in views if v["closing"] is not None]
    return {
        "total": sum(v["closing"] for v in known),
        "verified": sum(v["closing"] for v in known if v["status"] == "MATCH"),
        "unverified": sum(v["closing"] for v in known if v["status"] != "MATCH"),
        "personal_funds": sum(v["closing"] for v in known if v["personal_funds"]),
        "missing_accounts": [v["name"] for v in views if v["closing"] is None],
        "online": sum(v["closing"] for v in known if v["domain"] == "ONLINE"),
        "offline": sum(v["closing"] for v in known if v["domain"] == "OFFLINE"),
    }
