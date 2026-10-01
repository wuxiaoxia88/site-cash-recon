"""BILL_PROFIT: bill-basis profit components (reference only, 账单口径对照).

Formula (same as the existing finance morning report):
    income  = 进港 newIncomeTotalFee + 出港返利 settlementAmount + pendingSettlementAmt
    expense = 进港 expendTotalFee + 出港 totalFee + 服务违规成本
Violation cost needs extra parameters (``violation_center_settle_ids`` in config and
``ZTO_SERVICE_COST_X_SV_V`` in secrets); without them that component is missing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cashrecon import dates
from cashrecon.money import to_cents, to_cents_or_none
from cashrecon.sources.base import SourceBatch, SourceError
from cashrecon.zto import ZtoClient, ZtoError

INCOME_COMPONENTS = ("inbound_income", "rebate_settled", "rebate_pending")
EXPENSE_COMPONENTS = ("inbound_expense", "outbound_fee", "violation_cost")
COMPONENT_CN = {
    "inbound_income": "进港派费收入",
    "rebate_settled": "出港返利（已结算）",
    "rebate_pending": "出港返利（待结算）",
    "inbound_expense": "进港支出",
    "outbound_fee": "出港账单费用",
    "violation_cost": "服务违规成本",
}


def bill_profit_totals(components: dict[str, int | None]) -> dict[str, Any]:
    missing = [c for c in INCOME_COMPONENTS + EXPENSE_COMPONENTS if components.get(c) is None]
    income = sum(components.get(c) or 0 for c in INCOME_COMPONENTS)
    expense = sum(components.get(c) or 0 for c in EXPENSE_COMPONENTS)
    return {"income_cents": income, "expense_cents": expense, "profit_cents": income - expense,
            "missing": missing, "complete": not missing}


class BillProfitSource:
    code = "BILL_PROFIT"

    def __init__(self, client: ZtoClient, center_settle_ids: list[Any] | None = None,
                 x_sv_v: str | None = None) -> None:
        self.client, self.center_settle_ids, self.x_sv_v = client, center_settle_ids, x_sv_v

    def _call(self, adapter: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            data, _ = self.client.data(adapter, endpoint, payload)
        except ZtoError:
            return None
        return data if isinstance(data, dict) else None

    def fetch(self, day: date) -> SourceBatch:
        start, end = dates.window(day)
        batch = SourceBatch("BILL_PROFIT", day)
        inbound = self._call("inbound-bill", "query", {"signDateBegin": start, "signDateEnd": end, "orderType": 0,
                                                        "currentPage": 1, "pageSize": 10})
        outbound = self._call("outbound-bill", "query", {"startTime": start, "endTime": end, "queryDateType": 2,
                                                          "currentPage": 1, "pageSize": 10})
        rebate = self._call("outbound-rebate-bill", "day-sum", {"bizDateStart": start, "bizDateEnd": end})
        if inbound is None and outbound is None and rebate is None:
            raise SourceError("bill_profit_all_components_failed")
        batch.bill_profit = {
            "inbound_income": to_cents_or_none((inbound or {}).get("newIncomeTotalFee")),
            "inbound_expense": to_cents_or_none((inbound or {}).get("expendTotalFee")),
            "outbound_fee": to_cents_or_none((outbound or {}).get("totalFee")),
            "rebate_settled": to_cents_or_none((rebate or {}).get("settlementAmount")),
            "rebate_pending": to_cents_or_none((rebate or {}).get("pendingSettlementAmt")),
            "violation_cost": self._violation(start, end),
        }
        return batch

    def _violation(self, start: str, end: str) -> int | None:
        if not self.center_settle_ids or not self.x_sv_v:
            return None
        total = 0
        for page in range(1, 6):
            data = self._call("service-violation-cost", "summary-page", {
                "currentPage": page, "pageSize": 500, "billDateStart": start, "billDateEnd": end,
                "centerSettleIds": self.center_settle_ids, "xSvV": self.x_sv_v})
            if data is None:
                return None
            items = data.get("list") or data.get("records") or []
            total += sum(to_cents(i.get("siteDownSettleAmount") or 0) for i in items if isinstance(i, dict))
            if len(items) < 500:
                return total
        return None
