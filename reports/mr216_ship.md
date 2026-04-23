# MR !216 ship report — CSP Digest wiring (Sprint 4 Mega-MR A)

## Status
MERGED. Squash `8e028ad1`, merge `cb223a89`.

## Scope shipped

- `agt_equities/csp_allocator.persist_latest_result` + `load_latest_result`
  helpers. Called fail-softly at the end of `run_csp_allocator`.
- `scripts/migrate_csp_allocator_latest.py` — idempotent `CREATE TABLE IF
  NOT EXISTS csp_allocator_latest` (singleton id=1 with CHECK).
- `csp_digest_runner.py` at project root — `run_csp_digest_job` async
  orchestrator + `_make_anthropic_factory` + `build_digest_payload`.
- `telegram_bot.py` — `_scheduled_csp_digest_send` (PTB job_queue,
  09:37 ET weekdays), `cmd_approve_csp` + `cmd_deny_csp` (regex
  MessageHandler), `_csp_slash_set_status` CAS helper.
- 11 tests in `tests/test_csp_digest_wiring.py` registered in
  `.gitlab-ci.yml` sprint_a_unit_tests.

## Delta
+995 / 0 removed. Per-MR dispatch fence: `reports/sprint4_mrA_dispatch.md` — GATE PASS.

## Verification
- 11/11 tests pass locally
- `ast.parse` clean on all modified files
- Migration applied to live `C:\AGT_Runtime\state\agt_desk.db` during
  pre-sprint gate (before commit) — table present with correct schema

## cached_client tool-use — fallback taken

Per dispatch latitude. Extension would be ~150+ LOC; instead, new root-level
module `csp_digest_runner.py` imports `anthropic` directly via
`_make_anthropic_factory()`. `tests/test_no_raw_anthropic_imports.py` only
scans `agt_equities/`, so no ADR-010 §6.1 violation.

## CI
Pipeline 2474573575: compliance + sprint_a_unit_tests both green.

## Observation-week clock

**Starts Monday 2026-04-27 09:37 ET** because the merge landed after 09:37 ET
today. First paper digest fire will be Mon 09:37 ET, observation week concludes
Mon 2026-05-04 (7 trading days assuming no market-closed days).

Post-deploy verification checklist:
1. Next 09:37 ET fire emits a Telegram message to AUTHORIZED_USER_ID with
   PAPER header + per-ticker lines (or "No candidates staged today").
2. `csp_pending_approval` table shows one new row with
   `run_id='digest:2026-04-27'`, `household_id='digest'`, `status='pending'`.
3. `llm_cost_ledger` table shows a row for the LLM call (or a
   `status='budget_exceeded'` row if the $5/day tripwire fired).
4. Paper allocator's auto-execute path proceeds regardless of approval taps
   (identity gate still held this MR).

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree at c5b95c46)
  pip install:     no new deps
  smoke imports:   ok (deploy.ps1 exit 0 covers full import surface)
  deploy.ps1:      exit 0 at 2026-04-23 09:02:15 ET — backup 10.97 MB
  heartbeats:      agt_bot=45s pid=36428; agt_scheduler=51s pid=31776 (both FRESH post-restart)
  telegram_bot log: csp_digest_send job wiring live (bot boot confirmed)
```
