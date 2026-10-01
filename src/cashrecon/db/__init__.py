"""SQLite storage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cashrecon import dates
from cashrecon.db.schema import MIGRATIONS
from cashrecon.paths import ensure_private_dir, make_private_file


def now_text() -> str:
    return dates.fmt_time(dates.now())


class Store:
    """Thin wrapper over a SQLite connection with migrations and helpers."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            ensure_private_dir(self.path.parent)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path is not None:
            self.conn.execute("PRAGMA journal_mode = WAL")
            make_private_file(self.path)
        self.migrate()

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            with self.tx():
                for statement in _split(MIGRATIONS[index]):
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {index + 1}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction (connection runs in autocommit mode otherwise)."""
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- helpers -------------------------------------------------------------
    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else value

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def backup_to(self, target: Path) -> None:
        ensure_private_dir(target.parent)
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        make_private_file(target)

    def integrity_ok(self) -> bool:
        return self.scalar("PRAGMA integrity_check") == "ok"


def _split(script: str) -> list[str]:
    statements, buffer = [], []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buffer = []
    rest = "\n".join(buffer).strip()
    if rest:
        statements.append(rest)
    return [s for s in statements if s.strip(";").strip()]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
