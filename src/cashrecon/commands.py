"""Operational sub-commands. Heavy modules are imported lazily per command."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from cashrecon import dates
from cashrecon.config import Settings, load_settings
from cashrecon.db import Store
from cashrecon.paths import Paths


@contextmanager
def context(args: argparse.Namespace) -> Iterator[tuple[Settings, Store]]:
    settings = load_settings(Paths.resolve(args.home).ensure())
    store = Store(settings.paths.database)
    try:
        from cashrecon.engine.rules import ensure_default_rules
        ensure_default_rules(store)
        from cashrecon.engine.accounts import sync_accounts
        sync_accounts(store, settings)
        yield settings, store
    finally:
        store.close()


def add_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="单日 YYYY-MM-DD（默认昨日）")
    parser.add_argument("--from", dest="start", help="开始日期")
    parser.add_argument("--to", dest="end", help="结束日期（含）")


def day_list(args: argparse.Namespace) -> list[date]:
    if args.start or args.end:
        start = dates.parse_day(args.start or args.end)
        end = dates.parse_day(args.end or args.start)
        if end < start:
            raise SystemExit("结束日期早于开始日期")
        return list(dates.day_range(start, end))
    return [dates.parse_day(args.date) if args.date else dates.yesterday()]


# ---------------------------------------------------------------------- fetch
def cmd_fetch(args: argparse.Namespace) -> int:
    from cashrecon.ingest import fetch_days
    with context(args) as (settings, store):
        results = fetch_days(settings, store, day_list(args), only=args.source or None)
    failed = 0
    for r in results:
        mark = {"ok": "✓", "empty": "·", "failed": "✗"}.get(r.status, "?")
        extra = f" {r.error}" if r.error else ""
        notes = f" （{'；'.join(r.notes)}）" if r.notes else ""
        print(f"{mark} {r.day} {r.source:<11} 行数 {r.rows:>5} 变化 {r.changed:>5}{extra}{notes}")
        failed += r.status == "failed"
    return 1 if failed else 0


def cmd_accounts(args: argparse.Namespace) -> int:
    from cashrecon.sources.journal import list_portal_accounts
    from cashrecon.zto import ZtoClient
    settings = load_settings(Paths.resolve(args.home))
    client = ZtoClient.from_settings(settings)
    print("门户日记账账户（用于配置 accounts.portal_code）：")
    for item in list_portal_accounts(client):
        configured = settings.account_by_portal(item["portal_code"] or "")
        print(f"  {item['portal_code']}  {item['name']}  {item['type']}/{item['nature']} {item['bank']}"
              f" 尾号{item['tail'] or '-'}  → {configured.code if configured else '未配置'}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("fetch", help="采集/回补数据")
    add_range(p)
    p.add_argument("--source", action="append", help="只采集指定来源（可重复）")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("accounts", help="列出门户日记账账户，辅助配置")
    p.set_defaults(func=cmd_accounts)

    from cashrecon import commands_ops
    commands_ops.register(sub)
