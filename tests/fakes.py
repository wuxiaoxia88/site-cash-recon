"""Test doubles and synthetic upstream fixtures (no real data)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from cashrecon.zto import ZtoError


class FakeZto:
    """Mimics ZtoClient.data(); handlers map (adapter, endpoint) -> callable(payload) -> data."""

    def __init__(self, handlers: dict[tuple[str, str], Any]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str, dict]] = []

    def data(self, adapter: str, endpoint: str, payload: dict) -> tuple[Any, str]:
        self.calls.append((adapter, endpoint, payload))
        handler = self.handlers.get((adapter, endpoint))
        if handler is None:
            raise ZtoError("endpoint_missing")
        result = handler(payload) if callable(handler) else handler
        if isinstance(result, Exception):
            raise result
        return result, "primary"


def zt_detail_rows(opening: float, income: float, expenses: float, categories: list[tuple[str, str, str, str, float]]):
    def total(label: str, fee: float, inc=None, exp=None):
        return {"oneCategoryCode": "汇总", "oneCategoryName": "汇总", "descriptionName": label,
                "list": [{"fee": fee, "income": inc, "expenses": exp}]}
    rows = [total("期初余额", opening), total("当日发生额", round(income - expenses, 2), income, expenses),
            total("期末余额", round(opening + income - expenses, 2))]
    for l1, l2, l3, desc, fee in categories:
        rows.append({"oneCategoryCode": "X", "oneCategoryName": l1, "secondCategoryName": l2,
                     "thirdCategoryName": l3, "descriptionName": desc, "list": [{"fee": fee}]})
    return rows


def make_ledger(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE transactions (guid TEXT PRIMARY KEY, account_tail TEXT, trade_date TEXT, direction TEXT,
            amount REAL, balance REAL, counterparty TEXT, sms_arrival TEXT, raw_text TEXT, category TEXT);
        CREATE TABLE icbc_txns (guid TEXT PRIMARY KEY, day TEXT, ts TEXT, direction TEXT, biz_type TEXT,
            amount REAL, balance REAL, peer TEXT, category TEXT);
        CREATE TABLE alipay_txns (ref TEXT PRIMARY KEY, day TEXT, ts TEXT, cat TEXT, note TEXT, peer TEXT,
            goods TEXT, income REAL, expense REAL, balance REAL, category TEXT);
        CREATE TABLE recon_daily (day TEXT, account TEXT, open_balance REAL, income REAL, expense REAL,
            close_balance REAL, txn_count INTEGER, status TEXT, PRIMARY KEY (day, account));
    """)
    return conn


def make_monitor(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE flows (balance_no TEXT PRIMARY KEY, balance_code TEXT, balance_type TEXT, actual_amount REAL,
            cash_amount REAL, pay_site TEXT, rec_site TEXT, opera_type INTEGER, balance_time TEXT,
            source_name TEXT, raw_json TEXT, inserted_at TEXT);
    """)
    return conn


ALIPAY_HEADER = "账务流水号,业务流水号,商品名称,发生时间,收入金额（+元）,支出金额（-元）,账户余额（元）,交易渠道,业务类型,备注,商户订单号,对方账号"


def write_alipay_csv(root: Path, day: str, rows: list[tuple[str, str, str, str, str, str, str]]) -> Path:
    """rows: (acct_ref, biz_ref, time, income, expense, balance, note)."""
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["#支付宝账务明细查询", "#账号：[0000]", "#----账务明细列表----", ALIPAY_HEADER]
    for acct, biz, ts, inc, exp, bal, note in rows:
        lines.append(f"{acct}\t,{biz}\t,\t,{ts}\t,{inc}\t,{exp}\t,{bal}\t,支付宝\t,其它\t,{note}\t,{biz}\t,****\t")
    lines += ["#----账务明细列表结束----", "#导出时间：[x]"]
    path = folder / f"0000_{day.replace('-', '')}_账务明细.csv"
    path.write_bytes("\r\n".join(lines).encode("gbk"))
    (folder / f"0000_{day.replace('-', '')}_账务汇总.csv").write_bytes(b"summary")
    return path
