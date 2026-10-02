"""Database schema. Migrations are append-only; never edit a released step."""

from __future__ import annotations

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE accounts (
        account_code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        domain TEXT NOT NULL,
        collection TEXT NOT NULL,
        portal_code TEXT NOT NULL DEFAULT '',
        bank_tail TEXT NOT NULL DEFAULT '',
        personal_funds INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE flows (
        flow_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        account_code TEXT NOT NULL,
        biz_date TEXT NOT NULL,
        biz_time TEXT NOT NULL,
        direction TEXT NOT NULL CHECK (direction IN ('IN','OUT')),
        amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
        balance_after_cents INTEGER,
        counterparty TEXT NOT NULL DEFAULT '',
        src_category TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        initiator TEXT NOT NULL DEFAULT '',
        status_text TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL DEFAULT '{}',
        raw_hash TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        removed INTEGER NOT NULL DEFAULT 0,
        UNIQUE (source, source_ref)
    );
    CREATE INDEX idx_flows_day_account ON flows (biz_date, account_code);
    CREATE INDEX idx_flows_source_day ON flows (source, biz_date);

    CREATE TABLE balances (
        biz_date TEXT NOT NULL,
        account_code TEXT NOT NULL,
        source TEXT NOT NULL,
        opening_cents INTEGER,
        closing_cents INTEGER,
        inflow_cents INTEGER,
        outflow_cents INTEGER,
        note TEXT NOT NULL DEFAULT '',
        captured_at TEXT NOT NULL,
        PRIMARY KEY (biz_date, account_code, source)
    );

    CREATE TABLE zt_categories (
        biz_date TEXT NOT NULL,
        path TEXT NOT NULL,
        level1 TEXT NOT NULL,
        level2 TEXT NOT NULL,
        level3 TEXT NOT NULL,
        description TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        PRIMARY KEY (biz_date, path)
    );

    CREATE TABLE bill_profit (
        biz_date TEXT NOT NULL,
        component TEXT NOT NULL,
        amount_cents INTEGER,
        status TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        captured_at TEXT NOT NULL,
        PRIMARY KEY (biz_date, component)
    );

    CREATE TABLE fetches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        source TEXT NOT NULL,
        biz_date TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ok','empty','failed','skipped')),
        row_count INTEGER NOT NULL DEFAULT 0,
        changed_count INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        route TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL
    );
    CREATE INDEX idx_fetches_source_day ON fetches (source, biz_date, id);

    CREATE TABLE category_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL DEFAULT '*',
        field TEXT NOT NULL,
        op TEXT NOT NULL DEFAULT 'contains',
        pattern TEXT NOT NULL,
        direction TEXT NOT NULL DEFAULT '*',
        category TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        enabled INTEGER NOT NULL DEFAULT 1,
        origin TEXT NOT NULL DEFAULT 'default',
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );

    CREATE TABLE manual_decisions (
        flow_id TEXT PRIMARY KEY,
        decision TEXT NOT NULL,
        target_flow_id TEXT,
        category TEXT,
        note TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE manual_balances (
        account_code TEXT NOT NULL,
        as_of TEXT NOT NULL,
        balance_cents INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_code, as_of)
    );

    CREATE TABLE links (
        link_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        flow_a TEXT NOT NULL,
        flow_b TEXT,
        biz_date TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        fee_cents INTEGER NOT NULL DEFAULT 0,
        rule TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'auto'
    );
    CREATE INDEX idx_links_day ON links (biz_date);
    CREATE INDEX idx_links_a ON links (flow_a);
    CREATE INDEX idx_links_b ON links (flow_b);

    CREATE TABLE flow_states (
        flow_id TEXT PRIMARY KEY,
        biz_date TEXT NOT NULL,
        state TEXT NOT NULL,
        category TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        link_id TEXT
    );
    CREATE INDEX idx_flow_states_day ON flow_states (biz_date, state);

    CREATE TABLE daily_results (
        biz_date TEXT PRIMARY KEY,
        data_status TEXT NOT NULL,
        payload TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        computed_at TEXT NOT NULL
    );

    CREATE TABLE alerts (
        alert_key TEXT PRIMARY KEY,
        level TEXT NOT NULL,
        rule TEXT NOT NULL,
        biz_date TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        last_notified TEXT
    );
    CREATE INDEX idx_alerts_day ON alerts (biz_date);

    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        job TEXT NOT NULL,
        target TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT
    );

    CREATE TABLE deliveries (
        report_key TEXT NOT NULL,
        channel TEXT NOT NULL,
        status TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        receipt TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (report_key, channel)
    );

    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 2 — machine-readable review kind for grouping
    """
    ALTER TABLE flow_states ADD COLUMN kind TEXT NOT NULL DEFAULT '';
    """,
    # 3 — business period (accrual), staff directory
    """
    ALTER TABLE flows ADD COLUMN period_start TEXT;
    ALTER TABLE flows ADD COLUMN period_end TEXT;
    ALTER TABLE flow_states ADD COLUMN p_start TEXT;
    ALTER TABLE flow_states ADD COLUMN p_end TEXT;
    ALTER TABLE flow_states ADD COLUMN period_basis TEXT NOT NULL DEFAULT '';
    ALTER TABLE manual_decisions ADD COLUMN period_start TEXT;
    ALTER TABLE manual_decisions ADD COLUMN period_end TEXT;
    CREATE TABLE staff (code TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL);
    """,
]
