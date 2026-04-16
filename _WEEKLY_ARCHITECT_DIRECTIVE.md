# Weekly Architect Directive

**Issued:** 2026-04-16T10:00:00-07:00
**Expires:** 2026-04-21T16:00:00-07:00 (5 trading days)
**Author:** Opus Architect Review (confirmed by Yash)

---

## Status: ACTIVE

## Abort Conditions (auto-cancel this directive if ANY trigger)

- VIX moves +30% from directive-day close
- Any account NLV drops >5% from directive-day snapshot
- Circuit breaker trips (consecutive errors, NLV drop, reconciliation drift)
- Yash sends `/abort-directive` via Telegram

## Current Focus

**Primary:** Validate full Heitkoetter Wheel on paper accounts
- CC entry: VALIDATED (chimera test 2026-04-16)
- CSP entry: VALIDATED (131 contracts staged 2026-04-16)
- Harvest: operational, needs end-of-day validation pass
- Roll: engine built, needs live trigger observation
- Assignment detection: reconcile command built, needs paper assignment

**Secondary:** Accumulate session log data for readiness scoring

## Standing Orders (apply every task run)

1. Run circuit_breaker.py FIRST. If halted=True, skip all order activity.
2. Read _SAFETY_RAILS.md. Do not exceed any hard limit.
3. Check prior task results from autonomous_session_log. If last run had errors, investigate before proceeding.
4. RTH only: no scans or orders outside 9:30-16:00 ET.
5. Log every action to autonomous_session_log with full metrics JSON.

## This Week's Priorities (ranked)

1. **Run scan-daily at morning sweep** — CC + harvest + roll for all households
2. **Run scan-csp at morning sweep** — if VIX < 35 and accounts have margin
3. **Approve staged orders** — auto-approve paper orders that pass all gates
4. **Reconcile at every task boundary** — detect assignments, expirations, drift
5. **Log P&L coherence** — track filled vs expected, premium collected vs basis reduction

## Explicitly NOT Doing This Week

- No live account operations (ever, per _SAFETY_RAILS.md)
- No walker.py or flex_sync.py modifications
- No schema migrations
- No new feature development — pure validation

## Metrics to Track

- Orders staged per day (target: 5-15 meaningful, not 0 and not MAX)
- Fill rate (staged → filled %)
- Gate rejection rate per gate (which gates block most?)
- Reconciliation drift (should be 0% on paper)
- Error count per task run (target: 0)

## Notes for Next Architect Review

- Watch for paper account position buildup — do we need to manage paper capital?
- CSP allocator depends on v_available_nlv — verify NLV snapshots are fresh
- Roll engine needs a below-basis CC to trigger — may need to wait for market move
