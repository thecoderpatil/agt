# Phase 2 Pre-Flight Report

Generated: 2026-04-07

## (a) Backup Verification

| Item | Value |
|------|-------|
| Source | `C:\AGT_Telegram_Bridge\agt_desk.db` |
| Backup | `C:\AGT_Telegram_Bridge\agt_desk.db.phase1_baseline_20260407` |
| Source size | 921,600 bytes |
| Backup size | 921,600 bytes |
| Size match | Yes |
| Backup method | `sqlite3.Connection.backup()` (online, WAL-safe) |

**Spot-check row counts (5 tables):**

| Table | Source | Backup | Match |
|-------|--------|--------|-------|
| pending_orders | 205 | 205 | MATCH |
| premium_ledger | 16 | 16 | MATCH |
| ticker_universe | 597 | 597 | MATCH |
| api_usage | 3 | 3 | MATCH |
| cc_cycle_log | 127 | 127 | MATCH |

## (b) Live DB Inventory

**20 tables, 2,377 total rows:**

| Table | Rows | Category |
|-------|------|----------|
| pending_orders | 205 | Operational (kept) |
| live_blotter | 2 | Operational (kept) |
| executed_orders | 3 | Operational (kept) |
| premium_ledger | 16 | **Legacy (Phase 5 drop)** |
| premium_ledger_history | 0 | **Legacy (Phase 5 drop)** |
| csp_decisions | 0 | Operational (kept) |
| ticker_universe | 597 | Operational (kept) |
| conviction_overrides | 0 | Operational (kept) |
| cc_cycle_log | 127 | **Legacy (Phase 5 rename → cc_decision_log)** |
| roll_watchlist | 0 | Operational (kept) |
| mode_transitions | 0 | Operational (kept) |
| fill_log | 7 | **Legacy (Phase 5 drop)** |
| api_usage | 3 | Operational (kept) |
| api_usage_by_model | 1 | Operational (kept) |
| trade_ledger | 1,359 | **Legacy (Phase 5 drop)** |
| dividend_ledger | 15 | **Legacy (Phase 5 drop)** |
| nav_snapshots | 4 | **Legacy (Phase 5 drop)** |
| deposit_ledger | 37 | **Legacy (Phase 5 drop)** |
| historical_offsets | 3 | **Legacy (Phase 5 drop)** |
| inception_config | 3 | **Legacy (Phase 5 drop)** |

**12 indexes** on operational/legacy tables. No master_log indexes (tables don't exist yet).

**flex_sync would write to:** 12 master_log_* tables + master_log_sync (audit). Total 13 new tables.

**Dashboard currently reads from:** premium_ledger, trade_ledger, dividend_ledger, nav_snapshots, deposit_ledger, historical_offsets, inception_config (7 legacy tables).

**Table name collisions:** None. All 16 new tables (12 master_log + sync + inception_carryin + bot_order_log + cc_decision_log) are absent from the live DB.

## (c) Schema State

**0 master_log_* tables exist in production.** No drift possible — tables will be created fresh by `register_master_log_tables()` on next `init_db()` call.

## (d) init_db() Registration

**telegram_bot.py lines 630-632:**
```python
        # ── Master Log Refactor v3: Bucket 2 + Bucket 3 new tables ──
        from agt_equities.schema import register_master_log_tables
        register_master_log_tables(conn)
```

Confirmed present. The 16 new tables will be created on next bot startup via `init_db()`.

## (e) V3 Fixture

| Item | Value |
|------|-------|
| File | `tests/fixtures/master_log_inception_v3.xml` |
| Size | 3,864,358 bytes (3.69 MB) |
| FlexStatements | 4 |
| Period | Last365CalendarDays (2025-04-07 → 2026-04-06) |
| Total trades | 1,466 |
| fxRateToBase | Present on Trades rows (value = 1, all USD) |

**Per-account trade counts:** U22076329=331, U21971297=781, U22076184=13, U22388499=341.

## (f) V3 Reconciliation

| Cross-check | Result | Notes |
|-------------|--------|-------|
| A | **48/49** | ADBE -$109.95 (accepted residual) |
| B | **14/14** | All within $0.10/share |
| C | **2/4** | U22388499 -$21.53, U22076329 +$109.95 (accepted) |
| Frozen | **0** | |
| Tests | **34/34** | |
| Parity | **438/438** | |

**No regression from v2 → v3 fixture.** Gate criteria preserved.

## Pre-Flight Status: PASS

All pre-flight checks pass. Ready for Task 2.1 (dry-run live sync) pending Yash authorization.

## Production DB: untouched (0 master_log tables, 20 existing tables unchanged)
