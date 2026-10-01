"""ZT_FLOW: per-record ZT prepaid-account settlements.

Two implementations:
  * ``monitor_db`` — read the upstream advance-payment monitor SQLite (read-only)
  * ``api``        — zto-cli ``advance-payment-balance-record/query``

ZT produces thousands of small settlements per day; the daily P&L comes from
ZT_SUMMARY, so only fund-movement types (withdrawals, top-ups, transfers) and
large single items are stored. Full-day totals are always recorded as a
balance row (source ``ZT_FLOW``) for the completeness check against ZT_SUMMARY.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from cashrecon import dates
from cashrecon.money import to_cents
from cashrecon.sources.base import (
    BalanceRecord,
    FlowRecord,
    SourceBatch,
    SourceError,
    one_line,
    open_readonly,
)
from cashrecon.zto import ZtoClient

DEFAULT_MOVEMENT_TYPES = ("线下提现", "中天余额提现", "中通支付充值", "转账")
INITIATOR = re.compile(r"(\d+(?:\.\d+)+)\s*掌中通发起")
OPERA = {0: "", 1: "红冲", 2: "重发"}


def initiator_of(remark: str) -> str:
    match = INITIATOR.search(remark or "")
    return match.group(1) if match else ""


class _Collector:
    def __init__(self, account: str, day: date, keep_types: tuple[str, ...], large_cents: int) -> None:
        self.batch = SourceBatch("ZT_FLOW", day, flow_sources=("ZT_FLOW",))
        self.account, self.keep_types, self.large_cents = account, keep_types, large_cents
        self.inflow = self.outflow = self.count = 0

    def add(self, *, ref: str, kind: str, signed: int, when: str, remark: str, opera: int | None,
            counterparty: str = "") -> None:
        self.count += 1
        if signed > 0:
            self.inflow += signed
        else:
            self.outflow += -signed
        if signed == 0 or (kind not in self.keep_types and abs(signed) < self.large_cents):
            return
        self.batch.flows.append(FlowRecord(
            source="ZT_FLOW", source_ref=ref, account_code=self.account, biz_time=when,
            direction="IN" if signed > 0 else "OUT", amount_cents=abs(signed),
            counterparty=one_line(counterparty, 60), src_category=kind, summary=one_line(remark),
            initiator=initiator_of(remark), status_text=OPERA.get(opera or 0, str(opera or ""))))

    def finish(self) -> SourceBatch:
        self.batch.balances.append(BalanceRecord(
            self.account, self.batch.day.isoformat(), "ZT_FLOW", None, None, self.inflow, self.outflow,
            f"{self.count} 笔逐笔合计"))
        return self.batch


class ZtFlowMonitorSource:
    code = "ZT_FLOW"

    def __init__(self, path: str, account: str, keep_types: tuple[str, ...] = DEFAULT_MOVEMENT_TYPES,
                 large_cents: int = 100000) -> None:
        if not path:
            raise SourceError("monitor_db not configured")
        self.path, self.account, self.keep_types, self.large_cents = path, account, keep_types, large_cents

    def fetch(self, day: date) -> SourceBatch:
        start, end = dates.window(day)
        collector = _Collector(self.account, day, self.keep_types, self.large_cents)
        conn = open_readonly(self.path)
        try:
            latest = conn.execute("SELECT MAX(balance_time) FROM flows").fetchone()[0] or ""
            if latest < end:
                collector.batch.complete = False
                collector.batch.notes.append(f"监控库最新记录 {latest}，当日可能不完整")
            for r in conn.execute("SELECT balance_no, balance_type, cash_amount, pay_site, rec_site, opera_type, "
                                  "balance_time, raw_json FROM flows WHERE balance_time BETWEEN ? AND ? "
                                  "ORDER BY balance_time, balance_no", (start, end)):
                try:
                    remark = (json.loads(r["raw_json"] or "{}") or {}).get("remark") or ""
                except (TypeError, ValueError):
                    remark = ""
                collector.add(ref=str(r["balance_no"]), kind=str(r["balance_type"]), signed=to_cents(r["cash_amount"]),
                              when=str(r["balance_time"]), remark=str(remark), opera=r["opera_type"],
                              counterparty=str(r["pay_site"] or r["rec_site"] or ""))
        finally:
            conn.close()
        return collector.finish()


class ZtFlowApiSource:
    """Experimental: the upstream endpoint currently answers S500 on some deployments."""

    code = "ZT_FLOW"
    PAGE = 100
    MAX_PAGES = 500

    def __init__(self, client: ZtoClient, account: str, keep_types: tuple[str, ...] = DEFAULT_MOVEMENT_TYPES,
                 large_cents: int = 100000) -> None:
        self.client, self.account, self.keep_types, self.large_cents = client, account, keep_types, large_cents

    def fetch(self, day: date) -> SourceBatch:
        start, end = dates.window(day)
        collector = _Collector(self.account, day, self.keep_types, self.large_cents)
        seen: set[str] = set()
        for page in range(1, self.MAX_PAGES + 1):
            data, route = self.client.data("advance-payment-balance-record", "query", {"request": {
                "beginTime": start, "endTime": end, "currentPage": page, "limit": self.PAGE}})
            collector.batch.route = route
            items = _items(data)
            for item in items:
                ref = str(item.get("balanceNo") or "")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                collector.add(ref=ref, kind=str(item.get("balanceTypeName") or item.get("balanceType") or ""),
                              signed=to_cents(item.get("cashAmount") or 0),
                              when=str(item.get("balanceTime") or start), remark=str(item.get("remark") or ""),
                              opera=item.get("operaType"),
                              counterparty=str(item.get("paySiteName") or item.get("recSiteName") or ""))
            if len(items) < self.PAGE:
                break
        else:
            raise SourceError("zt_flow_pagination_incomplete")
        return collector.finish()


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
    raise SourceError("zt_flow_response_invalid")
