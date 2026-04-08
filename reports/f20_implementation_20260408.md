# F20 — Sub-account Routing — Implementation Report

**Date:** 2026-04-08
**Sprint:** F20-impl
**Tests:** 465/465 (14 new)

---

## Diff Summaries

### F20-1: Schema migration
- `agt_equities/schema.py`: Added idempotent `ALTER TABLE ... ADD COLUMN originating_account_id TEXT` migration
- Added `originating_account_id TEXT` to both CREATE TABLE DDL instances (main + TRANSMITTING migration rebuild)
- Updated 14 test file DDLs to include the new column

### F20-2: Allocation helper
- `telegram_bot.py`: Added `allocate_excess_proportional(excess_contracts, accounts_with_shares)` pure function
- Placed before `_compute_overweight_scope` at module level
- Handles: proportional split, sub-lot skip (<100sh), remainder to largest, deterministic sort

### F20-3: Multi-row CC staging
- `telegram_bot.py:_stage_dynamic_exit_candidate`: Replaced single-row INSERT with multi-row loop
- Calls `allocate_excess_proportional` with `position["accounts_with_shares"]`
- Each account gets its own `uuid.uuid4()` audit_id
- `contracts`, `shares`, `gate1_freed_margin`, `gate1_realized_loss` scaled proportionally per row
- All INSERTs wrapped in single `with conn:` transaction (atomic)
- Fallback to household primary if allocation returns empty (defensive)
- Summary includes routing detail: "Routed: 2c->Individual, 1c->Roth IRA"

### F20-4: TRANSMIT routing + fail-closed guard
- `telegram_bot.py:handle_dex_callback`: Replaced `HOUSEHOLD_MAP[household][0]` with `row["originating_account_id"]`
- Fail-closed guard: if NULL, logs TRANSMIT_BLOCKED_NULL_ACCOUNT, alerts operator, cancels row, returns
- Single placeOrder site confirmed (line 7010)

### F20-5: STK_SELL minimal hardening
- `agt_equities/rule_engine.py:stage_stock_sale_via_smart_friction`: Added `originating_account_id` to INSERT, writes NULL
- TODO comment for Followup #20b

### F20-6: Column ownership
- Added `originating_account_id: write-once at staging, never modified after (F20)` to orphan scan docstring

---

## Empirical Verification: Allocation Helper

All four worked-example scenarios pass:

```
Scenario 1 — Individual 300 + Roth 200, excess 3 → {'U21971297': 2, 'U22076329': 1} ✓
Scenario 2 — Single account 500sh, excess 4 → {'U21971297': 4} ✓
Scenario 3 — Roth 50sh (sub-lot) skipped → {'U21971297': 2} ✓
Scenario 4 — Excess 1, two accounts → {'U21971297': 1} (largest gets remainder) ✓
```

## placeOrder Guard Coverage

```
$ grep -n placeOrder telegram_bot.py
4672: ib_conn.placeOrder(target_trade.contract, target_trade.order)  # /approve CSP/CC — out of scope
7010: trade = ib_conn.placeOrder(contract, order)                    # Dynamic exit TRANSMIT — GUARDED
7231: trade = ib_conn.placeOrder(contract, order)                    # pending_orders — out of scope
```

Only line 7010 is in the dynamic exit TRANSMIT path. Guarded by F20-4 null check at lines 6974-7008.

---

F20 done | tests: 465/465 | sub-account routing | STOP | reports/f20_implementation_20260408.md
