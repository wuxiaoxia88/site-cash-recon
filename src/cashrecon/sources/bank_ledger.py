"""BANK_LEDGER: read-only access to the upstream bank-collection ledger (ledger.sqlite).

Produces three flow sources:
  * ``BANK_SMS`` — corporate card SMS / statement rows (``transactions``)
  * ``ICBC``     — ICBC card SMS rows (``icbc_txns``)
  * ``ALIPAY``   — owner Alipay rows; the official daily 账务明细 CSV is preferred when
                   ``alipay_bill_dir`` is configured, otherwise the ``alipay_txns`` table.
Balances: chain closing from the last row of the day, and the upstream daily
reconciliation (``recon_daily``) as an additional reported balance (source ``BANK_RECON``).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from cashrecon.config import Settings
from cashrecon.money import to_cents, to_cents_or_none
from cashrecon.sources.base import (
    BalanceRecord,
    FlowRecord,
    SourceBatch,
    SourceError,
    one_line,
    open_readonly,
)


@dataclass
class ChainRow:
    flow: FlowRecord
    balance: int | None


def _signed(flow: FlowRecord) -> int:
    return flow.amount_cents if flow.direction == "IN" else -flow.amount_cents


def chain_balances(rows: list[ChainRow], previous_closing: int | None) -> tuple[int | None, int | None, bool]:
    """Return (opening, closing, chain_ok) for rows ordered by time.

    ``chain_ok`` is False when some row's balance does not follow from the previous one.
    """
    if not rows:
        return previous_closing, previous_closing, True
    first = rows[0]
    opening = previous_closing
    if first.balance is not None:
        derived = first.balance - _signed(first.flow)
        opening = derived if previous_closing is None else previous_closing
    ok = True
    running = opening
    for row in rows:
        if running is not None:
            running += _signed(row.flow)
            if row.balance is not None and row.balance != running:
                ok = False
                running = row.balance
        elif row.balance is not None:
            running = row.balance
    closing = rows[-1].balance if rows[-1].balance is not None else running
    return opening, closing, ok


def parse_alipay_csv(path: Path, account_code: str, categories: dict[str, str]) -> list[ChainRow]:
    raw = path.read_bytes()
    for encoding in ("gbk", "gb18030", "utf-8-sig"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SourceError("alipay_csv_encoding")
    header: dict[str, int] | None = None
    rows: list[ChainRow] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = [f.strip().strip("\t").strip() for f in line.split(",")]
        if fields[0] == "账务流水号":
            header = {name: i for i, name in enumerate(fields)}
            continue
        if header is None:
            continue
        record: defaultdict[str, str] = defaultdict(str, {n: fields[i] for n, i in header.items() if i < len(fields)})
        get = record.__getitem__
        income = to_cents(get("收入金额（+元）") or "0")
        expense = abs(to_cents(get("支出金额（-元）") or "0"))
        if income == 0 and expense == 0:
            continue
        business_ref = get("业务流水号")
        note = get("备注")
        flow = FlowRecord(
            source="ALIPAY",
            source_ref=get("账务流水号"),
            account_code=account_code,
            biz_time=get("发生时间"),
            direction="IN" if income else "OUT",
            amount_cents=income or expense,
            balance_after_cents=to_cents_or_none(get("账户余额（元）")),
            counterparty=one_line(get("对方账号"), 60),
            src_category=categories.get(business_ref) or "/".join(x for x in (get("业务类型"), note) if x),
            summary=one_line(note or get("商品名称")),
            raw={"business_ref": business_ref, "channel": get("交易渠道"), "biz_type": get("业务类型"),
                 "goods": get("商品名称")},
        )
        rows.append(ChainRow(flow, flow.balance_after_cents))
    if header is None:
        raise SourceError("alipay_csv_header_missing")
    return rows


class BankLedgerSource:
    code = "BANK_LEDGER"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.source("BANK_LEDGER")
        if not self.cfg.get("ledger_db"):
            raise SourceError("ledger_db not configured")

    def _account(self, key: str) -> str | None:
        code = self.cfg.get(key)
        if code and not self.settings.has_account(code):
            raise SourceError(f"{key} refers to unknown account {code}")
        return code or None

    # ------------------------------------------------------------------ pieces
    def _bank_sms(self, conn: sqlite3.Connection, day: str, account: str) -> list[ChainRow]:
        tail = self.settings.account(account).bank_tail
        sql = ("SELECT guid, trade_date, sms_arrival, direction, amount, balance, counterparty, category "
               "FROM transactions WHERE trade_date = ?" + (" AND account_tail = ?" if tail else "") +
               " ORDER BY sms_arrival, rowid")
        rows = conn.execute(sql, (day, tail) if tail else (day,)).fetchall()
        result = []
        for r in rows:
            arrival = str(r["sms_arrival"] or "")
            flow = FlowRecord(
                source="BANK_SMS", source_ref=str(r["guid"]), account_code=account,
                biz_time=arrival if arrival[:10] == day else f"{day} 00:00:00",
                direction="IN" if r["direction"] == "收入" else "OUT",
                amount_cents=abs(to_cents(r["amount"])),
                balance_after_cents=to_cents_or_none(r["balance"]),
                counterparty=one_line(r["counterparty"], 60), src_category=str(r["category"] or ""))
            result.append(ChainRow(flow, flow.balance_after_cents))
        return result

    def _icbc(self, conn: sqlite3.Connection, day: str, account: str) -> list[ChainRow]:
        rows = conn.execute("SELECT guid, ts, direction, biz_type, amount, balance, peer, category "
                            "FROM icbc_txns WHERE day = ? ORDER BY ts, rowid", (day,)).fetchall()
        result = []
        for r in rows:
            ts = str(r["ts"] or day)
            flow = FlowRecord(
                source="ICBC", source_ref=str(r["guid"]), account_code=account,
                biz_time=(ts + ":00") if len(ts) == 16 else (ts if len(ts) >= 19 else f"{day} 00:00:00"),
                direction="IN" if r["direction"] == "收入" else "OUT",
                amount_cents=abs(to_cents(r["amount"])),
                balance_after_cents=to_cents_or_none(r["balance"]),
                counterparty=one_line(r["peer"], 60),
                src_category="/".join(x for x in (str(r["biz_type"] or ""), str(r["category"] or "")) if x))
            result.append(ChainRow(flow, flow.balance_after_cents))
        return result

    def _alipay_table(self, conn: sqlite3.Connection, day: str, account: str) -> list[ChainRow]:
        rows = conn.execute("SELECT ref, ts, cat, note, peer, goods, income, expense, balance, category "
                            "FROM alipay_txns WHERE day = ? ORDER BY ts, rowid", (day,)).fetchall()
        result = []
        for r in rows:
            income, expense = to_cents(r["income"] or 0), abs(to_cents(r["expense"] or 0))
            if not income and not expense:
                continue
            flow = FlowRecord(
                source="ALIPAY", source_ref=str(r["ref"]), account_code=account, biz_time=str(r["ts"]),
                direction="IN" if income else "OUT", amount_cents=income or expense,
                balance_after_cents=to_cents_or_none(r["balance"]),
                counterparty=one_line(r["peer"], 60),
                src_category=str(r["category"] or r["cat"] or ""), summary=one_line(r["note"] or r["goods"]),
                raw={"business_ref": r["ref"]})
            result.append(ChainRow(flow, flow.balance_after_cents))
        return result

    def _alipay_categories(self, conn: sqlite3.Connection, day: str) -> dict[str, str]:
        rows = conn.execute("SELECT ref, category, cat FROM alipay_txns WHERE day = ?", (day,)).fetchall()
        return {str(r["ref"]): str(r["category"] or r["cat"] or "") for r in rows}

    def _alipay_csv_path(self, day: str) -> Path | None:
        root = self.cfg.get("alipay_bill_dir")
        if not root:
            return None
        folder = Path(root).expanduser() / day
        files = sorted(p for p in folder.glob("*账务明细*.csv") if "汇总" not in p.name) if folder.is_dir() else []
        return files[0] if len(files) == 1 else None

    def _previous_closing(self, conn: sqlite3.Connection, table: str, date_col: str, day: str,
                          order: str, extra: str = "", params: tuple = ()) -> int | None:
        row = conn.execute(f"SELECT balance FROM {table} WHERE {date_col} < ? AND balance IS NOT NULL{extra} "
                           f"ORDER BY {order} DESC, rowid DESC LIMIT 1", (day, *params)).fetchone()
        return None if row is None else to_cents(row[0])

    # ------------------------------------------------------------------ fetch
    def fetch(self, day: date) -> SourceBatch:
        text = day.isoformat()
        batch = SourceBatch("BANK_LEDGER", day)
        conn = open_readonly(self.cfg["ledger_db"])
        try:
            conn.execute("BEGIN")  # one consistent snapshot of the upstream file
            plans: list[tuple[str, str, list[ChainRow], int | None]] = []
            corp = self._account("corp_account")
            if corp:
                tail = self.settings.account(corp).bank_tail
                extra, params = (" AND account_tail = ?", (tail,)) if tail else ("", ())
                plans.append(("BANK_SMS", corp, self._bank_sms(conn, text, corp),
                              self._previous_closing(conn, "transactions", "trade_date", text,
                                                     "trade_date DESC, sms_arrival", extra, params)))
            icbc = self._account("icbc_account")
            if icbc:
                plans.append(("ICBC", icbc, self._icbc(conn, text, icbc),
                              self._previous_closing(conn, "icbc_txns", "day", text, "day DESC, ts")))
            alipay = self._account("alipay_account")
            if alipay:
                csv_path = self._alipay_csv_path(text)
                if csv_path is not None:
                    rows = parse_alipay_csv(csv_path, alipay, self._alipay_categories(conn, text))
                    label = "ALIPAY"
                else:
                    if self.cfg.get("alipay_bill_dir"):
                        batch.notes.append("支付宝官方账单缺失，使用采集库数据")
                    rows = self._alipay_table(conn, text, alipay)
                    label = "ALIPAY"
                plans.append((label, alipay, rows,
                              self._previous_closing(conn, "alipay_txns", "day", text, "day DESC, ts")))
            for source, account, rows, previous in plans:
                opening, closing, ok = chain_balances(rows, previous)
                inflow = sum(r.flow.amount_cents for r in rows if r.flow.direction == "IN")
                outflow = sum(r.flow.amount_cents for r in rows if r.flow.direction == "OUT")
                note = "" if ok else "余额链不连续（可能漏采或乱序）"
                if not ok:
                    batch.notes.append(f"{source} {note}")
                batch.balances.append(BalanceRecord(account, text, source, opening, closing, inflow, outflow, note))
                batch.flows.extend(r.flow for r in rows)
            batch.flow_sources = tuple(dict.fromkeys(p[0] for p in plans))
            batch.balances.extend(self._recon_daily(conn, text))
            conn.rollback()
        except sqlite3.Error as exc:
            raise SourceError(f"ledger_read_failed: {exc.__class__.__name__}") from None
        finally:
            conn.close()
        return batch

    def _recon_daily(self, conn: sqlite3.Connection, day: str) -> list[BalanceRecord]:
        labels: dict[str, Any] = self.cfg.get("recon_labels") or {}
        if not labels:
            return []
        records = []
        for row in conn.execute("SELECT account, open_balance, income, expense, close_balance, status "
                                "FROM recon_daily WHERE day = ?", (day,)):
            account = labels.get(str(row["account"]))
            if account and self.settings.has_account(account):
                records.append(BalanceRecord(
                    account, day, "BANK_RECON", to_cents_or_none(row["open_balance"]),
                    to_cents_or_none(row["close_balance"]), to_cents_or_none(row["income"]),
                    None if row["expense"] is None else abs(to_cents(row["expense"])), str(row["status"] or "")))
        return records
