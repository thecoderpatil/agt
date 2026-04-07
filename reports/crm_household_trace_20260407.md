# CRM Full-Household Trace

Generated: 2026-04-07

## Root Cause: LONG OPTION EXPIRY DECREMENTING SHORT COUNTER

The CRM premature closure is **not** caused by the puts/calls split (Task 1C fixed that). It's caused by a **different bug**: the `expire_worthless` handler decrements `open_short_puts` or `open_short_calls` regardless of whether the expiring option was SHORT or LONG.

### The critical sequence (events 44-52)

| idx | date | account | event | qty | strike | exp | rt | puts | calls | shares |
|-----|------|---------|-------|-----|--------|-----|----|------|-------|--------|
| 44 | 20251125 | U22076329 | **long_opt_open** | 1 | 220.0 | 20251128 | P | 1 | 0 | 300 |
| 45 | 20251125 | U21971297 | csp_close | 1 | 220.0 | 20251128 | P | 0 | 0 | 300 |
| 46 | 20251126 | U21971297 | csp_open | 1 | 220.0 | 20251128 | P | 1 | 0 | 300 |
| 47 | 20251126 | U21971297 | csp_open | 1 | 202.5 | 20251205 | P | 2 | 0 | 300 |
| 48 | 20251126 | U21971297 | csp_open | 1 | 200.0 | 20251205 | P | 3 | 0 | 300 |
| 49 | 20251128 | **U22076329** | **expire_worthless** | 1 | 220.0 | 20251128 | P | **2** | 0 | 300 |
| 50 | 20251128 | U21971297 | expire_worthless | 1 | 220.0 | 20251128 | P | 1 | 0 | 300 |
| 51 | 20251204 | U21971297 | csp_close | 1 | 202.5 | 20251205 | P | 0 | 0 | 300 |
| 52 | 20251204 | U21971297 | csp_close | 1 | 200.0 | 20251205 | P | **-1** | 0 | 300 |

**Event 44**: U22076329 buys a 220P (LONG put open). This is a hedge, not a short position. `long_opt_open` correctly does NOT increment `open_short_puts`. puts stays at 1.

**Event 45**: U21971297 closes (BTC) its short 220P. puts: 1→0.

**Event 49**: The U22076329 220P expires. The Walker classifies this as `expire_worthless` and decrements `open_short_puts` by 1. **But this was a LONG put, not a SHORT put.** The decrement should not happen. puts: 2→**1** (should stay at 2).

This -1 error propagates through events 50-52, where legitimate short put closures bring puts to -1 instead of 0. At event 59, a new CSP_OPEN brings puts from -1 to 0 instead of 0 to 1. The cascade continues until EOD 20251211 where puts=0 with 1 short put still open → premature closure.

### Proposed fix

The `expire_worthless` handler needs to know whether the expiring option was a SHORT position (opened via CSP_OPEN or CC_OPEN) or a LONG position (opened via LONG_OPT_OPEN). Currently it blindly decrements based on right (P/C).

**Option A: Track long option count separately**
```python
# New fields on Cycle:
open_long_puts: int
open_long_calls: int

# LONG_OPT_OPEN handler:
if ev.right == 'P':
    cycle.open_long_puts += int(ev.quantity)
else:
    cycle.open_long_calls += int(ev.quantity)

# LONG_OPT_CLOSE handler:
if ev.right == 'P':
    cycle.open_long_puts -= int(ev.quantity)
else:
    cycle.open_long_calls -= int(ev.quantity)

# expire_worthless handler:
# Check if this is a long or short expiry by checking if open_long > 0 for this right
# If long options exist for this right, decrement long first; otherwise decrement short
```

**Problem with Option A:** Without per-contract tracking, the Walker can't distinguish between a long 220P expiring and a short 220P expiring when both exist simultaneously.

**Option B: Per-contract tracking (most correct)**
Maintain a set of open option contracts `{(account, strike, expiry, right, direction)}` where direction is 'LONG' or 'SHORT'. On expiry, look up the matching contract and decrement the correct counter.

**Option C: Match expiry to opening event**
On `expire_worthless`, scan backward through cycle events for the matching `CSP_OPEN`/`CC_OPEN` (for short) or `LONG_OPT_OPEN` (for long) with same strike+expiry+account. If the match is a long open, decrement long counter (or do nothing to short counter). If short open, decrement short counter.

**Recommendation: Option C** — minimal structural change, uses existing event history, handles the CRM case correctly. Complexity is O(n) scan per expiry event, but expiry events are infrequent relative to total events.

### Verification

With Option C applied, the CRM trace would show:
- Event 49: expire_worthless 220P matches `long_opt_open` at event 44 → does NOT decrement `open_short_puts`
- puts stays at 2 after event 49 (not 1)
- Events 50-52: legitimate short put closures bring puts from 2→1→0→-1... wait, that's still -1.

Actually event 50 (expire_worthless 220P from U21971297) — this one IS a short put. It was opened at event 46 (csp_open 220P U21971297 20251128). So it correctly decrements puts. After event 50: puts = 2 - 1 (event 50 short Ep) = 1. Then events 51-52 close 202.5P and 200P → puts = 1 - 1 - 1 = -1.

Wait, that's still wrong. Let me recount. After event 48: puts = 3 (events 46, 47, 48 each +1). Event 49: long Ep, no decrement → puts = 3. Event 50: short Ep (220P from U21971297) → puts = 2. Event 51: BTC 202.5P → puts = 1. Event 52: BTC 200P → puts = 0. Then at events 59-62: 4 CSP opens → puts = 4. Events 63-64: 3 BTC → puts = 1. EOD 20251211: puts = 1 ≠ 0 → **cycle does NOT close**. Event 65 Ep processes normally.

**Option C resolves CRM.**
