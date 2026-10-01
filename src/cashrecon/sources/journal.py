"""JOURNAL: portal site-journal (网点日记账) records and per-account daily summary."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from cashrecon import dates
from cashrecon.config import Settings
from cashrecon.money import to_cents, to_cents_or_none
from cashrecon.sources.base import BalanceRecord, FlowRecord, SourceBatch, SourceError, one_line
from cashrecon.zto import ZtoClient

PAGE_SIZE = 100
SUMMARY_MAX_DAYS = 31
MAX_PAGES = 200
UNCATEGORIZED = {"", "-", "未归类", "None"}
RAW_KEYS = ("id", "serialNumber", "accountName", "oneCategoryName", "secondCategoryName",
            "thirdCategoryName", "descriptionName", "bizRemark", "bussinessSourseStr",
            "registrationStatusName", "payStatusName", "recordedStatusFormat", "afterBalance",
            "flowInvoiceAmount", "feeFlowTypeFormat", "communicationUnit", "billCode", "creator")


def unmapped_code(portal_code: str) -> str:
    return f"UNMAPPED:{portal_code}"


def category_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("oneCategoryName", "secondCategoryName", "descriptionName"):
        value = str(row.get(key) or "").strip()
        if value not in UNCATEGORIZED and value not in parts:
            parts.append(value)
    return "/".join(parts)


def map_record(row: dict[str, Any], settings: Settings) -> FlowRecord:
    portal = str(row.get("accountCode") or "")
    account = settings.account_by_portal(portal)
    stamp = row.get("registerDate")
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        raise SourceError("journal_record_time_invalid")
    direction = {"收款": "IN", "付款": "OUT"}.get(str(row.get("feeFlowTypeFormat") or ""))
    if direction is None:
        direction = {1: "IN", 2: "OUT"}.get(row.get("feeFlowType"))
    if direction is None:
        raise SourceError("journal_record_direction_unknown")
    if row.get("flowInvoiceAmount") is None or row.get("id") is None:
        raise SourceError("journal_record_incomplete")
    status = "/".join(str(row.get(k) or "") for k in
                      ("registrationStatusName", "payStatusName", "recordedStatusFormat", "bussinessSourseStr"))
    return FlowRecord(
        source="JOURNAL",
        source_ref=str(row["id"]),
        account_code=account.code if account else unmapped_code(portal),
        biz_time=dates.fmt_time(dates.from_millis(stamp)),
        direction=direction,
        amount_cents=abs(to_cents(row["flowInvoiceAmount"])),
        balance_after_cents=to_cents_or_none(row.get("afterBalance")),
        counterparty=one_line(row.get("communicationUnit"), 60),
        src_category=category_text(row),
        summary=one_line(row.get("bizRemark")),
        status_text=status,
        raw={k: row.get(k) for k in RAW_KEYS if row.get(k) not in (None, "")},
    )


def _walk_accounts(nodes: Any):
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        children = node.get("childList")
        if isinstance(children, list) and children:
            yield from _walk_accounts(children)
        else:
            yield node


def parse_account_summary(data: Any, settings: Settings, day: date) -> list[BalanceRecord]:
    records = []
    for node in _walk_accounts(data):
        account = settings.account_by_portal(str(node.get("accountCode") or ""))
        if account is None:
            continue
        total = next((d for d in node.get("detailList") or [] if isinstance(d, dict)
                      and d.get("dimensionDate") == "合计"), None)
        if total is None:
            continue
        expence = to_cents_or_none(total.get("expence"))
        records.append(BalanceRecord(
            account.code, day.isoformat(), "JOURNAL",
            opening_cents=to_cents_or_none(total.get("initFirstBalance")),
            closing_cents=to_cents_or_none(total.get("endBalance")),
            inflow_cents=to_cents_or_none(total.get("revenue")),
            outflow_cents=None if expence is None else abs(expence)))
    return records


class JournalSource:
    code = "JOURNAL"

    def __init__(self, client: ZtoClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch(self, day: date) -> SourceBatch:
        start, end = dates.window(day)
        batch = SourceBatch("JOURNAL", day, flow_sources=("JOURNAL",))
        rows: list[dict[str, Any]] = []
        total = None
        for page in range(1, MAX_PAGES + 1):
            data, batch.route = self.client.data("site-journal-record", "page", {
                "startDate": start, "endDate": end, "currentPage": page, "pageSize": PAGE_SIZE})
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise SourceError("journal_page_invalid")
            if total is None:
                total = int(data.get("totalRow") or 0)
            rows.extend(r for r in data["list"] if isinstance(r, dict))
            if len(rows) >= total or not data["list"]:
                break
        else:
            raise SourceError("journal_pagination_incomplete")
        if total is not None and len(rows) < total:
            raise SourceError("journal_pagination_incomplete")
        seen: set[str] = set()
        for row in rows:
            flow = map_record(row, self.settings)
            if flow.source_ref in seen:
                continue
            seen.add(flow.source_ref)
            if flow.biz_date != day.isoformat():
                batch.notes.append(f"记录 {flow.source_ref} 交易日期 {flow.biz_date} 不在查询日")
            if flow.account_code.startswith("UNMAPPED:"):
                batch.notes.append(f"未配置的日记账账户：{row.get('accountName')}")
            batch.flows.append(flow)
        summary, _ = self.client.data("site-journal-summary", "account-summary", {
            "startDate": start, "endDate": end, "currentPage": 1, "pageSize": 100})
        batch.balances.extend(parse_account_summary(summary, self.settings, day))
        self._carry_idle_accounts(batch, day)
        return batch

    def _carry_idle_accounts(self, batch: SourceBatch, day: date) -> None:
        """Accounts without entries on ``day`` are absent from the daily summary; derive their
        closing balance from a 31-day window (the portal's maximum) ending on ``day``."""
        present = {b.account_code for b in batch.balances}
        idle = [a for a in self.settings.accounts if a.portal_code and not a.is_auto and a.code not in present]
        if not idle:
            return
        window_start = (day - timedelta(days=SUMMARY_MAX_DAYS - 1)).isoformat() + " 00:00:00"
        _, end = dates.window(day)
        data, _ = self.client.data("site-journal-summary", "account-summary", {
            "startDate": window_start, "endDate": end, "currentPage": 1, "pageSize": 100})
        wanted = {a.code for a in idle}
        for record in parse_account_summary(data, self.settings, day):
            if record.account_code in wanted and record.closing_cents is not None:
                batch.balances.append(BalanceRecord(record.account_code, day.isoformat(), "JOURNAL",
                                                    record.closing_cents, record.closing_cents, 0, 0,
                                                    "当日无登记（由近 31 天汇总推得期末）"))


def list_portal_accounts(client: ZtoClient) -> list[dict[str, Any]]:
    """Portal journal accounts for configuration help (no card numbers returned)."""
    data, _ = client.data("site-journal-account", "list", {"currentPage": 1, "pageSize": 100})
    items = data.get("list") if isinstance(data, dict) else None
    result = []
    for item in items or []:
        number = str(item.get("accountNo") or "")
        result.append({
            "portal_code": item.get("accountCode"),
            "name": item.get("accountName"),
            "type": item.get("journalAccountTypeDesc"),
            "nature": item.get("accountNatureDesc"),
            "bank": item.get("bankName") or "",
            "tail": number[-4:] if number else "",
            "manual_code": item.get("accountManualCode"),
            "portal_balance_cents": to_cents_or_none(item.get("remainMoney")),
        })
    return result
