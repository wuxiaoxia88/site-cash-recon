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


def cmd_run(args: argparse.Namespace) -> int:
    from cashrecon import dates
    from cashrecon.commands import context
    from cashrecon.pipeline import JobLocked, Runner, job_lock
    with context(args) as (settings, store):
        try:
            with job_lock(settings):
                runner = Runner(settings, store, deliver=not args.no_deliver, dry_run=args.dry_run)
                summary = runner.run(args.job, dates.parse_day(args.as_of) if args.as_of else None)
        except JobLocked:
            print("已有任务在运行，本次跳过")
            return 0
    print(f"运行 {summary['run_id']}：{ {'success': '成功', 'partial': '部分完成', 'failed': '失败'}[summary['status']] }")
    for d in summary["deliveries"]:
        print(f"  投递 {d['report']} {d['channel']}: {d['status']} {d['detail']}")
    for e in summary["errors"]:
        print(f"  问题：{e}")
    return 1 if summary["status"] == "failed" else 0


def cmd_deliver(args: argparse.Namespace) -> int:
    from cashrecon import dates
    from cashrecon.commands import context
    from cashrecon.delivery import deliver
    from cashrecon.reports import render_report
    with context(args) as (settings, store):
        artifact = render_report(store, settings, args.cadence, dates.parse_day(args.date) if args.date else None)
        results = deliver(store, settings, artifact, dry_run=args.dry_run, force=args.force,
                          channels=args.channel or None)
    for r in results:
        print(f"{r.channel}: {r.status} {r.detail} {r.receipt}")
    return 1 if any(r.status == "failed" for r in results) else 0


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

    p = sub.add_parser("run", help="完整任务：采集→对账→报表→投递（调度器调用此命令）")
    p.add_argument("job", choices=("daily", "retry", "weekly", "monthly"))
    p.add_argument("--as-of", help="以该日期作为“今天”运行（补跑/演练）")
    p.add_argument("--no-deliver", action="store_true", help="只生成报表，不投递")
    p.add_argument("--dry-run", action="store_true", help="投递演练：生成邮件预览，不实际发送")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("deliver", help="投递指定报表")
    p.add_argument("cadence", choices=("daily", "weekly", "monthly"))
    p.add_argument("--date")
    p.add_argument("--channel", action="append", choices=("kb", "mail"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="忽略投递台账强制重发（慎用）")
    p.set_defaults(func=cmd_deliver)
