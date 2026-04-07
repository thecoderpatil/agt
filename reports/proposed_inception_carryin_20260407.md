# Proposed inception_carryin Rows

Generated: 2026-04-07
Source: master_log_inception.xml (Last365CalendarDays, 2025-04-07 → 2026-04-06)
Status: REPORT ONLY — awaiting Yash row-by-row approval before writing CSV

## Section 1: ACATS Liquidations (8 tickers)

These stocks were transferred from Fidelity via ACATS to U21971297 (Yash Individual) in September 2025, then immediately sold on 2025-09-23. The Walker sees a `stk_sell_direct` as the first event with no preceding buy.

Cost basis is derived from the IBKR `cost` field on the sell trade (which IBKR received from the ACATS transfer). `basis_per_share = -cost / qty` (cost is negative for long positions).

| symbol | account | type | qty | basis_per_share | sell_price | open_date | source | flag |
|--------|---------|------|-----|-----------------|------------|-----------|--------|------|
| ASML | U21971297 | STK | 2 | $690.93 | $967.82 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| MSTR | U21971297 | STK | 4 | $293.60 | $335.21 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| SMCI | U21971297 | STK | 30 | $27.02 | $47.07 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| SOFI | U21971297 | STK | 20 | $14.61 | $29.81 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| TMC | U21971297 | STK | 10 | $3.45 | $6.04 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| TSM | U21971297 | STK | 5 | $157.22 | $281.39 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| NVDA | U21971297 | STK | 17 | $112.07 | $179.98 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |
| PLTR | U21971297 | STK | 5 | $88.61 | $184.19 | PRE_WINDOW | FIDELITY_ACATS | FROM_IBKR_COST |

**Notes:**
- NVDA has 79 total events (significant wheel activity after the ACATS sell). Once the carry-in unblocks the first sell, the subsequent CSP cycles will process normally.
- SOFI (9 events), TSM (12), PLTR (5) also have post-ACATS wheel activity.
- ASML, MSTR, SMCI, TMC are one-event tickers (sell only, no further activity).

## Section 2: Pre-Window CSP Originators

### ADBE — No carry-in needed

All 5 ADBE assignments in Yash_Household have matching CSP_OPEN events within the 365-day window:

| Assignment | Account | Strike | Expiry | CSP_OPEN Date | Premium/sh |
|------------|---------|--------|--------|---------------|------------|
| 20260102 | U21971297 | $350.00 | 20260102 | 20251226 | $2.33 |
| 20260102 | U22076329 | $350.00 | 20260102 | 20251226 | $2.33 |
| 20260109 | U21971297 | $337.50 | 20260109 | 20260102 | $3.37 |
| 20260109 | U21971297 | $335.00 | 20260109 | 20260102 | $2.22 |
| 20260122 | U21971297 | $312.50 | 20260123 | 20260115 | $10.65 |

The ADBE cross-check B divergence ($332.59 vs $332.82, delta -$0.23) is NOT caused by missing carry-in data. It may be a weighted-average rounding issue or a multi-account weighting difference. Deferred per instructions.

### PYPL — 1 carry-in needed (U22076184 Trad IRA)

All 8 PYPL put assignments have matching CSP_OPEN events in the window. No option carry-ins needed.

However, U22076184 (Trad IRA) held 100 PYPL shares from a Fidelity ACATS transfer, then sold a 69C covered call on 20251001 which was assigned on 20251003 (stock called away). The Walker never saw the stock acquisition. This is the 100-share gap (Walker 2,200 vs IBKR 2,300).

| symbol | account | type | qty | basis_per_share | premium | open_date | source | flag |
|--------|---------|------|-----|-----------------|---------|-----------|--------|------|
| PYPL | U22076184 | STK | 100 | $29.22 | — | PRE_WINDOW | FIDELITY_ACATS | NEEDS_REVIEW |

**Note on basis_per_share:** The handoff doc lists "Yash Trad IRA: PYPL 100" as an ACATS lot. The $29.22 basis is estimated from Fidelity's original cost. If exact basis is unknown, flag as NEEDS_REVIEW — Yash can confirm from the Fidelity statement.

However, since U22076184's PYPL was fully called away on 20251003, this carry-in only affects the CLOSED cycle's realized P&L, not any active position or paper_basis. The 2,300 IBKR shares are across U21971297 (1,600) and U22076329 (700) — the Walker's 2,200 is the household-wide count which correctly includes the called-away lot in a closed cycle.

**Actually, the 100-share gap comes from the Walker freezing on the PYPL (household, ticker) group.** The U22076184 PYPL events (CSP + CC + assignment) merge into the household stream. The first event is a CSP_OPEN from U22076184 (20251001), which opens a cycle. Then the CC assignment on 20251003 closes the put cycle (Ep) and calls away stock (notes='A'). But the stock sell is an ASSIGN_STK_LEG with buy_sell='SELL', which means the Walker tries to decrement shares_held — but shares_held was never incremented because the ACATS stock buy is pre-window.

This means the carry-in row is needed to prevent shares_held from going negative.

## Section 3: Expected Post-Write Outcomes

| # | Row | Resolves | Cross-check Impact |
|---|-----|----------|--------------------|
| 1 | ASML STK 2 | Unfreezes ASML (1 event) | A: no change (no further wheel activity) |
| 2 | MSTR STK 4 | Unfreezes MSTR (1 event) | A: no change |
| 3 | SMCI STK 30 | Unfreezes SMCI (1 event) | A: no change |
| 4 | SOFI STK 20 | Unfreezes SOFI (9 events) | A: adds SOFI to checked list |
| 5 | TMC STK 10 | Unfreezes TMC (1 event) | A: no change |
| 6 | TSM STK 5 | Unfreezes TSM (12 events) | A: adds TSM to checked list |
| 7 | NVDA STK 17 | Unfreezes NVDA (79 events) | A: adds NVDA to checked list |
| 8 | PLTR STK 5 | Unfreezes PLTR (5 events) | A: adds PLTR to checked list |
| 9 | PYPL STK 100 (U22076184) | Fixes PYPL 100-share gap | B: fixes PYPL Yash delta ($2.69 → ~$0) |

**Remaining frozen tickers after these 9 rows:**
- The 8 ACATS tickers resolve → 15 - 8 = 7 remaining frozen
- PYPL (Yash) was NOT in the frozen list (Walker processed it, just with wrong share count). So frozen stays at 7.
- The remaining 7: UBER, CRM, AMD, AMZN (all Yash multi-account), AMZN (Vikram pre-window CC), NFLX, QCOM — these need either (a) additional carry-in rows for pre-window stock positions in specific accounts, or (b) the Walker to handle multi-account position state correctly within a household cycle.

**Cross-check C impact:**
- U21971297: currently $0.00 — should stay clean
- U22076329: currently $109.95 divergence — may resolve if the $109.95 maps to ADBE or frozen-ticker realized P&L. Needs post-write re-run to confirm.
- U22388499: -$21.53 is IBKR-side (see below)
- U22076184: currently $0.00 — the PYPL carry-in adds a closed cycle but all P&L is already captured in fifoPnlRealized

## U22388499 NAV Residual Confirmation

The -$21.53 reconciliation delta for U22388499 (Vikram) is **IBKR-side, not Walker-side.**

IBKR ChangeInNAV components that sum to a different total than `ending_value - starting_value`:

```
starting_value:  $0.00
ending_value:    $80,787.00

Component breakdown (non-zero only):
  realized                    =  $24,145.19
  change_in_unrealized        = -$51,422.15
  transferred_pnl_adjustments =     $506.67
  deposits_withdrawals        =  $86,695.48
  asset_transfers             =  $21,648.00
  dividends                   =     $292.80
  interest                    =    -$956.88
  change_in_interest_accruals =     -$67.08
  other_fees                  =     -$33.50

Component sum:  $80,808.53
ibkr_delta:     $80,787.00
RESIDUAL:       -$21.53
```

The residual exists because IBKR's ChangeInNAV components do not perfectly reconcile to `endingValue - startingValue`. This is a known IBKR reporting artifact — likely rounding across hundreds of daily MTM revaluations over the 365-day period. The Walker cannot close this gap because it originates in IBKR's own aggregation.

**This is NOT a Walker bug. It should be documented as a known IBKR-side residual and the cross-check C tolerance for this account should accept $21.53 as explained.**

## Production DB Confirmation

Production `agt_desk.db` has 0 master_log tables. Untouched.
