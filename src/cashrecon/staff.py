"""Staff directory: 掌中通 account code (ztCode) → display name, used to name ZT withdrawal initiators."""

from __future__ import annotations

from typing import Any

from cashrecon.config import Settings
from cashrecon.db import Store, now_text
from cashrecon.logging_setup import get_logger
from cashrecon.zto import ZtoClient, ZtoError

log = get_logger("staff")


def parse_directory(data: Any) -> dict[str, str]:
    items = data.get("data") if isinstance(data, dict) else data
    result = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("ztCode") and item.get("name"):
            result[str(item["ztCode"])] = str(item["name"]).strip()
    return result


def refresh(store: Store, settings: Settings, client: ZtoClient | None = None) -> int:
    """Fetch the directory and upsert it; returns the number of entries (0 when unavailable)."""
    if not settings.site_code:
        return 0
    try:
        client = client or ZtoClient.from_settings(settings)
        data, _ = client.data("boss-operations", "salesman-basic-info-site", {"filters": {"siteCode": settings.site_code}})
    except ZtoError as exc:
        log.warning("staff directory unavailable: %s", exc.code)
        return 0
    directory = parse_directory(data)
    now = now_text()
    with store.tx():
        for code, name in directory.items():
            store.execute("INSERT INTO staff (code, name, updated_at) VALUES (?,?,?) ON CONFLICT(code) DO UPDATE "
                          "SET name=excluded.name, updated_at=excluded.updated_at", (code, name, now))
    return len(directory)


def names(store: Store, settings: Settings | None = None) -> dict[str, str]:
    result = {r["code"]: r["name"] for r in store.query("SELECT code, name FROM staff")}
    for code, mapping in (settings.withdraw_initiators if settings else {}).items():
        if mapping.get("name"):
            result[code] = mapping["name"]
    return result


def label(code: str, directory: dict[str, str]) -> str:
    if not code:
        return "未知"
    name = directory.get(code)
    return f"{name}（{code}）" if name else code
