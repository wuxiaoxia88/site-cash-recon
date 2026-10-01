"""Job orchestration: fetch → reconcile → alerts → reports → delivery, with self-healing.

Jobs
  daily    yesterday + any of the previous ``self_heal_days`` days that are missing or incomplete
  retry    only incomplete days and undelivered reports in the last ``self_heal_days`` days
  weekly   last complete week (missing days are fetched first)
  monthly  last complete month (missing days are fetched first)

Every run is recorded in ``runs`` and the log file. Any unhandled error marks the run
failed and triggers a best-effort notice e-mail, so failures are never silent.
"""

from __future__ import annotations

import os
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

from cashrecon import dates
from cashrecon.config import Settings
from cashrecon.db import Store, dumps, now_text
from cashrecon.logging_setup import get_logger
from cashrecon.paths import atomic_write

log = get_logger("pipeline")
LOCK_STALE_SECONDS = 3 * 3600


class JobLocked(RuntimeError):
    pass


@contextmanager
def job_lock(settings: Settings):
    path = settings.paths.lock
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - path.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise JobLocked("another run is in progress") from None
        log.warning("removing stale lock (%.0f s old)", age)
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _incomplete_days(store: Store, start: date, end: date) -> list[date]:
    days = []
    for day in dates.day_range(start, end):
        row = store.one("SELECT data_status FROM daily_results WHERE biz_date = ?", (day.isoformat(),))
        if row is None or row["data_status"] != "OK":
            days.append(day)
    return days


def _undelivered(store: Store, settings: Settings, start: date, end: date) -> list[date]:
    channels = [c for c in ("kb", "mail") if settings.delivery.get(c, {}).get("enabled")]
    if not channels:
        return []
    result = []
    for day in dates.day_range(start, end):
        key = f"daily:{day.isoformat()}"
        statuses = {r["channel"]: r["status"] for r in
                    store.query("SELECT channel, status FROM deliveries WHERE report_key = ?", (key,))}
        if any(statuses.get(c) in (None, "failed") or (c == "kb" and statuses.get(c) == "unknown")
               for c in channels):
            result.append(day)
    return result


def backup(store: Store, settings: Settings, keep: int = 30) -> None:
    target = settings.paths.backups / f"cashrecon-{dates.today().strftime('%Y%m%d')}.db"
    if not target.exists():
        store.backup_to(target)
    olds = sorted(settings.paths.backups.glob("cashrecon-*.db"))[:-keep]
    for old in olds:
        old.unlink(missing_ok=True)


class Runner:
    def __init__(self, settings: Settings, store: Store, *, deliver: bool = True, dry_run: bool = False) -> None:
        self.settings, self.store = settings, store
        self.deliver_enabled, self.dry_run = deliver, dry_run
        self.run_id = dates.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.summary: dict[str, Any] = {"steps": [], "deliveries": [], "errors": []}

    def _step(self, name: str, func, *args, **kwargs):
        t0 = time.monotonic()
        try:
            value = func(*args, **kwargs)
            self.summary["steps"].append({"step": name, "ok": True, "seconds": round(time.monotonic() - t0, 1)})
            return value
        except Exception as exc:
            log.error("step %s failed: %s", name, exc.__class__.__name__)
            log.debug("traceback", exc_info=True)
            self.summary["steps"].append({"step": name, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:300]})
            self.summary["errors"].append(name)
            return None

    def _days_job(self, fetch_days: list[date], report_days: list[date]) -> None:
        from cashrecon.engine import reconcile
        from cashrecon.ingest import fetch_days as do_fetch
        if fetch_days:
            results = self._step("fetch", do_fetch, self.settings, self.store, fetch_days, run_id=self.run_id) or []
            required = set(self.settings.required_sources()) | {"*"}
            self.summary["fetch_failed"] = [f"{r.day} {r.source}" for r in results
                                            if r.status == "failed" and r.source in required]
            self.summary["fetch_failed_optional"] = [f"{r.day} {r.source}" for r in results
                                                     if r.status == "failed" and r.source not in required]
        all_days = sorted(set(fetch_days) | set(report_days))
        self._step("reconcile", reconcile, self.store, self.settings, all_days)
        for day in report_days:
            self._report_and_deliver("daily", day)

    def _report_and_deliver(self, cadence: str, ref: date) -> None:
        from cashrecon.analysis import action_items, load_history
        from cashrecon.engine.result import load_daily_result
        from cashrecon.reports import render_report
        artifact = self._step(f"report:{cadence}:{ref}", render_report, self.store, self.settings, cadence, ref)
        if artifact is None:
            return
        if cadence == "daily":
            payload = load_daily_result(self.store, ref)
            if payload:
                from cashrecon import alerts
                items = [i.to_dict() for i in action_items(payload, self.settings, load_history(self.store, ref))]
                self._step("alerts", alerts.record, self.store, ref.isoformat(), items)
        if self.deliver_enabled:
            from cashrecon.delivery import deliver
            results = self._step(f"deliver:{artifact.report_key}", deliver, self.store, self.settings, artifact,
                                 dry_run=self.dry_run) or []
            for r in results:
                self.summary["deliveries"].append({"report": artifact.report_key, "channel": r.channel,
                                                   "status": r.status, "detail": r.detail})
                if r.status == "failed":
                    self.summary["errors"].append(f"deliver:{r.channel}")

    def run(self, job: str, as_of: date | None = None) -> dict[str, Any]:
        today = as_of or dates.today()
        target = today - timedelta(days=1)
        heal = int(self.settings.rules.get("self_heal_days", 7))
        window_start = target - timedelta(days=heal)
        self.store.execute("INSERT INTO runs (run_id, job, target, status, started_at) VALUES (?,?,?,?,?)",
                           (self.run_id, job, target.isoformat(), "running", now_text()))
        status = "failed"
        try:
            if job == "daily":
                missing = _incomplete_days(self.store, window_start, target - timedelta(days=1))
                fetch = sorted(set(missing) | {target})
                report = sorted(set(fetch) | set(_undelivered(self.store, self.settings, window_start,
                                                              target - timedelta(days=1))))
                self.summary["days"] = [d.isoformat() for d in report]
                self._days_job(fetch, report)
            elif job == "retry":
                fetch = _incomplete_days(self.store, window_start, target)
                report = sorted(set(fetch) | set(_undelivered(self.store, self.settings, window_start, target)))
                self.summary["days"] = [d.isoformat() for d in report]
                if report:
                    self._days_job(fetch, report)
            elif job in ("weekly", "monthly"):
                start, end = dates.previous_week(today) if job == "weekly" else dates.previous_month(today)
                missing = _incomplete_days(self.store, start, end)
                if missing:
                    self._days_job(missing, [])
                self._report_and_deliver(job, start)
                self.summary["period"] = [start.isoformat(), end.isoformat()]
            else:
                raise ValueError(f"unknown job {job}")
            self._step("backup", backup, self.store, self.settings)
            status = "partial" if self.summary["errors"] or self.summary.get("fetch_failed") else "success"
        except Exception as exc:
            self.summary["errors"].append(f"fatal: {exc.__class__.__name__}: {exc}"[:300])
            self.summary["traceback"] = traceback.format_exc()[-2000:]
            log.exception("run %s failed", job)
        finally:
            critical = [s for s in self.summary["steps"]
                        if not s["ok"] and (s["step"] == "reconcile" or s["step"].startswith("report:"))]
            if critical:
                status = "failed"
            self.store.execute("UPDATE runs SET status=?, summary=?, error=?, finished_at=? WHERE run_id=?",
                               (status, dumps({k: v for k, v in self.summary.items() if k != "traceback"}),
                                "; ".join(self.summary["errors"])[:1000], now_text(), self.run_id))
            atomic_write(self.settings.paths.logs / "last-run.json", dumps({"run_id": self.run_id, "job": job,
                                                                         "status": status, **self.summary}))
            if status != "success" and not self.dry_run:
                self._notify(job, status)
        self.summary["status"] = status
        self.summary["run_id"] = self.run_id
        return self.summary

    def _notify(self, job: str, status: str) -> None:
        from cashrecon.delivery import send_notice
        lines = [f"任务：{job}，运行编号 {self.run_id}，结果：{'失败' if status == 'failed' else '部分完成'}。",
                 "问题：" + ("；".join(self.summary["errors"]) or "；".join(self.summary.get("fetch_failed", []))),
                 f"日志：{self.settings.paths.logs / 'cashrecon.log'}",
                 "18:00 补跑会自动重试未完成的日期；也可在控制台手动重跑。"]
        prefix = "【紧急】" if status == "failed" else ""
        send_notice(self.settings, f"{prefix}【网点资金系统】{job} 运行{'失败' if status == 'failed' else '部分完成'}", lines)
