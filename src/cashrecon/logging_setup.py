"""Logging: daily-rotated file under the private logs dir, with secret masking."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

from cashrecon.paths import ensure_private_dir

LOGGER_NAME = "cashrecon"
_configured = False


class SecretFilter(logging.Filter):
    """Mask registered secret values and obvious credential patterns."""

    PATTERNS = [
        re.compile(r"(?i)(x-api-key|authorization|bearer|token|password|api_key)([\"'=:\s]+)([^\s\"',;]+)"),
    ]

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def _clean(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        for pattern in self.PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + m.group(2) + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = self._clean(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        return True


def setup_logging(log_dir: Path | None, *, verbose: bool = False, secrets: list[str] | None = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret_filter = SecretFilter(secrets)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console.addFilter(secret_filter)
    logger.addHandler(console)
    if log_dir is not None:
        ensure_private_dir(log_dir)
        handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / "cashrecon.log", when="midnight", backupCount=30, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        handler.addFilter(secret_filter)
        logger.addHandler(handler)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(LOGGER_NAME + ("." + name if name else ""))
