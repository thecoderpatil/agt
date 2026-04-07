# AGT EQUITIES — MASTER LOG REFACTOR (v3)

## Claude Code Mission Briefing

You are being asked to execute a substantial architectural refactor of a live options wheel trading desk codebase. This prompt is the complete specification. Read it end-to-end before planning. Then save a copy to `/docs/REFACTOR_SPEC_v3.md` in the repo so subagents can reference it.

This project was designed over multiple rounds of review with Codex and Gemini as auditors, with the project owner ("Yash") as the final architectural decision-maker. The design is locked. Your job is execution, not re-design. Where this spec leaves something ambiguous, report back and ask — do not improvise.

---

## 0. OPERATING RULES (read before anything else)

**Rule 1 — Report, don't auto-fix.** When you discover issues in the existing codebase, your job is to surface them in a structured report for Yash to review, not to silently fix them. This includes: dead code, bugs, redundant logic, commented-out blocks, mystery constants. Report. Wait. Let Yash decide.

**Rule 2 — Worked examples with real numbers.** Every test, every example, every assertion should use actual values from Yash's portfolio. Sample data is provided below under "Test Scenarios". Do not invent synthetic test data when real data is available.

**Rule 3 — If a fix might introduce new bugs, say so explicitly.** Especially for anything touching the order placement path, fill event handlers, or the database schema. Flag risk, don't hide it.

**Rule 4 — Bucket 2 (master_log_*) tables are PRISTINE MIRRORS.** Only `flex_sync.py` writes to them. Ever. No other module, no migration script, no "temporary fix" writes to master_log_* tables. If you find yourself needing to, that's a signal the design is wrong — stop and report.

**Rule 5 — Dual-write stays active for the ENTIRE soak period.** Do not remove legacy writes during Phases 2-4. Writes only stop at Phase 5, immediately before DROP. This is a non-negotiable safety net.

**Rule 6 — No destructive operations without Yash's explicit approval.** Dropping tables, deleting files, removing `CREATE TABLE IF NOT EXISTS` statements from `init_db()` — all of these require a checkpoint. Claude Code should present the diff and wait for approval before executing.

**Rule 7 — This refactor is about eliminating math in Python, not rewriting every module.** If a module doesn't read or write any of the dropped tables, don't touch it. Scope discipline matters.

---

## 1. PROJECT CONTEXT

**AGT Equities** is a proprietary options trading desk run by Yash from Puerto Rico (Act 60 tax-free yield), executing the Heitkoetter Wheel Strategy across 4 Interactive Brokers accounts in 2 households:

| Account | Type | Household |
|---|---|---|
| U21971297 | Individual | Yash_Household |
| U22076184 | Traditional IRA (Dormant) | Yash_Household |
| U22076329 | Roth IRA | Yash_Household |
| U22388499 | Vikram Individual | Vikram_Household |

**Tech stack** (unchanged by this refactor):
- Python 3 on Windows desktop
- SQLite local DB (`agt_desk.db`)
- `ib_async` for IBKR TWS API (live data + order placement)
- `python-telegram-bot` for the operator UI (27 slash commands)
- Anthropic API (Claude) as the decision brain via conversational tool layer
- `yfinance` for fundamentals and secondary market data

**Codebase layout** (pre-refactor, what exists today):
- `telegram_bot.py` (~9,500 lines) — the main service, contains all slash commands, DB schema via `init_db()`, fill event handlers, IB connection management, CSP scanner, CC ladder, Dynamic Exit, Rule 8 watchdog
- `dashboard_renderer.py` — the dashboard rendering logic (currently reads premium_ledger-based data)
- `telegram_dashboard_integration.py` — dashboard command wiring
- `pxo_scanner.py` — PXO (put credit spread) scanner
- `test_margin_logic.py` — IBKR margin calculation tests
- `Portfolio_Risk_Rulebook_v8.md` — the strategy rulebook (read this for context on Rules 1-9)
- `rulebook_llm_condensed.md` — LLM-friendly version of the rulebook
- `ARCHITECTURE_md.txt` — original system vision
- `boot_desk.bat` — Windows startup script

The working directory on Yash's Windows machine is `C:\AGT_Telegram_Bridge\` — you will see paths like `/C:/AGT_Telegram_Bridge/telegram_bot.py` in this spec.

---

## 2. WHY THIS REFACTOR EXISTS

The current system maintains its own ledger via **ten overlapping SQLite tables** populated by intercepting fill events from IBKR's TWS API. This creates the following problems:

1. **Drift surface**: the bot's local view of positions/basis/premium can diverge from IBKR's books. Every time it drifts, decision commands make wrong calls.
2. **Concurrency bugs**: fill events, dashboard reads, and DB writes race against each other.
3. **Reconciliation pain**: there's no single source of truth; we've been comparing bot state against IBKR Activity Statements manually.
4. **Carry-in kludges**: a `historical_offsets` table and `inception_config` table exist to patch up pre-IBKR positions. These are fragile.
5. **Math bugs are hard to detect**: adjusted_basis, walk-away P&L, and cycle attribution are computed in Python across multiple locations.

The refactor replaces this with a **three-bucket model**:

- **Bucket 1 — Real-time API state (no persistence)**: NLV, live positions, quotes, greeks, margin, order status. Fetched on demand from `ib_async`. Never written to SQLite.
- **Bucket 2 — Master Log mirror (daily sync from IBKR Flex Web Service)**: 12 tables mirroring 12 Flex sections. Written ONLY by `flex_sync.py`. Read-only for everyone else.
- **Bucket 3 — Operational state (bot's own bookkeeping)**: pending orders, decisions, universe, conviction overrides, mode transitions, API usage. Things IBKR doesn't track because they're internal to the bot's decision loop.

After the refactor, **the only significant math left in Python is the cycle Walker** — a pure function that derives wheel cycles from the settled Master Log data.

---

## 3. THE MASTER LOG FLEX QUERY (already configured)

Yash has already built and verified the Flex Query. Do not touch it; use it as-is.

```
Token:     ${AGT_FLEX_TOKEN}
Query ID:  1461095
Query Name: MASTER_LOG
Endpoint:  https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService
Flow:      GET SendRequest?t={TOKEN}&q={QUERY_ID}&v=3 → ReferenceCode
           Wait 20-25 seconds
           GET GetStatement?t={TOKEN}&q={REF}&v=3 → XML
Rate limit: 1 req/sec, 10 req/min per token; error code 1018 on exceed
```

The query contains **12 sections** across **4 accounts**. Verified empirically via test pull: 1.16 MB XML, ~444 trades YTD. All 12 sections present. The 4 accounts appear as 4 separate `<FlexStatement>` elements inside `<FlexStatements count="4">`.

### XML section tags (for parser)

| Section (UI label) | XML container tag | Row element | Element type |
|---|---|---|---|
| Account Information | `AccountInformation` | — | attribute-only element (no children) |
| Trades | `Trades` | `Trade` | container with children |
| Statement of Funds | `StmtFunds` | `StatementOfFundsLine` | container with children |
| Open Positions | `OpenPositions` | `OpenPosition` | container with children |
| Corporate Actions | `CorporateActions` | `CorporateAction` | container (may be empty) |
| Option Exercises/Assignments/Expirations | `OptionEAE` | `OptionEAE` | container with children |
| Net Asset Value in Base | `EquitySummaryInBase` | `EquitySummaryByReportDateInBase` | container with children |
| Change in NAV | `ChangeInNAV` | — | attribute-only element |
| Realized and Unrealized Performance Summary | `FIFOPerformanceSummaryInBase` | `FIFOPerformanceSummaryUnderlying` | container — **note the name mismatch** |
| Mark-to-Market Performance Summary | `MTMPerformanceSummaryInBase` | `MTMPerformanceSummaryUnderlying` | container with children |
| Open Dividend Accruals | `OpenDividendAccruals` | `OpenDividendAccrual` | container (may be empty) |
| Transfers | `Transfers` | `Transfer` | container (may be empty) |

**Critical gotchas:**
- `AccountInformation` and `ChangeInNAV` are **self-closing elements with data as attributes**, not containers. `ET.find("ChangeInNAV")[0]` would raise — use `.attrib` instead.
- The UI label "Realized and Unrealized Performance Summary in Base" maps to XML tag `FIFOPerformanceSummaryInBase`. Do not search for `RealizedUnrealized*`.
- `count="4"` in the outer `<FlexStatements>` element means 4 accounts. Iterate with `root.findall("FlexStatements/FlexStatement")` and process each independently.

### Field name mapping (UI label → XML attribute)

| UI label | XML attribute |
|---|---|
| Account ID | `accountId` |
| Account Alias | `acctAlias` *(NOT `accountAlias`)* |
| Model | `model` |
| Currency | `currency` |
| Asset Class | `assetCategory` *(NOT `assetClass`)* |
| Sub Category | `subCategory` |
| Symbol | `symbol` |
| Description | `description` |
| Conid | `conid` |
| Underlying Conid | `underlyingConid` |
| Underlying Symbol | `underlyingSymbol` |
| Multiplier | `multiplier` |
| Strike | `strike` |
| Expiry | `expiry` |
| Put/Call | `putCall` |
| Date/Time | `dateTime` (format: `YYYYMMDD;HHMMSS`) |
| Trade Date | `tradeDate` (format: `YYYYMMDD`) |
| Transaction Type | `transactionType` |
| Buy/Sell | `buySell` |
| Open/Close Indicator | `openCloseIndicator` *(values: `O`, `C`, or empty)* |
| Quantity | `quantity` |
| TradePrice | `tradePrice` |
| Proceeds | `proceeds` |
| IB Commission | `ibCommission` |
| Net Cash | `netCash` |
| Cost Basis | `cost` *(just "cost" on Trade rows)* |
| Realized P/L | `fifoPnlRealized` |
| MTM P/L | `mtmPnl` *(lowercase l)* |
| Notes/Codes | `notes` *(singular)* |
| IB Order ID | `ibOrderID` |
| IB Execution ID | `ibExecID` *(abbreviated)* |
| Transaction ID | `transactionID` |
| Trade ID | `tradeID` |
| Orig Trade ID | `origTradeID` |
| Related Transaction ID | `relatedTransactionID` |
| Cost Basis Price | `costBasisPrice` *(on OpenPosition)* |
| Cost Basis Money | `costBasisMoney` *(on OpenPosition)* |
| Position | `position` *(on OpenPosition — signed)* |
| Originating Order ID | `originatingOrderID` |
| Originating Transaction ID | `originatingTransactionID` |

**The `notes` code vocabulary (empirically derived from real data):**

On `transactionType="BookTrade"` (position-affecting system-generated events):
- `A` = Assignment (stock leg for assigned puts, or stock sell for assigned calls; also the option close leg)
- `Ep` = Expired worthless (option close leg only)
- `Ex` = Exercise (not yet seen in data — handle as a placeholder, log if encountered)

On `transactionType="ExchTrade"` (regular market executions):
- `P` = Partial execution (informational only — multiple fills of a single order share an `ibOrderID` and carry this flag)
- `L`, `O`, etc. = other informational flags — **not position-affecting, do not fail closed**

### The `netCashInBase` gotcha

Yash's spec originally included the field `Net Cash in Base` for the Trades section. The current query output contains only `netCash`, not `netCashInBase`. For Yash's current book (all 444 trades verified `currency=USD`), they're numerically identical. **The Walker must assert `currency == 'USD'` on every trade event and fail closed on any non-USD row** until the query can be updated. This is a deliberate safety net against future multi-currency trading.

---

## 4. THE THREE BUCKETS — DETAILED SPEC

### Bucket 1: Real-time API state (zero persistence)

Fetched on demand via `ib_async`. Never written to the database. Used by commands that need "what's true right now":

| Data | TWS API call |
|---|---|
| NLV, ExcessLiquidity, Cushion, MaintMarginReq, BuyingPower | `accountSummary()` |
| Live positions, mark price, market value, unrealized PnL | `portfolio()` |
| Day PnL per account | `reqPnL()` / `cancelPnL()` |
| Day PnL per position | `reqPnLSingle()` |
| Live quotes (bid/ask/last) | `reqMktData()` |
| Greeks, IV | derived from market data ticks |
| Option chain | `reqSecDefOptParams()` + `reqContractDetails()` |
| Working orders | `openOrders()` + order events |
| Fill events | `execDetailsEvent` + `commissionReportEvent` |

Rule: if a query needs "what's happening right now", use this bucket.

### Bucket 2: Master Log mirror (12 tables)

Populated by `flex_sync.py` only. Schemas below. Primary keys chosen to match IBKR's own IDs so UPSERT is trivial.

```sql
-- Bucket 2: Master Log Mirror
-- Only flex_sync.py writes to these tables.
-- Read-only for every other module.

CREATE TABLE master_log_account_info (
    account_id          TEXT PRIMARY KEY,
    acct_alias          TEXT,
    model               TEXT,
    last_synced_at      TEXT NOT NULL
);

CREATE TABLE master_log_trades (
    transaction_id      TEXT PRIMARY KEY,  -- IBKR's transactionID
    account_id          TEXT NOT NULL,
    acct_alias          TEXT,
    model               TEXT,
    currency            TEXT NOT NULL,
    asset_category      TEXT NOT NULL,  -- STK, OPT
    symbol              TEXT NOT NULL,
    description         TEXT,
    conid               INTEGER NOT NULL,
    underlying_conid    INTEGER,
    underlying_symbol   TEXT,
    multiplier          REAL,
    strike              REAL,
    expiry              TEXT,
    put_call            TEXT,          -- 'P', 'C', or NULL
    trade_id            TEXT,
    ib_order_id         INTEGER,
    ib_exec_id          TEXT,
    related_transaction_id TEXT,
    orig_trade_id       TEXT,
    date_time           TEXT NOT NULL, -- YYYYMMDD;HHMMSS
    trade_date          TEXT NOT NULL, -- YYYYMMDD
    report_date         TEXT,
    order_time          TEXT,
    open_date_time      TEXT,
    transaction_type    TEXT NOT NULL, -- ExchTrade, BookTrade
    exchange            TEXT,
    buy_sell            TEXT NOT NULL, -- BUY, SELL
    open_close          TEXT,          -- 'O', 'C', or empty
    order_type          TEXT,
    notes               TEXT,          -- '', 'A', 'Ep', 'Ex', 'P', 'L', 'O', etc.
    quantity            REAL NOT NULL, -- signed
    trade_price         REAL,
    proceeds            REAL,
    ib_commission       REAL,
    net_cash            REAL,          -- in 'currency'; for Yash = USD always
    cost                REAL,          -- "Cost Basis" column on trade rows
    fifo_pnl_realized   REAL,
    mtm_pnl             REAL,
    last_synced_at      TEXT NOT NULL
);
CREATE INDEX idx_mlt_account_ticker_date ON master_log_trades(account_id, underlying_symbol, trade_date);
CREATE INDEX idx_mlt_ib_order_id         ON master_log_trades(ib_order_id);
CREATE INDEX idx_mlt_ib_exec_id          ON master_log_trades(ib_exec_id);
CREATE INDEX idx_mlt_conid_date          ON master_log_trades(conid, date_time);

CREATE TABLE master_log_statement_of_funds (
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
    activity_code       TEXT NOT NULL,  -- DIV, WITHHOLD, FEE, TRADE, etc.
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
CREATE INDEX idx_mlsof_account_date ON master_log_statement_of_funds(account_id, date);
CREATE INDEX idx_mlsof_activity     ON master_log_statement_of_funds(activity_code);

CREATE TABLE master_log_open_positions (
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
    position            REAL NOT NULL,   -- signed
    mark_price          REAL,
    position_value      REAL,
    open_price          REAL,
    cost_basis_price    REAL,            -- IRS-adjusted (wash sales applied)
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

CREATE TABLE master_log_corp_actions (
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

CREATE TABLE master_log_option_eae (
    trade_id            TEXT PRIMARY KEY,  -- IBKR's tradeID on the OptionEAE row
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
    transaction_type    TEXT,   -- Assignment, Expiration, Exercise, Buy, Sell
    quantity            REAL,
    trade_price         REAL,
    close_price         REAL,
    proceeds            REAL,
    comm_tax            REAL,   -- "commisionsAndTax" in XML (note IBKR's typo)
    basis               REAL,   -- "costBasis" in XML
    realized_pnl        REAL,
    mtm_pnl             REAL,
    last_synced_at      TEXT NOT NULL
);

CREATE TABLE master_log_nav (
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
    dividend_accruals   REAL,
    interest_accruals   REAL,
    margin_financing_charge_accruals REAL,
    broker_interest_accruals_component REAL,
    broker_fees_accruals_component REAL,
    crypto              REAL,
    total               REAL,
    total_long          REAL,
    total_short         REAL,
    last_synced_at      TEXT NOT NULL,
    PRIMARY KEY (report_date, account_id)
);

CREATE TABLE master_log_change_in_nav (
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

CREATE TABLE master_log_realized_unrealized_perf (
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

CREATE TABLE master_log_mtm_perf (
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

CREATE TABLE master_log_div_accruals (
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

CREATE TABLE master_log_transfers (
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

-- Sync audit trail
CREATE TABLE master_log_sync (
    sync_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    flex_query_id       TEXT NOT NULL,
    from_date           TEXT,
    to_date             TEXT,
    reference_code      TEXT,     -- from SendRequest response
    sections_processed  INTEGER,
    rows_received       INTEGER,
    rows_inserted       INTEGER,
    rows_updated        INTEGER,
    status              TEXT NOT NULL,  -- 'running', 'success', 'error'
    error_message       TEXT
);
```

### Bucket 3: Operational state (bot's own bookkeeping)

New/modified tables to add or create. All others listed under "Kept as-is" below do not get touched.

```sql
-- Bucket 3: Operational State (bot's bookkeeping)

-- NEW: inception carry-in for pre-IBKR and ACATS transfer positions.
-- Populated manually from a version-controlled CSV in /data/inception_carryin.csv.
-- Covers BOTH stock and open short option carry-ins.
CREATE TABLE inception_carryin (
    household_id        TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    asset_class         TEXT NOT NULL,  -- 'STK' or 'OPT'
    symbol              TEXT NOT NULL,
    conid               INTEGER,
    right               TEXT,           -- 'P'/'C' for options, NULL for stock
    strike              REAL,
    expiry              TEXT,           -- YYYYMMDD for options, NULL for stock
    quantity            REAL NOT NULL,  -- signed; negative for short options
    basis_price         REAL,           -- strike-minus-premium for assigned stock; opening price per share for options
    as_of_date          TEXT NOT NULL,  -- the date we pretend the position "opened"
    source_broker       TEXT,           -- 'IBKR', 'FIDELITY', 'SCHWAB', etc.
    reason              TEXT,           -- 'PRE_IBKR', 'ACATS_IN', 'MANUAL_ADJ'
    notes               TEXT,
    PRIMARY KEY (account_id, asset_class, conid)
);

-- NEW: bot's own log of every order it sent to IBKR.
-- This is NOT a rename of executed_orders; it's a new table with audit fields
-- designed to join cleanly to master_log_trades via ib_exec_id.
CREATE TABLE bot_order_log (
    bot_order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref           TEXT,           -- bot-generated reference passed to ib.placeOrder
    ib_order_id         INTEGER,        -- IBKR-assigned, populated on submit ack
    account_id          TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    asset_class         TEXT NOT NULL,  -- 'STK' or 'OPT'
    right               TEXT,
    strike              REAL,
    expiry              TEXT,
    action              TEXT NOT NULL,  -- 'BUY' or 'SELL'
    quantity            INTEGER NOT NULL,
    order_type          TEXT,           -- 'LMT', 'MKT', etc.
    limit_price         REAL,
    tif                 TEXT,           -- 'DAY', 'GTC'
    placed_at           TEXT NOT NULL,  -- ISO timestamp
    placed_by           TEXT,           -- '/cc', '/approve', 'dynamic_exit', etc.
    status              TEXT NOT NULL,  -- 'SUBMITTED', 'FILLED', 'CANCELLED', 'REJECTED', 'REPLACED'
    fill_ib_exec_id     TEXT,           -- set when matching commissionReport arrives
    fill_price          REAL,
    fill_quantity       INTEGER,
    fill_commission     REAL,
    fill_time           TEXT,
    notes               TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_bot_order_ib_order_id ON bot_order_log(ib_order_id);
CREATE INDEX idx_bot_order_ib_exec_id  ON bot_order_log(fill_ib_exec_id);
CREATE INDEX idx_bot_order_status      ON bot_order_log(status);

-- RENAME: cc_cycle_log → cc_decision_log
-- Schema extends to capture "what the bot believed when it made the decision"
-- so post-hoc audits can distinguish bot error from strategy outcome.
CREATE TABLE cc_decision_log (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                      TEXT NOT NULL,
    account_id                  TEXT,
    mode                        TEXT,     -- 'DEFENSIVE', 'HARVEST', etc.
    strike                      REAL,
    expiry                      TEXT,
    bid                         REAL,
    annualized                  REAL,
    otm_pct                     REAL,
    dte                         INTEGER,
    walk_away_pnl               REAL,
    spot                        REAL,
    bot_believed_adjusted_basis REAL,
    basis_truth_level           TEXT,     -- 'SETTLED' or 'INTRADAY'
    as_of_report_date           TEXT,     -- the master_log_sync date used
    overlay_applied             BOOLEAN,  -- did the intraday overlay modify basis?
    master_log_sync_id          INTEGER,  -- FK-ish to master_log_sync.sync_id
    flag                        TEXT,     -- 'NORMAL', 'LOW_YIELD', 'NO_VIABLE_STRIKE', 'SKIPPED', 'HARVEST_OK'
    created_at                  TEXT NOT NULL
);
CREATE INDEX idx_cc_decision_ticker_created ON cc_decision_log(ticker, created_at);
```

### Kept as-is (no schema changes, only reads may be repointed)

| Table | Why it stays |
|---|---|
| `pending_orders` | Bot state: orders queued for human approval |
| `live_blotter` | Bot state: orders in flight to IBKR |
| `csp_decisions` | Pre-trade decision audit with scoring |
| `ticker_universe` | Static reference data (conviction tiers, flags) |
| `conviction_overrides` | Manual overrides to conviction |
| `roll_watchlist` | Operational state: tickers being watched for rolls |
| `mode_transitions` | M1→M2 transition log; kept explicit for audit |
| `api_usage`, `api_usage_by_model` | Anthropic API cost tracking |

### Dropped at Phase 5 (only after soak completes)

| Table | Replacement |
|---|---|
| `premium_ledger` | Walker over `master_log_trades` |
| `premium_ledger_history` | Walker (`get_closed_cycles()`) |
| `cc_cycle_log` | **Renamed** to `cc_decision_log` (not dropped — rename only) |
| `fill_log` | `master_log_trades` |
| `trade_ledger` | `master_log_trades` |
| `dividend_ledger` | `master_log_statement_of_funds` filtered by `activity_code` |
| `nav_snapshots` | `master_log_nav` |
| `deposit_ledger` | `master_log_transfers` + `master_log_statement_of_funds` |
| `historical_offsets` | `inception_carryin` |
| `inception_config` | `inception_carryin` |

**That is 9 tables dropped + 1 renamed = 10 legacy tables removed.**

---

## 5. THE WALKER — PURE FUNCTION CONTRACT

The Walker is the single most important piece of code in this refactor. It is a pure function that takes a chronologically sorted list of events for ONE `(household, ticker)` pair and returns a list of cycles. Zero I/O. Fully unit-testable. No dependencies on the database, on `ib_async`, or on any other module's state.

### Input event schema

```python
@dataclass(frozen=True)
class TradeEvent:
    """Canonical event representation. Fed to the Walker from trade_repo."""
    source:              Literal['FLEX_TRADE', 'FLEX_CORP_ACTION', 'INCEPTION_CARRYIN']
    account_id:          str
    household_id:        str
    ticker:              str              # underlying symbol
    trade_date:          str              # YYYYMMDD
    date_time:           str              # YYYYMMDD;HHMMSS
    ib_order_id:         Optional[int]
    transaction_id:      Optional[str]
    asset_category:      Literal['STK', 'OPT']
    right:               Optional[Literal['P', 'C']]
    strike:              Optional[float]
    expiry:              Optional[str]    # YYYYMMDD
    buy_sell:            Literal['BUY', 'SELL']
    open_close:          Optional[Literal['O', 'C']]
    quantity:            float            # always positive; sign derived from buy_sell
    trade_price:         float
    net_cash:            float            # signed; commissions baked in
    fifo_pnl_realized:   float            # signed
    transaction_type:    Literal['ExchTrade', 'BookTrade', 'CorpAction', 'InceptionCarryin']
    notes:               str              # '', 'A', 'Ep', 'Ex', 'P', 'L', 'O', etc.
    currency:            str              # MUST be 'USD' or Walker fails closed
    raw:                 dict             # full source row for debugging
```

### Event classification (Walker's internal taxonomy)

The Walker maps each `TradeEvent` to one of these canonical event types via a pure dispatch:

```python
class EventType(Enum):
    CSP_OPEN           = 'csp_open'            # sell put to open
    CSP_CLOSE          = 'csp_close'           # buy put to close (BTC)
    CC_OPEN            = 'cc_open'             # sell call to open
    CC_CLOSE           = 'cc_close'            # buy call to close (BTC)
    LONG_OPT_OPEN      = 'long_opt_open'       # buy option to open (hedge, not wheel)
    LONG_OPT_CLOSE     = 'long_opt_close'      # sell option to close a long position
    STK_BUY_DIRECT     = 'stk_buy_direct'      # ExchTrade stock BUY (not via assignment)
    STK_SELL_DIRECT    = 'stk_sell_direct'     # ExchTrade stock SELL (not via called-away)
    ASSIGN_STK_LEG     = 'assign_stk_leg'      # BookTrade notes=A, STK side
    ASSIGN_OPT_LEG     = 'assign_opt_leg'      # BookTrade notes=A, OPT side (close of assigned option)
    EXPIRE_WORTHLESS   = 'expire_worthless'    # BookTrade notes=Ep, OPT side
    EXERCISE_STK_LEG   = 'exercise_stk_leg'    # BookTrade notes=Ex, STK side
    EXERCISE_OPT_LEG   = 'exercise_opt_leg'    # BookTrade notes=Ex, OPT side
    CORP_ACTION        = 'corp_action'         # split, merger, spin-off
    CARRYIN_STK        = 'carryin_stk'         # from inception_carryin, STK
    CARRYIN_OPT        = 'carryin_opt'         # from inception_carryin, OPT
```

**Classification rules (exhaustive):**

```python
def classify_event(ev: TradeEvent) -> EventType:
    # Safety: currency must be USD
    if ev.currency != 'USD':
        raise UnknownEventError(f"Non-USD event: {ev.currency} on {ev.ticker}")

    if ev.source == 'INCEPTION_CARRYIN':
        return EventType.CARRYIN_STK if ev.asset_category == 'STK' else EventType.CARRYIN_OPT

    if ev.source == 'FLEX_CORP_ACTION':
        return EventType.CORP_ACTION

    # From here, source == 'FLEX_TRADE'
    if ev.transaction_type == 'BookTrade':
        # Position-affecting system-generated event. notes code is mandatory.
        if ev.notes == 'A':
            return EventType.ASSIGN_STK_LEG if ev.asset_category == 'STK' else EventType.ASSIGN_OPT_LEG
        elif ev.notes == 'Ep':
            if ev.asset_category != 'OPT':
                raise UnknownEventError(f"Unexpected Ep on non-OPT: {ev}")
            return EventType.EXPIRE_WORTHLESS
        elif ev.notes == 'Ex':
            return EventType.EXERCISE_STK_LEG if ev.asset_category == 'STK' else EventType.EXERCISE_OPT_LEG
        else:
            raise UnknownEventError(f"Unmapped BookTrade notes: {ev.notes!r} on {ev.ticker}")

    elif ev.transaction_type == 'ExchTrade':
        # Regular market execution. notes are informational (P, L, O, etc.) — ignored.
        if ev.asset_category == 'STK':
            return EventType.STK_BUY_DIRECT if ev.buy_sell == 'BUY' else EventType.STK_SELL_DIRECT
        elif ev.asset_category == 'OPT':
            if ev.buy_sell == 'SELL' and ev.open_close == 'O':
                return EventType.CSP_OPEN if ev.right == 'P' else EventType.CC_OPEN
            elif ev.buy_sell == 'BUY' and ev.open_close == 'C':
                return EventType.CSP_CLOSE if ev.right == 'P' else EventType.CC_CLOSE
            elif ev.buy_sell == 'BUY' and ev.open_close == 'O':
                return EventType.LONG_OPT_OPEN
            elif ev.buy_sell == 'SELL' and ev.open_close == 'C':
                return EventType.LONG_OPT_CLOSE
            else:
                raise UnknownEventError(
                    f"Unclassifiable ExchTrade: {ev.buy_sell}/{ev.open_close} on {ev.ticker}"
                )
        else:
            raise UnknownEventError(f"Unknown asset_category: {ev.asset_category}")

    else:
        raise UnknownEventError(f"Unknown transaction_type: {ev.transaction_type}")
```

### Cycle data model

```python
@dataclass
class Cycle:
    household_id:         str
    ticker:               str
    cycle_seq:            int              # 1, 2, 3, ... within (household, ticker)
    status:               Literal['ACTIVE', 'CLOSED']
    opened_at:            str              # trade_date of first event
    closed_at:            Optional[str]    # trade_date of closure (None if ACTIVE)

    # Running state
    shares_held:          float            # current shares (across all assignment lots)
    open_short_options:   int              # count of currently open short option contracts
    paper_basis:          Optional[float]  # weighted average cost of shares held
    premium_total:        float            # sum of net_cash from OPT events only
    stock_cash_flow:      float            # sum of net_cash from STK events only
    realized_pnl:         float            # sum of fifo_pnl_realized across all events

    # Event history
    events:               list[TradeEvent]
    event_types:          list[EventType]

    @property
    def adjusted_basis(self) -> Optional[float]:
        """Strategy basis: paper basis reduced by total OPT premium per share.

        This is the value used for wheel decisions (CC strike selection, walk-away
        analysis, etc.). It intentionally differs from IBKR's costBasisPrice,
        which applies IRS wash-sale and assigned-put-only rules.
        """
        if self.shares_held <= 0 or self.paper_basis is None:
            return None
        return self.paper_basis - (self.premium_total / self.shares_held)
```

### The walk_cycles function contract

```python
def walk_cycles(
    events: list[TradeEvent],
    excluded_tickers: set[str] = frozenset({'SPX', 'VIX', 'NDX', 'RUT', 'XSP'}),
) -> list[Cycle]:
    """
    Derive wheel cycles from a chronologically sorted event stream for ONE
    (household, ticker) pair.

    Preconditions:
    - All events share the same household_id and ticker.
    - Events are sorted by (trade_date, date_time, canonical_sort_order).
    - Canonical sort within same timestamp: option close legs before stock legs
      before option open legs. This ensures deterministic state transitions on
      same-second assignment clusters.
    - ticker not in excluded_tickers (caller filters; function does not).

    Postconditions:
    - Returns a list of Cycle objects in chronological order.
    - Active cycles appear last; at most one cycle has status='ACTIVE' per call.
    - Every event in the input is assigned to exactly one cycle OR classified as
      "orphan" (see below).

    Cycle-origination rule (the only rule that defines what a cycle is):
    - A cycle opens ONLY on a CSP_OPEN event when no ACTIVE cycle exists for
      this (household, ticker).
    - Direct stock buys (STK_BUY_DIRECT via ExchTrade) do NOT open a cycle.
    - A CARRYIN_OPT event of type 'short put' opens an implicit Cycle 0 even
      though the event itself is not a CSP_OPEN; the next incoming assignment
      of that carry-in option is attributed to Cycle 0.
    - Assignment stock legs that reference a carry-in short put via the paired
      assignment option leg continue the carry-in cycle.

    Cycle closure rule:
    - At END OF TRADE DATE (not per-event), evaluate: shares_held == 0 AND
      open_short_options == 0. If yes, mark cycle CLOSED with closed_at = that
      trade_date.
    - Rolls where BTC and STO happen on the same trade_date do NOT fragment the
      cycle because closure is only evaluated at EOD.
    - Rolls where BTC happens on day X and STO happens on day X+1 DO close cycle
      X and open a new cycle on X+1. This is mathematically correct: unhedged
      overnight = new cycle.

    Unknown event handling:
    - classify_event() raises UnknownEventError on any unmapped event.
    - Walker propagates the exception. Caller (trade_repo) catches it, logs it,
      sends a Telegram alert, and marks that (household, ticker) as FROZEN in
      an in-memory dict. Other tickers continue operating normally.

    Orphan events:
    - If a position-affecting event arrives for a (household, ticker) with no
      active cycle AND no matching carry-in row, the event is classified as an
      orphan. The Walker raises UnknownEventError with reason 'ORPHAN_ASSIGNMENT'
      or similar. This must be resolved by adding an inception_carryin row
      (typically via the version-controlled CSV).

    Walker does NOT compute:
    - NAV (use master_log_nav)
    - Daily P&L attribution / TWR (use master_log_change_in_nav)
    - Per-symbol tax basis (use master_log_realized_unrealized_perf)
    - Per-symbol day-over-day MTM (use master_log_mtm_perf)
    - Pending dividends (use master_log_div_accruals)
    - Any live/real-time data
    """
```

### Walker state transition rules

For each event, the Walker applies these state changes based on classified event type:

| EventType | shares_held | open_short_options | paper_basis | premium_total | stock_cash_flow |
|---|---|---|---|---|---|
| CSP_OPEN | — | +1 per contract | — | += net_cash | — |
| CSP_CLOSE | — | −1 per contract | — | += net_cash | — |
| CC_OPEN | — | +1 per contract | — | += net_cash | — |
| CC_CLOSE | — | −1 per contract | — | += net_cash | — |
| LONG_OPT_OPEN | — | — | — | += net_cash | — |
| LONG_OPT_CLOSE | — | — | — | += net_cash | — |
| STK_BUY_DIRECT | += qty | — | weighted avg update | — | += net_cash |
| STK_SELL_DIRECT | −= qty | — | unchanged (realize only) | — | += net_cash |
| ASSIGN_STK_LEG | += qty | — | weighted avg update at trade_price (=strike) | — | += net_cash |
| ASSIGN_OPT_LEG | — | −1 per contract | — | += net_cash (=0) | — |
| EXPIRE_WORTHLESS | — | −1 per contract | — | += net_cash (=0) | — |
| EXERCISE_STK_LEG | −= qty OR += qty* | — | weighted avg update | — | += net_cash |
| EXERCISE_OPT_LEG | — | −1 per contract OR += long | — | += net_cash | — |
| CORP_ACTION | special-case per type | — | adjusted per action | — | — |
| CARRYIN_STK | += qty | — | seeded from basis_price | — | — |
| CARRYIN_OPT | — | +1 if short put, +1 if short call | — | += basis_price * multiplier * qty (if reconstructing history) or 0 | — |

*Exercise of long put = sell stock; exercise of long call = buy stock.*

**Weighted average update formula** (for STK events that increase position):
```
new_paper_basis = ((old_paper_basis * old_shares) + (trade_price * delta_shares)) / new_shares
```

For STK events that DECREASE position (sells, called-away), paper_basis is NOT updated — the remaining shares retain their weighted average. Realized P&L is captured separately via `fifo_pnl_realized`.

**Premium total is OPT-only.** This is an explicit design choice validated by the UBER and META walkthroughs. Stock cash flows are tracked separately as `stock_cash_flow` and do NOT contribute to `adjusted_basis`.

### Test scenarios (derived from real walkthrough data)

Implement these as unit tests in `tests/test_walker.py`. Expected values come from actual IBKR data pulled 2026-04-06.

**Test 1: Premium-only cycle (UBER U22076329, events 1-5)**
- Inputs: 3 CSP opens, 3 BTC closes, all within 4 days, no stock ever acquired
- Expected: 1 cycle, status=CLOSED, duration=4 days, shares_held=0, premium_total≈$125, realized_pnl≈$125

**Test 2: Clean expiration cycle (UBER U22076329, events 6-7)**
- Inputs: 1 CSP open, 1 BookTrade-Ep close at expiry
- Expected: 1 cycle, status=CLOSED, duration=5 days, shares_held=0, premium_total≈$54, realized_pnl≈$54

**Test 3: Deep multi-assignment cycle (UBER U22076329, events 8-24)**
- Inputs: 4 CSPs opened, 3 assigned (for 300 shares total at weighted avg $74.67), 1 expired, then multiple CC attempts
- Expected: 1 ACTIVE cycle, shares_held=300, open_short_options=0, paper_basis≈$74.67, premium_total≈$406, adjusted_basis≈$73.32
- Cross-check: must match IBKR's `master_log_open_positions` position=300

**Test 4: Complete called-away cycle with carry-in (META U22388499, events 1-9)**
- Inputs: 1 carry-in row for short 657.5P 260102, then assignment stock leg at $657.5, then 3 CC cycles, final assignment called-away
- Expected: 1 cycle, status=CLOSED, duration=28 days, shares_held=0 (called away), premium_total≈$2,098 (YTD portion), realized_pnl≈$6,036

**Test 5: Complex cycle with long-put hedges (ADBE U21971297, events 27-34)**
- Inputs: 8 events in 3 minutes — 2 long puts opened, 2 short puts opened, all 4 closed
- Expected: state unchanged before/after (net zero on shares and open_short_options), premium_total net ≈ −$21

**Test 6: Rolls with different ibOrderIDs (ADBE events 17-18)**
- Inputs: Event 17 BTC of 312.5P 260116 at 13:40:55 (ibOrderID=4782044916), Event 18 STO of 312.5P 260123 at same second (ibOrderID=4782046202)
- Expected: both events in same cycle, no cycle fragmentation, EOD closure rule NOT triggered

**Test 7: Same-timestamp EOD assignment cluster (UBER events 10-12 on 2026-01-30)**
- Inputs: 1 STK BUY, 1 BookTrade-Ep on 4× 78P, 1 BookTrade-A on 83P, all at `20260130;162000`
- Expected: canonical sort applies option close legs before stock legs before option open legs; EOD state: shares_held=100, open_short_options=0 (5→1 via Ep, then 1→0 via A)

**Test 8: Strategy basis vs IBKR tax basis divergence (UBER)**
- Inputs: Cycle 3 full event history
- Expected: Walker strategy_basis ≈ $73.32, IBKR costBasisPrice ≈ $73.99, delta = $0.67/share
- Assertion: `abs(walker.adjusted_basis - ibkr.cost_basis_price) < 10.0` (delta is explained by IRS premium-reduces-basis on assigned puts only)

**Test 9: Unknown BookTrade notes code fails closed**
- Inputs: synthetic BookTrade with `notes="X"` (not A, Ep, or Ex)
- Expected: UnknownEventError raised with clear message including the unknown code

**Test 10: Non-USD event fails closed**
- Inputs: synthetic event with `currency="CAD"`
- Expected: UnknownEventError raised with clear message

### Canonical event sort order

Within a single `trade_date`, events must be sorted as:

```python
def canonical_sort_key(ev: TradeEvent) -> tuple:
    # Primary: date_time (YYYYMMDD;HHMMSS)
    # Secondary: stable ordering of same-second events
    #   - Option CLOSE legs first (A, Ep, Ex, regular C)
    #   - Stock legs second
    #   - Option OPEN legs last
    if ev.transaction_type == 'BookTrade':
        if ev.asset_category == 'OPT':
            leg_priority = 0  # option close legs first
        else:
            leg_priority = 1  # stock legs
    else:
        if ev.asset_category == 'OPT' and ev.open_close == 'C':
            leg_priority = 0
        elif ev.asset_category == 'STK':
            leg_priority = 1
        else:
            leg_priority = 2
    return (ev.date_time, leg_priority, ev.ib_order_id or 0, ev.transaction_id or '')
```

---

## 6. flex_sync.py — THE MIRROR WRITER

Location: `agt_equities/flex_sync.py` (create a new `agt_equities` package if one doesn't exist, otherwise put it next to `telegram_bot.py`).

### Responsibilities

1. Pull Flex Web Service XML from IBKR using the stored token + query ID.
2. Parse XML into structured rows per section.
3. UPSERT rows into `master_log_*` tables keyed on primary keys.
4. Write an audit row to `master_log_sync` for every run.
5. Alert via Telegram on any sync failure or sync-time parity violation.

### Configuration

```python
# Constants (can be moved to a config file later)
FLEX_TOKEN = "${AGT_FLEX_TOKEN}"  # from env var FLEX_TOKEN ideally
FLEX_QUERY_ID = "1461095"
FLEX_ENDPOINT_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_POLL_DELAY_SECONDS = 25  # between SendRequest and GetStatement
FLEX_MAX_POLL_RETRIES = 6
FLEX_LOOKBACK_DAYS = 30  # for incremental syncs
```

### Sync modes

```python
class SyncMode(Enum):
    INCEPTION = 'inception'    # full pull, use for initial load
    INCREMENTAL = 'incremental' # 30-day trailing window; for daily syncs
    ONESHOT = 'oneshot'        # user-initiated manual pull
```

### Main flow

```python
def run_sync(mode: SyncMode) -> SyncResult:
    """
    1. Write a 'running' row to master_log_sync.
    2. Determine date range:
       - INCEPTION: period=YearToDate in the query; for the initial load Yash
         will need to manually edit the query to period=InceptionToDate or
         MonthToDate or a specific range. Default to what the query says.
       - INCREMENTAL: override with fromDate=today-30d, toDate=today
       - ONESHOT: use query's own period
    3. SendRequest → get reference code.
    4. Poll GetStatement with backoff until ready (max 6 retries, 25s intervals).
    5. Parse XML with ElementTree.
    6. For each FlexStatement (one per account), for each section:
       - Parse rows with field mapping.
       - UPSERT into corresponding master_log_* table.
       - Track inserted vs updated row counts.
    7. Run sync-time parity invariant:
       - For every OptionEAE row, verify a matching master_log_trades row exists
         by (account_id, conid, trade_date, close-leg semantics).
       - If any OptionEAE row is unmatched, write an alert row to a new
         sync_anomalies table and send Telegram alert.
    8. Write 'success' or 'error' final row to master_log_sync.
    9. Return SyncResult with row counts and any anomalies.
    """
```

### UPSERT pattern

```python
def upsert_trades(rows: list[dict], sync_id: int) -> tuple[int, int]:
    """UPSERT master_log_trades. Returns (inserted, updated) counts."""
    inserted = updated = 0
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        for row in rows:
            cursor = conn.execute("""
                INSERT INTO master_log_trades (transaction_id, ...)
                VALUES (:transaction_id, ...)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    quantity = excluded.quantity,
                    trade_price = excluded.trade_price,
                    net_cash = excluded.net_cash,
                    ...
                    last_synced_at = excluded.last_synced_at
                WHERE
                    master_log_trades.quantity != excluded.quantity
                    OR master_log_trades.trade_price != excluded.trade_price
                    OR master_log_trades.net_cash != excluded.net_cash
                    -- etc: only update when something actually changed
            """, {**row, 'last_synced_at': now})
            if cursor.rowcount == 1:
                if cursor.lastrowid:
                    inserted += 1
                else:
                    updated += 1
        conn.commit()
    return inserted, updated
```

### Parity invariant check

```python
def verify_option_eae_parity(sync_id: int) -> list[dict]:
    """
    For every row in master_log_option_eae, verify a matching BookTrade exists
    in master_log_trades. Returns list of unmatched rows.

    Match criteria: same account_id, same conid, same trade_date (YYYYMMDD
    portion of date_time), and transaction_type='BookTrade'.

    This is a safety net for cash-settled index options or exotic events that
    might land in OptionEAE but not Trades. Empirically verified to match 58/58
    on Yash's equity wheel data, but we want to know immediately if that invariant
    ever breaks.
    """
    with get_db() as conn:
        unmatched = conn.execute("""
            SELECT eae.*
            FROM master_log_option_eae eae
            WHERE NOT EXISTS (
                SELECT 1 FROM master_log_trades t
                WHERE t.account_id = eae.account_id
                  AND t.conid = eae.conid
                  AND substr(t.date_time, 1, 8) = eae.date
                  AND t.transaction_type = 'BookTrade'
            )
        """).fetchall()
    return [dict(r) for r in unmatched]
```

### Telegram alerts

On any of: sync failure, parity violation, UnknownEventError during Walker runs, Walker freeze of a ticker — send a Telegram alert to the operator via the existing bot token. Use a new async helper `alert_operator(message: str, severity: Literal['INFO','WARN','ERROR'])` that the bot's existing message-sending code can provide.

---

## 7. trade_repo.py — THE READ INTERFACE

Location: `agt_equities/trade_repo.py`.

Sole responsibility: materialize `TradeEvent` streams and hand them to the Walker, then cache cycle results. No other module reads from `master_log_*` directly except `flex_sync.py` (which writes).

### Key functions

```python
def get_active_cycles(
    household: str | None = None,
    ticker: str | None = None,
    as_of_report_date: str | None = None,
) -> list[Cycle]:
    """
    Returns the ACTIVE cycle (if any) for each (household, ticker) matching
    the filter.

    If household=None, returns all households. If ticker=None, returns all tickers.
    If as_of_report_date=None, uses the latest master_log_sync.finished_at.

    This is the settled-state read path. No intraday overlay.
    """

def get_closed_cycles(
    household: str,
    ticker: str,
    limit: int = 20,
) -> list[Cycle]:
    """Returns closed cycles for one (household, ticker) in reverse chron order."""

def get_cycles_for_ticker(household: str, ticker: str) -> list[Cycle]:
    """All cycles (active + closed) for one ticker."""

def get_cycles_with_intraday_overlay(
    household: str,
    ticker: str,
) -> list[Cycle]:
    """
    Settled Walker cycles + today's bot_order_log overlay + today's live
    execDetailsEvent/commissionReportEvent stream.

    Used ONLY by order-driving commands: /cc, /exit, /dynamic_exit, /rollcheck.
    All other consumers use get_active_cycles() or get_closed_cycles().

    The overlay is applied by:
    1. Start with Walker output for as_of_report_date = latest settled sync
    2. For the active cycle, append synthetic TradeEvents from today's
       bot_order_log rows with status='FILLED' and fill_time > last_sync
    3. Append synthetic TradeEvents from today's live fill events captured
       in an in-memory buffer (see execution_bridge.py below)
    4. Re-run walk_cycles() on the extended event stream
    5. Mark returned cycle with as_of='INTRADAY'

    The overlay shares the SAME event classification and reducer as the
    settled walker — only the input stream differs.
    """

def verify_live_match(
    household: str,
    ticker: str,
    overlay_cycle: Cycle,
    live_position: float,
    live_short_options: int,
) -> bool:
    """
    Guardrail for order-driving commands.

    Returns True iff:
        overlay_cycle.shares_held == live_position
        AND overlay_cycle.open_short_options == live_short_options

    Compares ONLY counts, not basis or premium (which would be noisy false
    positives during live session).

    On False: caller must refuse the command and show a clear Telegram error:
        "Manual intervention or external fill detected for {ticker}.
         Bot operations paused for this ticker until next Flex sync."
    """
```

### Internal caching

Cache `get_active_cycles()` results by `(as_of_report_date)` key. Invalidate when `master_log_sync` writes a new success row. Overlay path is not cached (computed fresh each call).

---

## 8. execution_bridge.py — LIVE FILL CAPTURE FOR OVERLAY

Location: `agt_equities/execution_bridge.py`.

A thin module that subscribes to `ib_async` execution events and:
1. Writes fill metadata to `bot_order_log` (matching by `ib_order_id`).
2. Buffers `TradeEvent` representations of intraday fills in memory for the overlay path.

```python
class ExecutionBridge:
    def __init__(self, ib: IB):
        self.ib = ib
        self.intraday_events: list[TradeEvent] = []
        ib.execDetailsEvent += self._on_exec
        ib.commissionReportEvent += self._on_commission

    def _on_exec(self, trade: Trade, fill: Fill) -> None:
        """
        Convert ib_async Fill into a synthetic TradeEvent and append to
        intraday_events. Also UPDATE bot_order_log matching by ib_order_id.
        """

    def _on_commission(self, trade: Trade, fill: Fill, report: CommissionReport) -> None:
        """
        Update the corresponding bot_order_log row with commission and final
        net_cash. Update the corresponding intraday_events entry if already added.
        """

    def get_intraday_events(
        self, household: str, ticker: str, since: str
    ) -> list[TradeEvent]:
        """Filter buffered events by (household, ticker) and time."""

    def clear_intraday(self, before_date: str) -> None:
        """Clear buffer entries older than before_date (called after nightly sync)."""
```

**Important**: `ExecutionBridge` replaces the current fill handler logic that writes to `fill_log` / `premium_ledger` / `cc_cycle_log`. See the migration section for the phasing.

---

## 9. MIGRATION PHASES

### Phase 0 — Scaffolding (non-destructive)

Deliverables:
- [ ] Create `agt_equities/` package (or equivalent) with `flex_sync.py`, `trade_repo.py`, `execution_bridge.py`, `walker.py`, `__init__.py`
- [ ] Add all 12 `master_log_*` tables + `master_log_sync` to `init_db()`. These are ADDITIVE — no existing tables modified.
- [ ] Add `inception_carryin`, `bot_order_log`, `cc_decision_log` to `init_db()`. The `cc_cycle_log` table stays in parallel (rename happens at Phase 4).
- [ ] Create `/data/inception_carryin.csv` placeholder with schema comment header. Commit to git.
- [ ] Create `/tests/test_walker.py` with the 10 test scenarios from section 5. All tests initially fail (walker not implemented).

**Gate to Phase 1**: Yash reviews the schema migration diff. Approves or requests changes.

### Phase 1 — Walker implementation + first sync

Deliverables:
- [ ] Implement `walker.py` with `walk_cycles()`, `classify_event()`, `canonical_sort_key()`, and the Cycle dataclass
- [ ] All 10 walker unit tests pass
- [ ] Implement `flex_sync.py` with INCEPTION mode
- [ ] Run first sync: pull inception-to-date Flex XML (Yash will edit the query to `period=InceptionToDate` or set an explicit `fromDate=20250901`), populate all `master_log_*` tables
- [ ] Run sync-time parity check on OptionEAE. Report any unmatched rows.
- [ ] Implement `trade_repo.get_active_cycles()` (settled path only, no overlay yet)
- [ ] Generate the **Phase 1 Reconciliation Report** (see section 10 below)

**Gate to Phase 2**: Yash reviews the reconciliation report. Every active cycle must match `master_log_open_positions` on shares and short contract counts. Every mismatch gets either an `inception_carryin` CSV row or an explanation. Yash signs off.

### Phase 2 — Read migration (dashboard + reports)

Deliverables:
- [ ] Repoint `/dashboard` to read from `trade_repo.get_active_cycles()` + `master_log_change_in_nav` for performance attribution + `master_log_nav` for NLV history
- [ ] Repoint `/cycles` to read `trade_repo.get_cycles_for_ticker()`
- [ ] Repoint `/fills` to read `master_log_trades` directly
- [ ] Repoint `/ledger` to read `trade_repo.get_active_cycles()`
- [ ] Repoint `/think` and `/deep` to pass Walker cycle context into LLM prompts (replacing premium_ledger-derived context)
- [ ] Add "Settled through {date}" footer to `/dashboard` showing the latest `master_log_sync.finished_at`
- [ ] Legacy tables (premium_ledger, fill_log, etc.) still being written by the bot — DO NOT DISABLE WRITES YET
- [ ] Side-by-side validation: generate a small harness that runs both old and new read paths for every /dashboard query and diffs the output. Log discrepancies.

**Gate to Phase 3**: Yash compares old vs new dashboard output for 3-5 days. New output must match old (modulo expected T-1 lag). Yash signs off.

### Phase 3 — Decision-command migration with overlay

Deliverables:
- [ ] Implement `execution_bridge.py` — subscribes to `execDetailsEvent` and `commissionReportEvent`, writes to `bot_order_log`, buffers intraday TradeEvents
- [ ] Implement `trade_repo.get_cycles_with_intraday_overlay()` — full overlay path
- [ ] Implement `trade_repo.verify_live_match()` guardrail
- [ ] Repoint `/cc` to read `trade_repo.get_cycles_with_intraday_overlay()` for adjusted_basis; call `verify_live_match()` and refuse if mismatch
- [ ] Repoint `/exit`, `/dynamic_exit`, `/rollcheck` similarly
- [ ] Repoint `/health` to read Walker (settled) + overlay for cycle context, live TWS API for positions/NLV/margin
- [ ] Label all basis displays as "Strategy Basis" explicitly in `/dashboard` and `/health`
- [ ] Refactor `_discover_positions()` helper to use live API + overlay
- [ ] Refactor `_load_premium_ledger_snapshot()` helper — rename to `_load_walker_snapshot()` and have it call `trade_repo`
- [ ] Refactor `run_cc_ladder()` to use `trade_repo` instead of `_load_premium_ledger_snapshot`
- [ ] Refactor conversational tool surface (LLM-exposed tools) — any tool that reads `premium_ledger` gets repointed
- [ ] Legacy tables still being written by the bot — STILL DO NOT DISABLE
- [ ] Decision log: every `/cc` decision writes to `cc_decision_log` with the new audit fields

**Gate to Phase 4**: Yash uses the system for 3-5 live trading sessions with decision commands on the new path. Any bug, false guardrail, or missing field blocks progression.

### Phase 4 — Soak period with dual-write

Deliverables:
- [ ] Bot runs normally. All reads come from the new path. Legacy writes continue as a safety net.
- [ ] Nightly reconciliation job runs: compare Walker active cycles against `master_log_open_positions` and against live `ib.portfolio()` (via overlay). Email or Telegram the daily report to Yash.
- [ ] Soak exit criteria are EVENT-BASED:
  - [ ] At least 1 assignment IN (CSP assigned) observed and handled correctly
  - [ ] At least 1 assignment OUT (CC called away) observed and handled correctly
  - [ ] At least 1 worthless expiration observed and handled correctly
  - [ ] At least 1 roll (BTC + STO same day) handled correctly
  - [ ] At least 1 same-day intraday fill followed by next-day Flex sync match
- [ ] Estimated calendar time: 30-45 days (typical wheel cycle length)

**Gate to Phase 5**: Yash reviews the full set of event-based criteria, confirms all satisfied, and explicitly approves Phase 5.

### Phase 5 — Drop legacy tables (IRREVERSIBLE)

Deliverables — execute in order:
- [ ] SQL dump backup of all 10 legacy tables to `backups/legacy_tables_{timestamp}.sql`
- [ ] Audit `init_db()` in `telegram_bot.py`. Remove `CREATE TABLE IF NOT EXISTS` for: `premium_ledger`, `premium_ledger_history`, `fill_log`, `trade_ledger`, `dividend_ledger`, `nav_snapshots`, `deposit_ledger`, `historical_offsets`, `inception_config`. Rename `cc_cycle_log` → `cc_decision_log` (add an ALTER TABLE RENAME migration).
- [ ] Remove fill event handlers that write to `fill_log` / `premium_ledger` / `cc_cycle_log`. Verify `ExecutionBridge` is the only subscriber to `execDetailsEvent` and `commissionReportEvent`.
- [ ] Remove helper functions that referenced dropped tables: `_load_premium_ledger_snapshot` (after it's been renamed/repurposed), any premium_ledger upsert logic, etc.
- [ ] Grep the codebase for every reference to the dropped table names. Report findings. Remove them after Yash review.
- [ ] `DROP TABLE` for the 9 dropped tables. Restart bot. Smoke-test all 27 commands.
- [ ] Commit and tag the release.

**This is the only irreversible step. Gate on Yash's explicit "go" for each sub-step.**

---

## 10. THE PHASE 1 RECONCILIATION REPORT

This is the most important artifact before going live on the new path. Generate it as a Markdown document.

```markdown
# Phase 1 Reconciliation Report

Generated: {timestamp}
Sync ID: {master_log_sync.sync_id}
Data range: {from_date} → {to_date}

## Summary
- Total active Walker cycles: {count}
- Matched against master_log_open_positions: {matched}
- Mismatched: {mismatched}
- Tickers frozen due to UnknownEventError: {frozen}
- Tickers requiring inception_carryin: {carryin}

## Per-household summary
{for each household: count of cycles, total shares, total short contracts}

## Active cycle details
{for each active cycle:}
  ### {household} / {ticker}
  - Walker state: shares={shares}, open_short_options={osp}, paper_basis=${basis}, adjusted_basis=${adj}
  - IBKR state: position={ibkr_pos}, cost_basis_price=${ibkr_cbp}
  - Delta: shares={shr_delta}, basis=${basis_delta}
  - Status: ✓ MATCH / ❌ MISMATCH / ⚠ CARRYIN NEEDED
  - Events in cycle: {count}
  - Cycle opened: {date}
  - Last event: {date}
  {if mismatch: detailed breakdown of the mismatch}

## Mismatches requiring action
{numbered list of every mismatch with proposed fix — usually an inception_carryin row}

## Orphan events
{any events that triggered UnknownEventError with the classification reason}

## Sync-time parity violations
{from verify_option_eae_parity()}

## Recommended next steps
{auto-generated based on findings}
```

Save to `reports/phase1_reconciliation_{timestamp}.md` and print a summary to stdout.

---

## 11. PER-CONSUMER REFACTOR TABLE

For each of the 27 existing slash commands + key helper functions, this is the expected behavior after Phase 3:

| Handler | Old reads | New reads | Change |
|---|---|---|---|
| `/start` | — | — | none |
| `/status` | live_blotter, pending_orders | unchanged | none |
| `/orders` | live_blotter | unchanged | none |
| `/budget` | api_usage | unchanged | none |
| `/clear`, `/stop` | — | — | none |
| `/reconnect` | ib.connect() | unchanged | none |
| `/scan` | ticker_universe, TWS API, yfinance | unchanged | none |
| `/vrp` | yfinance, TWS API | unchanged | none |
| `/sync_universe` | TWS API, yfinance | unchanged | none |
| `/cleanup_blotter` | live_blotter | unchanged | none |
| `/approve` | live_blotter, ib.placeOrder | unchanged for order placement; stop writing to legacy tables at Phase 5 | medium |
| `/reject` | live_blotter | unchanged | none |
| `/dashboard` | premium_ledger, trade_ledger, dividend_ledger, nav_snapshots | trade_repo.get_active_cycles() + master_log_change_in_nav + master_log_nav | rewrite |
| `/think` | csp_decisions, ticker_universe, premium_ledger | csp_decisions, ticker_universe, trade_repo | small |
| `/deep` | same as /think | same | small |
| `/health` | premium_ledger, TWS API | trade_repo.get_cycles_with_intraday_overlay() for basis; TWS API for live | medium |
| `/cc` | premium_ledger, TWS API, csp_decisions | trade_repo.get_cycles_with_intraday_overlay() + verify_live_match() guardrail; TWS API unchanged | medium |
| `/mode1` | premium_ledger, TWS API | trade_repo + TWS API | small |
| `/rollcheck` | live_blotter, premium_ledger, TWS API | live positions from TWS; cycle context from trade_repo | medium |
| `/cycles TICKER` | cc_cycle_log | trade_repo.get_cycles_for_ticker() | rewrite (now shows wheel cycles, not just CC sales) |
| `/fills` | fill_log | master_log_trades directly | small |
| `/ledger` | premium_ledger | trade_repo.get_active_cycles() | small |
| `/dynamic_exit` | premium_ledger | trade_repo.get_cycles_with_intraday_overlay() | medium |
| `/exit` | premium_ledger, live_blotter, ib.placeOrder | trade_repo for basis; rest unchanged | small |
| `/override` | conviction_overrides | unchanged | none |
| `/status_orders` | live_blotter | unchanged | none |

Helper functions to also refactor:
- `_discover_positions()` (around line 7560) — rewrite to use live TWS portfolio + overlay for basis
- `_load_premium_ledger_snapshot()` (around line 1519) — rename to `_load_walker_snapshot()`, delegate to `trade_repo`
- `run_cc_ladder()` (around line 2821) — replace `_load_premium_ledger_snapshot` call with `trade_repo` call
- Conversational tool surface (around line 3227) — audit every tool that accepts ticker and reads premium data; repoint to `trade_repo`
- Fill event handlers (around line 1552, 1606, 1066) — at Phase 5, unregister the ones that write to legacy tables; `ExecutionBridge` replaces them

---

## 12. FILE LAYOUT — WHAT TO CREATE

```
C:\AGT_Telegram_Bridge\
├── telegram_bot.py           (existing; will be modified in Phases 2-5)
├── dashboard_renderer.py     (existing; will be replaced in Phase 2)
├── telegram_dashboard_integration.py (existing)
├── pxo_scanner.py            (existing; untouched)
├── test_margin_logic.py      (existing; untouched)
├── boot_desk.bat             (existing; untouched)
├── Portfolio_Risk_Rulebook_v8.md (existing; read-only reference)
├── rulebook_llm_condensed.md (existing; read-only reference)
├── ARCHITECTURE_md.txt       (existing; update at end of refactor)
├── agt_desk.db               (existing; new tables added in Phase 0)
│
├── agt_equities/             (NEW PACKAGE)
│   ├── __init__.py
│   ├── walker.py             (pure walk_cycles + classify_event + Cycle dataclass)
│   ├── flex_sync.py          (Flex Web Service client + UPSERT)
│   ├── trade_repo.py         (read interface with overlay)
│   ├── execution_bridge.py   (live fill capture, replaces legacy handlers)
│   ├── schema.py             (all CREATE TABLE + ALTER statements)
│   └── parity.py             (sync-time invariant checks)
│
├── data/                     (NEW DIR)
│   └── inception_carryin.csv (version-controlled carry-in seed)
│
├── tests/                    (NEW DIR)
│   ├── test_walker.py        (10 scenarios from section 5)
│   ├── test_flex_parser.py   (XML parsing against a fixture)
│   ├── test_trade_repo.py    (read paths against a fixture DB)
│   └── fixtures/
│       ├── master_log_sample.xml   (a real Flex pull, anonymized if needed)
│       └── expected_cycles.json    (Walker output Yash has verified)
│
├── reports/                  (NEW DIR)
│   ├── phase1_reconciliation_{timestamp}.md
│   └── daily_reconciliation_{date}.md
│
├── backups/                  (NEW DIR)
│   └── legacy_tables_{timestamp}.sql
│
└── docs/                     (NEW DIR)
    └── REFACTOR_SPEC_v3.md   (a copy of this prompt, for reference)
```

---

## 13. SUBAGENT DISPATCH STRATEGY

This refactor is highly parallelizable after Phase 0. Recommended subagent structure:

**Phase 0-1 (sequential — foundation)**:
- Agent A1: Schema migration (create `agt_equities/schema.py`, add tables to `init_db()`, write migration script)
- Agent A2: Walker implementation + unit tests (pure function, fully testable in isolation)
- Agent A3: Flex parser + `flex_sync.py` (can work in parallel with A2 against the master_log_v2.xml fixture)
- Agent A4: trade_repo settled path (depends on A2 completion)
- Orchestrator: Run Phase 1 sync, generate reconciliation report, present to Yash

**Phase 2 (parallel — read migration)**:
- Agent B1: `/dashboard` rewrite
- Agent B2: `/cycles`, `/fills`, `/ledger` rewrites
- Agent B3: `/think`, `/deep` repointing
- Agent B4: Side-by-side validation harness
- Orchestrator: Ensure all B agents share the same `trade_repo` API and don't introduce inconsistencies

**Phase 3 (mostly parallel — decision-command migration)**:
- Agent C1: `execution_bridge.py` + bot_order_log writes + intraday buffer
- Agent C2: Overlay path in trade_repo (depends on C1)
- Agent C3: `/cc` refactor + guardrail (depends on C2)
- Agent C4: `/exit`, `/dynamic_exit`, `/rollcheck` refactor (depends on C2)
- Agent C5: `/health` refactor
- Agent C6: Helper function refactors (`_discover_positions`, `run_cc_ladder`, etc.)
- Agent C7: Conversational tool surface audit + repointing
- Orchestrator: Coordinate across C agents, resolve any shared-helper conflicts

**Phase 4-5 (sequential — soak and cutover)**:
- Single agent: Daily reconciliation cron setup, then wait for soak criteria
- Single agent: Phase 5 execution with explicit Yash approval at each sub-step

---

## 14. REVIEW GATES — WHEN TO STOP AND ASK YASH

These are mandatory checkpoints. Do not proceed past any of them without Yash's explicit approval in the chat.

1. **End of Phase 0**: Schema migration diff review. Show the SQL additions and confirm.
2. **End of Phase 1**: Phase 1 Reconciliation Report review. Every mismatch must be classified.
3. **Mid-Phase 2**: After /dashboard rewrite, show side-by-side output for 3-5 recent runs.
4. **End of Phase 2**: Full side-by-side validation review.
5. **Mid-Phase 3**: After /cc refactor, simulate a CC decision on test data and show the trade_repo overlay computation.
6. **End of Phase 3**: Integration test — run bot in live mode with decision commands repointed, show Yash the first 3 decisions made on the new path.
7. **Every 7 days during Phase 4**: Daily reconciliation summary. Any anomaly pauses the soak.
8. **Start of Phase 5**: Explicit go/no-go. Yash must type "proceed with drops" literally.
9. **Each sub-step of Phase 5**: Pause after each DROP and confirm bot still works before next.

---

## 15. THINGS YOU MAY ENCOUNTER — AND HOW TO HANDLE THEM

**Encounter**: You find a module reading from `premium_ledger` that wasn't in the refactor table above.
→ STOP. Add it to the refactor table. Ask Yash whether it needs migration or deprecation. Do not silently repoint.

**Encounter**: The sync-time parity check finds an unmatched OptionEAE row.
→ STOP. Log full details of the unmatched row. Send Telegram alert. Do not proceed with Phase 1 reconciliation until Yash investigates.

**Encounter**: The Walker raises UnknownEventError on a ticker.
→ STOP for that ticker. Other tickers continue. Log the event. Ask Yash to map the new event type (may require extending `classify_event()` and a new test case).

**Encounter**: The Phase 1 reconciliation shows a stock position the Walker doesn't know about, and there's no obvious CSP in the event history.
→ This is the carry-in case. Ask Yash to add an `inception_carryin.csv` row and re-run. For the META case, the carry-in is likely an ACATS transfer from Fidelity — Yash has indicated he will run a Fidelity report to populate these.

**Encounter**: IBKR's `costBasisPrice` doesn't match the Walker's `paper_basis`.
→ This is EXPECTED. It's the strategy-basis vs tax-basis divergence documented in sections 5 and 11. Do not "fix" it. Report it in the reconciliation as informational, not as a mismatch. The mismatch assertion is on `shares_held` only.

**Encounter**: You find dead code, commented-out blocks, or obviously-broken logic in the existing codebase.
→ DO NOT DELETE. Report in a separate "Observations" section at the end of the phase. Yash will decide.

**Encounter**: A test fails that you think is wrong.
→ DO NOT change the test. The tests are derived from real portfolio data that Yash has verified. If the test is failing, the Walker is wrong.

**Encounter**: The existing `init_db()` has `CREATE TABLE IF NOT EXISTS` for tables you're supposed to drop.
→ This is expected — do NOT remove those statements until Phase 5. Otherwise a bot restart would recreate the dropped tables.

**Encounter**: You need to add a new helper function or utility not mentioned in this spec.
→ Go ahead, but document it and justify it in your phase report.

---

## 16. QUICK REFERENCE — THE FLEX PULL

For any subagent that needs to test against fresh data, here's a minimal working example:

```python
import urllib.request
import time
import xml.etree.ElementTree as ET

TOKEN = "${AGT_FLEX_TOKEN}"
QUERY_ID = "1461095"
BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"

def pull_flex() -> ET.Element:
    # Step 1: SendRequest
    req = urllib.request.Request(
        f"{BASE}/SendRequest?t={TOKEN}&q={QUERY_ID}&v=3",
        headers={"User-Agent": "AGT-Equities/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        send_body = r.read().decode()
    ref = ET.fromstring(send_body).find("ReferenceCode").text

    # Step 2: Poll GetStatement
    time.sleep(25)
    req = urllib.request.Request(
        f"{BASE}/GetStatement?t={TOKEN}&q={ref}&v=3",
        headers={"User-Agent": "AGT-Equities/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        xml_body = r.read().decode()
    return ET.fromstring(xml_body)

# Iterate accounts (3 active + 1 dormant)
root = pull_flex()
for fs in root.findall("FlexStatements/FlexStatement"):
    account_id = fs.attrib["accountId"]
    trades = fs.findall("Trades/Trade")
    # etc.
```

Rate limit: 1 req/sec, 10 req/min per token. Do not run parallel `pull_flex()` calls.

---

## 17. ONE FINAL THING

This refactor is the foundation of AGT Equities' operational reliability going forward. Get it right and the system is trivially auditable and nearly bulletproof. Get it wrong and we've spent weeks building a second ledger that drifts from IBKR in a new and more creative way.

The two most important things to preserve through the migration:
1. **Bucket 2 purity.** `master_log_*` tables are a mirror. Never a ledger. Never writable by anyone except `flex_sync.py`.
2. **Walker purity.** The Walker is a pure function of events. No side effects. No I/O. No "just this one special case". Special cases go in classification, not in state transitions.

Everything else is negotiable. These two are not.

When in doubt, stop and ask Yash.

---

---

## 18. ADDENDA (post-spec updates from Yash, April 2026)

### Update 0: Phase 5 must remove ALL references to dropped tables

The Phase 5 audit step says "remove `CREATE TABLE IF NOT EXISTS` for dropped tables." This must be extended to: **remove all references (CREATE, INSERT, SELECT, UPDATE, DELETE) to dropped tables.** Specifically, lines 608-628 of `telegram_bot.py` contain `INSERT OR IGNORE INTO inception_config` statements that run on every bot startup. When `inception_config` is dropped in Phase 5, those INSERTs must also be removed or the bot will crash. The same applies to `historical_offsets` (lines 572-606 contain both the CREATE and INSERT OR IGNORE statements).

### Update 1: inception_carryin.csv is empty at bootstrap

The CSV is intentionally empty. The October 2025 ACAT transfer from Fidelity included only long stock lots, no open short options. All transferred stocks were either sold off, absorbed into larger wheel positions with clean IBKR history, or are zero-basis restructuring artifacts (TRAW.CVR) that IBKR already tracks correctly.

The Walker is expected to derive every active cycle from `master_log_trades` alone, provided `flex_sync.py` pulls from `fromDate=2025-09-01` for the initial load.

### Update 2: flex_sync.py inception load starts from 2025-09-01

When `run_sync(mode=SyncMode.INCEPTION)` is called for the first-ever bootstrap, `flex_sync.py` should programmatically set the Flex Web Service request to cover September 1, 2025 through today. This is done by editing the Flex Query's Date Range via the Client Portal UI before calling SendRequest, OR by passing `fromDate=20250901&toDate=<today>` as URL parameters to SendRequest if the Flex Web Service supports date overrides at the HTTP level (verify in IBKR docs; the v=3 endpoint may or may not accept these). If neither path works, stop and ask Yash to edit the query's Period field in the Client Portal UI to cover the inception range, then re-run.

### Update 3: Phase 1 reconciliation expected outcome

Expected Phase 1 outcome for AGT Equities initial migration: **zero mismatches** requiring `inception_carryin` entries. Every active cycle in Walker output should trace back to a `CSP_OPEN` event in the Sept 2025 – present window. Any position that the Walker cannot trace is a **BUG** and should be investigated immediately, not patched with a carry-in row.

**Exception:** The single `TRAW.CVR` share in U22076329 is expected to appear as "ungrouped stock" — not attached to any cycle, carrying IBKR's own $0.00 cost basis. This is correct; do not create a cycle for it.

---

END OF SPEC.
