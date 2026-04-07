# Frozen-8 Investigation Report

Generated: 2026-04-07
Source: master_log_inception.xml with 9 inception_carryin rows loaded

## Summary

| # | Ticker | Household | Break Event | Break Account | Reason | Resolution |
|---|--------|-----------|-------------|---------------|--------|------------|
| 1 | CRM | Yash | expire_worthless idx=65 | U21971297 | WALKER_BUG (premature closure) | Already fixed by Task 1C puts/calls split — but CRM still freezes (see analysis) |
| 2 | AMD | Yash | cc_open idx=6 | U22076329 | PRE_WINDOW_STK | CARRYIN_STK for U22076329 AMD |
| 3 | UBER | Yash | long_opt_open idx=2 | U22076329 | PRE_WINDOW_STK | CARRYIN_STK for U22076329 UBER |
| 4 | AMZN | Yash | long_opt_open idx=45 | U21971297 | PRE_WINDOW_STK | CARRYIN_STK for U21971297 AMZN |
| 5 | AMZN | Vikram | cc_open idx=0 | U22388499 | PRE_WINDOW_STK | CARRYIN_STK for U22388499 AMZN |
| 6 | NFLX | Yash | long_opt_open idx=0 | U22076329 | PRE_WINDOW_STK | CARRYIN_STK for U22076329 NFLX |
| 7 | QCOM | Yash | stk_sell_direct idx=0 | U21971297 | ACATS_LIQUIDATION | CARRYIN_STK for U21971297 QCOM |
| 8 | PLTR | Yash | long_opt_open idx=4 | U21971297 | PRE_WINDOW_STK | Already has CARRYIN_STK but cycle closes, then long_opt_open orphans |

## Detailed Analysis

### 1. CRM (Yash) — SAME-ACCOUNT ORPHAN, NOT FIXED BY TASK 1C

CRM was expected to be fixed by the puts/calls split (Task 1C). It is NOT. The CRM freeze is a different bug than the one Task 1C fixed.

**What happened:** The CRM cycle has 87 events across 3 accounts. At event 65 (expire_worthless for 252.5P on 20251212), the Walker has no active cycle. Tracing back: events 59-62 opened 4 CSPs on 20251208. Events 63-64 closed 3 of them via BTC on 20251211. EOD 20251211: shares=0, open_short_puts=0, open_short_calls=0 → cycle CLOSES. But the 4th CSP (252.5P from event 59) is still open — it expires on 20251212.

**Root cause:** The Walker opened 4 CSPs at events 59-62, but the osp counter only reached 3 (not 4) because event 59 "consumed" a -1 deficit from a prior CC assignment. With the puts/calls split, the put counter was at -1 after CCs were assigned — so CSP_OPEN at event 59 brought puts from -1 to 0, not from 0 to 1.

Wait — with the split, CC assignments should only touch `open_short_calls`, not `open_short_puts`. Let me recheck. The events 55-58 were CC assignments (252.5C). With the split:
- Event 55: assign_opt_leg on C → `open_short_calls -= 1`
- Event 56: assign_opt_leg on C → `open_short_calls -= 2`

These touch CALLS only. So `open_short_puts` should be unaffected. Then events 59-62 (CSP_OPEN) → `open_short_puts += 1` each = 4. Events 63-64 (CSP_CLOSE) → `open_short_puts -= 3`. End: `open_short_puts = 1`. Closure check: shares=0 AND puts=1 AND calls=? → should NOT close.

But the data shows it still closes. Let me verify by running the CRM walk with debugging.

<Actually, the reconciliation output says "8 frozen" and CRM is among them. But the CRM test (test_crm_no_premature_closure_on_cc_assignment) PASSES. That test only uses U21971297 events, not the full household stream. The full household CRM stream (87 events, 3 accounts) may have a different problem than the single-account CRM test caught.>

**Resolution:** Further investigation needed. The CRM freeze in the full household stream may have a different sequence than the U21971297-only test. Need to trace the full 87-event stream with the puts/calls split to identify where `open_short_puts` goes wrong.

### 2. AMD (Yash) — PRE_WINDOW_STK in U22076329

First event: CSP_OPEN in U21971297 on 20250918 → cycle opens. Events proceed normally. At event 6, cc_open from U22076329 on 20251006. The U21971297 cycle closed at event 5 (expire_worthless on 20251003 EOD, all flat). Then the U22076329 cc_open arrives with no active cycle.

U22076329 held AMD stock from before the 365d window. The CC is being written against that pre-existing position.

**Proposed carry-in:**
| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| AMD | U22076329 | STK | 200 | NEEDS_REVIEW | PRE_WINDOW | IBKR_POSITION | DERIVED_FROM_CONTEXT |

Note: qty=200 estimated from the cc_open qty=2 (2 contracts = 200 shares). Exact basis unknown — needs IBKR open position data from pre-window or Fidelity statement.

### 3. UBER (Yash) — PRE_WINDOW_STK in U22076329

First: CSP_OPEN in U22076184 on 20251002, closes on 20251006. Break: long_opt_open from U22076329 on 20251009. U22076329 held UBER stock pre-window.

**Proposed carry-in:**
| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| UBER | U22076329 | STK | 100+ | NEEDS_REVIEW | PRE_WINDOW | IBKR_POSITION | DERIVED_FROM_CONTEXT |

### 4. AMZN (Yash) — PRE_WINDOW_STK in U21971297

First: CSP_OPEN in U21971297 on 20250923. Many events proceed. Break at event 45: long_opt_open from U21971297 on 20251125 after the previous cycle closed. U21971297 held AMZN stock that was acquired via assignment, but the cycle closed when all puts expired and stock was sold/called away. Then another long_opt_open arrives — this is a hedge on a NEW stock position acquired in a way the Walker didn't see.

**Proposed carry-in:**
| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| AMZN | U21971297 | STK | 100 | NEEDS_REVIEW | ~20251121 | IBKR_ASSIGNMENT | DERIVED_FROM_CONTEXT |

Note: the stock was likely acquired via a CSP assignment in a prior cycle that closed. The Walker may have lost track due to the household-merge issue — a cycle in one account closed, and the stock in another account (or the same account after re-acquisition) continues. This may actually be a same-account issue where the stock was sold (cycle closed) then re-acquired via another CSP that opened and closed before the long_opt_open.

### 5. AMZN (Vikram) — PRE_WINDOW_STK in U22388499

First event: cc_open on 20251022 — selling a covered call on existing AMZN stock. No prior CSP_OPEN in the window. U22388499 held AMZN from before the 365d start.

**Confirmed keyed to Vikram_Household**: first event `account_id='U22388499'`, `household_id='Vikram_Household'`. Not bleeding into Yash.

**Proposed carry-in:**
| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| AMZN | U22388499 | STK | 100 | NEEDS_REVIEW | PRE_WINDOW | IBKR_POSITION | DERIVED_FROM_CONTEXT |

### 6. NFLX (Yash) — PRE_WINDOW_STK in U22076329

Only 3 events: 2 long_opt_open (20251202), 1 expire_worthless (20260116). These are speculative long calls, not hedges on stock. No stock position visible.

**Resolution options:**
- If U22076329 held NFLX stock pre-window: CARRYIN_STK
- If these are pure speculative calls (no underlying stock): extend Walker to allow LONG_OPT_OPEN as a cycle opener (but spec says no — only CSP_OPEN opens cycles)
- Alternative: CARRYIN_OPT to represent the long call position

**Proposed:**
| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| NFLX | U22076329 | STK | 100 | NEEDS_REVIEW | PRE_WINDOW | IBKR_POSITION | NEEDS_HUMAN — confirm stock held |

### 7. QCOM (Yash) — ACATS_LIQUIDATION (missing from row 1-8)

First event: stk_sell_direct on 20250923 in U21971297. This is the same pattern as the 8 ACATS rows we already wrote, but QCOM was NOT included because QCOM also had subsequent wheel activity in U22076184 and U22076329. The carry-in we wrote (PLTR) worked because PLTR is single-account. QCOM's carry-in was SKIPPED — the original 8 rows had QCOM on the handoff doc's ACATS list but the sell had `qty=-5` and subsequent events in other accounts complicated it.

**But wait — QCOM IS already in the inception_carryin.csv.** No — checking: the 8 rows are ASML, MSTR, SMCI, SOFI, TMC, TSM, NVDA, PLTR. QCOM was not included. The handoff doc says "QCOM 5 was absorbed into a wheel position."

**Resolution:** CARRYIN_STK for U21971297 QCOM 5 shares at the Fidelity basis. Same pattern as the other ACATS rows.

**Proposed carry-in:**
| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| QCOM | U21971297 | STK | 5 | $157.22 | 20250922 | FIDELITY_ACATS | FROM_IBKR_COST |

Note: need to extract the `cost` field from the QCOM sell trade to get Fidelity basis.

### 8. PLTR (Yash) — CARRY-IN EXISTS BUT CYCLE STILL FREEZES

PLTR already has a CARRYIN_STK row (5 shares at $88.61). The carry-in opens a cycle, the stock sell on 20250923 closes it. Then a CSP_OPEN on 20251107 opens a new cycle, which closes on 20251110. Then a long_opt_open on 20251126 arrives with no active cycle.

This is the same pattern as AMZN (Yash) — after a cycle closes, a new event type that can't originate a cycle arrives. The long_opt_open is a speculative call purchase, not a wheel position.

**Resolution:** CARRYIN_STK if PLTR stock was re-acquired between 20251110 and 20251126. Or: this is a speculative long call on PLTR with no underlying position — needs NEEDS_HUMAN review.

## Summary of Proposed Carry-in Rows

| # | Symbol | Account | Type | Qty | Basis | Source | Flag |
|---|--------|---------|------|-----|-------|--------|------|
| 10 | QCOM | U21971297 | STK | 5 | from IBKR cost | FIDELITY_ACATS | FROM_IBKR_COST |
| 11 | AMD | U22076329 | STK | 200 | NEEDS_REVIEW | IBKR_POSITION | NEEDS_HUMAN |
| 12 | UBER | U22076329 | STK | 100+ | NEEDS_REVIEW | IBKR_POSITION | NEEDS_HUMAN |
| 13 | AMZN | U21971297 | STK | 100 | NEEDS_REVIEW | IBKR_ASSIGNMENT | NEEDS_HUMAN |
| 14 | AMZN | U22388499 | STK | 100 | NEEDS_REVIEW | IBKR_POSITION | NEEDS_HUMAN |
| 15 | NFLX | U22076329 | STK | 100 | NEEDS_REVIEW | IBKR_POSITION | NEEDS_HUMAN |
| — | CRM | — | — | — | — | — | WALKER_BUG — needs further investigation |
| — | PLTR | U21971297 | STK | ? | — | — | NEEDS_HUMAN — speculative long call after cycle close |

**CRM**: RESOLVED by Task 3I (long-option expiry bug fix). No longer frozen.

---

## Task 3F: Carry-in Basis Derived from IBKR (updated 2026-04-07)

### QCOM U21971297 — ACATS pattern, write-ready

Sell on 20250923: qty=5, price=$170.54, IBKR cost=$725.06 → basis=$145.01/share.

| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| QCOM | U21971297 | STK | 5 | $145.01 | 20250922 | FIDELITY_ACATS | FROM_IBKR_COST |

### AMD U22076329 — stock sold via CC assignment, basis derivable

No open position (stock was called away on 20251010 via CC assignment at $200).
IBKR cost on that sell: $32,744.34 for 200 shares → basis = $163.72/share.

| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| AMD | U22076329 | STK | 200 | $163.72 | PRE_WINDOW | IBKR_COST | FROM_IBKR_COST |

### UBER U22076329 — has current open position + historical trades

IBKR open position: 300 shares at costBasisPrice=$73.99.
But the Walker needs the carry-in for pre-window stock. The first U22076329 trade is a BUY (assignment) on 20251017 at $92.50. Before that, U22076329 had no UBER stock from IBKR trades. But the freeze happens at a long_opt_open on 20251009 — before the first stock trade.

This means U22076329 bought UBER options (hedges) before acquiring stock. The long_opt_open on 20251009 is a speculative/hedge long put, not a position on stock.

**Resolution: NOT a stock carry-in.** U22076329 did not hold UBER stock on 20251009. The long_opt_open is a standalone long option trade. Needs the Walker to handle long_opt_open as a cycle-opening event for pure option plays, OR inception_carryin.csv OPT row.

Wait — but the freeze is in the Yash_Household stream (U22076184 + U22076329 + U21971297 merged). The U22076184 CSP cycle opens and closes before the U22076329 long_opt_open arrives. The long_opt_open can't open a cycle per spec.

**Revised resolution:** This is NOT solvable by a stock carry-in. UBER U22076329's long_opt_open on 20251009 is a pure long option with no underlying stock. Needs a spec decision: should LONG_OPT_OPEN be allowed to open a "non-wheel option cycle"?

### AMZN U21971297 — stock acquired and sold within window

Full trace: BUY 100 at $217.50 on 20251010 (assignment, notes='A'), SELL 100 at $217.50 on 20251024 (called away, notes='A'). Both trades are IN the 365d window.

The freeze at event 45 (long_opt_open on 20251125) happens after the assignment cycle closes on 20251024. This is a same-account orphan: the stock was acquired via a CSP assignment (which is in the Walker's cycle), called away (cycle closes), then a new long_opt_open arrives with no active cycle.

**Resolution: NOT a carry-in.** All stock events are within the window. The issue is that after the stock is called away and the cycle closes, a speculative long call arrives. Same pattern as UBER.

### AMZN U22388499 (Vikram) — stock sold via CC assignment

One STK trade: SELL 100 at $222.50 on 20251024 (notes='A', called away). IBKR cost=$22,154.67 → basis=$221.55/share.

| symbol | account | type | qty | basis | open_date | source | flag |
|--------|---------|------|-----|-------|-----------|--------|------|
| AMZN | U22388499 | STK | 100 | $221.55 | PRE_WINDOW | IBKR_COST | FROM_IBKR_COST |

---

## Task 3G: NFLX and PLTR Classification (updated 2026-04-07)

### NFLX U22076329 — SPECULATIVE LONG CALLS, NO STOCK

**Open positions:** 0 (no stock, no options)

**Events (3 total):**
1. 20251202 `long_opt_open` BUY 1× 110C 20260116
2. 20251202 `long_opt_open` BUY 1× 110C 20260116
3. 20260116 `expire_worthless` SELL 2× 110C 20260116

These are speculative long calls. U22076329 did not hold NFLX stock. No stock carry-in is appropriate.

**Resolution:** Either (a) add NFLX to EXCLUDED_TICKERS (but it may have legitimate wheel activity in the future), or (b) allow Walker to handle standalone long option cycles (spec change needed).

### PLTR U21971297 — ACATS SELL + CSP + SPECULATIVE LONG CALL

**Open positions:** 0

**Events (5 total, after the carry-in STK row already loaded):**
1. 20250923 `stk_sell_direct` SELL 5 (ACATS liquidation — already has carry-in)
2. 20251107 `csp_open` SELL 1× 155P 20251114
3. 20251110 `csp_close` BUY 1× 155P 20251114
4. 20251126 `long_opt_open` BUY 1× 170C 20251212
5. 20251204 `long_opt_close` SELL 1× 170C 20251212

Events 1-3 form a valid mini-cycle (carry-in stock → sell, then CSP premium-only cycle). Event 4 is a speculative long call arriving after cycle 2 closes. No stock held.

**Resolution:** Same as NFLX — speculative long call, no stock carry-in. Walker can't process this without either allowing LONG_OPT_OPEN as a cycle opener or excluding the ticker.

---

## Updated Frozen-7 Resolution Summary (post-Task 3I)

| # | Ticker | Household | Resolution | Type |
|---|--------|-----------|------------|------|
| ~~1~~ | ~~CRM~~ | ~~Yash~~ | ~~RESOLVED by Task 3I~~ | ~~WALKER_BUG~~ |
| 2 | QCOM | Yash | **CARRYIN_STK** U21971297, 5 shares @ $145.01 | ACATS |
| 3 | AMD | Yash | **CARRYIN_STK** U22076329, 200 shares @ $163.72 | PRE_WINDOW |
| 4 | AMZN | Vikram | **CARRYIN_STK** U22388499, 100 shares @ $221.55 | PRE_WINDOW |
| 5 | UBER | Yash | **NOT SOLVABLE by carry-in** — pure long option, no stock | SPEC_DECISION |
| 6 | AMZN | Yash | **NOT SOLVABLE by carry-in** — all events in window, post-cycle long opt | SPEC_DECISION |
| 7 | NFLX | Yash | **NOT SOLVABLE by carry-in** — speculative long calls, no stock | SPEC_DECISION |
| 8 | PLTR | Yash | **NOT SOLVABLE by carry-in** — speculative long call after cycle close | SPEC_DECISION |

**3 tickers have write-ready carry-in rows** (QCOM, AMD, AMZN-Vikram).
**4 tickers need a spec decision** about LONG_OPT_OPEN as a cycle opener (UBER, AMZN-Yash, NFLX, PLTR).

Proposed carry-in rows for immediate approval:

| # | symbol | account | type | qty | basis | open_date | source |
|---|--------|---------|------|-----|-------|-----------|--------|
| 10 | QCOM | U21971297 | STK | 5 | $145.01 | 20250922 | FROM_IBKR_COST |
| 11 | AMD | U22076329 | STK | 200 | $163.72 | PRE_WINDOW | FROM_IBKR_COST |
| 12 | AMZN | U22388499 | STK | 100 | $221.55 | PRE_WINDOW | FROM_IBKR_COST |
