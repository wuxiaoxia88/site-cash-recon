from __future__ import annotations

from datetime import date

import pytest

from cashrecon import dates
from cashrecon.config import ConfigError, parse_env_file, settings_from_dict
from cashrecon.db import Store
from cashrecon.money import MoneyError, fmt_yuan, to_cents


@pytest.mark.parametrize("value,expected", [
    ("12.34", 1234), (12.34, 1234), (1234.5600000001, 123456), ("-0.005", -1), (0, 0),
    ("1,234.5", 123450), (19921.7, 1992170), ("  3 ", 300),
])
def test_to_cents(value, expected):
    assert to_cents(value) == expected


@pytest.mark.parametrize("value", [None, "", "abc", True, float("nan")])
def test_to_cents_rejects(value):
    with pytest.raises(MoneyError):
        to_cents(value)


def test_fmt_yuan():
    assert fmt_yuan(123456789) == "1,234,567.89"
    assert fmt_yuan(-5) == "-0.05"
    assert fmt_yuan(100, sign=True) == "+1.00"
    assert fmt_yuan(None) == "—"


def test_periods():
    assert dates.previous_week(date(2026, 10, 1)) == (date(2026, 9, 21), date(2026, 9, 27))
    assert dates.previous_month(date(2026, 10, 3)) == (date(2026, 9, 1), date(2026, 9, 30))
    assert dates.month_of(date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert dates.from_millis(1790738888000).strftime("%Y-%m-%d %H:%M") == "2026-09-30 11:28"


def test_settings_validation(paths):
    with pytest.raises(ConfigError):
        settings_from_dict({"site": {"name": "x"}}, paths)
    with pytest.raises(ConfigError):
        settings_from_dict({"site": {"name": "x"}, "accounts": [{"code": "A", "name": "a", "type": "BAD"}]}, paths)
    with pytest.raises(ConfigError):
        settings_from_dict({"site": {"name": "x"}, "accounts": [
            {"code": "A", "name": "a", "type": "CASH"}, {"code": "A", "name": "b", "type": "CASH"}]}, paths)


def test_settings_accessors(settings, monkeypatch):
    assert settings.zt_account.code == "ZT_MAIN"
    assert settings.account_by_portal("P-CORP").code == "CORP"
    assert settings.low_balance_cents(settings.account("ZT_MAIN")) == 500000
    assert settings.low_balance_cents(settings.account("CORP")) == 100000
    assert settings.required_sources() == ["ZT_SUMMARY", "JOURNAL"]
    monkeypatch.setenv("ZTO_CLI_PRIMARY_KEY", "from-env")
    assert settings.secret("ZTO_CLI_PRIMARY_KEY") == "from-env"
    monkeypatch.setenv("ZTO_CLI_PRIMARY_KEY", "<主 API Key>")
    assert settings.secret("ZTO_CLI_PRIMARY_KEY") is None


def test_env_file(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("# c\nA=1\nexport B = \"two\"\nbad line\n", encoding="utf-8")
    assert parse_env_file(path) == {"A": "1", "B": "two"}


def test_store_migrates_and_backs_up(tmp_path):
    db = tmp_path / "x" / "c.db"
    with Store(db) as store:
        assert store.scalar("PRAGMA user_version") >= 1
        assert store.integrity_ok()
        store.set_meta("k", "v")
        with pytest.raises(RuntimeError):
            with store.tx():
                store.set_meta("k", "changed")
                raise RuntimeError("boom")
        assert store.get_meta("k") == "v"
        store.backup_to(tmp_path / "b.db")
    with Store(tmp_path / "b.db") as copy:
        assert copy.get_meta("k") == "v"
