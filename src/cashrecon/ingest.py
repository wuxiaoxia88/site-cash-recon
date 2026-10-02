"""Idempotent ingestion of source batches, plus the fetch orchestrator."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from cashrecon.config import Settings
from cashrecon.db import Store, dumps, now_text
from cashrecon.logging_setup import get_logger
from cashrecon.sources import build_sources
from cashrecon.sources.base import FlowRecord, Source, SourceBatch, SourceError
from cashrecon.zto import ZtoError

log = get_logger("ingest")

_FLOW_FIELDS = ("account_code", "biz_time", "direction", "amount_cents", "balance_after_cents", "counterparty",
                "src_category", "summary", "initiator", "status_text", "period_start", "period_end")


def flow_hash(flow: FlowRecord) -> str:
    payload = {name: getattr(flow, name) for name in _FLOW_FIELDS}
    payload["raw"] = flow.raw
    return hashlib.sha1(dumps(payload).encode("utf-8")).hexdigest()


def ingest(store: Store, batch: SourceBatch, changed_ids: list[str] | None = None) -> tuple[int, int]:
    """Upsert a batch. Returns ``(row_count, changed_count)``; changed flow ids go to ``changed_ids``."""
    now = now_text()
    day = batch.day.isoformat()
    changed = 0
    with store.tx():
        for flow in batch.flows:
            digest = flow_hash(flow)
            row = store.one("SELECT raw_hash, removed FROM flows WHERE flow_id = ?", (flow.flow_id,))
            values = (flow.account_code, flow.biz_date, flow.biz_time, flow.direction, flow.amount_cents,
                      flow.balance_after_cents, flow.counterparty, flow.src_category, flow.summary,
                      flow.initiator, flow.status_text, dumps(flow.raw), digest, flow.period_start, flow.period_end)
            if row is None:
                store.execute(
                    "INSERT INTO flows (account_code, biz_date, biz_time, direction, amount_cents, "
                    "balance_after_cents, counterparty, src_category, summary, initiator, status_text, raw_json, "
                    "raw_hash, period_start, period_end, flow_id, source, source_ref, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, flow.flow_id, flow.source, flow.source_ref, now, now))
                changed += 1
                if changed_ids is not None:
                    changed_ids.append(flow.flow_id)
            elif row["raw_hash"] != digest or row["removed"]:
                store.execute(
                    "UPDATE flows SET account_code=?, biz_date=?, biz_time=?, direction=?, amount_cents=?, "
                    "balance_after_cents=?, counterparty=?, src_category=?, summary=?, initiator=?, status_text=?, "
                    "raw_json=?, raw_hash=?, period_start=?, period_end=?, last_seen=?, removed=0 WHERE flow_id=?",
                    (*values, now, flow.flow_id))
                changed += 1
                if changed_ids is not None:
                    changed_ids.append(flow.flow_id)
            else:
                store.execute("UPDATE flows SET last_seen=? WHERE flow_id=?", (now, flow.flow_id))
        if batch.complete:
            for source in batch.flow_sources:
                present = [f.source_ref for f in batch.flows if f.source == source and f.biz_date == day]
                placeholders = ",".join("?" * len(present)) or "''"
                cursor = store.execute(
                    f"UPDATE flows SET removed=1, last_seen=? WHERE source=? AND biz_date=? AND removed=0 "
                    f"AND source_ref NOT IN ({placeholders})", (now, source, day, *present))
                changed += cursor.rowcount
        for bal in batch.balances:
            store.execute(
                "INSERT INTO balances (biz_date, account_code, source, opening_cents, closing_cents, inflow_cents, "
                "outflow_cents, note, captured_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(biz_date, account_code, source) DO UPDATE SET opening_cents=excluded.opening_cents, "
                "closing_cents=excluded.closing_cents, inflow_cents=excluded.inflow_cents, "
                "outflow_cents=excluded.outflow_cents, note=excluded.note, captured_at=excluded.captured_at",
                (bal.biz_date, bal.account_code, bal.source, bal.opening_cents, bal.closing_cents,
                 bal.inflow_cents, bal.outflow_cents, bal.note, now))
        if batch.source == "ZT_SUMMARY":
            store.execute("DELETE FROM zt_categories WHERE biz_date = ?", (day,))
            for (l1, l2, l3, desc), amount in batch.zt_categories:
                path = "/".join((l1, l2, l3, desc))
                store.execute(
                    "INSERT INTO zt_categories (biz_date, path, level1, level2, level3, description, amount_cents, "
                    "captured_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(biz_date, path) DO UPDATE SET "
                    "amount_cents = zt_categories.amount_cents + excluded.amount_cents",
                    (day, path, l1, l2, l3, desc, amount, now))
        for component, amount in batch.bill_profit.items():
            store.execute(
                "INSERT INTO bill_profit (biz_date, component, amount_cents, status, captured_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(biz_date, component) DO UPDATE SET amount_cents=excluded.amount_cents, "
                "status=excluded.status, captured_at=excluded.captured_at",
                (day, component, amount, "ok" if amount is not None else "missing", now))
    return batch.row_count, changed


@dataclass
class FetchResult:
    source: str
    day: str
    status: str
    rows: int = 0
    changed: int = 0
    error: str = ""
    route: str = ""
    notes: list[str] = field(default_factory=list)
    changed_ids: list[str] = field(default_factory=list)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, ZtoError):
        return f"zto:{exc}"
    if isinstance(exc, SourceError):
        return str(exc)[:300]
    return exc.__class__.__name__


def fetch_days(settings: Settings, store: Store, days: Iterable[date], *, only: list[str] | None = None,
               run_id: str | None = None, sources: dict[str, Source] | None = None) -> list[FetchResult]:
    results: list[FetchResult] = []
    built_error: str | None = None
    if sources is None:
        try:
            sources = build_sources(settings, only=only)
        except (ZtoError, SourceError) as exc:
            built_error, sources = _safe_error(exc), {}
    for day in days:
        if built_error:
            results.append(_record(store, run_id, FetchResult("*", day.isoformat(), "failed", error=built_error),
                                   now_text()))
            continue
        for code, source in sources.items():
            started = now_text()
            t0 = time.monotonic()
            try:
                batch = source.fetch(day)
                changed_ids: list[str] = []
                rows, changed = ingest(store, batch, changed_ids)
                status = "ok" if rows else "empty"
                result = FetchResult(code, day.isoformat(), status, rows, changed, route=batch.route,
                                     notes=batch.notes, changed_ids=changed_ids)
                if not batch.complete:
                    result.notes.append("数据可能不完整")
            except Exception as exc:  # one failing source must not stop the others
                log.warning("fetch %s %s failed: %s", code, day, _safe_error(exc))
                log.debug("fetch traceback", exc_info=True)
                result = FetchResult(code, day.isoformat(), "failed", error=_safe_error(exc))
            log.info("fetch %s %s %s rows=%s changed=%s (%.1fs)", code, day, result.status, result.rows,
                     result.changed, time.monotonic() - t0)
            results.append(_record(store, run_id, result, started))
    return results


def _record(store: Store, run_id: str | None, result: FetchResult, started: str) -> FetchResult:
    error = result.error
    if result.notes:
        error = (error + " | " if error else "") + "；".join(result.notes)[:500]
    store.execute("INSERT INTO fetches (run_id, source, biz_date, status, row_count, changed_count, error, route, "
                  "started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (run_id, result.source, result.day, result.status, result.rows, result.changed, error,
                   result.route, started, now_text()))
    return result


def latest_fetch_status(store: Store, day: str) -> dict[str, dict[str, Any]]:
    rows = store.query("SELECT f.* FROM fetches f JOIN (SELECT source, MAX(id) AS id FROM fetches "
                       "WHERE biz_date = ? GROUP BY source) last ON f.id = last.id", (day,))
    return {r["source"]: dict(r) for r in rows}
