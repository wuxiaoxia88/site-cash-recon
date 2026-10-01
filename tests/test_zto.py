from __future__ import annotations

import io
import json
import urllib.error

import pytest

from cashrecon.zto import Route, ZtoClient, ZtoError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(script):
    """script: list of (route_prefix, outcome) consumed in order."""
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(json.dumps(outcome).encode())

    opener.calls = calls
    return opener


def client(script, clock=None):
    c = ZtoClient([Route("primary", "http://p", "kp" * 10), Route("fallback", "http://f", "kf" * 10)],
                  opener=make_opener(script))
    if clock:
        c.clock = clock
    return c


def test_primary_success():
    c = client([{"code": 200, "data": {"x": 1}}])
    assert c.data("site-journal-record", "page", {}) == ({"x": 1}, "primary")


def test_failover_on_network_and_business_errors():
    c = client([urllib.error.URLError("down"), {"code": 200, "data": [1]}])
    assert c.data("site-journal-record", "page", {}) == ([1], "fallback")
    c = client([{"code": 500, "status": "error"}, {"code": 200, "data": 2}])
    assert c.data("site-journal-record", "page", {})[1] == "fallback"


def test_sticky_fallback_then_probe_primary():
    now = [0.0]
    c = client([urllib.error.URLError("x"), {"code": 200, "data": 1}, {"code": 200, "data": 2},
                {"code": 200, "data": 3}], clock=lambda: now[0])
    assert c.data("site-journal-record", "page", {})[1] == "fallback"
    assert c.data("site-journal-record", "page", {})[1] == "fallback"  # sticky, primary not retried
    now[0] = 10_000
    assert c.data("site-journal-record", "page", {})[1] == "primary"


def test_all_routes_failed_and_safe_messages():
    c = client([urllib.error.HTTPError("http://p", 404, "nf", {}, None), TimeoutError()])
    with pytest.raises(ZtoError) as info:
        c.data("site-journal-record", "page", {})
    assert info.value.code == "all_routes_failed"
    assert "endpoint_missing" in str(info.value) and "kp" not in str(info.value)


def test_whitelist_blocks_unknown_endpoints():
    c = client([])
    with pytest.raises(ZtoError) as info:
        c.data("site-journal-write", "add", {})
    assert info.value.code == "endpoint_not_allowed"


def test_health():
    c = client([{"status": "ok", "auth": {"status": "ok"}}, urllib.error.URLError("x")])
    assert c.health() == [{"route": "primary", "ok": True, "auth": "ok"},
                          {"route": "fallback", "ok": False, "error": "network"}]
