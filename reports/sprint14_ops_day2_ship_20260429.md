DISPATCH: Sprint 14 ops follow-up + Day 2 verdict pull
STATUS: applied
TIER: STANDARD (no code change; ops + DB writes logged)
DATE: 2026-04-29

---

## Task 1 — AGT_CSP_TIMEOUT_DEFAULT machine env var

ACTION: Set Machine-level env var for paper auto-approve (Option C)

```powershell
[Environment]::SetEnvironmentVariable("AGT_CSP_TIMEOUT_DEFAULT", "auto_approve", "Machine")
[Environment]::GetEnvironmentVariable("AGT_CSP_TIMEOUT_DEFAULT", "Machine")
# → auto_approve
```

RESULT: DONE — confirmed read-back `auto_approve` at Machine scope.

---

## Task 2 — agt-scheduler restart

`nssm restart agt-scheduler` threw SERVICE_STOP_PENDING warning on first call (service
was slow to stop). Followed with explicit `nssm start`. Final state:

- Service status:  Running
- Heartbeat post-restart:  agt_scheduler=2026-04-29T12:10:24+00:00  (~0s lag)
- agt_bot heartbeat:       2026-04-29T12:10:21+00:00  (unaffected)

Sweeper at 10:10 ET will see AGT_CSP_TIMEOUT_DEFAULT=auto_approve from the restarted
process environment.

---

## Task 3 — Day 2 proof-report verdict (2026-04-28)

File: `C:/AGT_Runtime/bridge-current/reports/proof_20260428.md`
Generated: 2026-04-29T11:30:00.063Z (07:30 ET cron fired on time)

**Verdict: PASS_NO_ACTIVITY**
Rationale: all metrics green; zero engine activity

| Metric                            | Value    | Status |
|-----------------------------------|----------|--------|
| route_mismatches                  | 0        | PASS   |
| non_terminal_past_next_close      | 0        | PASS   |
| pct_same_day_terminal             | 100.0    | PASS   |
| stale_strike_submissions_succeeded| 0        | PASS   |
| stale_quote_submissions_succeeded | 0        | PASS   |
| direct_db_or_manual_interventions | 0        | PASS   |
| orders_missing_audit_evidence     | 0        | PASS   |
| sweeper_accumulated_stuck_over_24h| 0        | PASS   |
| tier_0_or_tier_1_incidents        | 0        | PASS   |
| heartbeat_gaps_over_180s          | 0        | PASS   |
| walker_reconstruction_defects     | 0        | PASS   |

Engine activity: 0 decisions / 0 staged / 0 submitted — expected (CSP scan was dead
on 04-28 due to UnboundLocalError fixed by MR !277).

Day 2 PASS_NO_ACTIVITY is the expected base-case: no new regressions introduced.
Phase B Day 1 FAIL (11 stuck orders) = pre-migration legacy backlog, now resolved below.

---

## Task 4 — Manual DB cleanup: 428-437 partially_filled → filled

Target: `C:\AGT_Runtime\state\agt_desk.db` (runtime DB, not dev fixture)

**Pre-conditions verified:**
- Rows 428-437: all status='partially_filled', tickers ARM/EXPE/INTC/WDAY
- Architect rationale: fill callbacks lost during MR !269/!278 transition; IB will not re-emit

**Log-first (operator_interventions id=1):**
```sql
INSERT INTO operator_interventions (occurred_at_utc, operator_user_id, kind,
  target_table, before_state, after_state, reason, notes)
VALUES ('2026-04-29T12:08:32.896Z', 'Architect-via-Coder', 'direct_sql',
  'pending_orders', 'partially_filled', 'filled',
  'Architect-approved: 2026-04-27 partial-fill callbacks lost during MR !269/!278 transition...',
  '{"ids":[428,429,430,431,432,433,434,435,436,437],"count":10}');
```

**UPDATE:**
```sql
UPDATE pending_orders SET status='filled'
WHERE id IN (428,429,430,431,432,433,434,435,436,437) AND status='partially_filled';
-- rows_affected = 10
```

**Post-verification:**
```
428|filled|ARM   429|filled|ARM   430|filled|ARM
431|filled|EXPE  432|filled|EXPE  433|filled|EXPE
434|filled|INTC  435|filled|INTC  436|filled|INTC
437|filled|WDAY
```

RESULT: 10 rows promoted to filled. operator_interventions logged (id=1).

---

## Task 5 — Probe id=438 (AAPL BUY LMT, ib_order_id=1290)

Row details:
- ticker: AAPL, sec_type: OPT, action: BUY, qty: 4, limit_price: 19.25
- expiry: 20260508, strike: 250.0, right: C
- account: DUP751004 (Paper-Yash)
- strategy: WHEEL-7 Liquidate BTC, mode: LIQUIDATE
- created_at: 2026-04-27T15:31:35 (near market close)
- payload flag: transmit=false

**Gateway probe (port 4002, clientId=98):**
- open_trades_count: 0
- ORDER_1290_NOT_IN_OPEN: True
- all_trades_count: 0
- completed_orders_count: 0
- ORDER_1290_NOT_IN_COMPLETED_EITHER: True
- executions_for_1290: 0

**Conclusion:** Order 1290 is absent from all gateway order lists. No fill execution
recorded. Day order created at 15:31 ET with `transmit=false` — IB treats these as
held orders tied to the API session. Session disconnect at market close → order silently
dropped by IB paper simulation. No fill occurred.

**Action: mark cancelled** (per dispatch: "If errored/cancelled → mark cancelled")

**Log-first (operator_interventions id=2):**
```sql
INSERT INTO operator_interventions (..., kind='direct_sql', target_table='pending_orders',
  target_id=438, before_state='sent', after_state='cancelled',
  reason='Paper gateway probe: order 1290 absent from open, completed, and executions...',
  notes='{"ib_order_id":1290,"probe_port":4002,"probe_date":"2026-04-29"}');
```

**UPDATE:**
```sql
UPDATE pending_orders SET status='cancelled', last_ib_status='cancelled',
  notes='Probe 2026-04-29: order absent from paper gateway open/completed/executions...'
WHERE id=438 AND status='sent';
-- rows_affected = 1
```

**Post-verification:** id=438 status=cancelled, last_ib_status=cancelled ✓

RESULT: Row 438 cancelled. operator_interventions logged (id=2).

---

## Summary

| Task | Result |
|------|--------|
| AGT_CSP_TIMEOUT_DEFAULT=auto_approve | DONE — Machine scope verified |
| agt-scheduler restarted | DONE — Running, heartbeat 12:10Z |
| Day 2 proof-report | PASS_NO_ACTIVITY — all 11 metrics green |
| rows 428-437 → filled | DONE — 10 rows, logged op_interventions id=1 |
| row 438 → cancelled | DONE — 1 row, logged op_interventions id=2 |

Phase B standing: Day 1 FAIL (pre-migration stuck orders) now cleared. Day 2
PASS_NO_ACTIVITY (scan was dead). Day 3 (2026-04-30) should be first real activity
day with MR !277 fix live and auto-approve enabled.
