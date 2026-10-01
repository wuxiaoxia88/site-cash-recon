from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

from cashrecon.config import settings_from_dict
from cashrecon.engine import reconcile
from cashrecon.engine.accounts import sync_accounts
from cashrecon.engine.rules import ensure_default_rules
from cashrecon.ingest import ingest
from cashrecon.reports import charts, period_for, render_report
from cashrecon.reports.model import ReportError
from cashrecon.sources.base import BalanceRecord, FlowRecord, SourceBatch
from tests.conftest import BASE_CONFIG

START = date(2026, 9, 1)


@pytest.fixture
def month(paths, store):
    settings = settings_from_dict(copy.deepcopy(BASE_CONFIG), paths)
    ensure_default_rules(store)
    sync_accounts(store, settings)
    opening = 1_000_000
    days = [START + timedelta(days=i) for i in range(30)]
    for i, day in enumerate(days):
        text = day.isoformat()
        income = 2_000_000 + i * 10_000
        cost = 1_800_000 if i % 7 else 2_600_000  # one loss day per week
        closing = opening + income - cost
        zt = [(("主营业务收入", "进港派件收入", "收派件派费收入", "收派件派费"), income),
              (("主营业务成本", "出港业务成本", "中转费", "付中转费"), -cost)]
        ingest(store, SourceBatch("ZT_SUMMARY", day, zt_categories=zt, balances=[
            BalanceRecord("ZT_MAIN", text, "ZT_SUMMARY", opening, closing, income, cost)]))
        ingest(store, SourceBatch("TEST", day, complete=False, flows=[
            FlowRecord("BANK_SMS", f"b{i}", "CORP", f"{text} 10:00:00", "IN", 50_000, src_category="经营收入"),
            FlowRecord("JOURNAL", f"j{i}", "STAFF_WECHAT", f"{text} 11:00:00", "OUT", 20_000,
                       src_category="派送成本/付业务员司机工资")]))
        for code in ("ZT_SUMMARY", "JOURNAL"):
            store.execute("INSERT INTO fetches (source, biz_date, status, row_count, started_at, finished_at) "
                          "VALUES (?, ?, 'ok', 3, 'x', 'x')", (code, text))
        opening = closing
    reconcile(store, settings, days, today=date(2026, 10, 1))
    return settings, store


def test_daily_report(month):
    settings, store = month
    art = render_report(store, settings, "daily", date(2026, 9, 30))
    html = art.html_path.read_text(encoding="utf-8")
    assert "网点资金日报" in html and "需要处理" in html and "利润表" in html and "<svg" in html
    assert art.report_key == "daily:2026-09-30"
    data = json.loads(art.json_path.read_text(encoding="utf-8"))
    assert data["p"]["profit"]["profit"] == 2_290_000 - 1_800_000 + 50_000 - 20_000
    assert data["delta"]["profit"] is not None
    assert "经营盈利" in data["headline"]


def test_loss_day_is_urgent(month):
    settings, store = month
    art = render_report(store, settings, "daily", date(2026, 9, 29))  # i=28 -> loss day
    kinds = [(i["level"], i["kind"]) for i in art.view["items"]]
    assert ("high", "loss") in kinds
    assert "亏损" in art.view["headline"]


def test_weekly_and_monthly(month):
    settings, store = month
    week = render_report(store, settings, "weekly", date(2026, 9, 16))
    assert (week.view["start"], week.view["end"]) == ("2026-09-14", "2026-09-20")
    assert week.view["days_present"] == 7 and len(week.view["loss_days"]) == 1
    assert week.view["prev"] is not None and week.view["delta_profit"] is not None
    monthly = render_report(store, settings, "monthly", date(2026, 9, 5))
    assert monthly.period_key == "2026-09" and monthly.csv_path.exists()
    assert monthly.csv_path.read_bytes().startswith("﻿".encode())
    assert monthly.view["days_present"] == 30 and monthly.view["missing_days"] == []


def test_partial_period_lists_missing_days(month):
    settings, store = month
    art = render_report(store, settings, "weekly", date(2026, 9, 29))  # 9-28..10-04, only 3 days exist
    assert len(art.view["missing_days"]) == 4
    assert "缺少 4 天" in art.view["headline"]
    with pytest.raises(ReportError):
        render_report(store, settings, "daily", date(2026, 8, 1))


def test_period_for_defaults():
    assert period_for("daily", date(2026, 9, 3)) == (date(2026, 9, 3), date(2026, 9, 3))
    assert period_for("weekly", date(2026, 9, 3)) == (date(2026, 8, 31), date(2026, 9, 6))
    assert period_for("monthly", date(2026, 2, 3)) == (date(2026, 2, 1), date(2026, 2, 28))


def test_charts():
    svg = charts.bar_chart(["a", "b"], [100, -50])
    assert svg.startswith("<svg") and "#cf222e" in svg
    assert charts.line_chart(["a"], [1]) == ""
    assert "<path" in charts.line_chart(["a", "b", "c"], [1, None, 3])
