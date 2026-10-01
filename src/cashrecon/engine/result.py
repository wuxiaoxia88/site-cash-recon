"""Assemble the daily result (single input for reports, alerts and analysis)."""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from cashrecon import dates
from cashrecon.config import Settings
from cashrecon.db import Store, dumps, now_text
from cashrecon.engine import categories
from cashrecon.engine.balances import account_views, position
from cashrecon.engine.profit import bill_profit_day, profit_day
from cashrecon.engine.rules import Classifier
from cashrecon.ingest import latest_fetch_status
from cashrecon.sources import SOURCE_CN

STATE_CN = {"NORMAL": "正常", "DUPLICATE": "重复（已剔除）", "TRANSFER": "内部划转", "PENDING": "在途",
            "REVIEW": "待核", "IGNORED": "已忽略"}
REVIEW_LOOKBACK_DAYS = 14


def source_health(store: Store, settings: Settings, day: date) -> tuple[str, list[dict[str, Any]]]:
    text = day.isoformat()
    latest = latest_fetch_status(store, text)
    items, status = [], "OK"
    for code, cfg in settings.sources.items():
        if not cfg.get("enabled"):
            continue
        required = bool(cfg.get("required", True))
        rec = latest.get(code) or latest.get("*")
        fetch_status = rec["status"] if rec else "never"
        note = rec["error"] if rec else "尚未采集"
        incomplete = bool(rec) and ("不完整" in (rec["error"] or ""))
        if fetch_status in ("failed", "never"):
            status = "MISSING" if required else ("PARTIAL" if status == "OK" else status)
        elif incomplete and status == "OK":
            status = "PARTIAL"
        items.append({"code": code, "name": SOURCE_CN.get(code, code), "required": required,
                      "status": fetch_status, "rows": rec["row_count"] if rec else 0, "note": note,
                      "incomplete": incomplete})
    # row-count anomaly against the previous 14 days
    for item in items:
        if item["status"] not in ("ok", "empty") or item["code"] not in ("JOURNAL", "BANK_LEDGER", "ZT_FLOW"):
            continue
        history = [r[0] for r in store.query(
            "SELECT row_count FROM fetches WHERE source = ? AND biz_date BETWEEN ? AND ? AND status IN ('ok','empty') "
            "GROUP BY biz_date HAVING id = MAX(id)",
            (item["code"], (day - timedelta(days=14)).isoformat(), (day - timedelta(days=1)).isoformat()))]
        if len(history) >= 7:
            median = statistics.median(history)
            if median >= 5 and item["rows"] < 0.3 * median:
                item["anomaly"] = f"行数 {item['rows']} 明显低于近两周中位数 {median:.0f}"
                if status == "OK":
                    status = "PARTIAL"
    return status, items


def _flow_rows(store: Store, where: str, params: tuple) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT f.flow_id, f.source, f.account_code, f.biz_date, f.biz_time, f.direction, f.amount_cents, "
        "f.src_category, f.summary, f.counterparty, f.initiator, s.state, s.category, s.reason, s.link_id, "
        "a.name AS account_name FROM flows f JOIN flow_states s USING (flow_id) "
        "LEFT JOIN accounts a ON a.account_code = f.account_code WHERE " + where +
        " ORDER BY f.biz_time", params)
    result = []
    for r in rows:
        item = dict(r)
        item["account_name"] = item["account_name"] or item["account_code"]
        item["category_name"] = categories.name(item["category"])
        item["source_name"] = SOURCE_CN.get(item["source"], item["source"])
        item["signed"] = item["amount_cents"] if item["direction"] == "IN" else -item["amount_cents"]
        result.append(item)
    return result


def recon_summary(store: Store, day: date) -> dict[str, Any]:
    text = day.isoformat()
    counts: dict[str, int] = {}
    amounts: dict[str, int] = {}
    for r in store.query("SELECT s.state, COUNT(*) n, SUM(f.amount_cents) a FROM flows f JOIN flow_states s "
                         "USING (flow_id) WHERE f.biz_date = ? AND f.source <> 'ZT_FLOW' GROUP BY s.state", (text,)):
        counts[r["state"]], amounts[r["state"]] = r["n"], r["a"] or 0
    start = (day - timedelta(days=REVIEW_LOOKBACK_DAYS)).isoformat()
    review = _flow_rows(store, "s.state = 'REVIEW' AND f.biz_date BETWEEN ? AND ?", (start, text))
    pending = _flow_rows(store, "s.state = 'PENDING' AND f.biz_date BETWEEN ? AND ?", (start, text))
    duplicates = store.scalar("SELECT COUNT(*) FROM links WHERE kind='DUPLICATE' AND biz_date = ?", (text,))
    return {"counts": counts, "amounts": amounts,
            "counts_cn": {STATE_CN.get(k, k): v for k, v in counts.items()},
            "duplicates": duplicates, "review": review, "pending": pending,
            "review_today": sum(1 for r in review if r["biz_date"] == text)}


def movements_detail(store: Store, settings: Settings, day: date, totals: dict[str, int]) -> dict[str, Any]:
    text = day.isoformat()
    zt = settings.zt_account.code if settings.zt_account else ""
    withdrawals = _flow_rows(store, "f.account_code = ? AND f.biz_date = ? AND f.direction = 'OUT' "
                             "AND f.src_category IN ('线下提现','中天余额提现')", (zt, text))
    for w in withdrawals:
        partner = store.one("SELECT f.account_code, a.name FROM links l JOIN flows f ON f.flow_id = l.flow_b "
                            "LEFT JOIN accounts a ON a.account_code = f.account_code WHERE l.flow_a = ?",
                            (w["flow_id"],))
        w["landed"] = partner["name"] if partner else None
    by_initiator: dict[str, dict[str, Any]] = {}
    for w in withdrawals:
        key = w["initiator"] or "未知"
        entry = by_initiator.setdefault(key, {"initiator": key, "count": 0, "amount": 0, "landed": 0})
        entry["count"] += 1
        entry["amount"] += w["amount_cents"]
        entry["landed"] += w["amount_cents"] if w["landed"] else 0
        mapping = settings.withdraw_initiators.get(key, {})
        entry["mapped"] = mapping.get("note") or mapping.get("account") or categories.name(mapping["category"]) \
            if mapping.get("category") else mapping.get("note") or mapping.get("account") or ""
    transfers = []
    for link in store.query("SELECT * FROM links WHERE biz_date = ? AND kind IN ('TRANSFER','ZT_TOPUP','ZT_WITHDRAW')",
                            (text,)):
        a = store.one("SELECT f.account_code, f.amount_cents, ac.name FROM flows f LEFT JOIN accounts ac "
                      "USING (account_code) WHERE flow_id = ?", (link["flow_a"],))
        b = store.one("SELECT f.account_code, ac.name FROM flows f LEFT JOIN accounts ac USING (account_code) "
                      "WHERE flow_id = ?", (link["flow_b"],)) if link["flow_b"] else None
        transfers.append({"kind": link["kind"], "from": a["name"] if a else "?", "to": b["name"] if b else "?",
                          "amount": link["amount_cents"], "fee": link["fee_cents"], "actor": link["actor"]})
    names = {code: categories.name(code) for code in totals}
    return {"totals": totals, "totals_cn": names, "withdrawals": withdrawals,
            "withdrawals_by_initiator": sorted(by_initiator.values(), key=lambda x: -x["amount"]),
            "transfers": transfers}


def build_daily_result(store: Store, settings: Settings, day: date, classifier: Classifier | None = None
                       ) -> dict[str, Any]:
    classifier = classifier or Classifier(store)
    data_status, sources = source_health(store, settings, day)
    views = account_views(store, settings, day)
    profit = profit_day(store, settings, classifier, day)
    payload = {
        "schema": 1,
        "day": day.isoformat(),
        "weekday": dates.weekday_cn(day),
        "site": settings.site_name,
        "generated_at": now_text(),
        "data_status": data_status,
        "sources": sources,
        "missing_sources": [s["name"] for s in sources if s["status"] in ("failed", "never")],
        "accounts": views,
        "position": position(views),
        "profit": profit,
        "bill_profit": bill_profit_day(store, day),
        "movements": movements_detail(store, settings, day, profit["movements"]),
        "recon": recon_summary(store, day),
        "rules_version": classifier.version[-40:],
    }
    return payload


def save_daily_result(store: Store, payload: dict[str, Any]) -> None:
    store.execute("INSERT INTO daily_results (biz_date, data_status, payload, rules_version, computed_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(biz_date) DO UPDATE SET data_status=excluded.data_status, "
                  "payload=excluded.payload, rules_version=excluded.rules_version, computed_at=excluded.computed_at",
                  (payload["day"], payload["data_status"], dumps(payload), payload["rules_version"],
                   payload["generated_at"]))


def load_daily_result(store: Store, day: date | str) -> dict[str, Any] | None:
    import json
    text = day if isinstance(day, str) else day.isoformat()
    row = store.one("SELECT payload FROM daily_results WHERE biz_date = ?", (text,))
    return json.loads(row["payload"]) if row else None
