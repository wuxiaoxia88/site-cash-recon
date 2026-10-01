"""Reconciliation, reporting, pipeline and maintenance commands."""

from __future__ import annotations

import argparse

from cashrecon.money import fmt_yuan


def cmd_recon(args: argparse.Namespace) -> int:
    from cashrecon.commands import context, day_list
    from cashrecon.engine import reconcile
    with context(args) as (settings, store):
        payloads = reconcile(store, settings, day_list(args))
    for p in payloads:
        profit = p["profit"]
        print(f"{p['day']} {p['weekday']} 数据{p['data_status']:<7} 经营利润 {fmt_yuan(profit['profit']):>12} "
              f"收入 {fmt_yuan(profit['income']):>12} 成本 {fmt_yuan(profit['cost']):>12} "
              f"头寸 {fmt_yuan(p['position']['total']):>12} 待核 {len(p['recon']['review'])}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from cashrecon import dates
    from cashrecon.commands import context
    from cashrecon.reports import render_report
    with context(args) as (settings, store):
        ref = dates.parse_day(args.date) if args.date else None
        artifact = render_report(store, settings, args.cadence, ref)
    print(artifact.view["headline"])
    print(f"报表：{artifact.html_path}")
    if args.open:
        import webbrowser
        webbrowser.open(artifact.html_path.as_uri())
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from cashrecon.commands import add_range
    p = sub.add_parser("recon", help="重算对账与日结果")
    add_range(p)
    p.set_defaults(func=cmd_recon)

    p = sub.add_parser("report", help="生成日报/周报/月报（不投递）")
    p.add_argument("cadence", choices=("daily", "weekly", "monthly"))
    p.add_argument("--date", help="日报日期；周报/月报取该日期所在的周/月（默认上一个完整周期）")
    p.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    p.set_defaults(func=cmd_report)
