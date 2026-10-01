"""Reconciliation engine entry point."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.logging_setup import get_logger

log = get_logger("engine")


def reconcile(store: Store, settings: Settings, days: Iterable[date], *, today: date | None = None) -> list[dict]:
    """Recompute matching for all flows, then daily results for ``days``."""
    from cashrecon import dates
    from cashrecon.engine.matching import Matcher, persist
    from cashrecon.engine.result import build_daily_result, save_daily_result
    from cashrecon.engine.rules import Classifier

    matcher = Matcher(store, settings, today=today or dates.today())
    result = matcher.run()
    persist(store, result)
    classifier = Classifier(store)
    payloads = []
    for day in days:
        payload = build_daily_result(store, settings, day, classifier)
        save_daily_result(store, payload)
        payloads.append(payload)
        log.info("recon %s status=%s profit=%s review=%s", day, payload["data_status"],
                 payload["profit"]["profit"], len(payload["recon"]["review"]))
    return payloads
