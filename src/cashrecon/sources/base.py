"""Common source types. Sources only fetch and map; they make no business decisions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol


class SourceError(RuntimeError):
    """A source could not deliver complete data for the requested day."""


@dataclass
class FlowRecord:
    source: str
    source_ref: str
    account_code: str
    biz_time: str
    direction: str
    amount_cents: int
    balance_after_cents: int | None = None
    counterparty: str = ""
    src_category: str = ""
    summary: str = ""
    initiator: str = ""
    status_text: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def biz_date(self) -> str:
        return self.biz_time[:10]

    @property
    def flow_id(self) -> str:
        return f"{self.source}:{self.source_ref}"

    def __post_init__(self) -> None:
        if self.direction not in ("IN", "OUT"):
            raise SourceError(f"bad direction {self.direction!r} for {self.flow_id}")
        if self.amount_cents < 0:
            raise SourceError(f"negative amount for {self.flow_id}")
        if len(self.biz_time) < 10:
            raise SourceError(f"bad time for {self.flow_id}")


@dataclass
class BalanceRecord:
    account_code: str
    biz_date: str
    source: str
    opening_cents: int | None = None
    closing_cents: int | None = None
    inflow_cents: int | None = None
    outflow_cents: int | None = None
    note: str = ""


@dataclass
class SourceBatch:
    source: str
    day: date
    flows: list[FlowRecord] = field(default_factory=list)
    balances: list[BalanceRecord] = field(default_factory=list)
    zt_categories: list[tuple[tuple[str, str, str, str], int]] = field(default_factory=list)
    bill_profit: dict[str, int | None] = field(default_factory=dict)
    flow_sources: tuple[str, ...] = ()
    complete: bool = True
    route: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.flows) + len(self.balances) + len(self.zt_categories) + len(self.bill_profit)


class Source(Protocol):
    code: str

    def fetch(self, day: date) -> SourceBatch: ...


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an upstream SQLite file strictly read-only."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise SourceError(f"database not found: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def one_line(text: object, limit: int = 200) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    return value[:limit]
