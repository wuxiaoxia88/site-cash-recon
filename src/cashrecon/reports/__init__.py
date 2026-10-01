"""Report rendering: HTML (self-contained) + JSON, and CSV for monthly reports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from cashrecon import dates
from cashrecon.config import Settings
from cashrecon.db import Store
from cashrecon.money import fmt_wan, fmt_yuan
from cashrecon.paths import atomic_write


@dataclass
class ReportArtifact:
    cadence: str
    period_key: str
    title: str
    html_path: Path
    json_path: Path
    csv_path: Path | None
    content_hash: str
    view: dict[str, Any]

    @property
    def report_key(self) -> str:
        return f"{self.cadence}:{self.period_key}"


def _yuan(cents: Any, sign: bool = False, blank_zero: bool = False) -> str:
    if cents is None:
        return "—"
    if blank_zero and cents == 0:
        return ""
    return fmt_yuan(int(cents), sign=sign)


def _pct(value: float) -> str:
    return f"{value:.0%}"


def _share(value: int, total: int) -> str:
    from cashrecon.reports.charts import share_bar
    return share_bar(value, total)


def environment() -> Environment:
    env = Environment(loader=PackageLoader("cashrecon.reports", "templates"),
                      autoescape=select_autoescape(["html", "j2"]), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update(yuan=_yuan, wan=lambda c: "—" if c is None else fmt_wan(int(c)), pct=_pct)
    env.globals["share"] = _share
    return env


def period_for(cadence: str, ref: date | None) -> tuple[date, date]:
    if cadence == "daily":
        day = ref or dates.yesterday()
        return day, day
    if cadence == "weekly":
        if ref is None:
            return dates.previous_week()
        start = ref - timedelta(days=ref.weekday())
        return start, start + timedelta(days=6)
    if ref is None:
        return dates.previous_month()
    return dates.month_of(ref)


def _monthly_csv(view: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["日期", "星期", "经营收入", "经营成本", "经营利润", "现金头寸", "数据状态", "待核笔数"])
    for d in view["daily"]:
        writer.writerow([d["day"], d["weekday"], d["income"] / 100, d["cost"] / 100, d["profit"] / 100,
                         d["position"] / 100, d["status"], d["review"]])
    writer.writerow([])
    writer.writerow(["科目", "类别", "中天", "线下", "合计"])
    for x in view["agg"]["lines"]:
        writer.writerow([x["name"], {"income": "收入", "cost": "成本"}.get(x["kind"], x["kind"]),
                         x["zt"] / 100, x["offline"] / 100, x["total"] / 100])
    return "﻿" + buffer.getvalue()  # BOM so Excel on Windows opens UTF-8 correctly


def render_report(store: Store, settings: Settings, cadence: str, ref: date | None = None) -> ReportArtifact:
    from cashrecon.reports.model import daily_view, period_view
    start, end = period_for(cadence, ref)
    view = daily_view(store, settings, start) if cadence == "daily" else period_view(store, settings, cadence, start, end)
    template = "daily.html.j2" if cadence == "daily" else "period.html.j2"
    html = environment().get_template(template).render(v=view)
    folder = settings.paths.reports / cadence / view["period_key"]
    stem = f"{view['title']}-{view['period_key']}"
    html_path, json_path = folder / f"{stem}.html", folder / f"{stem}.json"
    atomic_write(html_path, html)
    atomic_write(json_path, json.dumps(view, ensure_ascii=False, indent=1, default=str))
    csv_path = None
    if cadence == "monthly":
        csv_path = folder / f"{stem}.csv"
        atomic_write(csv_path, _monthly_csv(view))
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return ReportArtifact(cadence, view["period_key"], view["title"], html_path, json_path, csv_path, digest, view)
