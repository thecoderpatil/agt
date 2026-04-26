# Sprint 13 — loc_gate hardening + operator_interventions CHECK + heartbeat retention

**TL;DR (plain English):** Three follow-up fixes bundled atomically. Close the precommit_loc_gate AST gap so AnnAssign-declared constants (VALID_KINDS, NEW_COLUMNS, etc.) are caught by required_symbols and add an id_strings dispatch key for scheduler job-id strings. Add a DB-level CHECK constraint to operator_interventions.kind via MR !268-grade table-recreate migration. Ship a 30-day rolling archive job for daemon_heartbeat_samples to bound retention.

**Coder effort:** medium. Design + recon already done by Coder B; this is execution.

**Target base:** origin/main tip post-MR-!270 LOCAL_SYNC = `ebd6f0b7`. Verify via fetch.
**Branch:** `sprint-13-followups`
**Target MR iid:** !271 (verify at branch-create).
**Status:** DRAFT — paste to Coder A after their current task slot is free (post-Coder-A-validation-window-close, post-GitLab-Ultimate-close-out).

---

## Source documents (Coder reads both)

1. **Design doc** — `reports/sprint_13_design_draft_20260426.md` — full per-fix detail with code snippets, prohibitions, migration runbook (8 steps), risk assessment, ADR backlog status verification.
2. **Cross-references** — `project_loc_gate_ast_walker_gaps_20260426`, `feedback_loc_estimate_indentation_churn`, `reports/mr268_phase_b_foundation_ship_20260426.md` (migration discipline pattern).

This dispatch adds: formal expected_delta, commit message, CI delta, verification block, report format. Scope detail lives in the design doc; do NOT duplicate.

---

## Architect decisions (locked in design doc — restated)

- Fix 2 CHECK constraint: **table-recreate pattern**, MR !268-grade discipline (VACUUM INTO backup + integrity_check pre/post + dry-run + apply + table_info snapshot).
- Fix 3 retention: **SQLite archive table** mirroring `autonomous_session_log_archive`, 30-day rolling, weekly Sunday 03:00 ET cron in `agt_scheduler.py`.
- Tier: **CRITICAL single MR** (no split). `scripts/migrate_*.py` + `agt_scheduler.py` both trigger CRITICAL.
- ADR backlog A+B+E all shipped — no carry-forward.

---

## expected_delta (LOC gate)

Per Coder B's churn analysis: no block re-indentation; new files have all lines as `insert` operations; counts are exact.

```yaml expected_delta
files:
  scripts/precommit_loc_gate.py:
    added: 9
    removed: 0
    net: 9
    tolerance: 3
    required_sentinels: ["ast.AnnAssign", "id_strings"]
  scripts/migrate_operator_interventions_kind_check.py:
    added: 151
    removed: 0
    net: 151
    tolerance: 25
    required_sentinels: ["VACUUM INTO", "tx_immediate", "PRAGMA integrity_check", "CHECK(kind IN", "idx_oi_occurred", "idx_oi_kind_occurred", "idx_oi_target", "_already_has_check", "SKIP"]
  scripts/migrate_heartbeat_samples_archive.py:
    added: 39
    removed: 0
    net: 39
    tolerance: 8
    required_sentinels: ["daemon_heartbeat_samples_archive", "CREATE TABLE", "tx_immediate", "SKIP"]
  agt_scheduler.py:
    added: 44
    removed: 0
    net: 44
    tolerance: 8
    required_sentinels: ["_heartbeat_archive_job", "daemon_heartbeat_samples_archive", "heartbeat_archive", "day_of_week"]
  tests/test_sprint13_design.py:
    added: 70
    removed: 0
    net: 70
    tolerance: 15
    required_sentinels: ["sprint_a", "test_loc_gate_annassign_handled", "test_loc_gate_id_strings_key", "test_operator_interventions_migration_idempotent", "test_heartbeat_archive_job_registered"]
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 1
    required_sentinels: ["test_sprint13_design"]
```

Total estimated net: ~219 LOC across 6 files. Tolerance on the migration scripts bumped to 25-30% per `feedback_loc_overrun_proof_report_modules` (full-safety-scaffold migrations historically run 30-40% over).

---

## Commit message (canonical, ready for squash)

```
Sprint 13: loc_gate hardening + operator_interventions CHECK + heartbeat retention

Closes three follow-ups from prior sprints:

- scripts/precommit_loc_gate.py: AnnAssign branch in collect_top_level_symbols
  (closes AST gap from MR !268 — VALID_KINDS, NEW_COLUMNS, US_MARKET_HOLIDAYS
  now visible to required_symbols). New id_strings dispatch key for scheduler
  job-id strings (no AST representation, distinct from required_sentinels by
  intent).

- operator_interventions.kind: DB-level CHECK constraint via table-recreate
  pattern (SQLite doesn't support ALTER TABLE ADD CONSTRAINT for existing
  columns). MR !268-grade migration discipline: VACUUM INTO backup +
  integrity_check pre/post + dry-run + apply + table_info snapshot. Codifies
  VALID_KINDS application invariant in the schema. Three indexes recreated
  inside the same tx_immediate.

- daemon_heartbeat_samples 30-day rolling archive into new
  daemon_heartbeat_samples_archive table. Weekly cron Sunday 03:00 ET in
  agt_scheduler.py. Pattern mirrors autonomous_session_log_archive. First
  effective archive 2026-05-26+ (table only has data from 2026-04-26).

ADR backlog 2026-04-19 ruling items A+B+E all confirmed shipped during
prior sprints (csp_decisions/decision_outcomes; error-budget columns +
v_error_budget_72h view; CachedAnthropicClient). No carry-forward.

Refs: project_loc_gate_ast_walker_gaps_20260426,
      sprint_13_design_draft_20260426.md,
      mr268_phase_b_foundation_ship_20260426.md (migration pattern).
```

---

## Expected CI delta

Baseline post-MR-!270: **1454 passed / 6 failed / 1 skipped / 8 deselected** (pipeline 2480704757; pre-existing test_news_adapters API-key failures unchanged).

Expected post-merge main: **+4 passed → 1458 / 6 / 1 / 8**. Same 6 failed (pre-existing). Four new tests in `test_sprint13_design.py`.

Branch CI will likely show baseline parity (per `feedback_merge_request_event_stale_config` — new test files in the MR's `.gitlab-ci.yml` activate only on post-merge main pipeline). Coder must capture post-merge main pipeline result in ship report.

---

## Verification (pre-commit)

For each modified .py file:
1. `python -c "import ast; ast.parse(open('<file>').read())"` — syntax.
2. `wc -l <file>` — byte-length vs expected_delta.
3. Sentinel grep: confirm every `required_sentinels` entry present.
4. **Run precommit_loc_gate before any commit:**
```
python scripts/precommit_loc_gate.py \
    --dispatch reports/sprint_13_dispatch_20260426.md \
    --staged /tmp/precommit_loc_gate.py,/tmp/migrate_operator_interventions_kind_check.py,/tmp/migrate_heartbeat_samples_archive.py,/tmp/agt_scheduler.py,/tmp/test_sprint13_design.py,/tmp/.gitlab-ci.yml
```
Note: Coder runs the gate BEFORE its own AnnAssign + id_strings additions land. The gate must pass against this dispatch using the OLD (pre-Fix-1) code path. Once shipped, future dispatches benefit from the new key.

Halt on divergence without `shrinking:` clause.

5. Local pytest on the new test file:
```
.\.venv\Scripts\python.exe -m pytest tests/test_sprint13_design.py -v
```
Expect 4/4 passed.

---

## Migration runbook (Fix 2 — MR !268-grade)

Per design doc Section "Migration Runbook" Steps 1-8. Coder executes verbatim post-merge:

1. Post-merge sync + `nssm stop` both services.
2. Backup-via-API (handled inside migration script `--apply` flag → `VACUUM INTO`).
3. `--dry-run` migration → review printed DDL + row count.
4. `--apply` migration (script does its own integrity_check pre/post; aborts on non-`ok`).
5. Independent `PRAGMA integrity_check` post-apply → expect `ok`.
6. `PRAGMA table_info(operator_interventions)` snapshot + `sqlite_master` DDL snapshot to `reports/mr<iid>_post_migration_table_info.txt` — confirm "CHECK" appears.
7. Run `migrate_heartbeat_samples_archive.py` (no dry-run; idempotent CREATE IF NOT EXISTS) → expect `DONE: daemon_heartbeat_samples_archive created`. Then `deploy.ps1 -SourcePath C:\AGT_Telegram_Bridge\.worktrees\coder` → atomic 3-slot rotation + NSSM restart.
8. Verify CHECK constraint active: attempt INSERT with invalid kind, expect `sqlite3.IntegrityError`.

If integrity_check fails at any step OR INSERT-with-invalid-kind doesn't raise IntegrityError: nssm stop → restore from `agt_desk_<ts>_pre_kind_check.db` backup → restart → file incident → STOP and surface to Architect.

---

## Report format (Coder ship report — required fields)

Standard ship-report block per CLAUDE.md, plus:
- `MIGRATION_LOG`: full output of `migrate_operator_interventions_kind_check.py --apply` (pre/post integrity_check, row count, DDL snippet) + output of `migrate_heartbeat_samples_archive.py`.
- `BACKUP_PATH`: path of pre-migration backup file.
- `TABLE_INFO_SNAPSHOT`: contents of `reports/mr<iid>_post_migration_table_info.txt` (PRAGMA table_info + sqlite_master DDL).
- `CHECK_CONSTRAINT_VERIFICATION`: result of Step 8 — confirm `IntegrityError` raised on invalid-kind INSERT.
- `ARCHIVE_TABLE_VERIFICATION`: `daemon_heartbeat_samples_archive` exists, schema matches.
- `HEARTBEAT_ARCHIVE_JOB_REGISTERED`: confirm job id `heartbeat_archive` in registered list.
- `KNOWN_DEFERRALS`: anything cut from spec that requires follow-up MR.

Ship report file: `reports/mr<iid>_sprint_13_ship_20260426.md`.

---

## Standing prohibitions

- **`agt_equities/walker.py`** — pure function, never mutate.
- **`agt_equities/flex_sync.py`** — outside Decoupling Sprint A scope.
- **`agt_equities/order_lifecycle/operator_ledger.py`** — application-level VALID_KINDS stays as-is; DB CHECK mirrors it (no source-of-truth change in Python).
- **`telegram_bot.py`** — bot-side attested-sweeper DEFERRED at line ~21858 stays as-is (deferred to A5e cutover per Sprint 12 P1).
- **`agt_equities/rule_engine.py`** — `sweep_stale_dynamic_exit_stages` Option B refactor still deferred.

---

## Notes for Coder A

- Pace > polish. Bundle into one squash, commit message above is canonical.
- Migration safety is non-negotiable — backup-before-migrate-before-restart per MR !268 pattern. If integrity_check fails at any point, halt and surface.
- Verify origin/main tip via fetch before branch-create. Pull file bytes via GitLab raw API, NOT main worktree (per `feedback_main_worktree_stale_read`).
- Per `feedback_loc_estimate_indentation_churn`, no churn expected on these fixes (Coder B confirmed no block re-indentation). Estimates should be exact.
- Per `feedback_loc_overrun_proof_report_modules`, migration scripts have historical +30-40% LOC overrun. Tolerance bumped accordingly. If actual diverges further, surface and use shrinking clause.
- The gate's AnnAssign + id_strings additions land in this MR. The gate itself runs against this MR's dispatch using the OLD code (no AnnAssign, no id_strings) — expected_delta uses only `required_sentinels`, no AnnAssign-dependent or id_strings-dependent enforcement. After this ships, future dispatches can use both keys.

---

**End of dispatch. ~219 LOC across 6 files. ~3-5 hours Coder execution including migration runbook. CRITICAL tier (service restart required for `agt_scheduler.py` change + migrations).**
