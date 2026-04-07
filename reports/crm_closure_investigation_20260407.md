# CRM and ADBE Closure Investigation

Generated: 2026-04-07
Walker version: pre-Task-1 (10/10 tests passing, no modifications)

## CRM Investigation

### a) EOD closure logic

**walker.py lines 388-393:**
```python
# EOD closure check: after processing ALL events for this trade_date
if current is not None and current.shares_held == 0 and current.open_short_options == 0:
    current.status = 'CLOSED'
    current.closed_at = trade_date
    cycles.append(current)
    current = None
```

### b) Full event trace

87 events for CRM in Yash_Household, across accounts U21971297, U22076184, U22076329.

Critical sequence (events 58-65):

| idx | date | account | event_type | qty | strike | expiry | shares | osp |
|-----|------|---------|------------|-----|--------|--------|--------|-----|
| 56 | 20251205 162000 | U22076329 | assign_opt_leg | 2 | 252.5 | 20251205C | 300 | -1 |
| 57 | 20251205 162000 | U21971297 | assign_stk_leg | 100 | — | — | 200 | -1 |
| 58 | 20251205 162000 | U22076329 | assign_stk_leg | 200 | — | — | 0 | -1 |
| 59 | 20251208 104747 | U21971297 | csp_open | 1 | 252.5 | 20251212P | 0 | 0 |
| 60 | 20251208 105758 | U21971297 | csp_open | 1 | 250.0 | 20251212P | 0 | 1 |
| 61 | 20251208 110449 | U21971297 | csp_open | 1 | 250.0 | 20251212P | 0 | 2 |
| 62 | 20251208 120127 | U21971297 | csp_open | 1 | 252.5 | 20251212P | 0 | 3 |
| 63 | 20251211 104557 | U21971297 | csp_close | 2 | 250.0 | 20251212P | 0 | 1 |
| 64 | 20251211 133447 | U21971297 | csp_close | 1 | 252.5 | 20251212P | 0 | 0 |
| — | **EOD CLOSURE on 20251211** | — | — | — | — | — | **0** | **0** |
| 65 | 20251212 162000 | U21971297 | **expire_worthless** | 1 | 252.5 | 20251212P | **ORPHAN** | — |

### c) The exact closure event

**EOD on 20251211.** After events 63-64, both csp_close events reduce osp from 3 → 1 → 0. shares_held is already 0 (stock was called away on 20251205). EOD check: shares=0, osp=0 → **CLOSED**.

### d) The orphaned event

**Event 65:** expire_worthless of the 252.5P 20251212 on its expiration date (20251212). This put was opened at event 59 on 20251208 (csp_open, 252.5P 20251212). But the Walker closed the cycle on 20251211 because two other puts (250P) were bought back, bringing osp to 0.

### e) Was the closure correct or premature?

**PREMATURE.** At EOD 20251211, the cycle had:
- shares_held = 0 ✓
- open_short_options = 0 (after 2 BTC + 1 BTC = 3 closed out of 4 opened)

But **the 4th put was still open**. Events 59-62 opened 4 CSPs total:
1. Event 59: STO 252.5P 20251212 (osp +1 = 0... wait, actually osp was -1 entering this)

Let me re-read the trace. After event 58 (assign_stk_leg, shares go 300→0), osp is **-1**. That's because the assign_opt_leg at event 56 decremented osp by 2 (from 1 to -1). The -1 reflects the CC that was assigned: 2 covered calls were assigned (event 55-56: assign_opt_leg qty=1 and qty=2 for 252.5C), but the osp before that was 1 (one CC open). So 1 - 1 - 2 = -2? No, events 55-56 show osp going from 300/1 to 300/-1 (decrement by 2 from the 2× assign_opt_leg).

**The negative osp is the root cause.** The Walker's simple counter allows osp to go negative because it doesn't track per-contract state. When osp reaches -1 after the call assignment, the subsequent CSP_OPEN at event 59 brings it to 0 (not 1), and the next three CSP_OPENs bring it to 1, 2, 3. Then the 2 BTC events bring it to 1, 0 — but there's still one put open (the first one from event 59 that "consumed" the -1 deficit).

**The premature closure happens because osp reaches 0 while one put is still open.** The negative osp from the call assignment masked the count.

### f) Proposed minimal fix

The negative osp comes from assign_opt_leg decrementing a counter that doesn't distinguish puts from calls. When 2 covered calls are assigned (osp -=2) but only 1 CC was counted as open (osp was 1), osp goes to -1.

**Root cause:** The Walker uses a single `open_short_options` counter for both puts and calls. When a BookTrade assignment closes a covered call, it decrements the same counter that tracks CSPs. If the call was opened by a CC_OPEN from a different account within the household, and that account's CC_OPEN happened to arrive at the same time as another event that triggered a different decrement... the counter loses track.

**Minimal fix options:**

1. **Split `open_short_options` into `open_short_puts` and `open_short_calls`.** Each tracks its own count. CSP_OPEN/CSP_CLOSE/ASSIGN_OPT_LEG(put) affect `open_short_puts`. CC_OPEN/CC_CLOSE/ASSIGN_OPT_LEG(call)/EXPIRE_WORTHLESS(call) affect `open_short_calls`. Closure rule: shares=0 AND puts=0 AND calls=0. This prevents cross-contamination between put and call counters.

2. **Floor osp at 0.** Never let osp go negative — if an ASSIGN_OPT_LEG would reduce osp below 0, clamp to 0 and log a warning. This is a band-aid, not a fix.

3. **Track per-contract state** with a set of (strike, expiry, right, account) tuples. Only decrement when the matching contract is found. This is the most correct but most complex approach.

**Recommendation: Option 1 (split puts/calls).** It's the minimal structural change that eliminates the cross-contamination bug without adding per-contract tracking complexity.

---

## ADBE Investigation

### Event trace summary

84 events for ADBE in Yash_Household, across U21971297, U22076184, U22076329.

The Walker successfully processes all 84 events without freezing. It produces 8 closed cycles + 1 active cycle. Final state: shares=500, osp=3.

### Does ADBE have the same premature-closure pattern?

**No.** ADBE does not freeze. It runs to completion. However, the osp counter does show the same negative-value pattern:

- Event 52: osp goes to -1 after csp_close (lines 51-52 in trace)
- This is immediately corrected by a cc_open on the same day

The negative osp doesn't cause a premature closure in ADBE because the cycle always has shares > 0 at those moments, so the EOD closure rule (shares=0 AND osp=0) doesn't trigger.

### Cross-check A divergence ($109.95)

The ADBE $109.95 realized P&L divergence between Walker ($3,727.80) and IBKR ($3,837.75) is likely caused by IBKR's `total_realized_pnl` in `master_log_realized_unrealized_perf` aggregating across ALL ADBE conids (underlying + individual option contracts) while the Walker only counts `fifo_pnl_realized` from events it processed. The $109.95 delta matches approximately 1-2 option round trips' worth of realized P&L that may be attributed differently.

To confirm: the 84 Walker events span 3 accounts. IBKR's FIFO perf summary may include ADBE-related conids (individual option contracts) that the Walker groups differently. Specifically, events from the early cycles (0-7 in the trace, all closed premium-only cycles) contribute realized P&L in both the Walker and IBKR, but if IBKR attributes some realized P&L to the underlying stock conid (ADBE STK) vs the option conid (ADBE 260109P00335000), the grouping may differ.

**This needs a per-conid breakdown of IBKR's FIFO realized P&L to diagnose further.** Not a Walker bug — likely a cross-check aggregation mismatch.

### Key observation: ADBE osp goes to 0 multiple times between cycles

Events 76 and 78 show osp going to 0 while shares=500. The EOD closure rule doesn't fire because shares > 0. This is **correct behavior** — the cycle stays open because the stock position is still held.

The pattern is: sell CCs, they expire or get closed, osp goes to 0, then sell new CCs. The cycle correctly remains open throughout because shares are held.

### ADBE U22076329 CC at event 82

Notable: event 82 is a CC_OPEN from U22076329 (Roth) on ADBE 260417C260, while all prior ADBE stock is held in U21971297 (Individual). This is a cross-account covered call — U22076329 must also hold ADBE stock. The trace shows U22076329 was assigned 100 shares at event 44 (assign_stk_leg). That lot was called away at event 58 (assign_stk_leg via CC assignment). But event 82 writes another CC from U22076329... either U22076329 re-acquired ADBE stock via another assignment, or this CC is uncovered. Without per-account stock tracking, the Walker can't distinguish.

This is another symptom of the **single-counter, multi-account** limitation.
