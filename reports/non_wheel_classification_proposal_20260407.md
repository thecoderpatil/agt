# Non-Wheel Activity Classification Proposal

Generated: 2026-04-07

## (f) Classification of 4 frozen tickers

| Ticker | Household | Classification | CSP_OPEN? | CC_OPEN? | Assignments? | LONG_OPT? | Events |
|--------|-----------|----------------|-----------|----------|--------------|-----------|--------|
| UBER | Yash | **WHEEL** | Yes | Yes | Yes | Yes | 102 |
| AMZN | Yash | **WHEEL** | Yes | Yes | Yes | Yes | 62 |
| NFLX | Yash | **NON_WHEEL** | No | No | No | Yes | 3 |
| PLTR | Yash | **WHEEL** | Yes (via carry-in) | No | No | Yes | 6 |

**Only NFLX is genuinely non-wheel.** The other 3 all have CSP_OPEN events in their event streams. They freeze because a `long_opt_open` arrives after a wheel cycle closes mid-stream — the long option is embedded WITHIN the wheel stream, not a separate non-wheel stream.

### UBER (Yash) — WHEEL, freeze at event[2]
The U22076184 CSP cycle closes at event[1]. Event[2] is `long_opt_open` from U22076329 — a protective put purchased seconds before selling a new CSP (event[4]) in the SAME account. The long put is a hedge during position entry, not speculation. The 99 subsequent events include 30+ CSP opens, multiple assignments, CCs — clearly wheel activity.

### AMZN (Yash) — WHEEL, freeze at event[45]
Events 0-44 are pure wheel (CSPs, assignments, CCs, stock buys/sells). At event[45], `long_opt_open` arrives after the previous cycle closed. This long call is a speculative overlay ON the wheel ticker — happens between wheel cycles.

### PLTR (Yash) — WHEEL, freeze at event[4]
Carry-in stock → sell (events 0-1). CSP open → close (events 2-3). Then `long_opt_open` at event[4] after cycle 2 closes. Speculative long call.

### NFLX (Yash) — NON_WHEEL, 3 events
2 long call opens + 1 expiry. Zero short puts, zero stock, zero assignments. Pure speculation.

## (g) Risk: wheel tickers with parallel long options

**8 tickers have BOTH CSP_OPEN and LONG_OPT_OPEN:**

| Ticker | Events | Notes |
|--------|--------|-------|
| ADBE | 84 | Long puts used as hedges during position entry |
| AMD | 42 | Frozen — long opts after cycle close |
| AMZN | 62 | Frozen — long opts between cycles |
| CRM | 87 | Already resolved (Task 3I) — long put Ep was the bug |
| GOOGL | 29 | Not frozen — long opts within active cycles |
| NVDA | 80 | Not frozen — long opts within active cycles |
| PLTR | 6 | Frozen — long call after cycle close |
| UBER | 102 | Frozen — long put before CSP entry |

**The per-stream partition rule does NOT work.** Long options are embedded within wheel cycles (hedges, overlays). Partitioning at the (household, ticker) level would split these tickers incorrectly.

## Revised proposal: per-event classification within walk_cycles()

Instead of partitioning streams, handle non-cycle-opening events gracefully within the Walker:

### Option A: Allow long_opt_open to open a "satellite" mini-cycle
When `long_opt_open` arrives with no active cycle:
1. Create a temporary mini-cycle (different from a wheel cycle)
2. Track P&L
3. Close on `long_opt_close` or `expire_worthless`
4. If a CSP_OPEN arrives while the mini-cycle is open, promote it to a full wheel cycle
5. Mini-cycles are tagged `cycle_type='SATELLITE'` vs `cycle_type='WHEEL'`

### Option B: Absorb orphan long_opt events into the preceding closed cycle
When `long_opt_open` arrives after a cycle just closed:
1. Re-open the previous cycle
2. Process the long option events
3. Re-evaluate closure

**Problem:** violates the closure-is-final contract.

### Option C: Buffer orphan long options, attach to next CSP_OPEN cycle
When `long_opt_open` arrives with no active cycle:
1. Buffer it
2. When the next CSP_OPEN opens a new cycle, inject the buffered events
3. This handles the UBER pattern (protective put bought seconds before CSP entry)

**Problem:** doesn't handle NFLX (no next CSP ever comes).

### Recommended: Option A (satellite mini-cycles)
- Handles all 4 frozen tickers
- Preserves wheel cycle semantic boundary (only CSP_OPEN opens wheel cycles)
- Tracks non-wheel P&L without losing it from reconciliation
- Cross-check A includes satellite P&L in Walker total
- Low risk to existing wheel cycles (only affects no-cycle state)

## (b-e) Detailed design for Option A

```python
# walk_cycles() new behavior when current is None:
if et in (EventType.LONG_OPT_OPEN, EventType.LONG_OPT_CLOSE):
    seq += 1
    current = _new_cycle(hh, tk, seq, ev.trade_date)
    current.cycle_type = 'SATELLITE'  # new field
elif et == EventType.EXPIRE_WORTHLESS:
    # Check if this is a long option expiry (via backward scan)
    # If so, create a satellite mini-cycle, process, close immediately
    ...
```

**Cross-check A:** `walker_realized[key] = sum(c.realized_pnl for c in all_cycles)` already includes satellite cycles. No change needed.

**Frozen detection:** runs on all cycles. Satellite cycles never freeze because they handle their own events.

**Non-wheel tickers (NFLX):** classified as satellite-only (no wheel cycles). Correctly tracked.

## (h) Test impact

Of the 30 tests:
- **14 walker tests:** 0 would break (satellite cycles are additive; existing cycles unchanged). 2-3 new tests needed for satellite behavior.
- **10 flex parser tests:** 0 impact.
- **6 trade_repo tests:** 0 impact.

**Estimated new tests:** 3 (synthetic satellite open/close, NFLX non-wheel, UBER long-opt-before-CSP pattern).

## Production DB: untouched (0 master_log tables)
