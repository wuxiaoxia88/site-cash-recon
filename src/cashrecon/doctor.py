"""`cashrecon doctor`: verify configuration, credentials, sources and storage."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from cashrecon.config import ConfigError, Settings, load_settings, mask
from cashrecon.paths import Paths

SOURCE_ENDPOINTS: dict[str, list[tuple[str, str]]] = {
    "ZT_SUMMARY": [("advance-payment-flow-summary", "advance-payment-flow-summary"),
                   ("advance-payment-flow-summary", "summary-amount")],
    "ZT_FLOW": [("advance-payment-balance-record", "query")],
    "JOURNAL": [("site-journal-record", "page"), ("site-journal-account", "list"),
                ("site-journal-summary", "account-summary")],
    "BILL_PROFIT": [("inbound-bill", "query"), ("outbound-bill", "query"),
                    ("outbound-rebate-bill", "day-sum")],
}


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, text: str) -> None:
        print(f"  ✓ {text}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        print(f"  ! {text}")

    def fail(self, text: str) -> None:
        self.failures += 1
        print(f"  ✗ {text}")


def _check_sqlite(path_text: str, label: str, out: Report) -> None:
    path = Path(path_text).expanduser()
    if not path_text or not path.is_file():
        out.fail(f"{label}：文件不存在 {path_text or '(未配置)'}")
        return
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        out.ok(f"{label}：可只读打开（{len(tables)} 张表）")
    except sqlite3.Error as exc:
        out.fail(f"{label}：无法只读打开（{exc.__class__.__name__}）")


def run_doctor(paths: Paths, online: bool = True) -> int:
    out = Report()
    print(f"数据目录：{paths.home}")
    print("[配置]")
    try:
        settings: Settings = load_settings(paths)
    except ConfigError as exc:
        out.fail(str(exc))
        print("\n结果：配置无法加载")
        return 1
    out.ok(f"网点：{settings.site_name}（{len(settings.accounts)} 个账户）")
    enabled = [code for code in settings.sources if settings.source_enabled(code)]
    out.ok(f"启用来源：{', '.join(enabled) or '无'}")
    if not enabled:
        out.warn("没有启用任何数据源")

    print("[凭据]")
    for name in ("PRIMARY", "FALLBACK"):
        url = settings.secret(f"ZTO_CLI_{name}_URL")
        key = settings.secret(f"ZTO_CLI_{name}_KEY")
        if url and key:
            out.ok(f"zto-cli {name.lower()}：已配置（key {mask(key)}）")
        elif name == "PRIMARY":
            out.fail("zto-cli 主源未配置（ZTO_CLI_PRIMARY_URL / ZTO_CLI_PRIMARY_KEY）")
        else:
            out.warn("zto-cli 备源未配置")

    print("[数据库]")
    from cashrecon.db import Store
    try:
        with Store(paths.database) as store:
            if store.integrity_ok():
                out.ok(f"{paths.database} 完整性正常")
            else:
                out.fail("数据库完整性检查失败")
    except sqlite3.Error as exc:
        out.fail(f"数据库无法打开：{exc.__class__.__name__}")

    print("[上游只读库]")
    if settings.source_enabled("BANK_LEDGER"):
        _check_sqlite(str(settings.source("BANK_LEDGER").get("ledger_db", "")), "银行采集库", out)
    zt_flow = settings.source("ZT_FLOW")
    if settings.source_enabled("ZT_FLOW") and zt_flow.get("impl") == "monitor_db":
        _check_sqlite(str(zt_flow.get("monitor_db", "")), "中天监控库", out)

    if online:
        print("[zto-cli 连通]")
        from cashrecon.zto import ZtoClient, ZtoError
        try:
            client = ZtoClient.from_settings(settings)
        except ZtoError as exc:
            out.fail(f"无法创建客户端：{exc.code}")
        else:
            for item in client.health():
                if item.get("ok"):
                    out.ok(f"{item['route']}：健康（门户认证 {item.get('auth')}）")
                else:
                    (out.fail if item["route"] == "primary" else out.warn)(
                        f"{item['route']}：不可用（{item.get('error', 'status')}）")
            for route in client.routes:
                try:
                    available = client.adapters(route.name)
                except ZtoError as exc:
                    out.warn(f"{route.name}：无法读取适配器清单（{exc.code}）")
                    continue
                for source, needed in SOURCE_ENDPOINTS.items():
                    if source == "ZT_FLOW" and zt_flow.get("impl") != "api":
                        continue
                    if not settings.source_enabled(source):
                        continue
                    missing = [f"{a}/{e}" for a, e in needed if e not in available.get(a, set())]
                    if missing:
                        (out.fail if route.name == "primary" else out.warn)(
                            f"{route.name} 缺少 {source} 端点：{', '.join(missing)}")
                    else:
                        out.ok(f"{route.name} 具备 {source} 所需端点")

    print("[投递]")
    kb = settings.delivery.get("kb", {})
    if kb.get("enabled"):
        if settings.secret("ZTO_KB_AGENT_TOKEN"):
            out.ok("知识库令牌已配置")
        else:
            out.fail("知识库已启用但缺少 ZTO_KB_AGENT_TOKEN")
    else:
        out.ok("知识库投递：未启用")
    mail = settings.delivery.get("mail", {})
    if mail.get("enabled"):
        if mail.get("method", "agently") == "agently":
            if shutil.which("agently-cli"):
                out.ok("agently-cli 已安装")
            else:
                out.fail("邮件方式为 agently，但未找到 agently-cli")
        elif not settings.secret("SMTP_HOST"):
            out.fail("邮件方式为 smtp，但缺少 SMTP_HOST")
        if not mail.get("to"):
            out.fail("邮件已启用但未配置收件人")
    else:
        out.ok("邮件投递：未启用")

    print("[调度]")
    try:
        from cashrecon.scheduler import status as schedule_status
        for line in schedule_status(settings):
            out.ok(line)
    except Exception as exc:  # scheduler is optional during development
        out.warn(f"调度状态未知：{exc.__class__.__name__}")

    print(f"\n结果：{out.failures} 个错误，{out.warnings} 个提醒")
    return 1 if out.failures else 0
