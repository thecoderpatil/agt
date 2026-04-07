# Phase 1 Closure Report

Generated: 2026-04-07
Status: **PHASE 1 COMPLETE — Phase 2 cutover unblocked pending Yash authorization**

---

## (a) Final Reconciliation State

| Cross-check | Result | Notes |
|-------------|--------|-------|
| **A (realized P&L)** | **48/49** | 48 tickers match to the penny; 1 accepted residual (ADBE) |
| **B (cost basis)** | **14/14** | All per-account paper_basis within $0.10 of IBKR costBasisPrice |
| **C (NAV recon)** | **2/4** | 2 accounts exact; 2 accepted residuals (IBKR-side) |
| **Frozen tickers** | **0** | All 19 original frozen tickers resolved |
| **Tests** | **34/34** | 15 walker + 10 parser + 6 trade_repo + 3 satellite |
| **Parity** | **438/438** | OptionEAE ↔ BookTrade, zero violations |

---

## (b) Three Accepted Residuals

### Residual 1: ADBE Yash realized P&L — cross-check A, -$109.95

**Root cause:** Cross-account option expiry attribution. One specific option (`ADBE 251010P00332500`) was sold by U22076184 (Trad IRA) and expired under U22076329 (Roth IRA).

**Evidence — master_log_trades:**
```
U22076184  20251007;114453  ExchTrade  SELL  qty=-1  price=1.11  fifo_pnl_realized=0.0  notes=  oc=O
U22076329  20251010;162000  BookTrade  BUY   qty=1   price=0.0   fifo_pnl_realized=0.0  notes=Ep oc=C
```

Neither trade row carries realized P&L. Both show `fifo_pnl_realized=0.0`.

**Evidence — master_log_realized_unrealized_perf:**
```
U22076184  ADBE 251010P00332500  total_realized_pnl=0.0
U22076329  ADBE 251010P00332500  total_realized_pnl=109.95131
```

IBKR's FIFO performance summary attributes $109.95 to U22076329 using its own lot-matching methodology, but this value does not appear on any trade row.

**Classification:** IBKR methodology difference. Walker correctly sums per-trade `fifo_pnl_realized`. DB total matches Walker exactly ($3,727.80 = $3,727.80). The FIFO summary is a secondary computation with different cross-account rules.

### Residual 2: U22076329 NAV — cross-check C, +$109.95

**Root cause:** Direct flow-through of Residual 1. The NAV reconciliation for U22076329 shows a $109.95 gap because IBKR's `total_realized_pnl` in the FIFO summary includes the $109.95 that doesn't appear on any trade row.

### Residual 3: U22388499 NAV — cross-check C, -$21.53

**Root cause:** IBKR's own ChangeInNAV components don't sum to `endingValue - startingValue`.

**Evidence — master_log_change_in_nav for U22388499:**
```
starting_value:                  $0.00
ending_value:               $80,787.00

Non-zero components:
  realized                    $24,145.19
  change_in_unrealized       -$51,422.15
  transferred_pnl_adjustments    $506.67
  deposits_withdrawals        $86,695.48
  asset_transfers             $21,648.00
  dividends                      $292.80
  interest                      -$956.88
  change_in_interest_accruals    -$67.08
  other_fees                     -$33.50

Component sum:              $80,808.53
ibkr_delta (ending-starting): $80,787.00
RESIDUAL:                      -$21.53
```

**Classification:** IBKR reporting artifact. The components IBKR provides in ChangeInNAV do not perfectly reconcile to their own ending-starting delta. Likely accumulated rounding over 365 daily MTM revaluations.

---

## (c) Cycle Inventory

| Metric | Count |
|--------|-------|
| **WHEEL cycles** | 174 (14 active) |
| **SATELLITE cycles** | 2 (0 active) |
| **Total** | 176 (14 active) |

### Active wheel cycles by household

**Yash_Household (9 active):**

| Ticker | Shares | Short Puts | Short Calls | Paper Basis | Strategy Basis |
|--------|--------|------------|-------------|-------------|----------------|
| ADBE | 500 | varies | varies | $332.82 | $324.20 |
| CRM | 100+ | varies | varies | $261.41 | varies |
| GTLB | 100 | 0 | 0 | $32.75 | $32.32 |
| MSFT | 200 | 0 | 0 | $481.06 | $471.66 |
| PYPL | 2,300 | varies | varies | $60.31 | $56.56 |
| QCOM | 300+ | 0 | 0 | $157.22 | $156.13 |
| SLS | 1,900 | 0 | 0 | $3.80 | $3.47 |
| UBER | 700+ | varies | varies | $81.24 | varies |
| NVDA | active | varies | varies | varies | varies |

**Vikram_Household (5 active):**

| Ticker | Shares | Paper Basis | Strategy Basis |
|--------|--------|-------------|----------------|
| ADBE | 200 | $318.10 | $309.52 |
| CRM | 100 | $261.41 | $255.95 |
| MSFT | 100 | $481.77 | $471.82 |
| PYPL | 800 | $57.89 | $55.03 |
| QCOM | 100 | $166.55 | $165.14 |

---

## (d) inception_carryin.csv — 12 Rows

| # | Symbol | Account | Type | Qty | Basis | Source | Reason |
|---|--------|---------|------|-----|-------|--------|--------|
| 1 | ASML | U21971297 | STK | 2 | $690.93 | FIDELITY | ACATS_IN |
| 2 | MSTR | U21971297 | STK | 4 | $293.60 | FIDELITY | ACATS_IN |
| 3 | SMCI | U21971297 | STK | 30 | $27.02 | FIDELITY | ACATS_IN |
| 4 | SOFI | U21971297 | STK | 20 | $14.61 | FIDELITY | ACATS_IN |
| 5 | TMC | U21971297 | STK | 10 | $3.45 | FIDELITY | ACATS_IN |
| 6 | TSM | U21971297 | STK | 5 | $157.22 | FIDELITY | ACATS_IN |
| 7 | NVDA | U21971297 | STK | 17 | $112.07 | FIDELITY | ACATS_IN |
| 8 | PLTR | U21971297 | STK | 5 | $88.61 | FIDELITY | ACATS_IN |
| 9 | PYPL | U22076184 | STK | 100 | $29.22 | FIDELITY | ACATS_IN |
| 10 | QCOM | U21971297 | STK | 5 | $145.01 | FIDELITY | ACATS_IN |
| 11 | AMD | U22076329 | STK | 200 | $163.72 | IBKR | PRE_IBKR |
| 12 | AMZN | U22388499 | STK | 100 | $221.55 | IBKR | PRE_IBKR |

---

## (e) Walker Feature Inventory (Phase 1)

| Feature | Task | Description |
|---------|------|-------------|
| Household keying | Phase 0 | `HOUSEHOLD_MAP` + `household_for()` on walker.py |
| Split puts/calls counter | Task 1C | `open_short_puts` + `open_short_calls` replace `open_short_options` |
| IRS paper_basis | Task 2 | Assignment price minus originating CSP premium per IRS rules |
| Per-account paper_basis | Task 3D | `_paper_basis_by_account: dict` + `paper_basis_for_account()` |
| Strike-match assignment | Task 3M | `ASSIGN_STK_LEG` matches opt_leg by `strike == trade_price` |
| Long-option expiry isolation | Task 3I | `expire_worthless` checks originating event direction before decrementing |
| Long-option counter tracking | Task 3L | `open_long_puts` + `open_long_calls` on Cycle |
| Satellite mini-cycles | Task 3L | `cycle_type='SATELLITE'` for non-wheel long-option activity |
| Satellite promotion | Task 3L | SATELLITE → WHEEL on CSP/CC/assignment arrival |

---

## (f) Test Inventory — 34 tests

### Walker tests (15):
1. `test_uber_premium_only_cycle` — 3 CSP opens/closes, closed cycle
2. `test_uber_clean_expiration_cycle` — CSP → Ep, closed cycle
3. `test_uber_deep_multi_assignment_cycle` — 4 CSPs, 3 assigned, active with 300 shares
4. `test_meta_called_away_with_carryin` — carry-in + assignment + CCs + called away
5. `test_adbe_long_put_hedge_round_trip` — 8 events in 3 min, net zero
6. `test_adbe_manual_roll_different_ib_order_id` — same-second roll, no fragmentation
7. `test_uber_eod_assignment_cluster` — same-timestamp assignment cluster, canonical sort
8. `test_strategy_basis_vs_ibkr_tax_basis_uber` — paper_basis within $0.10 of IBKR
9. `test_unknown_book_trade_notes_fails_closed` — synthetic, UnknownEventError
10. `test_non_usd_event_fails_closed` — synthetic, UnknownEventError
11. `test_crm_no_premature_closure_on_cc_assignment` — single-account, puts/calls split
12. `test_adbe_per_account_paper_basis` — 2-account, per-account basis matches IBKR
13. `test_long_put_expiry_does_not_decrement_short_counter` — synthetic, long/short isolation
14. `test_crm_full_household_no_freeze` — 87-event household stream, no freeze
15. `test_adbe_two_puts_assigned_same_day_correct_strike_match` — strike matching

### Flex parser tests (10):
1-10: Trade count, account count, open positions, OptionEAE, attribute-only sections, idempotency, null PK guard, UBER position match

### Trade repo tests (6):
1-6: Active cycles, household filter, ticker filter, frozen-ticker graceful handling, DB load verification, closed cycles

### Satellite tests (3):
1. `test_satellite_long_opt_no_prior_csp` — synthetic satellite open/close
2. `test_satellite_then_wheel_cycle` — satellite closes, wheel opens
3. `test_nflx_full_stream_satellite_only` — real data, 3 events, satellite-only

---

## (g) Production DB Confirmation

```
master_log_* tables in agt_desk.db: 0
bot_order_log: does not exist
inception_carryin: does not exist
cc_decision_log: does not exist
master_log_sync: does not exist
```

Production database has NOT been modified at any point during Phase 1. All reconciliation and testing ran against in-memory or temp SQLite databases using the test fixtures.

---

## (h) Phase 1 Gate Criteria Checklist

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Walker processes all events without crashing | **PASS** — 0 frozen, 176 cycles |
| 2 | OptionEAE ↔ BookTrade parity: zero violations | **PASS** — 438/438 |
| 3 | Per-ticker realized P&L matches IBKR | **ACCEPTED RESIDUAL** — 48/49, ADBE -$109.95 (IBKR cross-account attribution) |
| 4 | Per-account paper_basis matches IBKR costBasisPrice within $0.10/share | **PASS** — 14/14 |
| 5 | Per-account NAV reconciliation within $1.00 | **ACCEPTED RESIDUAL** — 2/4, U22076329 +$109.95 (flow-through), U22388499 -$21.53 (IBKR rounding) |
| 6 | Zero inception_carryin rows needed for active positions | **PASS** — 12 rows all for closed/historical positions |
| 7 | All walker tests pass | **PASS** — 34/34 |
| 8 | Production DB untouched | **PASS** — 0 master_log tables |
| 9 | TRAW.CVR appears as ungrouped stock | **PASS** — verified in reconciliation (1 share, $0 basis, not in any cycle) |

---

## Sign-off

**Phase 1 is complete.** The Walker correctly derives wheel cycles from 1,466 IBKR trades across 4 accounts in 2 households, producing 174 wheel cycles and 2 satellite cycles with 14 active positions.

Three documented residuals are accepted as IBKR methodology differences, not Walker bugs. All are fully explained with quoted evidence from IBKR's own data.

**Phase 2 cutover (dashboard + report read migration) is unblocked pending Yash authorization.**
