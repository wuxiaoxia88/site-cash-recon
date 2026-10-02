"""zto-cli client with primary/fallback failover.

Only whitelisted read endpoints can be called. Errors carry a short safe code
and never include response bodies, URLs with credentials, or API keys.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cashrecon.logging_setup import get_logger

log = get_logger("zto")

READ_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("site-journal-account", "list"),
    ("site-journal-account", "all-list"),
    ("site-journal-record", "page"),
    ("site-journal-record", "settle-type-options"),
    ("site-journal-summary", "summary"),
    ("site-journal-summary", "account-summary"),
    ("advance-payment-flow-summary", "advance-payment-flow-summary"),
    ("advance-payment-flow-summary", "summary-amount"),
    ("advance-payment-balance-record", "query"),
    ("advance-payment-daily-balance", "query"),
    ("inbound-bill", "query"),
    ("outbound-bill", "query"),
    ("outbound-rebate-bill", "day-sum"),
    ("service-violation-cost", "summary-page"),
    ("boss-operations", "salesman-basic-info-site"),
})


class ZtoError(RuntimeError):
    """Safe error: ``code`` is a short machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


@dataclass
class Route:
    name: str
    base_url: str
    api_key: str


@dataclass
class ZtoClient:
    routes: list[Route]
    timeout: float = 60.0
    sticky_seconds: float = 600.0
    opener: Callable[..., Any] = urllib.request.urlopen
    clock: Callable[[], float] = time.monotonic
    _primary_failed_at: float | None = field(default=None, init=False)
    request_count: dict[str, int] = field(default_factory=dict, init=False)

    @classmethod
    def from_settings(cls, settings: Any) -> ZtoClient:
        routes = []
        for name in ("PRIMARY", "FALLBACK"):
            url = settings.secret(f"ZTO_CLI_{name}_URL")
            key = settings.secret(f"ZTO_CLI_{name}_KEY")
            if url and key:
                routes.append(Route(name.lower(), url.rstrip("/"), key))
        if not routes:
            raise ZtoError("zto_not_configured", "set ZTO_CLI_PRIMARY_URL/KEY in secrets.env")
        zto = settings.raw.get("zto", {}) if isinstance(settings.raw, dict) else {}
        return cls(routes, timeout=float(zto.get("timeout_seconds", 60)),
                   sticky_seconds=float(zto.get("sticky_minutes", 10)) * 60)

    # ------------------------------------------------------------------ http
    def _request(self, route: Route, method: str, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"X-API-Key": route.api_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(route.base_url + path, data=data, headers=headers, method=method)
        self.request_count[route.name] = self.request_count.get(route.name, 0) + 1
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            code = "endpoint_missing" if exc.code == 404 else f"http_{exc.code}"
            raise ZtoError(code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            kind = "timeout" if isinstance(reason, TimeoutError) else "network"
            raise ZtoError(kind) from None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ZtoError("bad_json") from None

    def _ordered_routes(self) -> list[Route]:
        if len(self.routes) > 1 and self._primary_failed_at is not None:
            if self.clock() - self._primary_failed_at < self.sticky_seconds:
                return self.routes[1:] + self.routes[:1]
            self._primary_failed_at = None
        return list(self.routes)

    # ------------------------------------------------------------------ api
    def data(self, adapter: str, endpoint: str, payload: dict[str, Any]) -> tuple[Any, str]:
        """Call a read endpoint; returns ``(data, route_name)``."""
        if (adapter, endpoint) not in READ_ENDPOINTS:
            raise ZtoError("endpoint_not_allowed", f"{adapter}/{endpoint}")
        errors = []
        for route in self._ordered_routes():
            try:
                body = self._request(route, "POST", f"/api/data/{adapter}/{endpoint}", payload)
                if not isinstance(body, dict):
                    raise ZtoError("bad_envelope")
                if body.get("code") not in (None, 200) or body.get("status") in ("error", "fail", "failed") \
                        or body.get("success") is False:
                    raise ZtoError("business_error", str(body.get("code")))
                if route is self.routes[0]:
                    self._primary_failed_at = None
                return body.get("data"), route.name
            except ZtoError as exc:
                errors.append(f"{route.name}:{exc.code}")
                if exc.code == "endpoint_missing":
                    # A capability gap on this route, not an outage: do not make the fallback sticky.
                    log.info("zto %s/%s not available on %s", adapter, endpoint, route.name)
                    continue
                log.warning("zto %s/%s failed on %s: %s", adapter, endpoint, route.name, exc.code)
                if route is self.routes[0] and len(self.routes) > 1:
                    self._primary_failed_at = self.clock()
        raise ZtoError("all_routes_failed", ",".join(errors))

    def health(self) -> list[dict[str, Any]]:
        results = []
        for route in self.routes:
            try:
                body = self._request(route, "GET", "/api/health")
                ok = isinstance(body, dict) and body.get("status") == "ok"
                auth = (body.get("auth") or {}).get("status") if isinstance(body, dict) else None
                results.append({"route": route.name, "ok": ok, "auth": auth})
            except ZtoError as exc:
                results.append({"route": route.name, "ok": False, "error": exc.code})
        return results

    def adapters(self, route_name: str | None = None) -> dict[str, set[str]]:
        """Adapter → endpoint names available on a route (for doctor)."""
        route = next((r for r in self.routes if route_name in (None, r.name)), self.routes[0])
        body = self._request(route, "GET", "/api/agent")
        items = body.get("adapters") if isinstance(body, dict) else body
        result: dict[str, set[str]] = {}
        for item in items or []:
            if isinstance(item, dict) and item.get("name"):
                result[item["name"]] = {e.get("name") for e in item.get("endpoints") or [] if isinstance(e, dict)}
        return result
