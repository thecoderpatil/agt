# W3.7 Implementation Report — Hypothesis Property-Based Test Scaffold

**Date:** 2026-04-07
**Author:** Coder (Claude Code)
**Status:** COMPLETE
**Tests:** 114/114 (91 existing + 23 new: 18 property + 5 seed)
**Runtime:** 19.71s total, 6.00s property suite

---

## Files Created/Changed

| File | Change |
|------|--------|
| `requirements-dev.txt` | NEW — `hypothesis>=6.150,<7` |
| `tests/property/__init__.py` | NEW — empty package marker |
| `tests/property/strategies.py` | NEW — 10 composite strategies for Walker event generation |
| `tests/property/test_walker_properties.py` | NEW — 18 property tests + 5 @example seed tests |

**No production code changed.** Walker purity preserved. Zero DB/network/filesystem access in tests.

---

## Hypothesis Configuration

| Setting | Value |
|---------|-------|
| Version | 6.151.11 (pinned `>=6.150,<7`) |
| max_examples | 100 per test |
| deadline | None (disabled — Walker is CPU-bound, no I/O flakiness) |
| suppress_health_check | [too_slow] |

---

## Strategy Inventory (`tests/property/strategies.py`)

| Strategy | Type | Generates |
|----------|------|-----------|
| `csp_open_event_st()` | composite | CSP_OPEN (ExchTrade OPT SELL O P) |
| `cc_open_event_st()` | composite | CC_OPEN (ExchTrade OPT SELL O C) |
| `long_opt_open_event_st()` | composite | LONG_OPT_OPEN (ExchTrade OPT BUY O) |
| `stk_buy_event_st()` | composite | STK_BUY_DIRECT (ExchTrade STK BUY) |
| `stk_sell_event_st()` | composite | STK_SELL_DIRECT (ExchTrade STK SELL) |
| `assignment_pair_st()` | composite | CSP_OPEN + ASSIGN_OPT_LEG + ASSIGN_STK_LEG triple |
| `transfer_pair_st()` | composite | TRANSFER_OUT + TRANSFER_IN matched pair |
| `excluded_ticker_events_st()` | composite | 1-5 events with excluded ticker (SPX/VIX/NDX) |
| `satellite_long_opt_sequence_st()` | composite | LONG_OPT_OPEN + LONG_OPT_CLOSE pair |
| `valid_event_sequence_st()` | composite | N events starting with CSP_OPEN, random follow-ups |

Design principle: **constrain, don't filter.** All strategies generate events that satisfy walker preconditions (single ticker, single household, chronological dates). No `assume()` calls.

---

## Property Tests (18 total)

### Group A: Input Validation (3)

| # | Test | Invariant | Runtime |
|---|------|-----------|---------|
| 1 | `test_mixed_ticker_raises` | I15 | <0.01s |
| 2 | `test_mixed_household_raises` | I16 | <0.01s |
| 3 | `test_unknown_account_warns` | I5/I14 | 0.01s |

### Group B: Non-Negativity (4)

| # | Test | Invariant | Runtime |
|---|------|-----------|---------|
| 4 | `test_shares_never_negative` | I6/I8 | 0.62s |
| 5 | `test_short_puts_never_negative` | I7/I9 | 0.59s |
| 6 | `test_short_calls_never_negative` | I8/I10 | 0.59s |
| 7 | `test_orphan_expire_emits_counter_guard` | W3.6 | 0.01s |

### Group C: Cycle Semantics (4)

| # | Test | Invariant | Runtime |
|---|------|-----------|---------|
| 8 | `test_closed_iff_all_counters_zero` | I14 | 0.59s |
| 9 | `test_realized_pnl_additivity` | I2 | 0.53s |
| 10 | `test_cycle_keying_consistent` | I3 | 0.53s |
| 11 | `test_excluded_ticker_no_cycles` | I7 | 0.01s |

### Group D: Satellite & Transfer (4)

| # | Test | Invariant | Runtime |
|---|------|-----------|---------|
| 12 | `test_satellite_no_shares_at_creation` | I11 | 0.15s |
| 13 | `test_transfer_in_never_originates_wheel` | I13 | 0.01s |
| 14 | `test_transfer_conservation` | I19 | 0.08s |
| 15 | `test_satellite_stays_satellite_without_promotion_event` | I12 | 0.14s |

### Group E: Determinism & Ordering (2)

| # | Test | Invariant | Runtime |
|---|------|-----------|---------|
| 16 | `test_determinism` | I20 | 0.67s |
| 17 | `test_eod_ordering_independence` | — | 0.42s |

### Group F: Paper Basis (1)

| # | Test | Invariant | Runtime |
|---|------|-----------|---------|
| 18 | `test_assignment_basis_determinism` | I17 | 0.22s |

---

## @example Seed Tests (5)

| # | Test | Source | Pattern |
|---|------|--------|---------|
| 19 | `test_adbe_assignment_chain_non_negative` | ADBE live data | Two puts assigned same day, strike-match |
| 20 | `test_adbe_assignment_chain_basis_determinism` | ADBE live data | paper_basis determinism |
| 21 | `test_pypl_roll_no_premature_closure` | PYPL live data | Close + reopen same second |
| 22 | `test_guard_trip_long_expiry` | W3.1 regression | Long put expire shouldn't decrement short counter |
| 23 | `test_vikram_transfer_conservation` | Vikram transfer | Intra-household paired transfer preserves shares |

---

## Walker Bugs Found

**None.** All 18 property tests passed on the first run (after fixing two test-logic bugs in the test file itself — not walker bugs). The walker's invariants hold under randomized input.

---

## Runtime Report

| Suite | Tests | Time |
|-------|-------|------|
| Existing (test_walker.py + others) | 91 | 13.71s |
| Property (test_walker_properties.py) | 23 | 6.00s |
| **Total** | **114** | **19.71s** |

Well under the 60s target. max_examples=100 provides good coverage at this budget.

---

## Invariant Coverage Summary

| Invariant | Unit-tested | Property-tested | Combined |
|-----------|-------------|-----------------|----------|
| I1 (share conservation) | Partial | via I19 transfer | YES |
| I2 (P&L additivity) | Partial | test_realized_pnl_additivity | YES |
| I3 (cycle keying) | YES | test_cycle_keying_consistent | YES |
| I5 (cross-household guard) | YES | test_unknown_account_warns | YES |
| I6-I10 (non-negativity) | Partial | 3 property tests + guard emission | YES |
| I11 (satellite isolation) | YES | test_satellite_no_shares_at_creation | YES |
| I12 (satellite promotion) | YES | test_satellite_stays_satellite | YES |
| I13 (transfer origination) | YES | test_transfer_in_never_originates | YES |
| I14 (closure semantics) | YES | test_closed_iff_all_counters_zero | YES |
| I15 (mixed ticker) | Implicit | test_mixed_ticker_raises | YES |
| I16 (mixed household) | Implicit | test_mixed_household_raises | YES |
| I17 (paper basis IRS) | YES | test_assignment_basis_determinism | YES |
| I19 (transfer conservation) | YES | test_transfer_conservation | YES |
| I20 (determinism) | No | test_determinism | YES |
| EOD ordering | No | test_eod_ordering_independence | YES |
| Warning emission (W3.6) | YES | test_orphan_expire_emits_guard | YES |

---

## Followups (NOT in W3.7 scope)

- **CORP_ACTION property tests** — deferred per approval. Handler is synthetic-tested only; no real Flex shapes yet.
- **Long-option expiry reverse-scan exhaustive coverage** — complex composite strategy. The reverse-scan heuristic has known edge cases but is not failing under current randomized input.
- **I1 (share conservation) dedicated test** — currently covered indirectly via transfer conservation. A dedicated test counting STK_BUY/SELL deltas vs final shares_held would be stronger.
- **Stateful testing (Hypothesis RuleBasedStateMachine)** — model the walker as a state machine, generate arbitrary event sequences, assert invariants at every step. High value but complex to build.

---

## Constraints Verified

- [x] Walker purity preserved — zero DB/network/filesystem access in tests
- [x] max_examples=100, full suite under 60s (19.71s actual)
- [x] Tests pass on current walker code — no bugs found, no tests weakened
- [x] Coordinates with W3.6 WalkerWarning dataclass (asserts on .code and .severity)
- [x] No production code changes

**STOP. Report complete.**
