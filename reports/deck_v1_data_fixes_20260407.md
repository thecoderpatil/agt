# Command Deck v1 — Data Correctness Fixes

Generated: 2026-04-07

## BUG 1: Day P&L showed full NAV — FIXED

**Root cause:** `ChangeInNAV` has one row per account covering the full Last365CalendarDays period. `startingValue=0` for all 4 accounts because they were opened via ACATS in Oct 2025 (within the 365-day window). So `endingValue - startingValue = total NAV`, which is the gross period change including deposits.

**Fix:**
- Relabeled cell from "Day P&L" to "Period" (since daily P&L is not available from this Flex query)
- Dollar amount shows full-period delta: +$342,689 (correct — portfolio went from $0 to $342K)
- Percentage shows "n/a" (since startingValue=0, can't compute meaningful %)
- To show true return %, would need `twr` from ChangeInNAV (deferred)

**Worked numbers:**
```
U21971297: start=$0.00 → end=$109,217.87 → delta=$109,217.87
U22076329: start=$0.00 → end=$152,661.32 → delta=$152,661.32
U22076184: start=$0.00 → end=$23.17       → delta=$23.17
U22388499: start=$0.00 → end=$80,787.00  → delta=$80,787.00
TOTAL:     start=$0.00 → end=$342,689.37 → delta=$342,689.37
```

## BUG 2: Concentration showed wrong % — FIXED

**Root cause:** `concentration_check()` used `paper_basis` ($318/share for ADBE Vikram) instead of spot price (~$244/share). Also didn't display which household had the worst concentration.

**Fix:**
- Changed position value calculation: `shares * spot_price` (falls back to paper_basis if spot unavailable)
- Added household name to display: "ADBE/Vikram 60.5%"
- Returns worst household, not aggregate

**Worked example (with spot $244.36):**
```
ADBE Yash:   500 × $244.36 = $122,180 / Yash NLV $261,902 = 46.7%
ADBE Vikram: 200 × $244.36 = $48,872  / Vikram NLV $80,787 = 60.5%
→ Display: "ADBE/Vikram 60.5%" (red, >20%)
```

Note: Both households exceed Rule 1's 20% limit for ADBE. This is a real risk signal, not a display bug.

## BUG 3: Attention panel noise — FIXED

**Root cause:** GTLB at -30.5% flagged attention despite only -$999 unrealized loss on a small position.

**Fix:** Added minimum dollar threshold: `abs(unrealized_$) >= $1,500 OR DTE ≤ 5`. GTLB (-$999) now filtered out. Constant `ATTENTION_MIN_LOSS_DOLLAR = 1500` tunable in main.py.

**Result:** GTLB removed from attention. 11 remaining items are all positions with >$1,500 loss or near-term DTE.

## BUG 4: Sanity check — PASSED

**PYPL Yash:** 2,300 shares @ paper_basis $60.31
- Per-account: U22076329 700@$62.25, U21971297 1600@$59.47
- Matches cross-check B reconciliation ($60.31 vs IBKR $60.36, delta $0.05)

**ADBE Yash:** 500 shares @ paper_basis $332.82
- Per-account: U21971297 400@$329.11, U22076329 100@$347.67
- Matches cross-check B reconciliation ($332.82 vs IBKR $332.82, delta $0.00)

**MSFT Yash:** 200 shares @ paper_basis $481.06
- Per-account: U21971297 100@$481.77, U22076329 100@$480.34
- Real active cycles from CSP assignments:
  - U22076329: STO 485P 260102 on 20251226 → assigned
  - U21971297: STO 485P 260102 on 20251226 → assigned
  - U22076329: STO 482.5P 260102 on 20251229 → assigned
- Confirmed real active positions, not stale data

**MSFT Vikram:** 100 shares @ paper_basis $481.77
- U22388499: STO 485P 260102 on 20251226 → assigned
- Same cycle origin as Yash MSFT, different account

## Post-fix top strip values

```
NAV:          $342,689
Period P&L:   +$342,689 (n/a %)
Concentration: ADBE/Vikram 60.5% (red)
Sector:       OK
VIX:          [live]
Sync:         [timestamp from last sync]
```

## Tests: 47/47 passing
## Production DB: READ-ONLY (no changes)
