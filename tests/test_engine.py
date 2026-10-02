from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from cashrecon.config import settings_from_dict
from cashrecon.db import now_text
from cashrecon.engine import reconcile, reconcile_full
from cashrecon.engine.accounts import sync_accounts
from cashrecon.engine.matching import Matcher
from cashrecon.engine.result import load_daily_result
from cashrecon.engine.rules import ensure_default_rules
from cashrecon.ingest import ingest
from cashrecon.sources.base import BalanceRecord, FlowRecord, SourceBatch
from tests.conftest import BASE_CONFIG

D1, D2, D3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)


def flow(source, ref, account, day, direction, yuan, category="", counterparty="", initiator="", time="10:00:00",
         status=""):
    return FlowRecord(source, ref, account, f"{day.isoformat()} {time}", direction, round(yuan * 100),
                      counterparty=counterparty, src_category=category, initiator=initiator, status_text=status)


def add(store, day, flows=(), balances=(), zt=(), source="TEST"):
    batch = SourceBatch(source, day, flows=list(flows), balances=list(balances),
                        zt_categories=list(zt), complete=False)
    ingest(store, batch)


@pytest.fixture
def env(paths, store):
    data = copy.deepcopy(BASE_CONFIG)
    data["withdraw_initiators"] = {"000.1": {"account": "ICBC_CARD"}, "000.3": {"account": "ICBC_CARD"},
                                   "000.9": {"category": "COST_OTHER", "note": "其他用途"}}
    settings = settings_from_dict(data, paths)
    ensure_default_rules(store)
    sync_accounts(store, settings)
    return settings, store


def states(store):
    return {r["flow_id"]: (r["state"], r["category"]) for r in store.query("SELECT * FROM flow_states")}


def test_dedup_topup_withdraw_and_transfers(env):
    settings, store = env
    add(store, D1, [
        # journal duplicate of a bank receipt, journal carries the better category
        flow("JOURNAL", "j1", "CORP", D1, "IN", 500, "收客户运费", time="09:00:00"),
        flow("BANK_SMS", "b1", "CORP", D1, "IN", 500, "", "客户甲", time="11:00:00"),
        # two identical amounts on consecutive days pair in order
        flow("JOURNAL", "j2", "CORP", D1, "IN", 165, "收客户运费"),
        flow("JOURNAL", "j3", "CORP", D2, "IN", 165, "收客户运费"),
        flow("BANK_SMS", "b2", "CORP", D1, "IN", 165, "经营收入"),
        flow("BANK_SMS", "b3", "CORP", D2, "IN", 165, "经营收入"),
        # journal entry on an auto account without a bank row -> review
        flow("JOURNAL", "j4", "CORP", D1, "OUT", 280, "付物料系统面单押金"),
        # ZT top-up from the corporate card (journal + bank + ZT side)
        flow("JOURNAL", "j5", "CORP", D1, "OUT", 20000, "中天余额充值（支付宝）"),
        flow("BANK_SMS", "b4", "CORP", D1, "OUT", 20000, "", "中通快递股份有限公司"),
        flow("ZT_FLOW", "z1", "ZT_MAIN", D1, "IN", 20000, "中通支付充值"),
        # withdrawals: default labour cost / mapped to an own account (landed or not) / mapped category
        flow("ZT_FLOW", "w1", "ZT_MAIN", D1, "OUT", 3000, "线下提现", initiator="000.1"),
        flow("ICBC", "i1", "ICBC_CARD", D3, "IN", 3000, "跨行汇款"),
        flow("ZT_FLOW", "w2", "ZT_MAIN", D1, "OUT", 10000, "线下提现", initiator="000.2"),
        flow("ZT_FLOW", "w3", "ZT_MAIN", D1, "OUT", 7000, "线下提现", initiator="000.9"),
        flow("ZT_FLOW", "w4", "ZT_MAIN", D1, "OUT", 4000, "线下提现", initiator="000.3"),
        # paying ZTO head office without a ZT top-up = buying waybill numbers
        flow("BANK_SMS", "b9", "CORP", D1, "OUT", 28500, "", "中通快递股份有限公司"),
        # internal transfer with a small fee, plus an outgoing transfer with no partner
        flow("ALIPAY", "a1", "OWNER_ALIPAY", D1, "OUT", 1000, "提现至银行卡"),
        flow("JOURNAL", "j6", "STAFF_WECHAT", D1, "IN", 998, ""),
        flow("ALIPAY", "a2", "OWNER_ALIPAY", D1, "OUT", 3225, "账户间互转", "****y"),
        # manual account normal costs and a contradictory registration
        flow("JOURNAL", "j7", "STAFF_WECHAT", D1, "OUT", 2258, "派送成本/付业务员司机工资"),
        flow("JOURNAL", "j8", "STAFF_WECHAT", D1, "OUT", 330, "收客户运费"),
        flow("JOURNAL", "j9", "STAFF_WECHAT", D1, "OUT", 50, "草稿支出", status="草稿/待付款"),
    ])
    result = Matcher(store, settings, today=date(2026, 9, 10)).run()
    s = {fid: (f.state, f.category) for fid, f in result.flows.items()}
    assert s["JOURNAL:j1"] == ("DUPLICATE", "INC_PICKUP")
    assert s["BANK_SMS:b1"] == ("NORMAL", "INC_PICKUP")  # category inherited from journal
    assert s["JOURNAL:j2"][0] == s["JOURNAL:j3"][0] == "DUPLICATE"
    links = {(lk["flow_a"], lk["flow_b"]) for lk in result.links}
    assert ("JOURNAL:j2", "BANK_SMS:b2") in links and ("JOURNAL:j3", "BANK_SMS:b3") in links
    assert s["JOURNAL:j4"][0] == "REVIEW"
    assert s["JOURNAL:j5"][0] == "DUPLICATE"
    assert s["BANK_SMS:b4"] == s["ZT_FLOW:z1"] == ("TRANSFER", "XFER_ZT_TOPUP")
    assert s["ZT_FLOW:w1"] == s["ICBC:i1"] == ("TRANSFER", "XFER_ZT_WITHDRAW")
    assert s["ZT_FLOW:w2"] == ("NORMAL", "COST_LABOR")  # contractor/courier withdrawal = labour cost
    assert s["ZT_FLOW:w3"] == ("NORMAL", "COST_OTHER")
    assert s["ZT_FLOW:w4"][0] == "REVIEW"  # mapped to an own account but never arrived
    assert s["BANK_SMS:b9"] == ("NORMAL", "COST_WAYBILL")
    assert s["ALIPAY:a1"] == s["JOURNAL:j6"] == ("TRANSFER", "XFER_INTERNAL")
    fee_link = next(lk for lk in result.links if lk["flow_a"] == "ALIPAY:a1")
    assert fee_link["fee_cents"] == 200
    assert s["ALIPAY:a2"][0] == "REVIEW"
    assert s["JOURNAL:j7"] == ("NORMAL", "COST_LABOR")
    assert s["JOURNAL:j8"][0] == "REVIEW" and "矛盾" in result.flows["JOURNAL:j8"].reason
    assert s["JOURNAL:j9"][0] == "IGNORED"
    recent = Matcher(store, settings, today=date(2026, 9, 2)).run()
    assert recent.flows["ZT_FLOW:w4"].state == "PENDING"
    assert recent.flows["BANK_SMS:b9"].state == "PENDING"  # top-up may still arrive


def test_manual_decisions_override(env):
    settings, store = env
    add(store, D1, [
        flow("ALIPAY", "a2", "OWNER_ALIPAY", D1, "OUT", 3225, "账户间互转"),
        flow("JOURNAL", "j1", "STAFF_ALIPAY", D1, "IN", 3225, "其他收入"),
        flow("JOURNAL", "j2", "STAFF_WECHAT", D1, "OUT", 77, "其他支出"),
        flow("ZT_FLOW", "w2", "ZT_MAIN", D1, "OUT", 10000, "线下提现", initiator="000.2"),
    ])
    now = now_text()
    store.execute("INSERT INTO manual_decisions (flow_id, decision, target_flow_id, category, note, actor, created_at) VALUES ('ALIPAY:a2','transfer','JOURNAL:j1',NULL,'店主转人工账户',?,?)",
                  ("tester", now))
    store.execute("INSERT INTO manual_decisions (flow_id, decision, target_flow_id, category, note, actor, created_at) VALUES ('JOURNAL:j2','ignore',NULL,NULL,'测试数据',?,?)",
                  ("tester", now))
    store.execute("INSERT INTO manual_decisions (flow_id, decision, target_flow_id, category, note, actor, created_at) VALUES ('ZT_FLOW:w2','normal',NULL,'COST_LABOR','承包区结算',?,?)",
                  ("tester", now))
    result = Matcher(store, settings, today=date(2026, 9, 30)).run()
    assert result.flows["ALIPAY:a2"].state == result.flows["JOURNAL:j1"].state == "TRANSFER"
    assert result.flows["JOURNAL:j2"].state == "IGNORED"
    assert (result.flows["ZT_FLOW:w2"].state, result.flows["ZT_FLOW:w2"].category) == ("NORMAL", "COST_LABOR")


def test_daily_result_profit_balances_and_status(env):
    settings, store = env
    zt = [(("主营业务收入", "进港派件收入", "收派件派费收入", "收派件派费"), 1836352),
          (("主营业务成本", "出港业务成本", "付寄件签收派费", "付寄件派费"), -1661993),
          (("主营业务成本", "出港业务成本", "中转费", "付中转费"), -471842),
          (("未归类", "", "", "收中通支付充值"), 4000000),
          (("未归类", "", "", "中天余额提现"), -3580000),
          (("未归类", "", "", "神秘科目"), 1000)]
    add(store, D1, zt=zt, balances=[BalanceRecord("ZT_MAIN", "2026-09-01", "ZT_SUMMARY", 361663, 230883,
                                                  6847124, 6977904)], source="ZT_SUMMARY")
    add(store, D1, [
        flow("BANK_SMS", "b1", "CORP", D1, "IN", 55000, "经营收入"),
        flow("JOURNAL", "j7", "STAFF_WECHAT", D1, "OUT", 2258, "派送成本/付业务员司机工资"),
        flow("JOURNAL", "j8", "STAFF_WECHAT", D1, "IN", 9, "神秘收入"),
    ], balances=[
        BalanceRecord("CORP", "2026-09-01", "BANK_SMS", 1000000, 6500000, 5500000, 0),
        BalanceRecord("CORP", "2026-09-01", "BANK_RECON", 1000000, 6500000, 5500000, 0),
        BalanceRecord("OWNER_ALIPAY", "2026-09-01", "ALIPAY", 5000, 5000, 0, 0),
        BalanceRecord("OWNER_ALIPAY", "2026-09-01", "BANK_RECON", 5000, 4000, 0, 0),
        BalanceRecord("STAFF_WECHAT", "2026-08-31", "JOURNAL", 600000, 600000, 0, 0),
    ])
    store.execute("INSERT INTO manual_balances VALUES ('STAFF_ALIPAY','2026-09-01 20:00', 1, '', 'x', ?)", (now_text(),))
    for code in ("ZT_SUMMARY", "JOURNAL"):
        store.execute("INSERT INTO fetches (source, biz_date, status, started_at, finished_at) "
                      "VALUES (?, '2026-09-01', 'ok', 'x', 'x')", (code,))
    payload = reconcile(store, settings, [D1], today=date(2026, 9, 5))[0]
    profit = payload["profit"]
    lines = {line["code"]: line["total"] for line in profit["lines"]}
    assert lines["INC_DELIVERY"] == 1836352
    assert lines["INC_PICKUP"] == 5500000
    assert lines["COST_SEND_DISPATCH"] == -1661993 and lines["COST_LINEHAUL"] == -471842
    assert lines["COST_LABOR"] == -225800 - 3580000  # ZT withdrawals are contractor/courier labour cost
    assert profit["income"] == 1836352 + 5500000
    assert profit["cost"] == 1661993 + 471842 + 225800 + 3580000
    assert profit["profit"] == profit["income"] - profit["cost"]
    assert profit["cash"]["profit"] == profit["profit"]  # nothing deferred on this day
    assert profit["movements"]["XFER_ZT_TOPUP"] == 4000000
    assert profit["unclassified"]["count"] == 2  # ZT 神秘科目 + offline 神秘收入
    views = {v["code"]: v for v in payload["accounts"]}
    assert views["ZT_MAIN"]["status"] == "MATCH" and views["ZT_MAIN"]["below_low"]
    assert views["CORP"]["status"] == "MATCH"
    assert views["OWNER_ALIPAY"]["status"] == "DIFF"
    assert views["STAFF_WECHAT"]["carried"] and views["STAFF_WECHAT"]["closing"] == 600000
    assert views["STAFF_ALIPAY"]["status"] == "MISSING"
    assert payload["data_status"] == "OK"
    assert payload["position"]["total"] == 230883 + 6500000 + 5000 + 600000
    stored = store.one("SELECT data_status FROM daily_results WHERE biz_date='2026-09-01'")
    assert stored["data_status"] == "OK"


def test_data_status_missing_when_required_source_failed(env):
    settings, store = env
    store.execute("INSERT INTO fetches (source, biz_date, status, error, started_at, finished_at) "
                  "VALUES ('JOURNAL', '2026-09-01', 'failed', 'zto:all_routes_failed', 'x', 'x')")
    payload = reconcile(store, settings, [D1], today=date(2026, 9, 5))[0]
    assert payload["data_status"] == "MISSING"
    assert "门户日记账" in payload["missing_sources"]
    assert states(store) == {}


# ------------------------------------------------------------------ business periods, sweeps, refresh
def test_allocate_sums_exactly():
    from cashrecon.engine.profit import allocate
    start, end = date(2026, 8, 1), date(2026, 8, 31)
    shares = [allocate(-10204783, start, end, start + timedelta(days=i)) for i in range(31)]
    assert sum(shares) == -10204783 and max(shares) - min(shares) <= 1
    assert allocate(100, start, end, date(2026, 9, 1)) == 0


def test_periods_payroll_journal_manual_and_duplicate(env):
    settings, store = env
    s20 = date(2026, 9, 20)
    add(store, s20, [
        flow("JOURNAL", "pay", "STAFF_ALIPAY", s20, "OUT", 102047.83, "付业务员工资"),
        flow("JOURNAL", "stn", "STAFF_ALIPAY", s20, "OUT", 56236.74, "付驿站入库费"),
        flow("JOURNAL", "tmp", "STAFF_WECHAT", s20, "OUT", 120, "工资-操作"),  # small: same day
        flow("JOURNAL", "rent", "STAFF_WECHAT", s20, "OUT", 3000, "场地租金"),
        flow("JOURNAL", "dupj", "CORP", s20, "IN", 160000, "收客户运费"),
        flow("BANK_SMS", "dupb", "CORP", s20, "IN", 160000, "经营收入"),
    ])
    store.execute("UPDATE flows SET period_start='2026-09-01', period_end='2026-09-30' WHERE flow_id='JOURNAL:rent'")
    store.execute("UPDATE flows SET period_start='2026-08-01', period_end='2026-08-31' WHERE flow_id='JOURNAL:dupj'")
    store.execute("INSERT INTO manual_decisions (flow_id, decision, period_start, period_end, note, actor, created_at) "
                  "VALUES ('JOURNAL:tmp','period','2026-09-19','2026-09-19','','t','x')")
    flows = Matcher(store, settings, today=date(2026, 10, 1)).run().flows
    assert (flows["JOURNAL:pay"].p_start, flows["JOURNAL:pay"].period_basis) == (date(2026, 8, 1), "payroll")
    assert flows["JOURNAL:stn"].category == "COST_STATION_DISPATCH" and flows["JOURNAL:stn"].p_end == date(2026, 8, 31)
    assert (flows["JOURNAL:tmp"].p_start, flows["JOURNAL:tmp"].period_basis) == (date(2026, 9, 19), "manual")
    assert flows["JOURNAL:rent"].period_basis == "journal"
    assert (flows["BANK_SMS:dupb"].p_start, flows["BANK_SMS:dupb"].period_basis) == (date(2026, 8, 1), "journal_dup")


def test_fund_sweep_and_owner_handover(paths, store):
    data = copy.deepcopy(BASE_CONFIG)
    data["accounts"].append({"code": "FUND", "name": "余额宝", "type": "ALIPAY", "collection": "derived"})
    data["sweeps"] = [{"account": "OWNER_ALIPAY", "fund": "FUND", "owner_hints": ["店主甲"],
                       "hint_accounts": ["STAFF_ALIPAY"]}]
    settings = settings_from_dict(data, paths)
    ensure_default_rules(store)
    sync_accounts(store, settings)
    d = date(2026, 9, 20)
    add(store, d, [
        flow("ALIPAY", "s1", "OWNER_ALIPAY", d, "OUT", 20125, "账户间互转", "****y"),
        flow("ALIPAY", "s2", "OWNER_ALIPAY", d, "IN", 2000, "未分类", "****y"),
        flow("JOURNAL", "h1", "STAFF_ALIPAY", d, "IN", 177816.84, "直链代取收入/收大客户快递费"),
    ])
    store.execute("UPDATE flows SET summary='余额自动转入' WHERE flow_id='ALIPAY:s1'")
    store.execute("UPDATE flows SET summary='转出到余额' WHERE flow_id='ALIPAY:s2'")
    store.execute("UPDATE flows SET summary='店主甲' WHERE flow_id='JOURNAL:h1'")
    store.execute("INSERT INTO manual_balances VALUES ('FUND','2026-09-19 20:00', 50000000, '', 't', 'x')")
    payload = reconcile(store, settings, [d], today=date(2026, 9, 25))[0]
    st = states(store)
    assert st["ALIPAY:s1"] == st["ALIPAY:s2"] == st["JOURNAL:h1"] == ("TRANSFER", "XFER_INTERNAL")
    assert payload["profit"]["income"] == 0  # the hand-over is not revenue
    fund = next(a for a in payload["accounts"] if a["code"] == "FUND")
    assert fund["closing"] == 50000000 + 2012500 - 200000 - 17781684
    assert fund["status"] == "BOOK_ONLY" and fund["collection"] == "系统推算"


def test_late_payroll_refreshes_previous_month(env):
    settings, store = env
    aug = [date(2026, 8, 1) + timedelta(days=i) for i in range(31)]
    for day in aug:
        store.execute("INSERT INTO fetches (source, biz_date, status, started_at, finished_at) VALUES "
                      "('JOURNAL', ?, 'ok', 'x', 'x')", (day.isoformat(),))
    reconcile(store, settings, aug, today=date(2026, 9, 1))
    before = load_daily_result(store, "2026-08-15")["profit"]["cost"]
    s20 = date(2026, 9, 20)
    add(store, s20, [flow("JOURNAL", "pay", "STAFF_ALIPAY", s20, "OUT", 31000, "付业务员工资")])
    payloads, refreshed = reconcile_full(store, settings, [s20], today=date(2026, 9, 21))
    assert len(refreshed) == 31 and date(2026, 8, 15) in refreshed
    assert load_daily_result(store, "2026-08-15")["profit"]["cost"] == before + 100000
    sep20 = payloads[0]["profit"]
    assert sep20["cost"] == 0 and sep20["cash"]["cost"] == 3100000
    assert sep20["deferred"][0]["period"] == "2026-08-01～2026-08-31"
