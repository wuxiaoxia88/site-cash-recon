from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("leakcheck", Path(__file__).parents[1] / "scripts" / "leakcheck.py")
leakcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(leakcheck)


def labels(text, deny=()):
    return [f.split(": ")[-1] for f in leakcheck.scan_text("f", text, list(deny))]


def test_detects_generic_patterns():
    assert "secret-assignment" in labels("API_KEY=abcdefghijklmnopqrstuv")  # leakcheck: allow
    assert "mobile" in labels("call 13912345678 now")  # leakcheck: allow
    assert "card-number" in labels("card 6222021102003470000")  # leakcheck: allow
    assert "email" in labels("mail someone@qq.com")  # leakcheck: allow
    assert "private-ip" in labels("http://192.168.1.10:9800")  # leakcheck: allow


def test_allows_placeholders_and_examples():
    assert labels("ZTO_CLI_PRIMARY_KEY=<主 API Key>") == []
    assert labels("user@example.com") == []
    assert labels("ZTO_KB_AGENT_TOKEN=<token>") == []
    assert labels("token = ZTO_KB_AGENT_TOKEN") == []


def test_denylist_terms():
    assert "private-term (denylist)" in labels("网点 甲乙丙 日报", ["甲乙丙"])
    assert labels("hash 143210", ["4321"]) == []
    assert "private-term (denylist)" in labels("尾号 4321", ["4321"])
