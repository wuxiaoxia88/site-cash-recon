from __future__ import annotations

import re
from datetime import date

import pytest
import tomli_w

from cashrecon.db import Store
from cashrecon.engine import reconcile
from cashrecon.engine.accounts import sync_accounts
from cashrecon.engine.rules import ensure_default_rules
from cashrecon.ingest import ingest
from cashrecon.sources.base import BalanceRecord, FlowRecord, SourceBatch
from cashrecon.web import create_app
from tests.conftest import BASE_CONFIG

DAY = date(2026, 9, 30)


@pytest.fixture
def client(paths, settings, monkeypatch):
    paths.config.write_text(tomli_w.dumps(BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("cashrecon.dates.today", lambda: date(2026, 10, 2))
    with Store(paths.database) as db:
        ensure_default_rules(db)
        sync_accounts(db, settings)
        ingest(db, SourceBatch("TEST", DAY, complete=False, flows=[
            FlowRecord("ALIPAY", "a1", "OWNER_ALIPAY", "2026-09-30 10:00:00", "OUT", 322500, src_category="账户间互转",
                       counterparty="****y"),
            FlowRecord("JOURNAL", "j1", "STAFF_WECHAT", "2026-09-30 11:00:00", "OUT", 6500, src_category="神秘支出")],
            balances=[BalanceRecord("STAFF_WECHAT", "2026-09-30", "JOURNAL", 100000, 93500, 0, 6500)]))
        reconcile(db, settings, [DAY], today=date(2026, 10, 2))
    app = create_app(paths)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, paths


def token(c, url="/review"):
    html = c.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def state(paths, fid):
    with Store(paths.database) as db:
        return db.one("SELECT state, category FROM flow_states WHERE flow_id = ?", (fid,))


def test_pages_render(client):
    c, _ = client
    for url in ("/", "/reports", "/review", "/flows?date=2026-09-30", "/balances", "/rules", "/runs", "/api/status"):
        assert c.get(url).status_code == 200, url
    assert "转出至体系外" in c.get("/review").get_data(as_text=True)


def test_csrf_and_path_traversal(client):
    c, _ = client
    assert c.post("/review/decide", data={"flow_id": "ALIPAY:a1", "decision": "ignore"}).status_code == 400
    assert c.get("/reports/daily/x/..%2F..%2Fconfig.toml").status_code == 404


def test_decide_and_undo(client):
    c, paths = client
    t = token(c)
    assert state(paths, "ALIPAY:a1")["state"] == "REVIEW"
    r = c.post("/review/decide", data={"csrf": t, "flow_id": "ALIPAY:a1", "decision": "normal",
                                       "category": "OWNER_DRAW", "note": "店主提取"})
    assert r.status_code == 302
    assert tuple(state(paths, "ALIPAY:a1")) == ("NORMAL", "OWNER_DRAW")
    c.post("/review/undo", data={"csrf": t, "flow_id": "ALIPAY:a1"})
    assert state(paths, "ALIPAY:a1")["state"] == "REVIEW"


def test_rule_from_flow(client):
    c, paths = client
    t = token(c)
    c.post("/rules/from-flow", data={"csrf": t, "rule": "src_category|JOURNAL:j1", "category": "COST_OTHER"})
    assert state(paths, "JOURNAL:j1")["category"] == "COST_OTHER"
    rules = c.get("/rules").get_data(as_text=True)
    assert "神秘支出" in rules and "自定义" in rules


def test_manual_balance_entry(client):
    c, paths = client
    t = token(c, "/balances")
    c.post("/balances", data={"csrf": t, "account": "STAFF_WECHAT", "as_of": "2026-09-30T20:00", "amount": "935.50"})
    page = c.get("/balances").get_data(as_text=True)
    assert "935.50" in page and "+0.50" in page
    with Store(paths.database) as db:
        import json
        payload = json.loads(db.scalar("SELECT payload FROM daily_results WHERE biz_date='2026-09-30'"))
    wechat = next(a for a in payload["accounts"] if a["code"] == "STAFF_WECHAT")
    assert wechat["status"] == "DIFF"


def test_set_business_period(client):
    c, paths = client
    t = token(c)
    c.post("/review/decide", data={"csrf": t, "flow_id": "JOURNAL:j1", "decision": "period",
                                   "period_start": "2026-09-01", "period_end": "2026-09-30"})
    with Store(paths.database) as db:
        row = db.one("SELECT p_start, p_end, period_basis FROM flow_states WHERE flow_id='JOURNAL:j1'")
    assert tuple(row) == ("2026-09-01", "2026-09-30", "manual")
