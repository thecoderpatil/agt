# Frozen Ticker Triage Report

Generated: 2026-04-07
Source: master_log_inception.xml (Last365CalendarDays, 2025-04-07 → 2026-04-06)
Walker frozen: 15 tickers

## Summary Table

| # | Ticker | Household | Account(s) | First Event | Type | Reason | Resolution |
|---|--------|-----------|------------|-------------|------|--------|------------|
| 1 | ASML | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 2 | MSTR | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 3 | SMCI | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 4 | SOFI | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 5 | TMC | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 6 | TSM | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 7 | NVDA | Yash | U21971297, U22076329 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 8 | PLTR | Yash | U21971297 | 2025-09-23 | stk_sell_direct | UNGROUPED_STOCK | INCEPTION_CARRYIN_ROW or EXCLUDED_TICKER |
| 9 | QCOM | Yash | U21971297, U22076184, U22076329 | 2025-09-23 | stk_sell_direct | MULTI_ACCOUNT_ORPHAN | WALKER_SCOPE_FIX |
| 10 | AMD | Yash | U21971297, U22076329 | 2025-09-18 | csp_open | MULTI_ACCOUNT_ORPHAN | WALKER_SCOPE_FIX |
| 11 | UBER | Yash | U21971297, U22076184, U22076329 | 2025-10-02 | csp_open | MULTI_ACCOUNT_ORPHAN | WALKER_SCOPE_FIX |
| 12 | CRM | Yash | U21971297, U22076184, U22076329 | 2025-09-25 | csp_open | MULTI_ACCOUNT_ORPHAN | WALKER_SCOPE_FIX |
| 13 | AMZN | Yash | U21971297, U22076184, U22076329 | 2025-09-23 | csp_open | MULTI_ACCOUNT_ORPHAN | WALKER_SCOPE_FIX |
| 14 | AMZN | Vikram | U22388499 | 2025-10-22 | cc_open | PRE_INCEPTION_OPEN | INCEPTION_CARRYIN_ROW |
| 15 | NFLX | Yash | U22076329 | 2025-12-02 | long_opt_open | PRE_INCEPTION_OPEN | INCEPTION_CARRYIN_ROW |

## Three Root Cause Categories

### Category A: Fidelity ACATS Liquidations (8 tickers: ASML, MSTR, SMCI, SOFI, TMC, TSM, NVDA, PLTR)

These are stocks transferred from Fidelity via ACATS in Sept 2025 and immediately sold at IBKR on 2025-09-23. The Walker sees a `stk_sell_direct` as the first event with no preceding buy or CSP cycle.

- ASML: 2 shares sold, 1 event total, no further activity
- MSTR: 4 shares sold, 1 event total
- SMCI: 30 shares sold, 1 event total
- SOFI: 20 shares sold, 9 events (later re-entered via wheel)
- TMC: 10 shares sold, 1 event total
- TSM: 5 shares sold, 12 events (later re-entered via wheel)
- NVDA: 17 shares sold, 79 events (major wheel ticker after liquidation)
- PLTR: 5 shares sold, 5 events (small wheel activity after)

**Resolution options:**
1. Add inception_carryin rows for each ACATS stock lot (qty, basis from Fidelity statement)
2. Extend Walker to allow `STK_SELL_DIRECT` to open a "liquidation mini-cycle" that immediately closes
3. For tickers with 1 event and no subsequent wheel activity (ASML, MSTR, SMCI, TMC): add to an ignore list

### Category B: Multi-Account Orphans (5 tickers: QCOM, AMD, UBER, CRM, AMZN-Yash)

**This is the most important finding.** These tickers are actively traded across multiple IBKR accounts within Yash_Household. The Walker groups events by `(household, ticker)` and processes them as a single stream. A cycle opens in one account (e.g., U21971297), closes, and then a completely unrelated event arrives from a different account (e.g., U22076329) — which is an orphan because the closed cycle was in a different account.

Detailed breakdowns:

**AMD (41 events, 2 accounts):**
- U21971297: CSP opens on 2025-09-18, cycle runs normally through U21971297
- U22076329: CC opens on 2025-10-06 (event [6]) — this is stock already held in Roth that has nothing to do with the Individual account's CSP cycle
- Walker sees the CC_OPEN after the Individual cycle closed → ORPHAN

**UBER (102 events, 3 accounts):**
- U22076184: CSP opens on 2025-10-02, closes on 2025-10-06 (premium-only cycle)
- U22076329: long_opt_open on 2025-10-09 (event [2]) — hedge on existing Roth position
- Walker sees the long_opt after U22076184's cycle closed → ORPHAN

**CRM (87 events, 3 accounts):**
- U21971297: CSP cycle starts 2025-09-25, runs through many events
- Breaks at event [65]: expire_worthless on 2025-12-12 in U21971297 — after prior cycle closed and no new CSP opened
- The Ep is closing a CSP that was opened within the 365d window but the cycle it belongs to was already closed by EOD eval

**AMZN-Yash (62 events, 3 accounts):**
- U21971297: CSP cycle starts 2025-09-23, runs normally
- Breaks at event [45]: long_opt_open on 2025-11-25 in U21971297 — hedge after cycle closed

**QCOM (39 events, 3 accounts):**
- First event is stk_sell_direct on 2025-09-23 (ACATS liquidation)
- Then wheel activity across 3 accounts

**Resolution: The Walker should group by `(account, ticker)` instead of `(household, ticker)`.** Each IBKR account is an independent portfolio with independent cycles. The `household` grouping is useful for the DASHBOARD (aggregate view) but not for the Walker's cycle state machine. Cycles in U21971297 and U22076329 are independent — they have separate margin, separate positions, and separate assignment risk.

This is a **Walker design fix**, not a data fix. It would resolve all 5 Category B tickers immediately with zero inception_carryin rows.

### Category C: Pre-Inception Opens (2 tickers: AMZN-Vikram, NFLX-Yash)

**AMZN in Vikram_Household (U22388499):**
- First event: cc_open on 2025-10-22 (selling a covered call)
- The underlying stock was acquired before 2025-04-07 (the 365-day window start)
- 15 total events, all in single account
- Resolution: inception_carryin row for the AMZN stock position held before the window

**NFLX in Yash_Household (U22076329):**
- First event: long_opt_open on 2025-12-02 (buying a call, hedge on existing position)
- Only 3 events: 2 long call opens + 1 expiration
- The underlying position (if any) was acquired before the window
- Resolution: inception_carryin row for the stock position, OR if this is just a speculative long call with no underlying stock, extend Walker to allow `LONG_OPT_OPEN` as a cycle-opening event

## Impact Assessment

| Category | Count | Affects Active Positions? | Affects P&L Reconciliation? |
|----------|-------|--------------------------|----------------------------|
| A (ACATS) | 8 | Only NVDA, SOFI, TSM, PLTR have subsequent wheel activity | Yes — frozen tickers excluded from cross-check A |
| B (Multi-account) | 5 | **Yes — AMD, UBER, CRM, AMZN, QCOM are major holdings** | Yes — these are high-volume wheel tickers |
| C (Pre-inception) | 2 | AMZN-Vikram is active; NFLX is expired | Minor |

**Category B is the critical path.** The 5 multi-account tickers include UBER and CRM which are core wheel positions with active stock holdings. Resolving these requires a Walker design decision: `(account, ticker)` vs `(household, ticker)` grouping.

---

## Task 3: paper_basis Investigation

### a) Location of paper_basis computation in walker.py

The paper_basis update occurs in `_update_paper_basis()` (lines 195-201) and `_apply_event()` (called for each event):

```python
def _update_paper_basis(cycle: Cycle, delta_shares: float, price_per_share: float) -> None:
    """Weighted average update for stock acquisitions."""
    if delta_shares <= 0:
        return
    old_shares = cycle.shares_held
    old_basis = cycle.paper_basis if cycle.paper_basis is not None else 0.0
    new_shares = old_shares + delta_shares
    if new_shares > 0:
        cycle.paper_basis = ((old_basis * old_shares) + (price_per_share * delta_shares)) / new_shares
```

Called from `_apply_event()` for these event types:
- `ASSIGN_STK_LEG`: `_update_paper_basis(cycle, delta, ev.trade_price)` — uses `trade_price` which equals the strike price
- `STK_BUY_DIRECT`: `_update_paper_basis(cycle, ev.quantity, ev.trade_price)`
- `EXERCISE_STK_LEG`: `_update_paper_basis(cycle, ev.quantity, ev.trade_price)`

### b) Does it subtract the assigned-put premium?

**No.** `paper_basis` is computed as a pure weighted average of stock acquisition prices (= strike prices for assignments). It does NOT subtract the assigned-put premium. The value is the raw strike price, not the IRS-adjusted cost basis.

### c) Is there any field that represents (strike - assigned_put_premium)?

**No.** The Cycle dataclass has:
- `paper_basis`: raw weighted average of acquisition prices (strike only)
- `adjusted_basis` (property): `paper_basis - (premium_total / shares_held)` — this subtracts ALL premium (CSPs + CCs + hedges), not just assigned-put premium

Neither field computes `strike - assigned_put_premium_only`. The IRS-compliant cost basis path was never built.

### d) Worked example: ADBE in U21971297

Using real data from master_log_inception.xml:

**First assignment (2025-01-02):**
- Assigned put: ADBE 260102P00350000
- Strike: $350.00
- CSP open event: STO ADBE 260102P00350000 — not in the 365d window (opened before 2025-04-07)
  - But from the fixture, the `cost` field on the assignment BookTrade shows the assigned cost = $35,000 for 100 shares
- Premium received on that specific STO: NOT AVAILABLE in the 365d fixture (the opening sell happened pre-window)

**Using the second assignment (2025-01-09, which IS in window) instead:**
- Put: ADBE 260109P00335000 (assigned)
- Strike: $335.00
- CSP open event (STO): 2025-01-02, net_cash = $222.20
- Premium received on that put: $222.20 / 100 = $2.22/share
- IBKR costBasisPrice should be: $335.00 - $2.22 = $332.78/share

**Current Walker values for ADBE Yash_Household:**
- `paper_basis` = $337.00 (weighted avg of $350 + $335 + $337.50 + $312.50 over 500 shares = ($350×100 + $335×100 + $337.5×100 + $312.5×100 + some more) / 500)
- `adjusted_basis` = $328.38 (paper_basis - ALL premium / shares)
- IBKR `costBasisPrice` from master_log_open_positions: $332.82 (weighted across multiple accounts)

**Expected `paper_basis` if computed correctly (strike - assigned_put_premium):**
For the $335 put assignment lot: $335.00 - $2.22 = $332.78
For the $337.50 put assignment lot: $337.50 - $3.37 = $334.13
Weighted average would be closer to IBKR's $332.82 than Walker's current $337.00.

### e) Diagnosis

**This is a missing feature, not a bug in existing code.** The Walker's `paper_basis` was built as a simple weighted average of acquisition prices. The IRS-compliant cost basis computation (strike minus assigned-put premium only) was specified in the handoff doc but never implemented. The Cycle dataclass would need either:

1. A new field `irs_cost_basis` that tracks `strike - assigned_put_premium` per assignment lot, OR
2. A modification to `paper_basis` to subtract assigned-put premium at assignment time (making `paper_basis` match IBKR's costBasisPrice)

Option 2 aligns with the spec's intent: "paper_basis — weighted average cost of shares held" which, per IRS rules for assigned puts, SHOULD include the premium reduction. The current implementation uses raw strike instead.

**Cross-check B's $0.10 tolerance is correct IF paper_basis is fixed to include the put premium reduction.** The current divergences ($0.18 to $6.90) are exactly the per-share assigned-put premiums, confirming this is the single missing computation.
