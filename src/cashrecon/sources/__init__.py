"""Source registry: build enabled sources from configuration."""

from __future__ import annotations

from collections.abc import Callable

from cashrecon.config import Settings
from cashrecon.money import to_cents
from cashrecon.sources.base import Source, SourceError
from cashrecon.zto import ZtoClient

SOURCE_ORDER = ("ZT_SUMMARY", "ZT_FLOW", "JOURNAL", "BANK_LEDGER", "BILL_PROFIT")
SOURCE_CN = {
    "ZT_SUMMARY": "中天汇总",
    "ZT_FLOW": "中天逐笔",
    "JOURNAL": "门户日记账",
    "BANK_LEDGER": "银行采集",
    "BANK_SMS": "对公卡短信",
    "ICBC": "工行短信",
    "ALIPAY": "支付宝账单",
    "BANK_RECON": "银行采集日对账",
    "BILL_PROFIT": "账单口径",
    "MANUAL": "人工录入",
    "DERIVED": "系统推导",
}


def build_sources(settings: Settings, client_factory: Callable[[], ZtoClient] | None = None,
                  only: list[str] | None = None) -> dict[str, Source]:
    client: ZtoClient | None = None

    def zto() -> ZtoClient:
        nonlocal client
        if client is None:
            client = (client_factory or (lambda: ZtoClient.from_settings(settings)))()
        return client

    sources: dict[str, Source] = {}
    for code in SOURCE_ORDER:
        if not settings.source_enabled(code) or (only and code not in only):
            continue
        cfg = settings.source(code)
        zt_account = cfg.get("account") or (settings.zt_account.code if settings.zt_account else "ZT_MAIN")
        if code == "ZT_SUMMARY":
            from cashrecon.sources.zt_summary import ZtSummarySource
            sources[code] = ZtSummarySource(zto(), zt_account)
        elif code == "ZT_FLOW":
            from cashrecon.sources.zt_flow import DEFAULT_MOVEMENT_TYPES, ZtFlowApiSource, ZtFlowMonitorSource
            keep = tuple(cfg.get("movement_types") or DEFAULT_MOVEMENT_TYPES)
            large = to_cents(cfg.get("large_item_yuan", 1000))
            if cfg.get("impl", "api") == "monitor_db":
                sources[code] = ZtFlowMonitorSource(str(cfg.get("monitor_db", "")), zt_account, keep, large)
            else:
                sources[code] = ZtFlowApiSource(zto(), zt_account, keep, large)
        elif code == "JOURNAL":
            from cashrecon.sources.journal import JournalSource
            sources[code] = JournalSource(zto(), settings)
        elif code == "BANK_LEDGER":
            from cashrecon.sources.bank_ledger import BankLedgerSource
            sources[code] = BankLedgerSource(settings)
        elif code == "BILL_PROFIT":
            from cashrecon.sources.bill_profit import BillProfitSource
            sources[code] = BillProfitSource(zto(), cfg.get("violation_center_settle_ids"),
                                             settings.secret("ZTO_SERVICE_COST_X_SV_V"))
    return sources


__all__ = ["SOURCE_CN", "SOURCE_ORDER", "Source", "SourceError", "build_sources"]
