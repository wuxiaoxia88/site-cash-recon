"""Knowledge-base publishing via the KB Agent API (publish-package + job poll + read-back)."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from cashrecon.config import Settings
from cashrecon.logging_setup import get_logger

log = get_logger("kb")


PUBLISH_TIMEOUT = 180


class KbError(RuntimeError):
    pass


class KbTimeout(KbError):
    """The request was sent but no response arrived in time (outcome unknown)."""


class KbPublisher:
    channel = "kb"

    def __init__(self, settings: Settings, opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] | None = None, poll_seconds: float = 3.0, max_polls: int = 40) -> None:
        cfg = settings.delivery.get("kb", {})
        self.base_urls = [u.rstrip("/") for u in cfg.get("base_urls") or []]
        self.token = settings.secret("ZTO_KB_AGENT_TOKEN")
        if not self.base_urls or not self.token:
            raise KbError("kb_not_configured")
        self.agent = cfg.get("agent_name", "site-cash-recon")
        self.prefix = str(cfg.get("destination_prefix", "internal/reports/cash-recon")).strip("/")
        self.visibility = cfg.get("visibility", "internal")
        self.category = cfg.get("category", "经营报表")
        self.public_base = (cfg.get("public_base_url") or "").rstrip("/")
        self.slug = settings.site_slug
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep or time.sleep
        self.poll_seconds, self.max_polls = poll_seconds, max_polls

    def destination(self, cadence: str, period_key: str) -> str:
        return f"{self.prefix}/{self.slug}/{cadence}/{period_key}"

    def _call(self, base: str, path: str, payload: dict[str, Any] | None = None,
              timeout: float = 60) -> tuple[int, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(base + path, data=data, method="POST" if data else "GET", headers={
            "Authorization": f"Bearer {self.token}", "X-Agent-Name": self.agent, "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except TimeoutError:
            raise KbTimeout("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise KbTimeout("timeout") from None
            raise KbError(f"network:{exc.reason.__class__.__name__}") from None
        except (OSError, ValueError) as exc:
            raise KbError(f"network:{exc.__class__.__name__}") from None

    def publish(self, *, cadence: str, period_key: str, title: str, summary: str, html: str,
                extra_files: dict[str, str] | None = None) -> dict[str, Any]:
        destination = self.destination(cadence, period_key)
        files = [{"path": "index.html", "encoding": "utf-8", "content": html}]
        for name, content in (extra_files or {}).items():
            files.append({"path": name, "encoding": "utf-8", "content": content})
        payload = {"destination": destination, "type": "html", "message": summary,
                   "meta": {"title": title, "visibility": self.visibility, "category": self.category,
                            "tags": ["现金对账", "财务", title[:4]], "status": "published", "summary": summary},
                   "files": files}
        last_error = "kb_unreachable"
        for base in self.base_urls:
            try:
                if self._verify(base, destination, html, attempts=1):
                    return {"status": "verified", "job_id": "", "stage": "already_published",
                            "share_url": self._share_url(destination), "destination": destination, "base": base}
                status, body = self._call(base, "/api/agent/publish-package", payload, timeout=PUBLISH_TIMEOUT)
            except KbTimeout:
                # The request reached the server; it may still be processing. Never re-send to another
                # base URL — decide the outcome by reading the content back.
                verified = self._verify(base, destination, html, attempts=10)
                return {"status": "verified" if verified else "unknown", "job_id": "", "stage": "timeout",
                        "share_url": self._share_url(destination), "destination": destination, "base": base}
            except KbError as exc:
                last_error = str(exc)
                continue  # could not connect: nothing was sent, the next base URL is safe to try
            if status >= 300 or not isinstance(body, dict) or body.get("ok") is not True:
                raise KbError(f"publish_rejected:{status}")
            job = (body.get("data") or {})
            job_id = str(job.get("job_id") or "")
            share_url = job.get("share_url") or self._share_url(destination)
            stage = self._wait(base, job_id) if job_id else "unknown"
            verified = self._verify(base, destination, html) if stage == "published" else False
            return {"status": "verified" if verified else ("published" if stage == "published" else "unknown"),
                    "job_id": job_id, "stage": stage, "share_url": share_url, "destination": destination,
                    "base": base}
        raise KbError(last_error)

    def _share_url(self, destination: str) -> str:
        return f"{self.public_base}/{destination}/" if self.public_base else destination

    def _wait(self, base: str, job_id: str) -> str:
        stage = "queued"
        for _ in range(self.max_polls):
            status, body = self._call(base, "/api/agent/jobs/" + urllib.parse.quote(job_id))
            data = body.get("data") if isinstance(body, dict) else None
            stage = str((data or {}).get("stage") or (data or {}).get("status") or "unknown") if status == 200 else "unknown"
            if stage in ("published", "failed"):
                return stage
            self.sleep(self.poll_seconds)
        return stage

    def _verify(self, base: str, destination: str, html: str, attempts: int = 5) -> bool:
        expected = hashlib.sha256(html.encode("utf-8")).hexdigest()
        path = destination + "/index.html"
        for attempt in range(attempts):  # indexing can lag behind "published"
            try:
                status, body = self._call(base, "/api/agent/content?" + urllib.parse.urlencode({"path": path}))
            except KbError:
                status, body = 0, None
            data = body.get("data") if isinstance(body, dict) else None
            content = (data or {}).get("content")
            if status == 200 and isinstance(content, str):
                if hashlib.sha256(content.encode("utf-8")).hexdigest() == expected:
                    return True
                if attempts > 1:
                    log.warning("kb read-back differs for %s", destination)
            if attempt + 1 < attempts:
                self.sleep(self.poll_seconds)
        return False
