"""Keep the accounts table in sync with configuration."""

from __future__ import annotations

from cashrecon.config import Settings
from cashrecon.db import Store, now_text


def sync_accounts(store: Store, settings: Settings) -> None:
    now = now_text()
    codes = [a.code for a in settings.accounts]
    with store.tx():
        for a in settings.accounts:
            store.execute(
                "INSERT INTO accounts (account_code, name, type, domain, collection, portal_code, bank_tail, "
                "personal_funds, active, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_code) DO UPDATE SET name=excluded.name, type=excluded.type, "
                "domain=excluded.domain, collection=excluded.collection, portal_code=excluded.portal_code, "
                "bank_tail=excluded.bank_tail, personal_funds=excluded.personal_funds, active=excluded.active, "
                "updated_at=excluded.updated_at",
                (a.code, a.name, a.type, a.domain, a.collection, a.portal_code, a.bank_tail,
                 int(a.personal_funds), int(a.active), now))
        placeholders = ",".join("?" * len(codes))
        store.execute(f"UPDATE accounts SET active = 0, updated_at = ? WHERE account_code NOT IN ({placeholders})",
                      (now, *codes))
