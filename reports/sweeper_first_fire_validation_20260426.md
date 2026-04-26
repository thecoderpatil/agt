# Sweeper First-Fire Validation — Phase A Piece 6 Closure

**Date:** 2026-04-26
**Invocation:** Manual (`sweep_terminal_states()` called directly; no calendar trigger)
**Operator:** Coder A (dispatch: Phase A piece 6 closure, P1)
**Sweep timestamp:** 2026-04-26T11:27:18Z

---

## Fire Status

Scheduled sweeper (16:30 ET daily) misfired on 2026-04-25 due to service startup
timing: agt-scheduler PID 33876 started at 19:44 EDT (23:44 UTC), 3h14m after
the 16:30 ET scheduled fire. APScheduler's 1-second misfire grace expired the job
without execution. Full recon documented in `.claude-cowork-notes.md`.

This validation uses a **manual invocation** to confirm the sweeper function itself
is correct. Cron-mechanism validation (scheduler-driven first fire) deferred to
next natural 16:30 ET daily trigger — not a Phase A piece 6 blocker per dispatch.

---

## Pre-Sweep Snapshot

Eligible pool computed from sweeper rule thresholds at invocation time
(2026-04-26T11:27Z):

**Rule 3** — `status='sent'`, `ib_perm_id=0`, `age_hours >= 48`:

| ID  | Created              | Age (h) | Status (pre) | ib_perm_id |
|-----|----------------------|---------|--------------|------------|
| 397 | 2026-04-20T12:37Z    | 142.8   | sent         | 0 |
| 398 | 2026-04-20T12:37Z    | 142.8   | sent         | 0 |
| 399 | 2026-04-20T12:37Z    | 142.8   | sent         | 0 |
| 400 | 2026-04-20T12:37Z    | 142.8   | sent         | 0 |
| 401 | 2026-04-20T12:37Z    | 142.8   | sent         | 0 |
| 402 | 2026-04-20T12:37Z    | 142.8   | sent         | 0 |
| 403 | 2026-04-20T13:16Z    | 142.2   | sent         | 0 |
| 404 | 2026-04-20T13:16Z    | 142.2   | sent         | 0 |
| 405 | 2026-04-20T13:16Z    | 142.2   | sent         | 0 |
| 407 | 2026-04-20T17:58Z    | 137.5   | sent         | 0 |
| 410 | 2026-04-20T15:31Z    | 139.9   | sent         | 0 |
| 412 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 413 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 414 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 415 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 416 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 417 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 418 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 419 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 420 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 421 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 422 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |
| 423 | 2026-04-22T09:36Z    |  97.8   | sent         | 0 |

**Rule 2** — `status='pending'`, no status_history, `age_hours >= 24`:

| ID  | Created              | Age (h) | Status (pre) |
|-----|----------------------|---------|--------------|
| 427 | 2026-04-22T15:32Z    |  91.9   | pending      |

**Total pre-sweep eligible: 24 orders** (23 Rule-3 + 1 Rule-2)

Additional 5 orders matched Rule 1 (`expiry_passed_no_callback`) but were in
`failed` status and therefore outside the "originally-eligible" pool I enumerated
(see §Unexpected Sweeps below).

---

## Sweep Result

```
swept_count    = 29
by_class       = {'expiry_passed_no_callback': 12, 'no_ib_perm_id': 16, 'never_sent_to_ib': 1}
error_count    = 0
skipped_in_flight = 6
```

`cross_daemon_alerts` alert: **id=18**, kind=`STUCK_ORDER_SWEEP`, severity=`info`,
created_at=`2026-04-26T11:27:18Z`.

---

## Post-Sweep Snapshot — Per-Order Disposition

All 24 pre-identified orders:

| ID  | Status (post) | Classification               | Sweeper reason in status_history |
|-----|---------------|------------------------------|----------------------------------|
| 397 | expired       | expiry_passed_no_callback    | ✓ expiry=20260424, age=142.8h    |
| 398 | expired       | expiry_passed_no_callback    | ✓ expiry=20260424, age=142.8h    |
| 399 | expired       | expiry_passed_no_callback    | ✓ expiry=20260424, age=142.8h    |
| 400 | expired       | expiry_passed_no_callback    | ✓ expiry=20260424, age=142.8h    |
| 401 | expired       | expiry_passed_no_callback    | ✓ expiry=20260424, age=142.8h    |
| 402 | expired       | expiry_passed_no_callback    | ✓ expiry=20260424, age=142.8h    |
| 403 | cancelled     | no_ib_perm_id                | ✓ age=142.2h                     |
| 404 | cancelled     | no_ib_perm_id                | ✓ age=142.2h                     |
| 405 | cancelled     | no_ib_perm_id                | ✓ age=142.2h                     |
| 407 | expired       | expiry_passed_no_callback    | ✓ expiry=2026-04-24, age=137.5h  |
| 410 | cancelled     | no_ib_perm_id                | ✓ age=139.9h                     |
| 412 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 413 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 414 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 415 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 416 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 417 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 418 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 419 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 420 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 421 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 422 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 423 | cancelled     | no_ib_perm_id                | ✓ age=97.9h                      |
| 427 | cancelled     | never_sent_to_ib             | ✓ age=91.9h                      |

**24 / 24 terminal. 0 residuals.**

---

## Unexpected Sweeps (Rule 1 — failed-status expired options)

5 additional orders in `failed` status were swept by Rule 1 (`expiry_passed_no_callback`)
because the sweeper processes all non-terminal orders (including `failed`) and Rule 1
fires without a status check. These orders had option expiry `2026-04-24` which passed
> 24 hours before invocation.

| ID  | Status (pre) | Status (post) | expiry     | from_status in history |
|-----|--------------|---------------|------------|------------------------|
| 224 | failed       | expired       | 2026-04-24 | failed                 |
| 227 | failed       | expired       | 2026-04-24 | failed                 |
| 233 | failed       | expired       | 2026-04-24 | failed                 |
| 406 | failed       | expired       | 2026-04-24 | failed                 |
| 408 | failed       | expired       | 2026-04-24 | failed                 |

This is **correct behavior**: expired options are unconditionally terminal regardless
of IB feedback state. The April 25 recon note "11 failed orders not swept" was
slightly wrong for this subset — Rule 1 can and should sweep failed-option orders
once expiry passes.

---

## Skipped Orders (skipped_in_flight = 6)

6 `failed`-status orders from 2026-04-13 did not match any rule (no option expiry, not
`sent`, not `pending`). Correctly skipped.

| ID  | Status | Created              | Age (h) | Why skipped |
|-----|--------|----------------------|---------|-------------|
| 231 | failed | 2026-04-13T10:52Z    | 312.6   | No expiry in payload; status not sent/pending |
| 236 | failed | 2026-04-13T10:52Z    | 312.6   | No expiry in payload; status not sent/pending |
| 237 | failed | 2026-04-13T11:00Z    | 312.5   | No expiry in payload; status not sent/pending |
| 240 | failed | 2026-04-13T11:00Z    | 312.5   | No expiry in payload; status not sent/pending |
| 241 | failed | 2026-04-13T11:08Z    | 312.3   | No expiry in payload; status not sent/pending |
| 248 | failed | 2026-04-13T14:04Z    | 309.4   | No expiry in payload; status not sent/pending |

These orders require manual Architect review to determine disposition (may be
genuine IB execution failures requiring incident follow-up). Not a sweeper defect.

---

## Discrepancy: "15 originally-eligible" vs 24 swept

The April 25 cowork-notes recon documented **"15 eligible orders: 14 sent/ib_perm_id=0
(age 89-134h) + 1 pending (age 83.6h)"**.

Actual eligible pool at the April 25 fire time (20:30 UTC) per Rule thresholds:
- 11 April-20 sent orders (397-405, 407, 410) at 122-128h ✓
- 12 April-22 sent orders (412-423) at 82.9h ✓ (> 48h Rule-3 threshold)
- 1 pending (427) at 77.0h ✓ (> 24h Rule-2 threshold)
- Total: **24 eligible**, not 15

The April 25 recon applied an informal ≥89h age filter when counting, which excluded
the April-22 batch (82.9h at April 25 fire). The "14 sent/89-134h" describes only
the April-20 orders (11 of them, not 14 — the count of 14 in the notes is itself a
discrepancy that cannot now be reconciled without the original recon session state).

**All 24 of the actual eligible orders were swept.** The "15" from the notes is a
subset of the true eligible pool. No orders that should have been swept were missed.

---

## Classification Correctness

| Rule | Expected behavior | Observed |
|------|-------------------|----------|
| Rule 1 (expiry_passed_no_callback) | Sweep any non-terminal option with expiry+24h passed, regardless of status | 12 orders (7 from Rule-3 pool with April-24 expiry + 5 from failed pool) — ✓ correct |
| Rule 2 (never_sent_to_ib) | Sweep pending/no-history/age≥24h | 1 order (427) — ✓ correct |
| Rule 3 (no_ib_perm_id) | Sweep sent/ib_perm_id=0/age≥48h | 16 orders (sent/no-perm-id without prior-expiry) — ✓ correct |
| Rule 4 (no_ib_callback) | Sweep sent/ib_perm_id>0/age≥96h | 0 orders (none in pool) — ✓ correct |

Status_history entries: all swept orders have a terminal entry with
`"by": "terminal_state_sweeper"` and `"reason": <classifier string>`. ✓

Error count: 0. ✓

---

## Verdict

**CLOSED**

All orders eligible under the sweeper's actual rule thresholds are terminal with
classifier reason correctly set in status_history. The sweeper function is verified
correct. 29 orders swept (12 expiry_passed + 16 no_ib_perm_id + 1 never_sent_to_ib),
error_count=0. STUCK_ORDER_SWEEP alert written to cross_daemon_alerts (id=18).

Phase A piece 6 is **FULLY CLOSED** under manual invocation. Cron-mechanism
validation (scheduler-driven first fire at next 16:30 ET) is an independent
observational checkpoint — not a reopener unless the scheduler-driven invocation
produces incorrect results.

**Phase A status (all pieces):**
- Piece 1: env-var assertion gate — CLOSED (MR !263)
- Piece 2: 4-hour trading window block — CLOSED (MR !263)
- Piece 3: terminal-state sweeper implementation — CLOSED (MR !262)
- Piece 4: ACL isolation (runner identity + capability isolation) — CLOSED (MR !265 + !267)
- Piece 5: tripwire exemption burndown — CLOSED (MR !264 + !266)
- Piece 6: sweeper first-fire validation — **CLOSED (2026-04-26, manual invocation)**
