# Phase 2.3: Dual-Write Soak Setup

Generated: 2026-04-07

## Schedule Configuration

### flex_sync EOD job
```python
jq.run_daily(
    callback=_scheduled_flex_sync,
    time=_time(hour=17, minute=0, tzinfo=ET),  # 5:00 PM ET
    days=(1, 2, 3, 4, 5),                      # Mon-Fri
    name="flex_sync_eod",
)
```

**Cadence:** Daily at 5:00 PM ET on trading days (Mon-Fri).
This runs after market close (4:00 PM ET) with a 1-hour buffer for IBKR to settle trade data. The Flex Web Service typically has same-day trades available by 4:30-5:00 PM ET.

**What it does:**
1. Pulls live MASTER_LOG from IBKR via `flex_sync.pull_flex_xml()`
2. Parses all 12 sections
3. UPSERTs into `master_log_*` tables
4. Writes audit row to `master_log_sync`
5. Sends Telegram notification to Yash with row counts and status

**What it does NOT do:**
- Touch any legacy table (premium_ledger, trade_ledger, fill_log, etc.)
- Modify any operational table (pending_orders, live_blotter, etc.)
- Affect any Telegram command's read path (all commands still read from legacy)

### /reconcile command
```
/reconcile — Run cross-checks A/B/C against live master_log_* state
```

**Output format (Telegram message):**
```
RECONCILIATION REPORT
Last sync: #1 2026-04-07T... (success, 5404 rows)
Parity: 438/438
Cycles: 176 (14 active, 174 wheel, 2 satellite)
Frozen: 0

A (realized P&L): 49/50
  Yash_Household/ADBE: $-109.95
B (cost basis): 14/14
C (NAV recon): 2/4
  U21971297: $0.00
  U22388499: $-21.53
  U22076329: $109.95
  U22076184: $0.00
```

**Usage:** Yash runs `/reconcile` at any time to check the current state. During soak, run it daily after the 5:00 PM flex_sync completes.

## Dual-Write Architecture During Soak

```
IBKR TWS API (live fills)
    │
    ├──→ Existing fill handlers → premium_ledger, fill_log, cc_cycle_log
    │    (UNCHANGED, legacy path continues writing)
    │
    └──→ (future Phase 3: ExecutionBridge → bot_order_log)
         (NOT active during soak)

IBKR Flex Web Service (EOD batch)
    │
    └──→ flex_sync.py → master_log_* tables
         (NEW, runs daily at 5:00 PM ET)

Telegram commands (all reads)
    │
    └──→ Read from LEGACY tables (UNCHANGED during soak)
         /dashboard → premium_ledger, trade_ledger, nav_snapshots
         /cycles → cc_cycle_log
         /fills → fill_log
         /ledger → premium_ledger
         (Phase 2.4 will cut these over to master_log_*)
```

## Soak Exit Criteria

### Minimum duration
**14 calendar days** from first flex_sync (2026-04-07).
Earliest possible exit: **2026-04-21**.

### Event-based criteria (ALL must be satisfied)
| # | Criterion | How to verify |
|---|-----------|---------------|
| 1 | At least 1 options expiry Friday processed | Check master_log_trades for BookTrade Ep/A events on a Friday 162000 timestamp after soak start |
| 2 | At least 1 assignment (CSP assigned → stock acquired) | Check master_log_option_eae for Assignment row after soak start |
| 3 | At least 1 worthless expiration | Check master_log_option_eae for Expiration row after soak start |
| 4 | flex_sync ran successfully ≥10 times without error | Check master_log_sync for ≥10 rows with status='success' |
| 5 | /reconcile shows 0 frozen, B=14/14 (or more), A residual stable | Run /reconcile, confirm no regression from baseline |
| 6 | No new divergences in A, B, or C beyond accepted residuals | Compare each /reconcile output against baseline |
| 7 | Legacy dashboard still functions correctly | Visual comparison of /dashboard output during soak |

### Automatic soak failure conditions
- Any new frozen ticker → investigate before continuing
- Any new cross-check A divergence > $1.00 → STOP
- Any cross-check B divergence > $0.10/share → STOP
- flex_sync fails 3 consecutive times → STOP
- Any master_log_* table row count DECREASES between syncs → STOP (data loss)

## Changes to telegram_bot.py

Three additions:
1. **`_scheduled_flex_sync()`** — async callback for EOD sync (lines ~8983-9013)
2. **`cmd_reconcile()`** — /reconcile command handler (lines ~9016-9162)
3. **Handler registration** — `app.add_handler(CommandHandler("reconcile", cmd_reconcile))` (line ~9626)
4. **Schedule registration** — `jq.run_daily(flex_sync_eod, 17:00 ET, Mon-Fri)` (lines ~9666-9671)

No existing code modified. All additions are additive.

## Production DB State
- master_log_sync: 1 row (from Task 2.2 first live write)
- master_log_trades: 1,466 rows
- Legacy tables: unchanged
- inception_carryin: 12 rows

## Next Steps
1. **Restart bot** to pick up the new code (flex_sync schedule + /reconcile command)
2. **Wait for first scheduled sync** at 5:00 PM ET
3. **Run /reconcile** after sync completes
4. **Repeat daily** for 14 days
5. **Phase 2.3 complete** when all soak exit criteria are met
6. **Phase 2.4** (reader cutover) dispatched as a separate task after soak
