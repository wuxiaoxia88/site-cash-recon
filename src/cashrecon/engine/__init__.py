"""Reconciliation engine entry point."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.logging_setup import get_logger

log = get_logger("engine")
MAX_REFRESH_DAYS = 400


def _state_map(store: Store) -> dict[str, tuple]:
    return {r["flow_id"]: (r["state"], r["category"], r["p_start"], r["p_end"])
            for r in store.query("SELECT flow_id, state, category, p_start, p_end FROM flow_states")}


def _days_of(entry: tuple | None) -> set[date]:
    if not entry or not entry[2] or not entry[3]:
        return set()
    start, end = date.fromisoformat(entry[2]), date.fromisoformat(entry[3])
    if (end - start).days > MAX_REFRESH_DAYS:
        start = end - timedelta(days=MAX_REFRESH_DAYS)
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def reconcile_full(store: Store, settings: Settings, days: Iterable[date], *, today: date | None = None
                   ) -> tuple[list[dict], list[date]]:
    """Recompute matching for all flows, then daily results for ``days`` plus every earlier day whose
    business-period allocation changed (e.g. last month's payroll registered today).

    Returns ``(payloads for days, refreshed other days)``.
    """
    from cashrecon import dates
    from cashrecon.engine.matching import Matcher, persist
    from cashrecon.engine.result import build_daily_result, save_daily_result
    from cashrecon.engine.rules import Classifier

    today = today or dates.today()
    days = sorted(set(days))
    before = _state_map(store)
    result = Matcher(store, settings, today=today).run()
    persist(store, result)
    after = _state_map(store)
    touched: set[date] = set()
    for flow_id in before.keys() | after.keys():
        if before.get(flow_id) != after.get(flow_id):
            touched |= _days_of(before.get(flow_id)) | _days_of(after.get(flow_id))
    existing = {r["biz_date"] for r in store.query("SELECT biz_date FROM daily_results")}
    refresh = sorted(d for d in touched if d not in days and d < today and d.isoformat() in existing)
    classifier = Classifier(store)
    payloads = []
    for day in days + refresh:
        payload = build_daily_result(store, settings, day, classifier)
        save_daily_result(store, payload)
        if day in days:
            payloads.append(payload)
            log.info("recon %s status=%s profit=%s review=%s", day, payload["data_status"],
                     payload["profit"]["profit"], len(payload["recon"]["review"]))
    if refresh:
        log.info("refreshed %s earlier day(s) whose business-period allocation changed", len(refresh))
    return payloads, refresh


def reconcile(store: Store, settings: Settings, days: Iterable[date], *, today: date | None = None) -> list[dict]:
    return reconcile_full(store, settings, days, today=today)[0]
