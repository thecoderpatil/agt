# Sprint 14 P2 — CSP Approval-Gate Flow: Dispatch

**Date:** 2026-04-28
**Branch:** sprint-14-p2-approval-gate
**Target:** main (squash)

## Changes

Five files:

1. `scripts/migrate_sprint14_p2_approval_gate.py` (NEW) — creates `csp_ticker_approvals`,
   extends `operator_interventions.kind` CHECK with 4 new CSP kinds.

2. `agt_equities/csp_digest/formatter.py` — rename `csp_approve:`/`csp_reject:` callbacks
   to `cta_approve:`/`cta_reject:` (incl. `_all` variants), remove paper-mode early `[]`
   return so paper digests also emit audit-trail buttons.

3. `csp_digest_runner.py` — Fix C: ticker dedup in `build_digest_payload`.
   Add `CSP_TICKER_APPROVAL_TIMEOUT_MINUTES = 30`. Add `_record_live_ticker_approvals`
   (inserts per-ticker `pending` rows on LIVE digest send). Wire into `run_csp_digest_job`.

4. `agt_scheduler.py` — Fix B: `_csp_timeout_sweeper_job` at 10:10 ET Mon-Fri.
   Reads `AGT_CSP_TIMEOUT_DEFAULT` → sets `timeout_approved` or `timeout_rejected`.
   Logs to `operator_interventions`.

5. `telegram_bot.py` — Fix A: gate in `_auto_execute_staged` (LEFT JOIN on
   `csp_ticker_approvals`, passes paper rows + non-CSP + approved/timeout_approved).
   Add `handle_csp_ticker_callback` (cta_ buttons, operator_interventions logging,
   immediate execute on approve tap). Register handler.

## LOC expectation

```yaml expected_delta
files:
  scripts/migrate_sprint14_p2_approval_gate.py:
    added: 228
    removed: 0
    net: 228
    tolerance: 15
    required_symbols:
      - run
    required_sentinels:
      - csp_ticker_approvals
      - csp_timeout_auto_approve

  agt_equities/csp_digest/formatter.py:
    added: 8
    removed: 8
    net: 0
    tolerance: 5
    required_sentinels:
      - "cta_approve:"
      - "cta_reject:"
      - "cta_approve_all:"

  csp_digest_runner.py:
    added: 45
    removed: 1
    net: 44
    tolerance: 10
    required_symbols:
      - _record_live_ticker_approvals
    required_sentinels:
      - CSP_TICKER_APPROVAL_TIMEOUT_MINUTES
      - _seen_tickers
      - effective_run_id

  agt_scheduler.py:
    added: 56
    removed: 0
    net: 56
    tolerance: 10
    required_symbols: []
    required_sentinels:
      - csp_timeout_sweeper
      - csp_timeout_auto_approve
    id_strings:
      - csp_timeout_sweeper

  telegram_bot.py:
    added: 131
    removed: 1
    net: 130
    tolerance: 15
    required_symbols:
      - handle_csp_ticker_callback
    required_sentinels:
      - "cta_approve:"
      - csp_ticker_approve
      - broker_mode_at_staging
      - timeout_approved
```
