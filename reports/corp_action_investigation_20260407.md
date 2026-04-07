# R3: Corporate Action Investigation

Generated: 2026-04-07
Status: REPORT ONLY

## (a) Schema

`master_log_corp_actions` has 27 columns including: transaction_id (PK), account_id, type, symbol, quantity, action_description, amount, proceeds, cost, realized_pnl, code.

## (b) Current row count

**0 rows.** No corporate actions in the 365-day Flex window (2025-04-07 → 2026-04-06).

This means:
- No splits, reverse splits, spinoffs, mergers, special dividends, CUSIP changes, or symbol changes occurred on any position held in any of the 4 IBKR accounts during this period.
- The `flex_sync` parser correctly handles the section (CorporateActions → CorporateAction rows), it's just empty.

## (c) Walker CORP_ACTION handler

```python
elif et == EventType.CORP_ACTION:
    pass
```

Dead code — unreachable because no corp action events exist. The `pass` is correct as a placeholder; it records the event in the cycle's event list (via the `_apply_event` prologue) but makes no state changes.

## (d-e) No corp actions exist, no examples or overlaps to report

**Zero corp action rows across all 4 accounts.**

## (f) Proposed: corp_action_quarantine table

```sql
CREATE TABLE IF NOT EXISTS corp_action_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    account_id TEXT,
    action_type TEXT NOT NULL,
    action_description TEXT,
    detected_at TEXT NOT NULL DEFAULT (datetime('now')),
    cleared_at TEXT,
    cleared_by TEXT,  -- '/approve', 'Yash manual', etc.
    notes TEXT
);
```

On detection of a new corp action row during flex_sync:
1. Insert into `corp_action_quarantine`
2. Send Telegram alert: "Corp action detected: {ticker} {type} — CC/CSP staging suspended"
3. All /cc and /scan commands check quarantine before staging

Quarantine is lifted only by explicit `/clear_quarantine TICKER` command.

## (g) Proposed Walker handling (for when corp actions appear)

| Type | Handler |
|------|---------|
| Split (forward) | Multiply `shares_held` by split ratio. Divide `paper_basis` by same ratio. |
| Reverse split | Opposite of split. |
| Spinoff | Create a new cycle for the spun-off ticker at allocated basis. Reduce parent cycle basis by the allocated amount. |
| CUSIP/symbol change | Remap the cycle's `ticker` field. No state change. |
| Special dividend | Add to `stock_cash_flow`. No basis change (IRS treats separately). |
| Merger (cash) | Close the cycle. Realized P&L = cash received - cost basis. |
| Merger (stock) | Remap to new ticker at cost-adjusted basis. |

## (h) No worked examples (0 rows)

## Risk assessment

**LOW IMMEDIATE RISK** — no corp actions in the current portfolio. However:
- NVDA has had a 10:1 split in recent history (June 2024, before IBKR account opening)
- Any future position in a company undergoing a split would break the Walker if not handled
- The quarantine table provides a safety net: detect → freeze → human review → implement

## Recommendation

1. **Create the quarantine table** in Phase 0 (additive, no behavior change)
2. **Add quarantine check** to /cc and /scan (Phase 3)
3. **Implement split handler** when the first split occurs (just-in-time, not speculative)
4. **Monitor**: flex_sync should alert on any new corp action row (tonight's soak enhancement)

## Production DB: READ-ONLY (no changes during investigation)
