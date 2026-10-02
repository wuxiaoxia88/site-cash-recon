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
                          channels=args.channel or None, ignore_disabled=args.once)
    for r in results:
        print(f"{r.channel}: {r.status} {r.detail} {r.receipt}")
    return 1 if any(r.status == "failed" for r in results) else 0


def cmd_web(args: argparse.Namespace) -> int:
    from cashrecon.paths import Paths
    from cashrecon.web import serve
    serve(Paths.resolve(args.home), host=args.host, port=args.port, open_browser=args.open)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from cashrecon import scheduler
    from cashrecon.config import load_settings
    from cashrecon.paths import Paths
    settings = load_settings(Paths.resolve(args.home))
    if args.action == "install":
        for line in scheduler.install(settings, with_console=args.with_console):
            print("已注册", line)
    elif args.action == "uninstall":
        print("已移除：", "、".join(scheduler.uninstall(settings)) or "无")
    else:
        for line in scheduler.status(settings):
            print(line)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from cashrecon.commands import context
    from cashrecon.pipeline import backup
    with context(args) as (settings, store):
        backup(store, settings)
        print(f"备份目录：{settings.paths.backups}")
    return 0


def cmd_staff(args: argparse.Namespace) -> int:
    from cashrecon import staff
    from cashrecon.commands import context
    with context(args) as (settings, store):
        count = staff.refresh(store, settings)
        directory = staff.names(store, settings)
    print(f"员工名册：本次更新 {count} 人，共 {len(directory)} 人")
    for code in sorted(directory):
        print(f"  {code}  {directory[code]}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from cashrecon.commands import add_range
    p = sub.add_parser("recon", help="重算对账与日结果")
    add_range(p)
    p.set_defaults(func=cmd_recon)

    p = sub.add_parser("report", help="生成日报/周报/月报（不投递）")
    p.add_argument("cadence", choices=("daily", "weekly", "monthly", "monthly_final"))
    p.add_argument("--date", help="日报日期；周报/月报取该日期所在的周/月（默认上一个完整周期）")
    p.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run", help="完整任务：采集→对账→报表→投递（调度器调用此命令）")
    p.add_argument("job", choices=("daily", "retry", "weekly", "monthly", "monthly_final"))
    p.add_argument("--as-of", help="以该日期作为“今天”运行（补跑/演练）")
    p.add_argument("--no-deliver", action="store_true", help="只生成报表，不投递")
    p.add_argument("--dry-run", action="store_true", help="投递演练：生成邮件预览，不实际发送")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("deliver", help="投递指定报表")
    p.add_argument("cadence", choices=("daily", "weekly", "monthly", "monthly_final"))
    p.add_argument("--date")
    p.add_argument("--channel", action="append", choices=("kb", "mail"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="忽略投递台账强制重发（慎用）")
    p.add_argument("--once", action="store_true", help="本次忽略配置中的投递开关（用于人工试发，不影响定时任务）")
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("web", help="启动本地网页控制台")
    p.add_argument("--host", help="监听地址（默认 127.0.0.1；开放局域网需设置 CASHRECON_WEB_PASSWORD）")
    p.add_argument("--port", type=int)
    p.add_argument("--open", action="store_true", help="启动后打开浏览器")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("schedule", help="注册/移除/查看定时任务（macOS launchd / Windows 任务计划）")
    p.add_argument("action", choices=("install", "uninstall", "status"))
    p.add_argument("--with-console", action="store_true", help="同时让网页控制台开机常驻")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("backup", help="立即备份数据库")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("staff", help="从系统更新掌中通账号→姓名（用于显示中天提现发起人）")
    p.set_defaults(func=cmd_staff)
