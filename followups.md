# AGT Equities — Followups

**Purpose:** Long-lived, append-only log of known issues, technical debt, and deferred work that must survive HANDOFF_ARCHITECT bumps. Items are numbered, never renumbered, never deleted. Closed items get marked `[CLOSED]` with the closing commit SHA but remain in the file for audit history.

**Triage cadence:** Sprint boundaries. Architect reviews open items when drafting the next HANDOFF_ARCHITECT_vN.md and promotes anything sprint-scoped into that handoff's "Next Sessions" block.

**Authoring rules:**
- Append at bottom only. Never renumber.
- Each item: title, severity, discovered-in context, diagnosis, fix options, priority, owner.
- Closed items: prepend `[CLOSED — <sha>]` to the title. Add a closing note. Do not delete.
- Every `HANDOFF_ARCHITECT_vN.md` Knowledge Base Delta section must reference this file in the KEEP list.

---

## Followup #1 — `tests/test_phase3a.py::test_fail_closed_no_data` cache pollution

**Severity:** Low (test fragility; no deck or production risk)
**Discovered:** 2026-04-10, during ADR-005 STEP 8.7 full-suite verification
**Origin commit:** Pre-dates ADR-005. Verified via five-experiment matrix on dirty/clean HEAD × cache-present/cache-cleared combinations.

### Diagnosis

`evaluate_rule_7` reads from `agt_desk_cache/corporate_intel/` via `_get_cached_earnings_date()`. The cache directory is populated as a side effect of other tests in the suite running before `test_fail_closed_no_data`. When the cache contains a fresh `MSFT_calendar.json`, MSFT evaluates GREEN instead of RED (earnings date is outside the R7 14-day window) and the test's "both tickers must be RED" assertion fails.

Root cause: no test isolation between corporate_intel cache writers and `test_fail_closed_no_data`. The rule being tested (R7 fail-closed semantics) is real evaluator code and is still behaviorally correct in production — only the test's assertion is fragile.

### Fix options

1. Scope the cache behind a pytest fixture using `tmp_path` or `monkeypatch.setenv` on whatever env var `_get_cached_earnings_date` honors for its base path, so each test gets a fresh cache dir.
2. Gate `test_fail_closed_no_data` on an explicit `monkeypatch` that zeroes out the cache lookup for that test's duration.
3. Find the test that populates the cache mid-suite and move its writes to `tmp_path` so nothing escapes the test boundary.

### Phase 2 relevance

This is the same class of bug we'll hit in RIA multi-tenancy — shared disk cache not keyed by tenant. A cache hit for `MSFT_calendar.json` populated by tenant A will silently feed tenant B's evaluator with tenant A's data. The Phase 2 scoping should treat the corporate_intel cache as a tenant-scoped resource (either keyed by tenant or fully rebuilt per-tenant), not as a shared disk cache.

**Priority:** Low for test fix in isolation. Medium-High when Phase 2 multi-tenant RIA work begins — at that point this becomes a tenant isolation bug, not just a test hygiene issue.

**Owner:** Unassigned. Likely folded into Phase 2 corporate intel cache scoping work.

---

## Followup #2 — [CLOSED — SUPERSEDED by ADR-014] `_get_current_desk_mode` cross-tenant mode bleed

**Severity:** Medium (tenant isolation risk, Phase 2 blocker)
**Discovered:** 2026-04-10, during ADR-005 STEP 7b V2 router test fixing
**Origin commit:** Pre-dates ADR-005. Structural.

### Diagnosis

`telegram_bot._get_current_desk_mode()` calls `agt_equities.mode_engine.get_current_mode(conn)` which reads from a single global `mode_history` table. There is no tenant scoping on the read path. In a multi-tenant RIA architecture where tenant A is in WARTIME and tenant B is in PEACETIME, every mode-gated surface across the codebase (`_pre_trade_gates` Gate 1, scheduled job pause logic, Cure Console banner, Smart Friction Integer Lock activation) will read the single global mode and apply it to all tenants.

This is the companion bug to Followup #1 — both are "process-global state with no tenant key" issues. The surfaces are different (cache file vs SQLite table) but the class is the same.

The symptom that surfaced the bug: `tests/test_v2_state_router.py::test_router_stages_state3_defend_roll` was unexpectedly reading WARTIME from the test DB state. The test DB had a WARTIME row in `mode_history` left over from an earlier test that exercised mode transitions. `_get_current_desk_mode()` fell through to the latest row regardless of test context. The fix in ADR-005 STEP 7b was to monkeypatch the mode explicitly in each affected test. The underlying cross-scope bleed was NOT fixed — it's filed here for Phase 2.

### Fix options

1. **Tenant-scoped mode engine.** Add `household_id` (or `tenant_id` in the RIA model) to `mode_history` schema. `get_current_mode(conn, tenant_id)` becomes the canonical accessor. All callers must pass a tenant ID.
2. **Derived per-tenant mode.** Compute mode from per-tenant rule evaluations on-demand rather than storing transitions in a shared table.
3. **Short-term mitigation only.** Document that single-tenant operation is the only supported mode until Phase 2 lands. Do nothing else. (Current state.)

### Phase 2 relevance

This is a blocker for the RIA pivot. You cannot multi-tenant the desk without first tenant-scoping the mode engine. Fix option 1 is the minimum viable fix; option 2 is the architecturally cleaner version and should be evaluated against whether mode transitions need historical persistence for audit.

**Priority:** Medium now, Critical when Phase 2 begins.

**Owner:** Unassigned. Phase 2 scope.

---

## Followup #3 — Walker spinoff (SO) and demerger (DW) handling unimplemented

**Severity:** Medium (blocks RIA onboarding for clients with legacy spinoff-prone positions)
**Discovered:** 2026-04-10, during Phase 2 intel gathering (Track 2)
**Origin commit:** Pre-dates ADR-005. Structural gap in walker's corporate action handler.

### Diagnosis

`walker.py` handles these IBKR corp action types: split (ratio * shares), SD (special dividend basis reduction), TC/IC (ticker/CUSIP change no-op), CM/TM (cash/tender merger position close). It does NOT handle:

- **SO (Spinoff)** — `logger.warning("Walker: corp action SO on %s — spinoff handling TBD")`. Cycle state is not updated. The walker silently drops the event's economic impact on basis.
- **DW (Demerger)** — same pattern, same warning, same silent drop.

For Yash/Vikram's current positions this is likely latent risk, but for RIA onboarding with legacy positions (AT&T Warner Bros Discovery spinoff, 3M Solventum, GE Healthcare/Vernova, etc.) the bot will silently mis-classify cycle state the first time a client holds a position that spins off.

### Fix options

1. Implement proper SO handling: new cycle_seq for the spinoff child, allocate a fraction of parent basis to child per the corporate action's cost basis allocation ratio (sourced from IBKR Flex or manual input), keep parent basis at remainder.
2. Mark positions with pending spinoffs as NEEDS_MANUAL_REVIEW and pause V2 router + Rule 8 staging until manually reconciled.
3. Option 2 as a short-term stopgap, option 1 as the permanent fix.

**Priority:** Low now (Yash/Vikram don't have spinoff exposure as of 2026-04-10), Medium-High before first RIA client onboards.

**Owner:** Unassigned. Workstream C (RIA onboarding) blocker.

---

## Followup #4 — Walker same-day drift window during market hours

**Severity:** Low-Medium (affects intraday V2 router classifications on same-day-active positions)
**Discovered:** 2026-04-10, during Phase 2 intel gathering (Track 2)
**Origin commit:** Structural. Walker reads from `master_log_trades` which is written by `flex_sync_eod` at 5pm ET only.

### Diagnosis

V2 router reads walker via `_load_premium_ledger_snapshot` with `READ_FROM_MASTER_LOG=True`. Walker reconstructs cycles from `master_log_trades` + `inception_carryin`. `master_log_trades` is populated exclusively by `flex_sync_eod` scheduled at 5pm ET.

Consequence: during market hours, walker sees "yesterday at 5pm" state. If Yash rolled a position at 10am (BTC old short call + STO new short call), walker doesn't see the new cycle state until tomorrow's 5pm sync. The intraday V2 scan at 2pm will classify the position against yesterday's basis, premium, and open legs.

Mitigating factors:
- Same-day activity on a specific ticker being evaluated by V2 router is rare outside active roll scenarios
- V2 router STATE_1/STATE_3 classifications are triggered on already-open positions, not positions actively being rolled
- Operator is usually aware of same-day activity and would not blindly `/approve` a contradictory V2 staging

### Fix options

1. **Same-day delta reconciliation inside walker.** Walker reads master_log_trades for authoritative state, then overlays `pending_orders` + `fill_log` rows since last `flex_sync_eod` timestamp to produce a merged intraday view. Requires walker to accept an optional `as_of_datetime` param and to know where to find the same-day delta source.
2. **V2 router skips positions with same-day activity.** Query `fill_log` for any executions on the ticker since last sync. If found, V2 router skips classification and logs `SAME_DAY_ACTIVITY_SKIP`. Simpler but leaves the operator without V2 guidance on recently-touched positions.
3. **Hybrid:** walker exposes an `has_same_day_activity(ticker)` method; V2 router logs a WARNING banner in the digest but still classifies.

**Priority:** Medium. Address in Workstream A (ACB pipeline hardening). See ADR-006.

**Owner:** Assigned to Workstream A.

---

## Followup #5 — Per-account basis precision lost at `_load_premium_ledger_snapshot` API boundary

**Severity:** HIGH (Act 60 Chapter 2 tax compliance)
**Discovered:** 2026-04-10, during Phase 2 intel gathering (Track 1)
**Origin commit:** Structural. `_load_premium_ledger_snapshot(household_id, ticker)` signature predates per-account walker precision.

### Diagnosis

Walker's `Cycle` dataclass tracks basis per account via `_paper_basis_by_account: dict[account_id, (cost, shares)]`. It exposes two accessors:
- `Cycle.paper_basis` (property) — household weighted average across all accounts holding the ticker
- `Cycle.paper_basis_for_account(account_id)` — per-account basis, matches IBKR `costBasisPrice`, matches IRS tax lot

`_load_premium_ledger_snapshot` consumes only the household-aggregated property. V2 router therefore has no access to per-account basis, even though walker can provide it.

**Act 60 Chapter 2 implication:** Puerto Rico Act 60 requires strict per-account tax lot segregation for bifurcating pre-move and post-move capital gains. Yash's household holds positions across U21971297 (Individual) and U22076329 (Roth IRA). If the same ticker is held in both accounts with different cost bases, the household-aggregated average is **neither account's actual basis**. A V2 router STATE_1 ASSIGN (Act 60 velocity) decision based on household-averaged basis can:
1. Trigger an assignment on shares whose account-specific basis makes it a taxable gain rather than an Act 60 exempt disposal, OR
2. Suppress an assignment on shares whose account-specific basis would qualify as exempt under Act 60

Both failure modes have real tax consequences.

**Non-Act-60 implication:** Same issue applies to V2 STATE_3 DEFEND — the "below adjusted basis" classification must be evaluated against the specific account's ACB, not the household average.

### Fix

1. Extend `_load_premium_ledger_snapshot` signature to accept an optional `account_id` parameter.
2. When `account_id` is provided and `READ_FROM_MASTER_LOG=True`, pass it through to `Cycle.paper_basis_for_account(account_id)` and compute adjusted_basis against per-account premium allocation (premium must also be attributed per account — see TD below).
3. V2 router already has `acct_id = pos.account` at the scan loop level. Pass it through at every call site.
4. Legacy fallback path (`READ_FROM_MASTER_LOG=False`) cannot resolve per-account precision because `premium_ledger` is household-keyed only. When `account_id` is provided but the flag is False, either (a) fail-closed with a warning, or (b) log a drift warning and return the household aggregate. Ruling: fail-closed. If legacy fallback is active, V2 router must not make per-account classifications.

**Tech debt exposed by fix:** Walker's `premium_total` is tracked at the Cycle level (household scope), not per-account. Per-account `adjusted_basis = paper_basis_for_account - (premium_for_account / shares_for_account)` requires premium attribution logic that does not currently exist. Two options: (a) attribute premium proportionally to each account's share count at the time of the premium event, (b) attribute premium to whichever account held the short option leg. Option (b) is correct under IRS tax treatment. Requires walker enhancement.

**Priority:** HIGH. Workstream A mandate per Architect ruling 2026-04-10.

**Owner:** Assigned to Workstream A.

---

## Followup #6 — V2 router may not write to `cc_decision_log.bot_believed_adjusted_basis` / `basis_truth_level`

**Severity:** Low (audit surface, not live risk)
**Discovered:** 2026-04-10, during Phase 2 intel gathering (Track 2)
**Origin commit:** Unknown. The columns exist in `schema.py` with no traced writer from the V2 router path.

### Diagnosis

`cc_decision_log` schema (schema.py) includes two columns designed for basis drift forensics:
- `bot_believed_adjusted_basis REAL` — what ACB the bot used for the staging decision
- `basis_truth_level TEXT` — which source layer produced that ACB (e.g. 'WALKER', 'LEGACY_LEDGER', 'IBKR_AVGCOST')

These columns are ready-made audit infrastructure for ADR-005 TD1 drift forensics. Search during Phase 2 intel gathering did not surface any V2 router write path populating these fields. The `_log_cc_cycle` function writes some columns including `adjusted_basis` but not `bot_believed_adjusted_basis` or `basis_truth_level`.

### Fix options

1. Verify by grepping: `grep -n "bot_believed_adjusted_basis\|basis_truth_level" telegram_bot.py`
2. If unused, wire up V2 router STATE_1 and STATE_3 staging paths to write both columns.
3. If used by a different code path (e.g. the legacy `/cc` digest path), verify V2 is NOT silently bypassing them and wire up if needed.

**Priority:** Low. Include as a small side-task in Workstream A if Coder has bandwidth, otherwise defer.

**Owner:** Unassigned.

---

## Followup #7 — Dual corporate intel cache (file + DB) — canonical is unclear

**Severity:** Low (code hygiene)
**Discovered:** 2026-04-10, during Phase 2 intel gathering (Track 4)
**Origin commit:** Unknown. Both caches exist in current codebase.

### Diagnosis

Two corporate intel caches co-exist:
1. **File cache:** `agt_desk_cache/corporate_intel/{ticker}_calendar.json` — written by `YFinanceCorporateIntelligenceProvider.get_corporate_calendar(ticker)`, read by `rule_engine._get_cached_earnings_date(ticker)`. This is the cache that caused Followup #1's test pollution.
2. **DB cache:** `bucket3_corporate_cache` table (`ticker TEXT PRIMARY KEY, data_json TEXT NOT NULL, fetched_at TEXT NOT NULL, source TEXT DEFAULT 'yfinance'`). Schema defined in `schema.py`. Phase 2 intel search did not surface a writer or reader for this table.

### Fix options

1. Determine which cache is canonical (file or DB). Deprecate the other.
2. If both are active but for different purposes, document the distinction in a docstring at the table definition.
3. If the DB table is dead code (schema defined but never populated), drop it in a cleanup sprint.

**Priority:** Low. Non-blocking. Revisit during Workstream C schema cleanup.

**Owner:** Unassigned.

---

## Followup #8 — Walker treats stock dividends as splits via `ratio * shares`

**Severity:** Low (edge case, current positions unlikely affected)
**Discovered:** 2026-04-10, during Phase 2 intel gathering (Track 2)
**Origin commit:** Pre-dates ADR-005. Walker's corporate action handler.

### Diagnosis

Walker's corp action handler handles stock splits via `new_shares = shares * ratio`. This is correct for a 4-for-1 split. But some IBKR Flex "stock dividend" events are not splits — they are separate share issuances that need different basis treatment. The walker currently conflates the two under the split handling path.

This may be correct treatment for most cases (a 10% stock dividend is economically equivalent to a 1.1-for-1 split), but there are edge cases where stock dividends have different cost basis allocation rules. Need to validate against real IBKR Flex examples of stock dividend events.

### Fix options

1. Collect 3-5 real stock dividend events from IBKR Flex history and compare walker output to expected per-account basis.
2. If walker's handling is incorrect for any case, add a dedicated stock dividend branch to the corp action handler.
3. If walker's handling is correct in all observed cases, document the equivalence in a comment and close this followup.

**Priority:** Low. Non-blocking. Validate during Workstream B or C.

**Owner:** Unassigned.

---

## Followup #9 — Intraday delta watermark reconciliation gap

**Severity:** Low-Medium (edge case, production impact bounded)
**Discovered:** 2026-04-10, during ADR-006 STEP 0 pre-flight
**Origin commit:** Structural limitation of the `MAX(last_synced_at)` watermark approach introduced in ADR-006 commit `b874d81`.

### Diagnosis

`get_active_cycles_with_intraday_delta` uses `MAX(last_synced_at) FROM master_log_trades` as the watermark dividing "already in walker" from "needs delta overlay". This is correct when flex_sync is complete and timely. It has a gap when IBKR Flex reporting lags a same-day fill: the fill is recorded in `fill_log` at T+0, other trades sync at T+1 which advances `last_synced_at`, but the lagged fill doesn't appear in master_log until T+2. During T+1 → T+2, the fill is neither in master_log (not yet reconciled) nor in the delta window (watermark advanced past its `created_at`). The fill becomes invisible to the walker intraday view for that window.

### Fix options

1. **Per-row reconciliation flag.** Add a `reconciled_to_master_log INTEGER DEFAULT 0` column to `fill_log`. `flex_sync.py` marks rows reconciled by matching `exec_id` during each sync. `get_active_cycles_with_intraday_delta` reads `WHERE reconciled_to_master_log = 0` instead of `WHERE created_at > watermark`. The watermark approach is retired. This is the correct long-term fix.
2. **Dedicated flex_sync_log table.** Add a `flex_sync_log` table with `sync_id, sync_mode, status, started_at, completed_at` and have `flex_sync.py` write a row at each sync start/end. Use `MAX(completed_at)` as a proper per-sync watermark. Better than current `MAX(last_synced_at)` but still has the same T+1/T+2 lag gap; only fixes the "partial syncs advancing the row watermark" concern.
3. **Accept the gap.** Document as known limitation. Valid for single-machine ops where Flex reporting lag is consistent.

### Phase relevance

Current state is option 3 (accept the gap). Yash's single-machine Wartime reality has consistent Flex reporting and the edge case is rare. Upgrade to option 1 when RIA multi-tenant goes live and Flex report lag becomes operationally relevant (multiple tenants, multiple Flex query windows, higher probability of lag).

**Priority:** Low now. Medium-High before first RIA client onboards.

**Owner:** Unassigned. Post-Workstream A follow-on.

---

## Followup #10 — `cc_decision_log` V2 router audit wiring

**Severity:** Low (forensic infrastructure, not live risk)
**Discovered:** 2026-04-10, during ADR-006 STEP 0 pre-flight (Blocker C triage)
**Origin commit:** Pre-existing dead table; deferred from ADR-006 to keep commit atomic.

### Diagnosis

`cc_decision_log` exists in `schema.py` with columns designed for per-account decision audit:
- `ticker, account_id, mode, strike, expiry, bid, annualized, otm_pct, dte, walk_away_pnl, spot`
- `bot_believed_adjusted_basis REAL` — what ACB the bot used for the staging decision
- `basis_truth_level TEXT` — which source layer produced that ACB
- `as_of_report_date, overlay_applied, master_log_sync_id, flag, created_at`

The table has no writer anywhere in the codebase (verified via `grep -n "cc_decision_log" telegram_bot.py` returning zero matches during ADR-006 STEP 0). ADR-006 ships per-account ACB precision but does NOT wire the forensic audit trail — V2 router decisions are logged only via the unstructured `v2_rationale` text field in the `pending_orders` payload.

### Fix path

1. Wire V2 router STATE_1 ASSIGN and STATE_3 DEFEND to write a `cc_decision_log` row on every staging decision. Populate: `ticker, account_id, mode, strike, expiry, bid, bot_believed_adjusted_basis, basis_truth_level, flag`.
2. Set NULL for fields without clean V2 router sources: `annualized, otm_pct, dte, walk_away_pnl, spot, as_of_report_date, overlay_applied, master_log_sync_id`. OR determine per-field sources and populate them properly.
3. Decide whether `/cc` digest (via `_log_cc_cycle`) should ALSO write to `cc_decision_log`, or if `cc_cycle_log` remains the canonical `/cc` audit surface. Recommendation: keep them distinct — `cc_cycle_log` for `/cc` digest history, `cc_decision_log` for V2 router per-decision audit.
4. Address the `master_log_sync_id` field: either add a sync_id column to `flex_sync.py`'s master_log writes, or document the column as NULL-permitted in the audit log spec.

### Current fallback

Until this followup is resolved, V2 router decisions are audited via the `v2_rationale` text field in `pending_orders.payload`. This provides a free-form forensic trail for any specific staging decision (pnl_pct, ray, ev_ratio, debit, etc.) but is not structured-queryable. Sufficient for a few weeks of operation; insufficient for ongoing post-hoc analysis at scale.

**Priority:** Low. Promote to Medium before Workstream B/C begins if structured audit queries become needed.

**Owner:** Unassigned. Post-Workstream A follow-on.

---

## Followup #11 — Empirical verification of `EXPIRE_WORTHLESS` `ev.net_cash` values

**Severity:** Low (audit / hygiene)
**Discovered:** 2026-04-10, during ADR-006 DIFF 1 implementation triage (Coder caught internal contradiction in dispatch).

### Diagnosis

Walker's `EXPIRE_WORTHLESS` branch at `agt_equities/walker.py:410` unconditionally mutates `cycle.premium_total += ev.net_cash`. The original ADR-006 dispatch assumed this line was a no-op and excluded it from per-account attribution. Coder's code walk surfaced that the line is real and unconditional inside the branch — `premium_total` IS being incremented on every EXPIRE_WORTHLESS event, possibly by 0, possibly non-zero.

Three hypotheses:
- **H1:** IBKR emits EXPIRE_WORTHLESS with `net_cash=0` always. Line 410 is defensive dead code from a copy-paste. Safe to delete in a hygiene commit.
- **H2:** IBKR sometimes emits non-zero `net_cash` on expirations (commissions, fees, late corrections). Walker correctly accumulates them into `premium_total`. Per-account attribution must mirror.
- **H3:** Ruled out by code inspection — line 410 is definitively inside the `elif et == EventType.EXPIRE_WORTHLESS:` block.

ADR-006 resolved this via ruling R1 (universalize per-account attribution across all 9 `premium_total +=` sites, including line 410). This closes the correctness gap mechanically regardless of H1 or H2, but does not answer which hypothesis holds empirically.

### Fix path

1. Query production `master_log_trades` or `fill_log` for all historical EXPIRE_WORTHLESS-equivalent events. Report the distribution of `net_cash` values across the dataset.
2. If 100% are zero → propose a hygiene commit to delete line 410 in walker.py and the paired `_credit_premium_to_account` call. Pair count drops from 9 to 8 cleanly.
3. If any are non-zero → document the observed cases (commissions? fees? corrections? option assignment fees?) and keep both lines with a comment explaining why they're load-bearing, not defensive.

### Phase relevance

ADR-006 is correct under both hypotheses. This followup is hygiene, not correctness. Useful to answer before Workstream B rule engine refactor touches walker adjacencies.

**Priority:** Low. Schedule as a half-hour investigation task between Workstream A close-out and Workstream B kickoff.

**Owner:** Unassigned.

---

## Followup #12 — `backup.ps1` sqlite3.exe gap breaks pre-deploy backup

**Severity:** Low (deploy succeeds with -SkipBackup workaround; VACUUM INTO backup run via Python manually)
**Discovered:** 2026-04-19, during MR !163 post-merge deploy sequence
**Origin commit:** Present since backup.ps1 was written (pre-dates MR !163)

### Diagnosis
`scripts/deploy/backup.ps1` locates sqlite3.exe at `C:\AGT_Telegram_Bridge\.venv\Scripts\sqlite3.exe`
or falls back to PATH. Neither location has sqlite3.exe on this machine. The standalone SQLite
CLI is not installed. `deploy.ps1` calls `backup.ps1` by default, causing deploy to fail at the
backup step unless `-SkipBackup` is passed. The VACUUM INTO backup was run manually via Python's
`sqlite3` stdlib as a workaround before passing `-SkipBackup`.

### Fix options
1. Port `backup.ps1` to use `python -c "import sqlite3; ..."` instead of sqlite3.exe CLI
2. Install sqlite3.exe to `C:\AGT_Telegram_Bridge\.venv\Scripts\` or system PATH
3. Add a Python fallback path to backup.ps1 (try sqlite3.exe, fall back to python -c)

**Priority:** Medium — deploy is currently fragile; each deploy requires manual backup workaround
**Owner:** Unassigned

---

## Followup template (for future additions)

```
## Followup #N — <short title>

**Severity:** Low | Medium | High | Critical
**Discovered:** <date>, during <context>
**Origin commit:** <sha or "Pre-dates X">

### Diagnosis
<what's wrong and why>

### Fix options
1. <option>
2. <option>

**Priority:** <with justification>
**Owner:** <name or Unassigned>
```
