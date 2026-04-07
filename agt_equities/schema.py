"""
agt_equities.schema — DDL for all Master Log Refactor v3 tables.

Bucket 2: master_log_* (12 mirror tables + 1 sync audit)
Bucket 3 additions: inception_carryin, bot_order_log, cc_decision_log

All statements use CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
so they are safe to run on every bot startup (additive, idempotent).
"""

# ---------------------------------------------------------------------------
# Bucket 2 — Master Log Mirror tables
# ---------------------------------------------------------------------------

_BUCKET_2_TABLES = [
    # 1. Account Info
    """
    CREATE TABLE IF NOT EXISTS master_log_account_info (
        account_id          TEXT PRIMARY KEY,
        acct_alias          TEXT,
        model               TEXT,
        last_synced_at      TEXT NOT NULL
    );
    """,

    # 2. Trades
    """
    CREATE TABLE IF NOT EXISTS master_log_trades (
        transaction_id      TEXT PRIMARY KEY,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        model               TEXT,
        currency            TEXT NOT NULL,
        asset_category      TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        description         TEXT,
        conid               INTEGER NOT NULL,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        trade_id            TEXT,
        ib_order_id         INTEGER,
        ib_exec_id          TEXT,
        related_transaction_id TEXT,
        orig_trade_id       TEXT,
        date_time           TEXT NOT NULL,
        trade_date          TEXT NOT NULL,
        report_date         TEXT,
        order_time          TEXT,
        open_date_time      TEXT,
        transaction_type    TEXT NOT NULL,
        exchange            TEXT,
        buy_sell            TEXT NOT NULL,
        open_close          TEXT,
        order_type          TEXT,
        notes               TEXT,
        quantity            REAL NOT NULL,
        trade_price         REAL,
        proceeds            REAL,
        ib_commission       REAL,
        net_cash            REAL,
        cost                REAL,
        fifo_pnl_realized   REAL,
        mtm_pnl             REAL,
        last_synced_at      TEXT NOT NULL
    );
    """,

    # 3. Statement of Funds
    """
    CREATE TABLE IF NOT EXISTS master_log_statement_of_funds (
        transaction_id      TEXT PRIMARY KEY,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        currency            TEXT NOT NULL,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        conid               INTEGER,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        report_date         TEXT,
        date                TEXT,
        settle_date         TEXT,
        activity_code       TEXT,  -- NULL for Starting/Ending Balance summary rows (levelOfDetail='BaseCurrency')
        activity_description TEXT,
        trade_id            TEXT,
        related_trade_id    TEXT,
        order_id            INTEGER,
        buy_sell            TEXT,
        trade_quantity      REAL,
        trade_price         REAL,
        trade_gross         REAL,
        trade_commission    REAL,
        trade_tax           REAL,
        debit               REAL,
        credit              REAL,
        amount              REAL,
        trade_code          TEXT,
        balance             REAL,
        level_of_detail     TEXT,
        orig_transaction_id TEXT,
        related_transaction_id TEXT,
        action_id           TEXT,
        last_synced_at      TEXT NOT NULL
    );
    """,

    # 4. Open Positions
    """
    CREATE TABLE IF NOT EXISTS master_log_open_positions (
        report_date         TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        conid               INTEGER NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        asset_category      TEXT NOT NULL,
        sub_category        TEXT,
        symbol              TEXT NOT NULL,
        description         TEXT,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        position            REAL NOT NULL,
        mark_price          REAL,
        position_value      REAL,
        open_price          REAL,
        cost_basis_price    REAL,
        cost_basis_money    REAL,
        percent_of_nav      REAL,
        fifo_pnl_unrealized REAL,
        side                TEXT,
        open_date_time      TEXT,
        originating_order_id       INTEGER,
        originating_transaction_id TEXT,
        last_synced_at      TEXT NOT NULL,
        PRIMARY KEY (report_date, account_id, conid)
    );
    """,

    # 5. Corporate Actions
    """
    CREATE TABLE IF NOT EXISTS master_log_corp_actions (
        transaction_id      TEXT PRIMARY KEY,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        conid               INTEGER,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        report_date         TEXT,
        date_time           TEXT,
        action_description  TEXT,
        type                TEXT,
        amount              REAL,
        proceeds            REAL,
        value               REAL,
        quantity            REAL,
        cost                REAL,
        realized_pnl        REAL,
        mtm_pnl             REAL,
        code                TEXT,
        action_id           TEXT,
        last_synced_at      TEXT NOT NULL
    );
    """,

    # 6. Option Exercise / Assignment / Expiration
    """
    CREATE TABLE IF NOT EXISTS master_log_option_eae (
        trade_id            TEXT PRIMARY KEY,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        conid               INTEGER,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        date                TEXT,
        transaction_type    TEXT,
        quantity            REAL,
        trade_price         REAL,
        close_price         REAL,
        proceeds            REAL,
        comm_tax            REAL,
        basis               REAL,
        realized_pnl        REAL,
        mtm_pnl             REAL,
        last_synced_at      TEXT NOT NULL
    );
    """,

    # 7. NAV
    """
    CREATE TABLE IF NOT EXISTS master_log_nav (
        report_date         TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        cash                REAL,
        cash_long           REAL,
        cash_short          REAL,
        stock               REAL,
        stock_long          REAL,
        stock_short         REAL,
        options             REAL,
        options_long        REAL,
        options_short       REAL,
        dividend_accruals   REAL,  -- DEAD: IBKR does not emit this on EquitySummaryByReportDateInBase.
                                   -- Populated (if ever) from OpenDividendAccruals section separately.
                                   -- Kept in schema for forward-compat; flex_sync writes NULL here.
        interest_accruals                       REAL,
        interest_accruals_long                  REAL,
        interest_accruals_short                 REAL,
        bond_interest_accruals_component        REAL,
        bond_interest_accruals_component_long   REAL,
        bond_interest_accruals_component_short  REAL,
        broker_fees_accruals_component          REAL,
        broker_fees_accruals_component_long     REAL,
        broker_fees_accruals_component_short    REAL,
        margin_financing_charge_accruals        REAL,
        margin_financing_charge_accruals_long   REAL,
        margin_financing_charge_accruals_short  REAL,
        crypto              REAL,
        crypto_long         REAL,
        crypto_short        REAL,
        total               REAL,
        total_long          REAL,
        total_short         REAL,
        last_synced_at      TEXT NOT NULL,
        PRIMARY KEY (report_date, account_id)
    );
    """,

    # 8. Change in NAV
    """
    CREATE TABLE IF NOT EXISTS master_log_change_in_nav (
        from_date           TEXT NOT NULL,
        to_date             TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        starting_value      REAL,
        mtm                 REAL,
        realized            REAL,
        change_in_unrealized REAL,
        cost_adjustments    REAL,
        transferred_pnl_adjustments REAL,
        deposits_withdrawals REAL,
        internal_cash_transfers REAL,
        asset_transfers     REAL,
        dividends           REAL,
        withholding_tax     REAL,
        change_in_dividend_accruals REAL,
        interest            REAL,
        change_in_interest_accruals REAL,
        broker_fees         REAL,
        change_in_broker_fee_accruals REAL,
        other_fees          REAL,
        other_income        REAL,
        commissions         REAL,
        other               REAL,
        ending_value        REAL,
        twr                 REAL,
        corporate_action_proceeds REAL,
        last_synced_at      TEXT NOT NULL,
        PRIMARY KEY (from_date, to_date, account_id)
    );
    """,

    # 9. Realized / Unrealized Performance
    """
    CREATE TABLE IF NOT EXISTS master_log_realized_unrealized_perf (
        report_date         TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        conid               INTEGER NOT NULL,
        acct_alias          TEXT,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        cost_adj            REAL,
        realized_st_profit  REAL,
        realized_st_loss    REAL,
        realized_lt_profit  REAL,
        realized_lt_loss    REAL,
        total_realized_pnl  REAL,
        unrealized_profit   REAL,
        unrealized_loss     REAL,
        unrealized_st_profit REAL,
        unrealized_st_loss  REAL,
        unrealized_lt_profit REAL,
        unrealized_lt_loss  REAL,
        total_unrealized_pnl REAL,
        total_fifo_pnl      REAL,
        transferred_pnl     REAL,
        code                TEXT,
        last_synced_at      TEXT NOT NULL,
        PRIMARY KEY (report_date, account_id, conid)
    );
    """,

    # 10. MTM Performance
    """
    CREATE TABLE IF NOT EXISTS master_log_mtm_perf (
        report_date         TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        conid               INTEGER NOT NULL,
        acct_alias          TEXT,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        previous_close_quantity REAL,
        previous_close_price REAL,
        close_quantity      REAL,
        close_price         REAL,
        transaction_mtm_pnl REAL,
        prior_open_mtm_pnl  REAL,
        commissions         REAL,
        other               REAL,
        other_accruals      REAL,
        total               REAL,
        total_accruals      REAL,
        code                TEXT,
        last_synced_at      TEXT NOT NULL,
        PRIMARY KEY (report_date, account_id, conid)
    );
    """,

    # 11. Dividend Accruals
    """
    CREATE TABLE IF NOT EXISTS master_log_div_accruals (
        report_date         TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        conid               INTEGER NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        ex_date             TEXT,
        pay_date            TEXT,
        quantity            REAL,
        tax                 REAL,
        fee                 REAL,
        gross_rate          REAL,
        gross_amount        REAL,
        net_amount          REAL,
        code                TEXT,
        last_synced_at      TEXT NOT NULL,
        PRIMARY KEY (report_date, account_id, conid)
    );
    """,

    # 12. Transfers
    """
    CREATE TABLE IF NOT EXISTS master_log_transfers (
        transaction_id      TEXT PRIMARY KEY,
        account_id          TEXT NOT NULL,
        acct_alias          TEXT,
        currency            TEXT,
        asset_category      TEXT,
        symbol              TEXT,
        description         TEXT,
        conid               INTEGER,
        underlying_conid    INTEGER,
        underlying_symbol   TEXT,
        multiplier          REAL,
        strike              REAL,
        expiry              TEXT,
        put_call            TEXT,
        report_date         TEXT,
        date                TEXT,
        date_time           TEXT,
        settle_date         TEXT,
        type                TEXT,
        direction           TEXT,
        transfer_company    TEXT,
        transfer_account    TEXT,
        transfer_account_name TEXT,
        delivering_broker   TEXT,
        quantity            REAL,
        transfer_price      REAL,
        position_amount     REAL,
        position_amount_in_base REAL,
        pnl_amount          REAL,
        pnl_amount_in_base  REAL,
        cash_transfer       REAL,
        code                TEXT,
        client_reference    TEXT,
        level_of_detail     TEXT,
        position_instruction_id    TEXT,
        position_instructionset_id TEXT,
        last_synced_at      TEXT NOT NULL
    );
    """,
]

_BUCKET_2_INDEXES = [
    # Trades indexes
    "CREATE INDEX IF NOT EXISTS idx_mlt_account_ticker_date ON master_log_trades(account_id, underlying_symbol, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_mlt_ib_order_id ON master_log_trades(ib_order_id);",
    "CREATE INDEX IF NOT EXISTS idx_mlt_ib_exec_id ON master_log_trades(ib_exec_id);",
    "CREATE INDEX IF NOT EXISTS idx_mlt_conid_date ON master_log_trades(conid, date_time);",
    # Statement of Funds indexes
    "CREATE INDEX IF NOT EXISTS idx_mlsof_account_date ON master_log_statement_of_funds(account_id, date);",
    "CREATE INDEX IF NOT EXISTS idx_mlsof_activity ON master_log_statement_of_funds(activity_code);",
]

# ---------------------------------------------------------------------------
# Bucket 2 — Sync audit table
# ---------------------------------------------------------------------------

_SYNC_AUDIT = [
    """
    CREATE TABLE IF NOT EXISTS master_log_sync (
        sync_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at          TEXT NOT NULL,
        finished_at         TEXT,
        flex_query_id       TEXT NOT NULL,
        from_date           TEXT,
        to_date             TEXT,
        reference_code      TEXT,
        sections_processed  INTEGER,
        rows_received       INTEGER,
        rows_inserted       INTEGER,
        rows_updated        INTEGER,
        status              TEXT NOT NULL,
        error_message       TEXT
    );
    """,
]

# ---------------------------------------------------------------------------
# Bucket 3 — New operational tables
# ---------------------------------------------------------------------------

_BUCKET_3_TABLES = [
    # Inception carry-in
    """
    CREATE TABLE IF NOT EXISTS inception_carryin (
        household_id        TEXT NOT NULL,
        account_id          TEXT NOT NULL,
        asset_class         TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        conid               INTEGER,
        right               TEXT,
        strike              REAL,
        expiry              TEXT,
        quantity            REAL NOT NULL,
        basis_price         REAL,
        as_of_date          TEXT NOT NULL,
        source_broker       TEXT,
        reason              TEXT,
        notes               TEXT,
        PRIMARY KEY (account_id, asset_class, conid)
    );
    """,

    # Bot order log
    """
    CREATE TABLE IF NOT EXISTS bot_order_log (
        bot_order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        order_ref           TEXT,
        ib_order_id         INTEGER,
        account_id          TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        asset_class         TEXT NOT NULL,
        right               TEXT,
        strike              REAL,
        expiry              TEXT,
        action              TEXT NOT NULL,
        quantity            INTEGER NOT NULL,
        order_type          TEXT,
        limit_price         REAL,
        tif                 TEXT,
        placed_at           TEXT NOT NULL,
        placed_by           TEXT,
        status              TEXT NOT NULL,
        fill_ib_exec_id     TEXT,
        fill_price          REAL,
        fill_quantity       INTEGER,
        fill_commission     REAL,
        fill_time           TEXT,
        notes               TEXT,
        updated_at          TEXT NOT NULL
    );
    """,

    # CC decision log
    """
    CREATE TABLE IF NOT EXISTS cc_decision_log (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker                      TEXT NOT NULL,
        account_id                  TEXT,
        mode                        TEXT,
        strike                      REAL,
        expiry                      TEXT,
        bid                         REAL,
        annualized                  REAL,
        otm_pct                     REAL,
        dte                         INTEGER,
        walk_away_pnl               REAL,
        spot                        REAL,
        bot_believed_adjusted_basis REAL,
        basis_truth_level           TEXT,
        as_of_report_date           TEXT,
        overlay_applied             BOOLEAN,
        master_log_sync_id          INTEGER,
        flag                        TEXT,
        created_at                  TEXT NOT NULL
    );
    """,

    # Order lifecycle extensions (R5)
    # These ALTER TABLE statements extend pending_orders with state machine fields.
    # Safe to run multiple times (column existence checked in init_db).

    # Corp action quarantine
    """
    CREATE TABLE IF NOT EXISTS corp_action_quarantine (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker          TEXT NOT NULL,
        account_id      TEXT,
        action_type     TEXT NOT NULL,
        action_description TEXT,
        detected_at     TEXT NOT NULL DEFAULT (datetime('now')),
        cleared_at      TEXT,
        cleared_by      TEXT,
        notes           TEXT
    );
    """,

    # Market data audit log
    """
    CREATE TABLE IF NOT EXISTS market_data_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       TEXT NOT NULL,
        ticker          TEXT NOT NULL,
        source          TEXT NOT NULL,  -- 'IBKR' or 'YFINANCE'
        latency_ms      REAL,
        success         INTEGER NOT NULL,  -- 1=true, 0=false
        error_class     TEXT  -- NETWORK, MARKET_CLOSED, NO_DATA, RATE_LIMIT, or empty
    );
    """,
]

_BUCKET_3_INDEXES = [
    # Bot order log indexes
    "CREATE INDEX IF NOT EXISTS idx_bot_order_ib_order_id ON bot_order_log(ib_order_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_order_ib_exec_id ON bot_order_log(fill_ib_exec_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_order_status ON bot_order_log(status);",
    # CC decision log index
    "CREATE INDEX IF NOT EXISTS idx_cc_decision_ticker_created ON cc_decision_log(ticker, created_at);",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_master_log_tables(conn) -> None:
    """Execute all DDL for Master Log Refactor v3. Safe to call on every startup.

    Called from telegram_bot.py init_db() via a single import line.
    All statements use IF NOT EXISTS — fully idempotent.
    """
    for group in (_BUCKET_2_TABLES, _BUCKET_2_INDEXES, _SYNC_AUDIT, _BUCKET_3_TABLES, _BUCKET_3_INDEXES):
        for stmt in group:
            conn.execute(stmt)

    # R5: Order state machine — extend pending_orders with lifecycle fields
    _extend_pending_orders(conn)

    # Phase 3A.5a: Add accelerator_clause to glide_paths
    _extend_glide_paths(conn)

    # Phase 3A.5b: Red Alert state (R9 hysteresis)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS red_alert_state (
            household        TEXT PRIMARY KEY,
            current_state    TEXT NOT NULL CHECK (current_state IN ('OFF', 'ON'))
                             DEFAULT 'OFF',
            activated_at     TEXT,
            activation_reason TEXT,
            conditions_met_count INTEGER NOT NULL DEFAULT 0,
            conditions_met_list  TEXT,
            last_updated     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Seed both households OFF on first run (idempotent via INSERT OR IGNORE)
    conn.execute(
        "INSERT OR IGNORE INTO red_alert_state (household, current_state) "
        "VALUES ('Yash_Household', 'OFF')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO red_alert_state (household, current_state) "
        "VALUES ('Vikram_Household', 'OFF')"
    )

    # Phase 3A.5c1: IV history for IV rank computation (252-day bootstrap)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_macro_iv_history (
            ticker      TEXT NOT NULL,
            trade_date  TEXT NOT NULL,
            iv_30       REAL NOT NULL,
            sample_source TEXT NOT NULL DEFAULT 'eod_macro_sync',
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, trade_date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_iv_hist_ticker_date
        ON bucket3_macro_iv_history(ticker, trade_date DESC)
    """)

    # Phase 3A.5c1: Corporate intelligence cache (Bucket 3)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_corporate_cache (
            ticker      TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'yfinance'
        )
    """)

    # Phase 3A.5c2-alpha: Dynamic Exit audit log + campaigns + earnings overrides
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_log (
            audit_id TEXT PRIMARY KEY,
            trade_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            household TEXT NOT NULL,
            desk_mode TEXT NOT NULL CHECK (desk_mode IN ('PEACETIME', 'AMBER', 'WARTIME')),
            action_type TEXT NOT NULL CHECK (action_type IN ('CC', 'STK_SELL')),
            household_nlv REAL NOT NULL,
            underlying_spot_at_render REAL NOT NULL,
            gate1_freed_margin REAL,
            gate1_realized_loss REAL,
            gate1_conviction_tier TEXT,
            gate1_conviction_modifier REAL,
            gate1_ratio REAL,
            gate2_target_contracts INTEGER,
            gate2_max_per_cycle INTEGER,
            walk_away_pnl_per_share REAL,
            strike REAL,
            expiry TEXT,
            contracts INTEGER,
            shares INTEGER,
            limit_price REAL,
            campaign_id TEXT,
            operator_thesis TEXT,
            attestation_value_typed TEXT,
            checkbox_state_json TEXT,
            render_ts REAL,
            staged_ts REAL,
            transmitted INTEGER NOT NULL DEFAULT 0,
            transmitted_ts REAL,
            re_validation_count INTEGER NOT NULL DEFAULT 0,
            final_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (final_status IN ('PENDING', 'STAGED', 'ATTESTED',
                                        'TRANSMITTED', 'CANCELLED',
                                        'DRIFT_BLOCKED', 'ABANDONED')),
            source TEXT NOT NULL DEFAULT 'scheduled_watchdog'
                CHECK (source IN ('scheduled_watchdog', 'manual_inspection',
                                  'cc_overweight', 'manual_stage')),
            fill_ts REAL,
            fill_price REAL,
            last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES bucket3_dynamic_exit_campaigns(campaign_id)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dyn_exit_status
        ON bucket3_dynamic_exit_log(final_status, household)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dyn_exit_ticker_date
        ON bucket3_dynamic_exit_log(ticker, trade_date DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dyn_exit_household
        ON bucket3_dynamic_exit_log(household, trade_date DESC)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_dynamic_exit_campaigns (
            campaign_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            household TEXT NOT NULL,
            inception_date TEXT NOT NULL,
            locked_target_shares INTEGER NOT NULL,
            locked_conviction_modifier REAL NOT NULL,
            locked_conviction_tier TEXT NOT NULL
                CHECK (locked_conviction_tier IN ('HIGH', 'NEUTRAL', 'LOW')),
            status TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'COMPLETE', 'ABANDONED')),
            shares_exited_cumulative INTEGER NOT NULL DEFAULT 0,
            last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_campaigns_active
        ON bucket3_dynamic_exit_campaigns(ticker, household)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_earnings_overrides (
            ticker TEXT PRIMARY KEY,
            override_value TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'manual_override'
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_earnings_overrides_expires
        ON bucket3_earnings_overrides(expires_at)
    """)

    # R5: Orphan order events table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orphan_order_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            ib_order_id INTEGER,
            ib_perm_id  INTEGER,
            status      TEXT,
            payload     TEXT,
            received_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # W3.6: Walker warnings log — persisted per sync/reconcile run
    conn.execute("""
        CREATE TABLE IF NOT EXISTS walker_warnings_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_id         TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            code            TEXT NOT NULL,
            severity        TEXT NOT NULL,
            ticker          TEXT,
            household       TEXT,
            account         TEXT,
            message         TEXT NOT NULL,
            context_json    TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_walker_warnings_sync_id
        ON walker_warnings_log(sync_id)
    """)

    # Phase 3A: Glide paths — per-rule forward-looking progress trackers
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glide_paths (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id    TEXT NOT NULL,
            rule_id         TEXT NOT NULL,
            ticker          TEXT,
            baseline_value  REAL NOT NULL,
            target_value    REAL NOT NULL,
            start_date      TEXT NOT NULL,
            target_date     TEXT NOT NULL,
            pause_conditions TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            notes           TEXT,
            UNIQUE(household_id, rule_id, ticker)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_glide_household
        ON glide_paths(household_id)
    """)

    # Phase 3A: Mode history — desk mode transitions audit log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mode_history (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
            old_mode            TEXT NOT NULL,
            new_mode            TEXT NOT NULL,
            trigger_rule        TEXT,
            trigger_household   TEXT,
            trigger_value       REAL,
            notes               TEXT
        )
    """)

    # Phase 3A: EL snapshots — live IBKR EL readings (Bucket 3)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS el_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            household           TEXT NOT NULL,
            timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
            excess_liquidity    REAL,
            nlv                 REAL,
            buying_power        REAL,
            source              TEXT NOT NULL DEFAULT 'ibkr_live'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_el_household_ts
        ON el_snapshots(household, timestamp)
    """)

    # Phase 3A: Sector overrides — manual industry classification corrections
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_overrides (
            ticker      TEXT PRIMARY KEY,
            sector      TEXT NOT NULL,
            sub_sector  TEXT,
            source      TEXT NOT NULL DEFAULT 'manual',
            notes       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def _extend_pending_orders(conn) -> None:
    """Add R5 order lifecycle columns to pending_orders if missing.
    Silently skips if pending_orders table doesn't exist (test DBs)."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if 'pending_orders' not in tables:
        return
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_orders)").fetchall()
    }
    extensions = [
        ("ib_order_id", "INTEGER"),
        ("ib_perm_id", "INTEGER"),
        ("status_history", "TEXT"),       # JSON array of {status, at, by, payload}
        ("fill_price", "REAL"),
        ("fill_qty", "INTEGER"),
        ("fill_commission", "REAL"),
        ("fill_time", "TEXT"),
        ("last_ib_status", "TEXT"),
    ]
    for col_name, col_type in extensions:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE pending_orders ADD COLUMN {col_name} {col_type}")


def _extend_glide_paths(conn) -> None:
    """Phase 3A.5a: Add accelerator_clause column to glide_paths if missing."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if 'glide_paths' not in tables:
        return
    gp_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(glide_paths)").fetchall()
    }
    if "accelerator_clause" not in gp_cols:
        conn.execute("ALTER TABLE glide_paths ADD COLUMN accelerator_clause TEXT")
