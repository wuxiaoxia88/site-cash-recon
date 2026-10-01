"""ZT_SUMMARY: daily ZT prepaid-account summary with the portal's category breakdown.

Uses ``advance-payment-flow-summary`` (detail rows incl. 期初/当日发生额/期末 and one
row per portal category) and, when available, ``summary-amount`` as a cross-check.
Scope is fixed to ``excludeSubSite=true`` (this site only, not sub-sites).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cashrecon import dates
from cashrecon.money import to_cents, to_cents_or_none
from cashrecon.sources.base import BalanceRecord, SourceBatch, SourceError
from cashrecon.zto import ZtoClient, ZtoError

TOTAL_ROWS = {"期初余额", "当日发生额", "期末余额"}


def _payload(day: date, detail: bool) -> dict[str, Any]:
    start, end = dates.window(day)
    payload: dict[str, Any] = {
        "oneCategoryCode": "", "secondCategoryCode": "", "descriptionCode": "",
        "excludeSubSite": True, "queryType": 1, "chooseType": 1,
        "settleDateStart": start, "settleDateEnd": end,
    }
    if detail:
        payload.update(pageSize=1000, currentPage=1)
    return payload


def _clean(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text in ("-", "汇总") else text


def parse_detail(rows: Any, account_code: str, day: date) -> SourceBatch:
    if not isinstance(rows, list) or not rows:
        raise SourceError("zt_summary_empty")
    batch = SourceBatch("ZT_SUMMARY", day)
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("list"), list):
            raise SourceError("zt_summary_row_invalid")
        label = str(row.get("descriptionName") or "").strip()
        items = [i for i in row["list"] if isinstance(i, dict)]
        fee = sum(to_cents(i["fee"]) for i in items if i.get("fee") is not None)
        if str(row.get("oneCategoryCode")) == "汇总" and label in TOTAL_ROWS:
            first = items[0] if items else {}
            totals[label] = {"fee": fee,
                             "income": to_cents_or_none(first.get("income")),
                             "expenses": to_cents_or_none(first.get("expenses"))}
            continue
        path = (_clean(row.get("oneCategoryName")) or "未归类", _clean(row.get("secondCategoryName")),
                _clean(row.get("thirdCategoryName")), label or "未命名")
        if fee:
            batch.zt_categories.append((path, fee))
    missing = TOTAL_ROWS - set(totals)
    if missing:
        raise SourceError("zt_summary_totals_missing")
    opening, closing = totals["期初余额"]["fee"], totals["期末余额"]["fee"]
    day_total = totals["当日发生额"]
    inflow = day_total["income"] if day_total["income"] is not None else 0
    outflow = abs(day_total["expenses"]) if day_total["expenses"] is not None else 0
    notes = []
    if opening + day_total["fee"] != closing:
        notes.append("期初+发生额≠期末")
    category_sum = sum(amount for _, amount in batch.zt_categories)
    if category_sum != day_total["fee"]:
        notes.append(f"科目合计与发生额相差{(category_sum - day_total['fee']) / 100:.2f}")
    batch.balances.append(BalanceRecord(account_code, day.isoformat(), "ZT_SUMMARY", opening, closing,
                                        inflow, outflow, "；".join(notes)))
    batch.notes.extend(notes)
    return batch


class ZtSummarySource:
    code = "ZT_SUMMARY"

    def __init__(self, client: ZtoClient, account_code: str) -> None:
        self.client = client
        self.account_code = account_code

    def fetch(self, day: date) -> SourceBatch:
        rows, route = self.client.data("advance-payment-flow-summary", "advance-payment-flow-summary",
                                       _payload(day, detail=True))
        batch = parse_detail(rows, self.account_code, day)
        batch.route = route
        try:
            amounts, _ = self.client.data("advance-payment-flow-summary", "summary-amount", _payload(day, False))
        except ZtoError:
            batch.notes.append("summary-amount 不可用，仅用明细汇总")
            return batch
        if isinstance(amounts, dict) and amounts.get("totalAmount") is not None:
            total = to_cents(amounts["totalAmount"])
            balance = batch.balances[0]
            if balance.closing_cents - balance.opening_cents != total:
                batch.notes.append("summary-amount 合计与余额变动不一致")
                balance.note = "；".join(filter(None, [balance.note, "summary-amount 不一致"]))
        return batch
