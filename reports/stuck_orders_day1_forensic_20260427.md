# Day 1 Stuck-Order Forensic — 2026-04-27

**Generated:** 2026-04-28T13:58 UTC  
**Analyst:** Coder B  
**Source:** proof_20260427 verdict=FAIL — non_terminal_past_next_close=11  
**DB:** C:\AGT_Telegram_Bridge\agt_desk.db (as of 2026-04-28T14:00 UTC)

---

## Executive Summary

The proof report flagged 11 non-terminal orders from the 2026-04-27 ET window
(`2026-04-27T04:00Z` → `2026-04-28T04:00Z`).  
All 11 are real Phase B orders — none are pre-migration artifacts.  
The report's `pre_migration_rows_excluded: 10` is a **proof_report.py bug**
(timezone mismatch; documented below).

**Two distinct bugs are causing stuck orders:**

| Bug | Affected | Status | Severity |
|-----|----------|--------|----------|
| B1: partial_fill not promoted to filled when fill_qty=qty | ids 428–437 (10 rows) | REAL BUG — orders economically complete but DB disagrees | High |
| B2: sent AAPL BUY ack/fill callback never received | id 438 (1 row) | REAL BUG — open or ghost order | Medium |

**Bonus defect (outside window):** id=411 (2026-04-20 AAPL SELL, 100/400 filled, pre-Phase-B, no engine) — orphan from before Phase B instrumentation.

---

## Proof Report Defect: pre_migration_rows_excluded=10

The report computes `migration_iso = MIN(staged_at_utc) WHERE engine IS NOT NULL` = `2026-04-27T13:36:12.275819+00:00` (UTC).  
The eligibility filter is `created_at >= migration_iso` (line ~136 in proof_report.py).  
**Problem:** `created_at` is stored as local ET (`2026-04-27T09:36:12`), not UTC.  
Text comparison: `'2026-04-27T09:36' >= '2026-04-27T13:36'` → FALSE.  
All 10 csp_allocator rows are misclassified as pre-migration.

**Effect on metrics:**
- `pre_migration_rows_excluded: 10` — inflated (should be 0)
- `pct_same_day_terminal: 66.67%` — computed over 3 eligible rows (ids 438-440) instead of 13 total
- `orders_missing_audit_evidence: 2` — applies to ids 439 (failed, no engine) and 440 (superseded, roll engine, no gate_verdicts)
- `non_terminal_past_next_close: 11` — correctly counts all 11 non-terminal rows (this query has NO migration_iso filter, so unaffected by the timezone bug)

**Fix needed:** `proof_report.py` should use `staged_at_utc` instead of `created_at`
for the migration_iso comparison, or normalize both to UTC before comparing.

---

## Cohort A: 10 csp_allocator CSP Sell Orders (ids 428–437)

**Run:** `5ca5712fb5bf476595798c9d4ef7ee17` — Phase B Day 1 CSP allocator run  
**Staged:** 2026-04-27T09:36:12 ET (13:36 UTC)  
**Broker:** Paper gateway (DUP751003 / DUP751004 / DUP751005)

### Root Cause

All 10 orders have `fill_qty = ordered_qty` — they are **fully filled** by IB's
paper trading engine. But `status` remains `partially_filled`.

The sequence:
1. IB paper engine sends `partially_filled` callback with partial fill
2. Bot updates `fill_qty = partial_amount`, `status = partially_filled`
3. IB paper engine sends additional fill callbacks completing the order
4. Bot **fails to promote** `status = filled` when `fill_qty >= ordered_qty`
   OR the final `filled` callback was received but not dispatched to the DB update

The lifecycle handler's partial-fill accumulator works, but the "promote to
filled when cumulative fill = ordered qty" check is absent or bugged.

### Per-Row Detail

| id  | Ticker | Account   | Strike | Expiry   | Qty | Fill Qty | Fill Price | ib_order_id | Acked           |
|-----|--------|-----------|--------|----------|-----|----------|------------|-------------|-----------------|
| 428 | ARM    | DUP751003 | 210.0  | 20260501 | 5   | 5        | 5.432      | 715         | 13:39:09 UTC    |
| 429 | ARM    | DUP751004 | 210.0  | 20260501 | 5   | 5        | 5.27       | 718         | 13:36:34 UTC    |
| 430 | ARM    | DUP751005 | 210.0  | 20260501 | 5   | 5        | 5.328      | 721         | 13:38:30 UTC    |
| 431 | EXPE   | DUP751003 | 240.0  | 20260501 | 4   | 4        | 1.8325     | 726         | 14:25:18 UTC    |
| 432 | EXPE   | DUP751004 | 240.0  | 20260501 | 4   | 4        | 2.01       | 729         | 14:34:17 UTC    |
| 433 | EXPE   | DUP751005 | 240.0  | 20260501 | 4   | 4        | 1.935      | 732         | 14:31:12 UTC    |
| 434 | INTC   | DUP751003 | 75.0   | 20260501 | 14  | 14       | 0.59       | 737         | 13:36:39 UTC    |
| 435 | INTC   | DUP751004 | 75.0   | 20260501 | 13  | 13       | 0.60       | 740         | 13:38:45 UTC    |
| 436 | INTC   | DUP751005 | 75.0   | 20260501 | 13  | 13       | 0.5862     | 743         | 13:38:30 UTC    |
| 437 | WDAY   | DUP751005 | 111.0  | 20260501 | 9   | 9        | 0.8822     | 748         | 14:08:02 UTC    |

All 10 are CSP sells (SELL PUT, paper gateway). Contracts sold = contracts ordered
in every case. Expiry May 1 2026.

### Disposition

- **Economically correct**: positions are open in IB paper accounts as expected
- **DB inconsistency only**: status stuck at `partially_filled` when should be `filled`
- **No financial risk** (paper mode)
- **Requires Architect approval** to UPDATE status → `filled` + log to `operator_interventions` (kind=`direct_sql`)
- **Requires code fix**: lifecycle handler must promote `partially_filled` → `filled` when `fill_qty >= ordered_qty`

---

## Cohort B: 1 Sent Order — AAPL BUY (id=438)

| Field | Value |
|-------|-------|
| id | 438 |
| Ticker | AAPL |
| Action | BUY |
| Account | DUP751004 |
| Strike | 250.0 |
| Expiry | 20260508 (May 8) |
| Qty | 4 |
| Limit price | 19.25 |
| ib_order_id | 1290 |
| created_at | 2026-04-27T15:31:35 ET |
| submitted_at_utc | 2026-04-27T19:31:37 UTC (3:31 PM ET) |
| acked_at_utc | None |
| last_ib_status | sent |
| engine | None |
| fill_qty | None |

### Analysis

This is a non-engine order (no `engine`, no `run_id`). Context from id=439 and
id=440 in the same account at near-identical timestamps:

- id=439: AAPL SELL, failed, no engine — same timestamp as 438 → likely a buy-to-close attempt that was routed as SELL (failed)  
- id=440: AAPL BTC, superseded, engine=roll, run_id=ec04b611... — roll engine issued this immediately after

Pattern: The roll engine (ec04b611) submitted a BTC (buy-to-close) order for AAPL at 15:33 ET,
which was marked `superseded`. The earlier BUY (id=438) at 15:31 ET was placed via a separate
code path (manual command?) and is stuck in `sent`.

The AAPL BUY at strike=250, expiry=May 8 would be buying back a CSP short — this is
a manual close attempt placed shortly before the roll engine's automatic supersede.

**Most likely scenario:** The manual BUY (id=438) was submitted to IB at 3:31 PM ET
and remains open on the paper gateway. The roll engine superseded it via id=440, but
the manual order's IB status was never updated (no cancel confirmation came back).

### Disposition

- **May be open on IB paper gateway** (ib_order_id=1290 at localhost:4002)
- Should be investigated: `reqOpenOrders()` or `reqAllOpenOrders()` against paper gateway
- If still open: cancel via IB to clean up
- If already closed (IB-side): manually update status to `cancelled` or `filled` + operator_interventions log
- **Requires Architect approval** before any DB write

---

## Bonus Orphan: id=411 (Outside Window, Pre-Phase-B)

| Field | Value |
|-------|-------|
| id | 411 |
| Ticker | AAPL |
| Action | SELL (CSP sell) |
| Account | DUP751004 |
| created_at | 2026-04-20T15:31:19 ET |
| ib_order_id | 608 |
| engine | None (pre-Phase B) |
| Qty | 400 |
| fill_qty | 100 |
| fill_price | 272.57 |
| last_ib_status | partially_filled |

A genuine pre-migration partial fill: 100/400 shares were sold, order stuck. No
Phase B columns (`staged_at_utc`, `engine`). Not counted in the Day 1 proof report
(outside the window). Exists in the DB as a permanent non-terminal orphan.

**Disposition:** Document for Architect; likely needs manual reconciliation. Since
pre-Phase-B, no gate_verdicts, no engine attribution — a direct_sql UPDATE to
`filled` (if 400 shares were eventually sold) or `partially_filled_terminal` if
only 100 were ever filled. Requires Flex data cross-reference and Architect decision.

---

## Summary Table

| id  | Created     | Ticker | Status           | Classification               | Urgent? |
|-----|-------------|--------|------------------|------------------------------|---------|
| 428 | 2026-04-27  | ARM    | partially_filled | B1: full fill missed, paper  | No      |
| 429 | 2026-04-27  | ARM    | partially_filled | B1: full fill missed, paper  | No      |
| 430 | 2026-04-27  | ARM    | partially_filled | B1: full fill missed, paper  | No      |
| 431 | 2026-04-27  | EXPE   | partially_filled | B1: full fill missed, paper  | No      |
| 432 | 2026-04-27  | EXPE   | partially_filled | B1: full fill missed, paper  | No      |
| 433 | 2026-04-27  | EXPE   | partially_filled | B1: full fill missed, paper  | No      |
| 434 | 2026-04-27  | INTC   | partially_filled | B1: full fill missed, paper  | No      |
| 435 | 2026-04-27  | INTC   | partially_filled | B1: full fill missed, paper  | No      |
| 436 | 2026-04-27  | INTC   | partially_filled | B1: full fill missed, paper  | No      |
| 437 | 2026-04-27  | WDAY   | partially_filled | B1: full fill missed, paper  | No      |
| 438 | 2026-04-27  | AAPL   | sent             | B2: open/ghost buy-to-close  | Medium  |
| 411 | 2026-04-20  | AAPL   | partially_filled | Pre-Phase-B orphan (bonus)   | Low     |

---

## Required Actions (Pending Architect Approval)

### Code Fixes

1. **B1 fix (telegram_bot.py):** In the IB `orderStatus`/`execDetails` callback,
   after accumulating `fill_qty`, check if `fill_qty >= ordered_qty` and if so,
   promote `status` to `filled`. This pattern may be in `_handle_ib_fill` or
   equivalent.

2. **B1 fix (proof_report.py):** Replace `created_at >= migration_iso` with
   `staged_at_utc >= migration_iso` (both UTC) to fix the timezone mismatch that
   causes `pre_migration_rows_excluded` to be inflated.

### DB Writes (Require Approval)

3. **UPDATE ids 428–437 → status='filled'** + operator_interventions entry
   (kind=`direct_sql`, detail="Day 1 forensic: fill_qty=ordered_qty confirmed,
   promoted to filled via direct_sql per stuck_orders_day1_forensic_20260427")

4. **Investigate id=438 via paper gateway** (`reqOpenOrders` on port 4002);
   update status based on IB state.

5. **Investigate id=411** via Flex data; reconcile fill status.
