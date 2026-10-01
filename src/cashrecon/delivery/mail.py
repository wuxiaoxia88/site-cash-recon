"""E-mail delivery: agently-cli (agent mailbox) or SMTP (works on any OS)."""

from __future__ import annotations

import json
import shutil
import smtplib
import ssl
import subprocess
import tempfile
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from cashrecon.config import Settings


class MailError(RuntimeError):
    pass


class MailSender:
    channel = "mail"

    def __init__(self, settings: Settings, runner: Callable[..., Any] = subprocess.run,
                 smtp_factory: Callable[..., Any] | None = None) -> None:
        cfg = settings.delivery.get("mail", {})
        self.to = [str(x) for x in cfg.get("to") or []]
        if not self.to:
            raise MailError("mail_recipients_missing")
        self.method = cfg.get("method", "agently")
        self.settings = settings
        self.runner = runner
        self.smtp_factory = smtp_factory

    def send(self, *, subject: str, html_body: str, attachments: list[Path]) -> dict[str, Any]:
        if self.method == "smtp":
            return self._smtp(subject, html_body, attachments)
        return self._agently(subject, html_body, attachments)

    # ------------------------------------------------------------- agently
    def _agently(self, subject: str, html_body: str, attachments: list[Path]) -> dict[str, Any]:
        exe = shutil.which("agently-cli")
        if not exe:
            raise MailError("agently_cli_not_found")
        with tempfile.TemporaryDirectory(prefix="cashrecon-mail-") as tmp:
            folder = Path(tmp)
            (folder / "body.html").write_text(html_body, encoding="utf-8")
            argv = [exe, "message", "+send", "--subject", subject, "--body-file", "./body.html",
                    "--body-format", "html", "--confirmed"]
            for addr in self.to:
                argv += ["--to", addr]
            for path in attachments:
                target = folder / path.name
                shutil.copyfile(path, target)
                argv += ["--attachment", "./" + target.name]
            try:
                done = self.runner(argv, cwd=folder, capture_output=True, text=True, timeout=120, check=False)
            except (subprocess.TimeoutExpired, OSError):
                return {"status": "unknown", "detail": "agently_timeout"}
        if done.returncode != 0:
            raise MailError(f"agently_exit_{done.returncode}")
        try:
            body = json.loads(done.stdout or "{}")
        except ValueError:
            return {"status": "unknown", "detail": "agently_output_unparsed"}
        data = body.get("data", body) if isinstance(body, dict) else {}
        if isinstance(data, dict) and data.get("queued") is True:
            return {"status": "sent", "detail": "queued"}
        return {"status": "unknown", "detail": "agently_not_queued"}

    # ---------------------------------------------------------------- smtp
    def _smtp(self, subject: str, html_body: str, attachments: list[Path]) -> dict[str, Any]:
        s = self.settings.secret
        host, user, password = s("SMTP_HOST"), s("SMTP_USER"), s("SMTP_PASSWORD")
        if not host:
            raise MailError("smtp_not_configured")
        port = int(s("SMTP_PORT", "465") or 465)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, s("SMTP_FROM") or user, ", ".join(self.to)
        msg.set_content("请使用支持 HTML 的邮件客户端查看本报表。")
        msg.add_alternative(html_body, subtype="html")
        for path in attachments:
            subtype = "csv" if path.suffix == ".csv" else "html"
            msg.add_attachment(path.read_bytes(), maintype="text", subtype=subtype, filename=path.name)
        factory = self.smtp_factory or (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)
        try:
            kwargs = {"context": ssl.create_default_context()} if factory is smtplib.SMTP_SSL else {}
            with factory(host, port, timeout=60, **kwargs) as client:
                if factory is smtplib.SMTP:
                    client.starttls(context=ssl.create_default_context())
                if user:
                    client.login(user, password or "")
                client.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"smtp_{exc.__class__.__name__}") from None
        return {"status": "sent", "detail": "smtp"}
