"""Unified cash-basis chart of accounts (统一科目)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    kind: str  # income | cost | non_pl | adjust | unclassified


CATEGORIES: list[Category] = [
    Category("INC_DELIVERY", "派件收入", "income"),
    Category("INC_PICKUP", "揽收/寄件收入", "income"),
    Category("INC_POLICY", "政策收入（返利/补贴/奖励）", "income"),
    Category("INC_VAS", "增值服务收入", "income"),
    Category("INC_STATION", "驿站收入", "income"),
    Category("INC_OTHER", "其他收入", "income"),
    Category("COST_LINEHAUL", "中转/操作/建包费", "cost"),
    Category("COST_SEND_DISPATCH", "寄件派费", "cost"),
    Category("COST_POLICY", "政策/考核支出", "cost"),
    Category("COST_VAS", "增值服务支出", "cost"),
    Category("COST_SUBSITE", "付下级网点派费", "cost"),
    Category("COST_WAYBILL", "面单/单号/物料", "cost"),
    Category("COST_LABOR", "人工（工资/社保）", "cost"),
    Category("COST_VEHICLE", "车辆（油费/保险/维修）", "cost"),
    Category("COST_STATION", "场地/驿站/快递柜/房租水电", "cost"),
    Category("COST_ADMIN", "管理费用（平台/办公/税费）", "cost"),
    Category("COST_FINANCE", "财务费用（手续费/利息）", "cost"),
    Category("COST_CLAIM", "罚款与理赔（净额）", "cost"),
    Category("COST_OTHER", "其他支出", "cost"),
    Category("XFER_INTERNAL", "账户间划转", "non_pl"),
    Category("XFER_ZT_TOPUP", "中天充值", "non_pl"),
    Category("XFER_ZT_WITHDRAW", "中天提现", "non_pl"),
    Category("PASS_THROUGH", "代收代付/押金/钱包往来", "non_pl"),
    Category("OWNER_DRAW", "转出至体系外账户（店主/个人）", "non_pl"),
    Category("FINANCING", "借贷本息", "non_pl"),
    Category("ADJUST", "余额修改/测试", "adjust"),
    Category("UNCLASSIFIED", "待分类", "unclassified"),
]

BY_CODE: dict[str, Category] = {c.code: c for c in CATEGORIES}
ORDER: dict[str, int] = {c.code: i for i, c in enumerate(CATEGORIES)}


def name(code: str) -> str:
    category = BY_CODE.get(code)
    return category.name if category else code


def kind(code: str) -> str:
    category = BY_CODE.get(code)
    return category.kind if category else "unclassified"


def is_pl(code: str) -> bool:
    return kind(code) in ("income", "cost")
