"""Deliver rendered reports to the configured channels, exactly once per content.

Ledger rules (table ``deliveries``):
  * KB publishing is idempotent (same destination is overwritten), so a changed
    report is re-published and failed/unknown attempts are retried.
  * Mail is not idempotent. A report is mailed once; it is mailed again only when
    the earlier mail carried incomplete data and the report is now complete. An
    ``unknown`` outcome (e.g. timeout after hand-off) is never retried automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cashrecon.config import Settings
from cashrecon.db import Store, dumps, now_text
from cashrecon.logging_setup import get_logger
from cashrecon.money import fmt_yuan
from cashrecon.paths import atomic_write
from cashrecon.reports import ReportArtifact, environment

log = get_logger("delivery")
STATUS_CN = {"OK": "完整", "PARTIAL": "部分不完整", "MISSING": "有缺失"}


@dataclass
class DeliveryResult:
    channel: str
    status: str  # verified | published | sent | skipped | failed | unknown | dry_run | disabled
    detail: str = ""
    receipt: str = ""


def _ledger(store: Store, key: str, channel: str) -> dict[str, Any] | None:
    row = store.one("SELECT * FROM deliveries WHERE report_key = ? AND channel = ?", (key, channel))
    return dict(row) if row else None


def _save(store: Store, key: str, channel: str, status: str, content_hash: str, receipt: str = "",
          error: str = "") -> None:
    store.execute("INSERT INTO deliveries (report_key, channel, status, content_hash, attempts, receipt, error, "
                  "updated_at) VALUES (?,?,?,?,1,?,?,?) ON CONFLICT(report_key, channel) DO UPDATE SET "
                  "status=excluded.status, content_hash=excluded.content_hash, attempts=deliveries.attempts+1, "
                  "receipt=excluded.receipt, error=excluded.error, updated_at=excluded.updated_at",
                  (key, channel, status, content_hash, receipt, error, now_text()))


def _data_status(artifact: ReportArtifact) -> str:
    view = artifact.view
    if "p" in view:
        return view["p"]["data_status"]
    return "OK" if not view.get("missing_days") and view.get("status_days", {}).get("OK") == view.get("days_present") \
        else "PARTIAL"


def mail_content(artifact: ReportArtifact, settings: Settings, link: str | None) -> tuple[str, str]:
    view = artifact.view
    status = _data_status(artifact)
    if "p" in view:
        p = view["p"]
        kpis = [("本月至今经营利润", fmt_yuan(view["mtd"]["profit"])),
                ("当日经营利润（按业务期间）", fmt_yuan(p["profit"]["profit"])),
                ("当日经营收入 / 成本", f"{fmt_yuan(p['profit']['income'])} / {fmt_yuan(p['profit']['cost'])}"),
                ("现金头寸", fmt_yuan(p["position"]["total"]))]
        if p.get("bill_profit"):
            kpis.append(("账单口径利润（对照）", fmt_yuan(p["bill_profit"]["profit_cents"])))
    else:
        a = view["agg"]
        kpis = [("经营利润（按业务期间）", fmt_yuan(a["profit"])), ("经营收入", fmt_yuan(a["income"])),
                ("经营成本", fmt_yuan(a["cost"])), ("期末现金头寸", fmt_yuan(view["position_end"]["total"])),
                ("亏损天数", f"{len(view['loss_days'])} / {view['days_present']}")]
    html = environment().get_template("mail.html.j2").render(v=view, kpis=kpis, link=link,
                                                             status=STATUS_CN.get(status, status))
    urgent = any(i["level"] == "high" for i in view["items"])
    prefix = "【紧急】" if urgent else ""
    profit = view["mtd"]["profit"] if "p" in view else view["agg"]["profit"]
    word = ("本月至今" if "p" in view else "") + ("盈利" if profit >= 0 else "亏损")
    todo = sum(1 for i in view["items"] if i["level"] in ("high", "medium"))
    subject = (f"{prefix}【{view['title']}】{settings.site_name} {view['period_label']}｜{word} "
               f"{fmt_yuan(abs(profit))} 元｜{todo} 项待处理")
    if status != "OK":
        subject += "｜数据不完整"
    return subject, html


def deliver(store: Store, settings: Settings, artifact: ReportArtifact, *, dry_run: bool = False,
            force: bool = False, channels: list[str] | None = None,
            ignore_disabled: bool = False) -> list[DeliveryResult]:
    """``ignore_disabled`` sends once even if the channel is disabled in config (manual test sends)."""
    key = artifact.report_key
    results: list[DeliveryResult] = []
    html = artifact.html_path.read_text(encoding="utf-8")
    status = _data_status(artifact)
    kb_cfg = settings.delivery.get("kb", {})
    mail_cfg = settings.delivery.get("mail", {})
    share_url = None

    # ------------------------------------------------------------------ KB
    if channels is None or "kb" in channels:
        if not kb_cfg.get("enabled") and not dry_run and not ignore_disabled:
            results.append(DeliveryResult("kb", "disabled"))
        else:
            row = _ledger(store, key, "kb")
            if row and row["status"] in ("verified", "published") and row["content_hash"] == artifact.content_hash \
                    and not force:
                results.append(DeliveryResult("kb", "skipped", "内容未变化，已发布", row["receipt"]))
                share_url = row["receipt"] or None
            elif dry_run:
                results.append(DeliveryResult("kb", "dry_run", "将发布到知识库"))
            else:
                from cashrecon.delivery.kb import KbError, KbPublisher
                try:
                    publisher = KbPublisher(settings)
                    receipt = publisher.publish(cadence=artifact.cadence, period_key=artifact.period_key,
                                                title=f"{artifact.title}-{settings.site_name}-{artifact.period_key}",
                                                summary=artifact.view["headline"], html=html)
                    share_url = receipt.get("share_url")
                    _save(store, key, "kb", receipt["status"], artifact.content_hash, share_url or "",
                          "" if receipt["status"] == "verified" else dumps(receipt))
                    results.append(DeliveryResult("kb", receipt["status"], receipt.get("stage", ""), share_url or ""))
                except KbError as exc:
                    _save(store, key, "kb", "failed", artifact.content_hash, error=str(exc))
                    results.append(DeliveryResult("kb", "failed", str(exc)))
    # ---------------------------------------------------------------- mail
    if channels is None or "mail" in channels:
        if not mail_cfg.get("enabled") and not dry_run and not ignore_disabled:
            results.append(DeliveryResult("mail", "disabled"))
        else:
            row = _ledger(store, key, "mail")
            previous_status = row["receipt"] if row else ""
            improved = row is not None and row["status"] == "sent" and previous_status != "OK" and status == "OK"
            if row and row["status"] == "sent" and not improved and not force:
                results.append(DeliveryResult("mail", "skipped", "已发送过"))
            elif row and row["status"] == "unknown" and not force:
                results.append(DeliveryResult("mail", "skipped", "上次发送结果未知，为避免重复不自动重发"))
            else:
                link = share_url or artifact.view.get("kb_link")
                subject, body = mail_content(artifact, settings, link)
                if improved:
                    subject += "（数据已补齐）"
                attachments = [artifact.html_path] + ([artifact.csv_path] if artifact.csv_path else [])
                if dry_run:
                    preview = artifact.html_path.with_name("mail-preview.html")
                    atomic_write(preview, body.replace("<body", f"<!-- 主题：{subject} -->\n<body", 1))
                    results.append(DeliveryResult("mail", "dry_run", subject, str(preview)))
                else:
                    from cashrecon.delivery.mail import MailError, MailSender
                    try:
                        outcome = MailSender(settings).send(subject=subject, html_body=body, attachments=attachments)
                        _save(store, key, "mail", outcome["status"], artifact.content_hash, status,
                              "" if outcome["status"] == "sent" else outcome.get("detail", ""))
                        results.append(DeliveryResult("mail", outcome["status"], outcome.get("detail", ""), subject))
                    except MailError as exc:
                        _save(store, key, "mail", "failed", artifact.content_hash, status, str(exc))
                        results.append(DeliveryResult("mail", "failed", str(exc)))
    for r in results:
        log.info("deliver %s %s %s %s", key, r.channel, r.status, r.detail)
    return results


def send_notice(settings: Settings, subject: str, lines: list[str]) -> str:
    """Best-effort operational e-mail (e.g. job failure). Returns a status string."""
    if not settings.delivery.get("mail", {}).get("enabled"):
        return "disabled"
    from cashrecon.delivery.mail import MailError, MailSender
    body = "<div style=\"font-family:sans-serif\">" + "".join(f"<p>{line}</p>" for line in lines) + "</div>"
    try:
        return MailSender(settings).send(subject=subject, html_body=body, attachments=[])["status"]
    except MailError as exc:
        log.error("notice mail failed: %s", exc)
        return "failed"
