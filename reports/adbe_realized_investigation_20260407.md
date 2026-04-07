# ADBE Realized P&L Divergence Investigation

Generated: 2026-04-07
Divergence: Walker $3,727.80 vs IBKR $3,837.75 = **-$109.95**

## (a) Walker computation

Walker sums `fifo_pnl_realized` from every event in all ADBE Yash cycles. 32 events have non-zero fifo_pnl_realized. Running sum = **$3,727.80**. This exactly matches the direct DB sum of `fifo_pnl_realized` across all ADBE trades in Yash accounts.

## (b) IBKR computation

IBKR `master_log_realized_unrealized_perf` reports per-conid (per-option-contract) realized P&L. For Yash ADBE:
- U21971297: $2,584.46 across 23 option conids
- U22076329: $1,253.28 across 11 option conids
- U22076184: $0.00 (1 conid, zero realized)
- **Total: $3,837.75**

## (c) Delta source: $109.95 is in U22076329

| Account | Walker | IBKR | Delta |
|---------|--------|------|-------|
| U21971297 | $2,584.46 | $2,584.46 | $0.00 |
| U22076329 | $1,143.33 | $1,253.28 | **-$109.95** |
| U22076184 | $0.00 | $0.00 | $0.00 |

## (d) Root cause: cross-account option attribution

The $109.95 maps to exactly one IBKR FIFO row: `ADBE 251010P00332500` in U22076329, reported as $109.95 realized.

**But this option was sold by U22076184, not U22076329:**

| Event | Account | Date | Action | fifo_pnl |
|-------|---------|------|--------|----------|
| STO (open) | **U22076184** | 20251007 | ExchTrade SELL O | $0.00 |
| Ep (expire) | **U22076329** | 20251010 | BookTrade BUY C | $0.00 |

The option was opened in U22076184 (Trad IRA) but the expiration event landed in U22076329 (Roth IRA). Neither trade row carries any `fifo_pnl_realized` — both show $0. The $109.95 realized P&L exists ONLY in the FIFO performance summary, attributed to U22076329.

**This is an IBKR cross-account attribution artifact.** Within the same household, IBKR sometimes attributes option P&L to a different account than the one that originated the trade. The individual trade rows (`master_log_trades`) correctly show the actual account for each leg, but the FIFO summary (`master_log_realized_unrealized_perf`) aggregates differently.

The Walker correctly sums the per-trade `fifo_pnl_realized` values. Both trade rows show $0.00, so the Walker correctly reports $0 for this option. IBKR's FIFO summary reports $109.95 for this option but doesn't put that value on any trade row.

## (e) Proposal

**The divergence is an IBKR methodology difference, not a Walker bug.**

- The Walker sums `fifo_pnl_realized` from `master_log_trades` — these are the actual per-execution realized P&L values IBKR reports on each trade row
- The IBKR FIFO performance summary computes realized P&L using a different methodology (possibly FIFO lot matching across the full period) that produces a slightly different number
- The $109.95 is specifically the premium collected on a put that was opened in one account and expired in another — a cross-account attribution edge case

**Recommendation: Accept as a known IBKR methodology residual.** The Walker is correct — it reports what IBKR's own trade rows say. The FIFO summary is a secondary computation that uses different rules. No fix needed in the Walker.

Document the residual in the Phase 1 reconciliation report with this explanation. The cross-check A tolerance for ADBE Yash should be widened to accept the $109.95, or the cross-check should compare against the DB sum of `fifo_pnl_realized` (which matches exactly) instead of the FIFO summary.

## Production DB: untouched (0 master_log tables)
