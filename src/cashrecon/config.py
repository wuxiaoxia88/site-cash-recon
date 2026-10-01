"""Load and validate the private site configuration.

``config.toml`` holds non-secret site settings; ``secrets.env`` holds keys and
tokens. Environment variables override ``secrets.env`` entries with the same
name. Secret values are never included in exception messages or logs.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cashrecon.money import to_cents
from cashrecon.paths import Paths

ACCOUNT_TYPES = {"ZT_PREPAY", "CORP_BANK", "BANK_CARD", "ALIPAY", "WECHAT", "CASH", "OTHER"}
ACCOUNT_TYPE_CN = {
    "ZT_PREPAY": "中天预付款",
    "CORP_BANK": "对公银行卡",
    "BANK_CARD": "银行卡",
    "ALIPAY": "支付宝",
    "WECHAT": "微信",
    "CASH": "现金",
    "OTHER": "其他",
}
COLLECTIONS = {"auto", "manual"}

DEFAULT_RULES: dict[str, Any] = {
    "dedup_days": 2,
    "transfer_days": 1,
    "withdraw_days": 3,
    "transfer_fee_floor_yuan": 5,
    "transfer_fee_ratio": 0.006,
    "zt_topup_counterparties": ["中通快递股份有限公司"],
    "recompute_lookback_days": 3,
    "self_heal_days": 7,
}

DEFAULT_ALERTS: dict[str, Any] = {
    "loss": True,
    "loss_streak_days": 3,
    "balance_diff_yuan": 100,
    "default_low_balance_yuan": 1000,
    "unmatched_single_yuan": 500,
    "unmatched_daily_yuan": 2000,
    "unclassified_ratio": 0.05,
    "withdraw_overdue_days": 3,
    "cooldown_hours": 24,
}

DEFAULT_SCHEDULE: dict[str, str] = {
    "daily": "12:10",
    "retry": "18:00",
    "weekly": "MON 12:20",
    "monthly": "3 12:30",
}

DEFAULT_WEB: dict[str, Any] = {"host": "127.0.0.1", "port": 8765}


class ConfigError(RuntimeError):
    """Configuration problem. Messages never contain secret values."""


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    type: str
    collection: str = "manual"
    portal_code: str = ""
    bank_tail: str = ""
    personal_funds: bool = False
    low_balance_cents: int | None = None
    active: bool = True

    @property
    def domain(self) -> str:
        return "ONLINE" if self.type == "ZT_PREPAY" else "OFFLINE"

    @property
    def type_cn(self) -> str:
        return ACCOUNT_TYPE_CN.get(self.type, self.type)

    @property
    def is_auto(self) -> bool:
        return self.collection == "auto"


@dataclass
class Settings:
    paths: Paths
    site_name: str
    site_slug: str
    accounts: list[Account]
    sources: dict[str, dict[str, Any]]
    rules: dict[str, Any]
    alerts: dict[str, Any]
    schedule: dict[str, str]
    delivery: dict[str, Any]
    web: dict[str, Any]
    analysis: dict[str, Any]
    withdraw_initiators: dict[str, dict[str, str]]
    secrets: dict[str, str] = field(repr=False, default_factory=dict)
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # -- accounts -----------------------------------------------------------
    def account(self, code: str) -> Account:
        for account in self.accounts:
            if account.code == code:
                return account
        raise ConfigError(f"unknown account code: {code}")

    def has_account(self, code: str) -> bool:
        return any(a.code == code for a in self.accounts)

    def account_by_portal(self, portal_code: str) -> Account | None:
        for account in self.accounts:
            if account.portal_code and account.portal_code == portal_code:
                return account
        return None

    @property
    def zt_account(self) -> Account | None:
        for account in self.accounts:
            if account.type == "ZT_PREPAY":
                return account
        return None

    # -- sources ------------------------------------------------------------
    def source(self, code: str) -> dict[str, Any]:
        return self.sources.get(code, {})

    def source_enabled(self, code: str) -> bool:
        return bool(self.sources.get(code, {}).get("enabled", False))

    def required_sources(self) -> list[str]:
        return [code for code, item in self.sources.items()
                if item.get("enabled") and item.get("required", True)]

    # -- secrets ------------------------------------------------------------
    def secret(self, key: str, default: str | None = None) -> str | None:
        value = os.environ.get(key) or self.secrets.get(key)
        if not value or value.startswith("<"):  # empty or unfilled template placeholder
            return default
        return value

    def low_balance_cents(self, account: Account) -> int:
        if account.low_balance_cents is not None:
            return account.low_balance_cents
        return to_cents(self.alerts["default_low_balance_yuan"])


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        raise ConfigError(f"cannot read secrets file: {path}") from None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        result[key] = value.strip().strip('"').strip("'")
    return result


def _merged(defaults: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    result.update(override or {})
    return result


def _account(item: dict[str, Any], index: int) -> Account:
    try:
        code = str(item["code"]).strip()
        name = str(item["name"]).strip()
        kind = str(item["type"]).strip().upper()
    except KeyError as exc:
        raise ConfigError(f"accounts[{index}] missing field: {exc.args[0]}") from None
    if not code or not name:
        raise ConfigError(f"accounts[{index}] code/name must not be empty")
    if kind not in ACCOUNT_TYPES:
        raise ConfigError(f"accounts[{index}] unknown type: {kind}")
    collection = str(item.get("collection", "manual")).strip().lower()
    if collection not in COLLECTIONS:
        raise ConfigError(f"accounts[{index}] collection must be auto or manual")
    low = item.get("low_balance_yuan")
    return Account(
        code=code,
        name=name,
        type=kind,
        collection=collection,
        portal_code=str(item.get("portal_code", "")).strip(),
        bank_tail=str(item.get("bank_tail", "")).strip(),
        personal_funds=bool(item.get("personal_funds", False)),
        low_balance_cents=None if low is None else to_cents(low),
        active=bool(item.get("active", True)),
    )


def settings_from_dict(data: dict[str, Any], paths: Paths, secrets: dict[str, str] | None = None) -> Settings:
    site = data.get("site") or {}
    if not isinstance(site, dict) or not str(site.get("name", "")).strip():
        raise ConfigError("[site] name is required")
    slug = str(site.get("slug", "")).strip() or "site"
    raw_accounts = data.get("accounts") or []
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigError("at least one [[accounts]] entry is required")
    accounts = [_account(item, i) for i, item in enumerate(raw_accounts)]
    codes = [a.code for a in accounts]
    if len(set(codes)) != len(codes):
        raise ConfigError("account codes must be unique")
    if sum(a.type == "ZT_PREPAY" for a in accounts) > 1:
        raise ConfigError("only one ZT_PREPAY account is supported")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict):
        raise ConfigError("[sources] must be a table")
    initiators = data.get("withdraw_initiators") or {}
    return Settings(
        paths=paths,
        site_name=str(site["name"]).strip(),
        site_slug=slug,
        accounts=accounts,
        sources={str(k).upper(): dict(v) for k, v in sources.items()},
        rules=_merged(DEFAULT_RULES, data.get("rules")),
        alerts=_merged(DEFAULT_ALERTS, data.get("alerts")),
        schedule=_merged(DEFAULT_SCHEDULE, data.get("schedule")),
        delivery=copy.deepcopy(data.get("delivery") or {}),
        web=_merged(DEFAULT_WEB, data.get("web")),
        analysis=copy.deepcopy(data.get("analysis") or {}),
        withdraw_initiators={str(k): dict(v) for k, v in initiators.items()},
        secrets=dict(secrets or {}),
        raw=data,
    )


def load_settings(paths: Paths | None = None) -> Settings:
    paths = paths or Paths.resolve()
    if not paths.config.is_file():
        raise ConfigError(f"config not found: {paths.config} (run `cashrecon init`)")
    try:
        data = tomllib.loads(paths.config.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid config.toml: {exc}") from None
    return settings_from_dict(data, paths, parse_env_file(paths.secrets))


def mask(value: str | None) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 6:
        return "***"
    return value[:3] + "***" + value[-2:]
