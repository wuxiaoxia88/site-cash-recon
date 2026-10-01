from __future__ import annotations

import copy
from datetime import date

import pytest

from cashrecon.config import settings_from_dict
from cashrecon.ingest import fetch_days, ingest
from cashrecon.sources.bank_ledger import BankLedgerSource, ChainRow, chain_balances
from cashrecon.sources.base import FlowRecord, SourceBatch, SourceError
from cashrecon.sources.bill_profit import BillProfitSource, bill_profit_totals
from cashrecon.sources.journal import JournalSource, map_record, parse_account_summary
from cashrecon.sources.zt_flow import ZtFlowMonitorSource, initiator_of
from cashrecon.sources.zt_summary import ZtSummarySource, parse_detail
from tests.conftest import BASE_CONFIG
from tests.fakes import FakeZto, make_ledger, make_monitor, write_alipay_csv, zt_detail_rows

DAY = date(2026, 9, 30)
MS = 1790738888000  # 2026-09-30 11:28:08 +08:00


def journal_row(rid, account="P-SW", flow="付款", amount="-65", desc="其他支出", one="其他支出", ms=MS):
    return {"id": rid, "accountCode": account, "accountName": "x", "registerDate": ms, "feeFlowTypeFormat": flow,
            "flowInvoiceAmount": amount, "descriptionName": desc, "oneCategoryName": one, "secondCategoryName": one,
            "afterBalance": 100.5, "registrationStatusName": "审核通过", "payStatusName": "已付款",
            "recordedStatusFormat": "未入账", "bussinessSourseStr": "PC端登记"}


# ----------------------------------------------------------------- ZT summary
def test_zt_summary_parses_totals_and_categories():
    rows = zt_detail_rows(1000, 300, 200, [
        ("主营业务收入", "进港派件收入", "收派件派费收入", "收派件派费", 300),
        ("主营业务成本", "出港业务成本", "中转费", "付中转费", -150),
        ("未归类", "-", "-", "中天余额提现", -50),
    ])
    batch = parse_detail(rows, "ZT_MAIN", DAY)
    bal = batch.balances[0]
    assert (bal.opening_cents, bal.closing_cents, bal.inflow_cents, bal.outflow_cents) == (100000, 110000, 30000, 20000)
    assert batch.zt_categories[2] == (("未归类", "", "", "中天余额提现"), -5000)
    assert batch.notes == []


def test_zt_summary_flags_inconsistency_and_empty():
    rows = zt_detail_rows(1000, 300, 200, [("主营业务收入", "a", "b", "c", 50)])
    assert "科目合计" in parse_detail(rows, "ZT_MAIN", DAY).notes[0]
    with pytest.raises(SourceError):
        parse_detail([], "ZT_MAIN", DAY)


def test_zt_summary_source_cross_checks_amount_endpoint():
    rows = zt_detail_rows(1000, 300, 200, [("主营业务收入", "a", "b", "c", 100)])
    client = FakeZto({("advance-payment-flow-summary", "advance-payment-flow-summary"): rows,
                      ("advance-payment-flow-summary", "summary-amount"): {"totalAmount": "99.00"}})
    batch = ZtSummarySource(client, "ZT_MAIN").fetch(DAY)
    assert any("summary-amount" in n for n in batch.notes)
    client.handlers.pop(("advance-payment-flow-summary", "summary-amount"))
    assert any("不可用" in n for n in ZtSummarySource(client, "ZT_MAIN").fetch(DAY).notes)


# ----------------------------------------------------------------- journal
def test_journal_record_mapping(settings):
    flow = map_record(journal_row(1, desc="付业务员司机工资", one="派送成本"), settings)
    assert flow.account_code == "STAFF_WECHAT"
    assert (flow.direction, flow.amount_cents, flow.biz_time) == ("OUT", 6500, "2026-09-30 11:28:08")
    assert flow.src_category == "派送成本/付业务员司机工资"
    assert flow.balance_after_cents == 10050
    unmapped = map_record(journal_row(2, account="P-NEW", flow="收款", amount="9"), settings)
    assert unmapped.account_code == "UNMAPPED:P-NEW" and unmapped.direction == "IN"
    with pytest.raises(SourceError):
        map_record({**journal_row(3), "feeFlowTypeFormat": "?", "feeFlowType": 9}, settings)


def test_journal_source_paginates_and_reads_summary(settings):
    rows = [journal_row(i) for i in range(1, 151)]

    def page(payload):
        start = (payload["currentPage"] - 1) * payload["pageSize"]
        return {"totalRow": len(rows), "list": rows[start:start + payload["pageSize"]]}

    summary = [{"accountCode": "微信", "childList": [{"accountCode": "P-SW", "detailList": [
        {"dimensionDate": "合计", "initFirstBalance": 500, "endBalance": 435, "revenue": 0, "expence": -65}]}]}]
    client = FakeZto({("site-journal-record", "page"): page, ("site-journal-summary", "account-summary"): summary})
    batch = JournalSource(client, settings).fetch(DAY)
    assert len(batch.flows) == 150
    assert [c[2]["currentPage"] for c in client.calls if c[1] == "page"] == [1, 2]
    bal = batch.balances[0]
    assert (bal.account_code, bal.opening_cents, bal.closing_cents, bal.outflow_cents) == ("STAFF_WECHAT", 50000, 43500, 6500)
    # idle manual accounts are carried from the 31-day window (same fake summary => only P-SW present)
    windows = [c[2]["startDate"] for c in client.calls if c[1] == "account-summary"]
    assert windows == ["2026-09-30 00:00:00", "2026-08-31 00:00:00"]


def test_journal_incomplete_pagination(settings):
    client = FakeZto({("site-journal-record", "page"): {"totalRow": 5, "list": []},
                      ("site-journal-summary", "account-summary"): []})
    with pytest.raises(SourceError):
        JournalSource(client, settings).fetch(DAY)


def test_parse_account_summary_skips_unknown(settings):
    data = [{"accountCode": "P-UNKNOWN", "detailList": [{"dimensionDate": "合计", "endBalance": 1}]}]
    assert parse_account_summary(data, settings, DAY) == []


# ----------------------------------------------------------------- bank ledger
def _flow(direction, cents):
    return FlowRecord("BANK_SMS", f"r{cents}", "CORP", "2026-09-30 10:00:00", direction, cents)


def test_chain_balances():
    rows = [ChainRow(_flow("OUT", 100), 900), ChainRow(_flow("IN", 50), 950)]
    assert chain_balances(rows, 1000) == (1000, 950, True)
    assert chain_balances(rows, None) == (1000, 950, True)
    assert chain_balances(rows, 2000)[2] is False  # gap between days
    assert chain_balances([], 700) == (700, 700, True)


@pytest.fixture
def bank_settings(paths, tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    conn = make_ledger(ledger)
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("g0", "0001", "2026-09-29", "收入", 100.0, 1000.0, "甲", "2026-09-29 09:00:00", "", "经营收入"),
        ("g1", "0001", "2026-09-30", "支出", 400.0, 600.0, "中通快递股份有限公司", "2026-09-30 10:39:23", "", None),
        ("g2", "0001", "2026-09-30", "收入", 55.5, 655.5, "乙", "2026-09-30 12:00:00", "", "经营收入"),
        ("x1", "9999", "2026-09-30", "收入", 1.0, 1.0, "其他卡", "2026-09-30 12:00:00", "", None),
    ])
    conn.execute("INSERT INTO icbc_txns VALUES ('i1','2026-09-30','2026-09-30 04:03','支出','贷款本息',175.58,1529.19,'',NULL)")
    conn.executemany("INSERT INTO alipay_txns VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("B1", "2026-09-29", "2026-09-29 20:00:00", "其它", "", "", "", 0, 0, 13000.0, ""),
        ("B2", "2026-09-30", "2026-09-30 05:42:32", "其它", "结算", "", "", 1.05, 0, 13001.05, "菜鸟驿站寄件收入"),
    ])
    conn.execute("INSERT INTO recon_daily VALUES ('2026-09-30','对公X',1000,55.5,400,655.5,2,'全部通过')")
    conn.commit()
    conn.close()
    bills = tmp_path / "bills"
    write_alipay_csv(bills, "2026-09-30", [
        ("A1", "B2", "2026-09-30 05:42:32", "1.05", "0.00", "13001.05", "结算"),
        ("A2", "P9", "2026-09-30 21:05:53", "0.00", "-95.00", "12906.05", ""),
    ])
    data = copy.deepcopy(BASE_CONFIG)
    data["sources"]["BANK_LEDGER"] = {"enabled": True, "ledger_db": str(ledger), "corp_account": "CORP",
                                      "icbc_account": "ICBC_CARD", "alipay_account": "OWNER_ALIPAY",
                                      "alipay_bill_dir": str(bills), "recon_labels": {"对公X": "CORP"}}
    return settings_from_dict(data, paths)


def test_bank_ledger_fetch(bank_settings):
    batch = BankLedgerSource(bank_settings).fetch(DAY)
    by_source = {}
    for f in batch.flows:
        by_source.setdefault(f.source, []).append(f)
    assert [f.source_ref for f in by_source["BANK_SMS"]] == ["g1", "g2"]  # other card excluded
    assert by_source["ICBC"][0].biz_time == "2026-09-30 04:03:00"
    alipay = by_source["ALIPAY"]
    assert [f.source_ref for f in alipay] == ["A1", "A2"]  # official CSV preferred
    assert alipay[0].src_category == "菜鸟驿站寄件收入"  # category joined from ledger table
    assert (alipay[1].direction, alipay[1].amount_cents) == ("OUT", 9500)
    balances = {(b.source, b.account_code): b for b in batch.balances}
    corp = balances[("BANK_SMS", "CORP")]
    assert (corp.opening_cents, corp.closing_cents, corp.note) == (100000, 65550, "")
    assert balances[("ALIPAY", "OWNER_ALIPAY")].closing_cents == 1290605
    assert balances[("BANK_RECON", "CORP")].closing_cents == 65550
    assert set(batch.flow_sources) == {"BANK_SMS", "ICBC", "ALIPAY"}


def test_bank_ledger_falls_back_to_table(bank_settings):
    batch = BankLedgerSource(bank_settings).fetch(date(2026, 9, 29))
    assert "支付宝官方账单缺失" in batch.notes[0]


# ----------------------------------------------------------------- ZT flow
def test_zt_flow_monitor(tmp_path):
    path = tmp_path / "monitor.db"
    conn = make_monitor(path)
    conn.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("n1", "c1", "线下提现", 1, -10000.0, "", "", 0, "2026-09-30 11:26:00", "", '{"remark":"000.303掌中通发起预付款提现"}', ""),
        ("n2", "c2", "派件派费", 1, 1.2, "", "", 0, "2026-09-30 11:27:00", "", "{}", ""),
        ("n3", "c3", "中转费", 1, -1500.0, "", "", 0, "2026-09-30 11:28:00", "", "{}", ""),
        ("n4", "c4", "派件派费", 1, 0.0, "", "", 0, "2026-09-30 11:29:00", "", "{}", ""),
        ("n5", "c5", "派件派费", 1, 3.0, "", "", 0, "2026-10-01 00:01:00", "", "{}", ""),
    ])
    conn.commit()
    conn.close()
    batch = ZtFlowMonitorSource(str(path), "ZT_MAIN").fetch(DAY)
    assert [f.source_ref for f in batch.flows] == ["n1", "n3"]  # movement + large only
    assert batch.flows[0].initiator == "000.303"
    total = batch.balances[0]
    assert (total.inflow_cents, total.outflow_cents) == (120, 1150000)
    assert batch.complete
    assert initiator_of("abc") == ""


# ----------------------------------------------------------------- bill profit
def test_bill_profit():
    client = FakeZto({("inbound-bill", "query"): {"newIncomeTotalFee": 100.5, "expendTotalFee": 20},
                      ("outbound-bill", "query"): {"totalFee": 50},
                      ("outbound-rebate-bill", "day-sum"): {"settlementAmount": 10, "pendingSettlementAmt": 5}})
    batch = BillProfitSource(client).fetch(DAY)
    totals = bill_profit_totals(batch.bill_profit)
    assert totals["income_cents"] == 11550 and totals["expense_cents"] == 7000
    assert totals["missing"] == ["violation_cost"] and not totals["complete"]


# ----------------------------------------------------------------- ingest
def test_ingest_idempotent_and_marks_removed(store):
    flows = [FlowRecord("JOURNAL", str(i), "CORP", "2026-09-30 10:00:00", "IN", 100 * i) for i in (1, 2, 3)]
    batch = SourceBatch("JOURNAL", DAY, flows=list(flows), flow_sources=("JOURNAL",))
    assert ingest(store, batch) == (3, 3)
    assert ingest(store, batch)[1] == 0
    batch.flows = [flows[0], FlowRecord("JOURNAL", "2", "CORP", "2026-09-30 10:00:00", "IN", 999)]
    assert ingest(store, batch)[1] == 2  # one changed + one removed
    rows = {r["source_ref"]: (r["amount_cents"], r["removed"]) for r in store.query("SELECT * FROM flows")}
    assert rows == {"1": (100, 0), "2": (999, 0), "3": (300, 1)}


def test_fetch_days_isolates_failures(settings, store):
    class Boom:
        code = "JOURNAL"

        def fetch(self, day):
            raise SourceError("journal_page_invalid")

    class Ok:
        code = "ZT_SUMMARY"

        def fetch(self, day):
            return SourceBatch("ZT_SUMMARY", day)

    results = fetch_days(settings, store, [DAY], sources={"JOURNAL": Boom(), "ZT_SUMMARY": Ok()})
    assert [(r.source, r.status) for r in results] == [("JOURNAL", "failed"), ("ZT_SUMMARY", "empty")]
    assert store.scalar("SELECT COUNT(*) FROM fetches WHERE status='failed'") == 1
