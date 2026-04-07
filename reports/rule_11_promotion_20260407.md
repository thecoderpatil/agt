# Rule 11 Promotion Report

Generated: 2026-04-07

## Promoted

Rule 11 (Portfolio Circuit Breaker) activated in Rulebook v9.0:
- **Hard cap:** 1.50x gross beta-weighted leverage per household
- **Hysteresis release:** 1.40x
- **Beta source:** yfinance Ticker.info['beta'] (REFERENCE, cached)

## Current state

| Household | Leverage | Cap | Status |
|-----------|----------|-----|--------|
| Yash | 2.18x | 1.50x | BREACHED |
| Vikram | 2.88x | 1.50x | BREACHED |

## Operational impact: ZERO

CSP selling was already paused pending Dynamic Exit completion. Rule 11 codifies the pause and prevents accidental re-entry above cap.

## Implementation

1. **risk.py:** `LEVERAGE_LIMIT = 1.50`, `LEVERAGE_RELEASE = 1.40`, `gross_beta_leverage()` function
2. **Command Deck:** "Lev" cell shows "Y 2.18x V 2.88x" in red
3. **CSP staging guard:** `/scan` checks all households before scanning. On breach:
   ```
   Rule 11 BLOCK: Yash at 2.18x (cap 1.50x). CSP staging halted.
   Reduce via Mode 1 CC harvest or Dynamic Exit before retrying.
   ```
4. **Mode 1 CC harvest:** Unaffected (de-risking allowed when breached)
5. **Rulebook v9:** `Portfolio_Risk_Rulebook_v9.md` updated with Rule 11 text + Rule 2 deployment governor table

## Tests: 63/63 passing (includes 3 leverage tests + 12 risk tests)

## Files modified
- `agt_deck/risk.py`: leverage function + constants
- `telegram_bot.py`: `_check_rule_11_leverage()` + `/scan` guard
- `Portfolio_Risk_Rulebook_v9.md`: Rule 11 + Rule 2 v9 table
