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

def register_operational_tables(conn) -> None:
    """Create operational tables (pending_orders, live_blotter, etc.).

    Migrated from telegram_bot.py init_db() in Cleanup Sprint A Purge 5.
    All statements use IF NOT EXISTS — safe to call on every startup.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY,
            payload JSON NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_blotter (
            order_id INTEGER PRIMARY KEY,
            account_id TEXT,
            ticker TEXT,
            sec_type TEXT,
            action TEXT,
            right TEXT,
            quantity INTEGER,
            limit_price REAL,
            live_mid REAL,
            natural_mid REAL,
            market_mid REAL,
            status TEXT,
            error TEXT,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS executed_orders (
            id INTEGER PRIMARY KEY,
            payload JSON NOT NULL,
            executed_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS premium_ledger (
            household_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            initial_basis REAL,
            total_premium_collected REAL,
            shares_owned INTEGER,
            PRIMARY KEY (household_id, ticker)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS csp_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            household_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            sector TEXT,
            spot_price REAL,
            strike REAL,
            expiry TEXT,
            dte INTEGER,
            bid REAL,
            annualized_yield REAL,
            iv_rank REAL,
            fundamental_score REAL,
            technical_score REAL,
            portfolio_fit_score REAL,
            composite_score REAL,
            decision TEXT NOT NULL,
            modified_ticker TEXT,
            modified_strike REAL,
            notes TEXT,
            vix_at_entry REAL,
            el_pct_at_entry REAL,
            portfolio_weight_after REAL,
            outcome TEXT,
            outcome_pnl REAL,
            outcome_date TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticker_universe (
            ticker TEXT PRIMARY KEY,
            company_name TEXT,
            gics_sector TEXT,
            gics_industry_group TEXT,
            index_membership TEXT,
            has_weekly_options INTEGER DEFAULT 0,
            avg_volume_30d REAL,
            market_cap REAL,
            last_updated TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker_universe_industry_group ON ticker_universe(gics_industry_group)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_status_created_at ON pending_orders(status, created_at, id)")

    # Decoupling Sprint B Unit B1: FA Block 1:N child-order tracking.
    # One row per child (per-account) order staged under a parent FA block.
    # Parent still lives in pending_orders; children live here with nullable
    # ib_perm_id / ib_order_id (populated async via IBKR openOrder callback).
    # margin_check_* columns are B5 scaffolding (CSP Allocator pre-stage) --
    # declared now so B5 isn't a second schema migration. See DT Q4 + blind
    # spots #1 (FA block margin contagion) and #2 (child permId race).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_order_children (
            id INTEGER PRIMARY KEY,
            parent_order_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            child_ib_order_id INTEGER,
            child_ib_perm_id INTEGER,
            status TEXT NOT NULL,
            margin_check_status TEXT,
            margin_check_reason TEXT,
            fill_price REAL,
            fill_qty INTEGER,
            fill_commission REAL,
            fill_time TIMESTAMP,
            last_ib_status TEXT,
            status_history JSON,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            FOREIGN KEY (parent_order_id) REFERENCES pending_orders(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_poc_parent "
        "ON pending_order_children(parent_order_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_poc_perm_id "
        "ON pending_order_children(child_ib_perm_id) "
        "WHERE child_ib_perm_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_poc_order_id "
        "ON pending_order_children(child_ib_order_id) "
        "WHERE child_ib_order_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_poc_account "
        "ON pending_order_children(account_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_poc_status "
        "ON pending_order_children(status)"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_orders_executed_at ON executed_orders(executed_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_blotter_account_ticker ON live_blotter(account_id, ticker, sec_type, action, right, status, order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csp_decisions_household_ticker ON csp_decisions(household_id, ticker, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_csp_decisions_decision ON csp_decisions(decision, composite_score)")

    # Tracker tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cc_cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            household TEXT, mode TEXT NOT NULL, strike REAL, expiry TEXT,
            bid REAL, annualized REAL, otm_pct REAL, dte INTEGER,
            walk_away_pnl REAL, spot REAL, adjusted_basis REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS roll_watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
            ticker TEXT NOT NULL, account_id TEXT, strike REAL, expiry TEXT,
            quantity INTEGER, mode TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')), resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mode_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            household TEXT, from_mode TEXT NOT NULL, to_mode TEXT NOT NULL,
            spot REAL, adjusted_basis REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_cycle_ticker ON cc_cycle_log(ticker, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roll_watchlist_status ON roll_watchlist(status, expiry)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mode_transitions_ticker ON mode_transitions(ticker, created_at)")

    # ALTER migrations
    mt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(mode_transitions)").fetchall()}
    if "overweight_since" not in mt_cols:
        conn.execute("ALTER TABLE mode_transitions ADD COLUMN overweight_since TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fill_log (
            exec_id TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL,
            quantity REAL, price REAL, premium_delta REAL,
            account_id TEXT NOT NULL DEFAULT '',
            household_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            inception_delta REAL,
            PRIMARY KEY (exec_id, account_id)
        )
    """)

    fl_cols = {row["name"] for row in conn.execute("PRAGMA table_info(fill_log)").fetchall()}
    if "inception_delta" not in fl_cols:
        conn.execute("ALTER TABLE fill_log ADD COLUMN inception_delta REAL")

    # Sprint B: fill_log composite PK migration (DT Shot 1 §7).
    # FA Block fills emit N execDetails per child account; composite PK
    # ensures per-account fill rows are never silently dropped by
    # INSERT OR IGNORE.  Migration: rebuild table if PK is single-column.
    _fl_pk = [r for r in conn.execute("PRAGMA table_info(fill_log)").fetchall()
              if r["pk"] > 0]
    if len(_fl_pk) == 1:
        conn.execute("""
            CREATE TABLE fill_log_b (
                exec_id TEXT NOT NULL, ticker TEXT NOT NULL,
                action TEXT NOT NULL, quantity REAL, price REAL,
                premium_delta REAL,
                account_id TEXT NOT NULL DEFAULT '',
                household_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                inception_delta REAL,
                PRIMARY KEY (exec_id, account_id)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO fill_log_b
                (exec_id, ticker, action, quantity, price, premium_delta,
                 account_id, household_id, created_at, inception_delta)
            SELECT exec_id, ticker, action, quantity, price, premium_delta,
                   COALESCE(account_id, ''), household_id, created_at,
                   inception_delta
            FROM fill_log
        """)
        conn.execute("DROP TABLE fill_log")
        conn.execute("ALTER TABLE fill_log_b RENAME TO fill_log")

    cc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cc_cycle_log)").fetchall()}
    if "flag" not in cc_cols:
        conn.execute("ALTER TABLE cc_cycle_log ADD COLUMN flag TEXT DEFAULT 'NORMAL'")

    tu_cols = {row["name"] for row in conn.execute("PRAGMA table_info(ticker_universe)").fetchall()}
    for col_name, col_type in (
        ("conviction_tier", "TEXT DEFAULT 'NEUTRAL'"), ("eps_revision_trend", "TEXT"),
        ("revenue_growth_vs_sector", "TEXT"), ("analyst_consensus_shift", "TEXT"),
        ("margin_trend", "TEXT"), ("conviction_updated_at", "TEXT"),
    ):
        if col_name not in tu_cols:
            conn.execute(f"ALTER TABLE ticker_universe ADD COLUMN {col_name} {col_type}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS conviction_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            original_tier TEXT NOT NULL, overridden_tier TEXT NOT NULL,
            justification TEXT NOT NULL, expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS premium_ledger_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, household_id TEXT NOT NULL,
            ticker TEXT NOT NULL, initial_basis REAL, total_premium_collected REAL,
            shares_owned INTEGER, archived_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    live_blotter_columns = {row["name"] for row in conn.execute("PRAGMA table_info(live_blotter)").fetchall()}
    for column_name, ddl in (
        ("account_id", "TEXT"), ("sec_type", "TEXT"), ("action", "TEXT"),
        ("right", "TEXT"), ("quantity", "INTEGER"), ("live_mid", "REAL"),
        ("natural_mid", "REAL"), ("market_mid", "REAL"), ("status", "TEXT"), ("error", "TEXT"),
    ):
        if column_name not in live_blotter_columns:
            conn.execute(f"ALTER TABLE live_blotter ADD COLUMN {column_name} {ddl}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            date TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, api_calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_by_model (
            date TEXT NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            api_calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (date, model)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            household_id TEXT NOT NULL, trade_date TEXT NOT NULL, trade_datetime TEXT,
            symbol TEXT NOT NULL, underlying TEXT, asset_category TEXT NOT NULL,
            trade_type TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL,
            proceeds REAL NOT NULL, realized_pnl REAL DEFAULT 0, commission REAL DEFAULT 0,
            return_category TEXT NOT NULL, source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, symbol, trade_datetime, quantity, price)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dividend_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            household_id TEXT NOT NULL, symbol TEXT NOT NULL, amount REAL NOT NULL,
            div_date TEXT NOT NULL, description TEXT, source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, symbol, div_date, amount)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nav_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            household_id TEXT NOT NULL, snapshot_date TEXT NOT NULL,
            nav_total REAL, nav_cash REAL, nav_stock REAL, nav_options REAL,
            net_deposits REAL DEFAULT 0, mwr_pct REAL, twr_pct REAL,
            source TEXT DEFAULT 'CSV', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, snapshot_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deposit_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            household_id TEXT NOT NULL, dep_date TEXT NOT NULL, amount REAL NOT NULL,
            dep_type TEXT, description TEXT, source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, dep_date, amount, description)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_ledger_date ON trade_ledger(trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_ledger_account ON trade_ledger(account_id, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_ledger_category ON trade_ledger(return_category, trade_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_offsets (
            account_id TEXT NOT NULL, household_id TEXT NOT NULL,
            period TEXT NOT NULL, premium_offset REAL DEFAULT 0,
            capgains_offset REAL DEFAULT 0, dividend_offset REAL DEFAULT 0,
            total_offset REAL DEFAULT 0, note TEXT,
            PRIMARY KEY (account_id, period)
        )
    """)
    conn.execute("""INSERT OR IGNORE INTO historical_offsets (account_id, household_id, period, total_offset, note) VALUES ('U21971297', 'Yash_Household', '2025', 12509.0, 'Fidelity Z30-836527 ($4,924) + Z32-346647 ($7,585) Jan-Sep 2025')""")
    conn.execute("""INSERT OR IGNORE INTO historical_offsets (account_id, household_id, period, total_offset, note) VALUES ('U22076329', 'Yash_Household', '2025', 25605.37, 'Fidelity 231-598209 Roth IRA Jan-Sep 2025')""")
    conn.execute("""INSERT OR IGNORE INTO historical_offsets (account_id, household_id, period, total_offset, note) VALUES ('U22076184', 'Yash_Household', '2025', 12888.54, 'Fidelity 263-000581 Rollover IRA Jan-Sep 2025')""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inception_config (
            key TEXT PRIMARY KEY, value REAL NOT NULL, note TEXT
        )
    """)
    conn.execute("""INSERT OR IGNORE INTO inception_config (key, value, note) VALUES ('starting_capital', 146959.04, 'Fidelity total portfolio Jan 1 2025')""")
    conn.execute("""INSERT OR IGNORE INTO inception_config (key, value, note) VALUES ('fidelity_remaining', 7412.00, 'Fidelity HSA + Cash Mgmt still held Dec 31 2025')""")
    conn.execute("""INSERT OR IGNORE INTO inception_config (key, value, note) VALUES ('fidelity_net_external', 48591.29, 'Fidelity net external deposits Jan-Sep 2025: $76,544 in - $27,953 out')""")

    # ── Execution kill-switch state (Sprint D safety) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_state (
            id INTEGER PRIMARY KEY CHECK (id=1),
            disabled INTEGER NOT NULL DEFAULT 1,
            set_by TEXT,
            set_at TEXT,
            reason TEXT
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO execution_state (id, disabled, set_by, set_at, reason)
        VALUES (1, 1, 'schema_init', datetime('now'), 'default disabled')
    """)

    # ── Decoupling Sprint A Unit A2: daemon heartbeat + orphan sweep ──
    # Per DT Q3 ruling. 90s stale TTL is set in agt_equities/health.py
    # (consumer-side), not as a DB constraint, to allow per-caller override
    # for tests and operator diagnostics.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daemon_heartbeat (
            daemon_name   TEXT PRIMARY KEY,
            last_beat_utc TEXT NOT NULL,
            pid           INTEGER NOT NULL,
            client_id     INTEGER,
            notes         TEXT
        )
    """)
    # Audit trail for orphan-sweep job runs. Separate from mode_history
    # because sweeps are operational events, not mode transitions.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orphan_sweep_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at_utc  TEXT NOT NULL,
            swept_count INTEGER NOT NULL,
            ttl_hours   REAL NOT NULL,
            notes       TEXT
        )
    """)
    # Cross-daemon alert bus (Sprint A unit A5b). Producers (typically
    # agt_scheduler jobs without a Telegram bot token) enqueue user-facing
    # events here; the bot process polls drain_pending_alerts() to render
    # them. Status state machine: pending -> in_flight -> sent | failed,
    # with retry-via-pending up to MAX_ATTEMPTS (see agt_equities/alerts.py).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cross_daemon_alerts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts    REAL NOT NULL,
            kind          TEXT NOT NULL,
            severity      TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            sent_ts       REAL,
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cross_daemon_alerts_status_created "
        "ON cross_daemon_alerts(status, created_ts, id)"
    )


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

    # Beta Impl 3: TRANSMITTING intermediate state for JIT handler
    _migrate_dyn_exit_add_transmitting(conn)

    # Beta Impl 5: exception_type column for R5 sell gate subclasses
    try:
        conn.execute(
            "ALTER TABLE bucket3_dynamic_exit_log "
            "ADD COLUMN exception_type TEXT"
        )
    except Exception:
        pass  # Column already exists

    # Followup #17: columns for orderRef linking, fill tracking, operator recovery
    for _f17_stmt in [
        "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN ib_order_id INTEGER",
        "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN ib_perm_id INTEGER",
        "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN fill_qty INTEGER",
        "ALTER TABLE bucket3_dynamic_exit_log ADD COLUMN commission REAL",
    ]:
        try:
            conn.execute(_f17_stmt)
        except Exception:
            pass  # Column already exists

    # Followup #20: originating account for sub-account routing
    try:
        conn.execute(
            "ALTER TABLE bucket3_dynamic_exit_log "
            "ADD COLUMN originating_account_id TEXT"
        )
        _log.info("schema: added originating_account_id column")
    except Exception:
        pass  # Column already exists

    # Followup #17: operator recovery audit trail (append-only)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recovery_audit_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_id            TEXT NOT NULL,
            operator_user_id    INTEGER NOT NULL,
            recovery_action     TEXT NOT NULL CHECK (recovery_action IN ('filled', 'abandoned')),
            pre_status          TEXT NOT NULL,
            post_status         TEXT NOT NULL,
            ib_order_id_provided INTEGER,
            operator_note       TEXT,
            recovery_ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
                                        'TRANSMITTING', 'TRANSMITTED',
                                        'CANCELLED', 'DRIFT_BLOCKED',
                                        'ABANDONED')),
            source TEXT NOT NULL DEFAULT 'scheduled_watchdog'
                CHECK (source IN ('scheduled_watchdog', 'manual_inspection',
                                  'cc_overweight', 'manual_stage')),
            exception_type TEXT
                CHECK (exception_type IS NULL OR exception_type IN (
                    'rule_8_dynamic_exit', 'thesis_deterioration',
                    'rule_6_forced_liquidation', 'emergency_risk_event')),
            fill_ts REAL,
            fill_price REAL,
            originating_account_id TEXT,
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

    # Phase 3A.5c2-alpha Task 6: add source column to dynamic_exit_log
    # Idempotent — ALTER TABLE is a no-op if column already exists (caught by try/except)
    try:
        conn.execute("""
            ALTER TABLE bucket3_dynamic_exit_log
            ADD COLUMN source TEXT NOT NULL DEFAULT 'scheduled_watchdog'
        """)
    except Exception:
        pass  # Column already exists

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bucket3_earnings_overrides (
            ticker TEXT PRIMARY KEY,
            override_value TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'manual_override',
            reason TEXT
        ) WITHOUT ROWID
    """)
    # Phase 3A.5c2-alpha Task 9: add reason column (idempotent)
    try:
        conn.execute("ALTER TABLE bucket3_earnings_overrides ADD COLUMN reason TEXT")
    except Exception:
        pass
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

    # Sprint 1B: add account_id to el_snapshots for per-account EL tracking
    try:
        conn.execute(
            "ALTER TABLE el_snapshots ADD COLUMN account_id TEXT"
        )
        _log.info("schema: added account_id column to el_snapshots")
    except Exception:
        pass  # Column already exists

    # Sprint 1E: Multi-tenant schema prep — add client_id to operational tables
    # No code reads client_id yet. DEFAULT 'AGT' backfills existing rows.
    import logging as _s1e_log
    _s1e_tables = [
        "bucket3_dynamic_exit_log",
        "pending_orders",
        "el_snapshots",
        "mode_history",
        "premium_ledger",
        "live_blotter",
        "executed_orders",
    ]
    _existing_tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for _tbl in _s1e_tables:
        if _tbl not in _existing_tables:
            _s1e_log.getLogger(__name__).info("schema: skipping client_id for %s (table not found)", _tbl)
            continue
        try:
            conn.execute(
                f"ALTER TABLE {_tbl} ADD COLUMN client_id TEXT DEFAULT 'AGT'"
            )
            _s1e_log.getLogger(__name__).info("schema: added client_id column to %s", _tbl)
        except Exception:
            pass  # Column already exists

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

    # Sprint B5: CSP Allocator pre-stage view — available_nlv per account.
    # Per DT Q4: NLV from el_snapshots; available_nlv = IBKR's excess_liquidity
    # (NOT a re-derivation from master_log_open_positions collateral math —
    # IBKR's portfolio-margin engine already nets box spreads, credit spreads,
    # CC assigned-share coverage, and all cross-position margin offsets).
    # encumbered_capital = nlv - excess_liquidity is a display-only derived
    # column; the allocator gates on available_nlv directly.
    conn.execute("""
        CREATE VIEW IF NOT EXISTS v_available_nlv AS
        SELECT
            account_id,
            household,
            nlv,
            excess_liquidity,
            (nlv - excess_liquidity) AS encumbered_capital,
            excess_liquidity          AS available_nlv,
            timestamp                 AS nlv_timestamp
        FROM (
            SELECT
                account_id, household, nlv, excess_liquidity, timestamp,
                ROW_NUMBER() OVER (
                    PARTITION BY account_id
                    ORDER BY timestamp DESC
                ) AS rn
            FROM el_snapshots
            WHERE account_id      IS NOT NULL
              AND nlv              IS NOT NULL
              AND excess_liquidity IS NOT NULL
        )
        WHERE rn = 1
    """)


def _register_autonomous_tables(conn) -> None:
    """Autonomous paper-trading session state (cross-run context).

    autonomous_session_log: append-only log of every scheduled task run.
    Each row captures what the task saw, decided, and did. Downstream
    tasks query recent rows to build context without needing shared memory.

    readiness_gate: tracks the 3-dimension live-readiness assessment.
    Dimension 1 (operational coverage): binary per-segment.
    Dimension 2 (risk discipline): continuous metrics over window.
    Dimension 3 (P&L coherence): strategy-level sanity checks.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_session_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name   TEXT NOT NULL,
            run_at      TEXT NOT NULL DEFAULT (datetime('now')),
            summary     TEXT,
            positions_snapshot  JSON,
            orders_snapshot     JSON,
            actions_taken       JSON,
            errors              JSON,
            metrics             JSON,
            notes               TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS readiness_gate (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            dimension   TEXT NOT NULL,
            segment     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'untested',
            last_tested TEXT,
            evidence    TEXT,
            notes       TEXT,
            UNIQUE(dimension, segment)
        )
    """)

    # Seed operational coverage segments if empty
    existing = conn.execute(
        "SELECT COUNT(*) FROM readiness_gate WHERE dimension = 'operational'"
    ).fetchone()[0]
    if existing == 0:
        segments = [
            ("csp_entry", "untested", "Scan → stage → approve → fill → DB"),
            ("csp_harvest", "untested", "Detect profitable short puts → BTC → DB"),
            ("put_assignment", "untested", "IB event → new shares → CC pivot"),
            ("cc_entry", "untested", "Scan → stage → approve → fill → DB"),
            ("cc_harvest", "untested", "Detect profitable short calls → BTC → DB"),
            ("cc_roll", "untested", "ITM detection → roll_engine → stage spread → fill"),
            ("call_assignment", "untested", "IB event → shares removed → cycle closed"),
            ("error_recovery", "untested", "Gateway drop → reconnect → state reconciliation"),
            ("multiday_persistence", "untested", "Positions survive overnight, reconcile"),
        ]
        for seg, status, notes in segments:
            conn.execute(
                "INSERT INTO readiness_gate (dimension, segment, status, notes) "
                "VALUES ('operational', ?, ?, ?)",
                (seg, status, notes),
            )
        conn.commit()


    # ── MR feat(remediation): remediation_incidents registry ──
    # State machine for the weekly remediation task. See agt_equities/remediation.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS remediation_incidents (
            incident_id       TEXT PRIMARY KEY,
            first_detected    TEXT NOT NULL,
            directive_source  TEXT,
            fix_authored_at   TEXT,
            mr_iid            INTEGER,
            branch_name       TEXT,
            status            TEXT NOT NULL DEFAULT 'new',
            rejection_reasons TEXT,
            last_nudged_at    TEXT,
            architect_reason  TEXT,
            updated_at        TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_remediation_incidents_status
        ON remediation_incidents(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_remediation_incidents_mr_iid
        ON remediation_incidents(mr_iid)
    """)

    # ── ADR-007 Step 3: structured `incidents` queue ──
    # Supersedes remediation_incidents as the machine-readable SoT for the
    # self-healing loop. Dual-written with remediation_incidents for two
    # sprints so the existing weekly remediation pipeline is not orphaned.
    # Schema follows ADR-007 §4.2 plus operational state-machine columns
    # (closed_at, consecutive_breaches, last_action_at) from the Step 3
    # kickoff. See agt_equities/incidents_repo.py for CRUD.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_key         TEXT NOT NULL,
            invariant_id         TEXT,
            severity             TEXT NOT NULL,
            scrutiny_tier        TEXT NOT NULL,
            status               TEXT NOT NULL DEFAULT 'open',
            detector             TEXT NOT NULL,
            detected_at          TEXT NOT NULL,
            closed_at            TEXT,
            last_action_at       TEXT,
            consecutive_breaches INTEGER NOT NULL DEFAULT 1,
            observed_state       TEXT,
            desired_state        TEXT,
            confidence           REAL,
            mr_iid               INTEGER,
            ddiff_url            TEXT,
            rejection_history    TEXT
        )
    """)
    # Partial unique index: only one active row per incident_key.
    # Closed rows (merged/resolved/rejected_permanently) do not block a
    # future reopen under the same key.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_active_key
        ON incidents(incident_key)
        WHERE status NOT IN ('merged','resolved','rejected_permanently')
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_status
        ON incidents(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_invariant_id
        ON incidents(invariant_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_incidents_mr_iid
        ON incidents(mr_iid)
    """)
    conn.commit()


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


def _migrate_dyn_exit_add_transmitting(conn) -> None:
    """Beta Impl 3: Add TRANSMITTING to bucket3_dynamic_exit_log CHECK constraint.

    SQLite cannot ALTER CHECK constraints on existing tables. For existing DBs
    where CREATE TABLE IF NOT EXISTS is a no-op, the old constraint stays frozen.

    Algorithm:
      1. Probe INSERT with final_status='TRANSMITTING'. If CHECK passes,
         constraint already allows the value — DELETE probe and return.
      2. If CHECK fails: table rebuild inside explicit transaction
         (RENAME → CREATE new → INSERT SELECT → DROP old → recreate indexes).

    The entire rebuild runs in a single BEGIN IMMEDIATE transaction so a failure
    at any point rolls back to pre-rebuild state (table still has old name).
    """
    import logging
    _log = logging.getLogger(__name__)

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if 'bucket3_dynamic_exit_log' not in tables:
        return  # Table doesn't exist yet; CREATE TABLE IF NOT EXISTS will handle it

    # Step 1: Probe — does the constraint already accept TRANSMITTING?
    try:
        conn.execute(
            "INSERT INTO bucket3_dynamic_exit_log "
            "(audit_id, trade_date, ticker, household, desk_mode, action_type, "
            " household_nlv, underlying_spot_at_render, final_status) "
            "VALUES ('__probe_transmitting__', '1970-01-01', '__PROBE__', "
            "        '__PROBE__', 'PEACETIME', 'CC', 0, 0, 'TRANSMITTING')"
        )
        conn.execute(
            "DELETE FROM bucket3_dynamic_exit_log "
            "WHERE audit_id = '__probe_transmitting__'"
        )
        conn.commit()
        return  # Constraint already valid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # Step 2: Rebuild table with updated CHECK constraint.
    # Entire rebuild in one explicit transaction for atomicity.
    _log.warning("Migrating bucket3_dynamic_exit_log: adding TRANSMITTING to CHECK constraint")
    try:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            "ALTER TABLE bucket3_dynamic_exit_log "
            "RENAME TO _dyn_exit_old_transmitting"
        )

        conn.execute("""
            CREATE TABLE bucket3_dynamic_exit_log (
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
                                            'TRANSMITTING', 'TRANSMITTED',
                                            'CANCELLED', 'DRIFT_BLOCKED',
                                            'ABANDONED')),
                source TEXT NOT NULL DEFAULT 'scheduled_watchdog'
                    CHECK (source IN ('scheduled_watchdog', 'manual_inspection',
                                      'cc_overweight', 'manual_stage')),
                exception_type TEXT
                    CHECK (exception_type IS NULL OR exception_type IN (
                        'rule_8_dynamic_exit', 'thesis_deterioration',
                        'rule_6_forced_liquidation', 'emergency_risk_event')),
                fill_ts REAL,
                fill_price REAL,
                originating_account_id TEXT,
                last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES bucket3_dynamic_exit_campaigns(campaign_id)
            ) WITHOUT ROWID
        """)

        # F2 fix: explicit column mapping via PRAGMA to handle ALTER-appended
        # columns whose physical ordinal differs from the new DDL order.
        _old_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(_dyn_exit_old_transmitting)"
        ).fetchall()]
        _col_list = ", ".join(_old_cols)
        conn.execute(
            f"INSERT INTO bucket3_dynamic_exit_log ({_col_list}) "
            f"SELECT {_col_list} FROM _dyn_exit_old_transmitting"
        )

        conn.execute("DROP TABLE _dyn_exit_old_transmitting")

        # Recreate indexes
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

        conn.commit()
        _log.info("Migration complete: TRANSMITTING added to CHECK constraint")
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        _log.error("Migration FAILED — table left in pre-migration state: %s", exc)
        raise
