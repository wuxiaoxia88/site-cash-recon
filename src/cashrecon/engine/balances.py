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


def account_view(store: Store, settings: Settings, account: Account, day: date) -> dict[str, Any]:
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
    view: dict[str, Any] = {
        "code": account.code, "name": account.name, "type": account.type_cn, "domain": account.domain,
        "collection": "自动采集" if account.is_auto else "人工登记", "personal_funds": account.personal_funds,
        "source": source, "opening": None, "inflow": None, "outflow": None, "closing": None,
        "calc_closing": None, "checks": [], "status": "MISSING", "note": "",
        "low_balance": settings.low_balance_cents(account),
    }
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
