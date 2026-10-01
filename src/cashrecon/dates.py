"""Business-date helpers (all business dates are Asia/Shanghai calendar days)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
_UTC8 = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(SHANGHAI)


def today() -> date:
    return now().date()


def yesterday(ref: date | None = None) -> date:
    return (ref or today()) - timedelta(days=1)


def parse_day(text: str | date) -> date:
    if isinstance(text, date):
        return text
    return date.fromisoformat(text.strip())


def day_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def previous_week(ref: date | None = None) -> tuple[date, date]:
    """Monday..Sunday of the last complete week before ``ref`` (default today)."""
    ref = ref or today()
    end = ref - timedelta(days=ref.weekday() + 1)
    return end - timedelta(days=6), end


def previous_month(ref: date | None = None) -> tuple[date, date]:
    ref = ref or today()
    end = ref.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def month_of(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def from_millis(ms: int) -> datetime:
    """Portal timestamps are epoch milliseconds displayed in UTC+8."""
    return datetime.fromtimestamp(ms / 1000, _UTC8)


def fmt_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def window(day: date) -> tuple[str, str]:
    """Portal query window strings for one calendar day."""
    text = day.isoformat()
    return f"{text} 00:00:00", f"{text} 23:59:59"


WEEKDAY_CN = "一二三四五六日"


def weekday_cn(day: date) -> str:
    return "周" + WEEKDAY_CN[day.weekday()]
