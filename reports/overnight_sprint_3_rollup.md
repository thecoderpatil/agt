# Overnight Sprint 3 Rollup — 2026-04-23

**Status:** PARTIAL. 6 of 8 MRs shipped (all CI green + merged). MR 2 + MR 7 + E-M-4 half of MR 4 + Investigation F carried forward.

**Final tip:** `2733904e` (post-!214 merge).

## Shipped MRs (CI green + merged)

| MR  | Scope                                          | Branch | Squash | Merge |
|-----|------------------------------------------------|--------|--------|-------|
| !209 | MR 1 — telegram asyncio.to_thread wraps (B-1..B-5) | feature/telegram-async-offload | 36802169 | fd5c98dd |
| !210 | MR 3 — tx_immediate sweep (E-M-5)             | feature/tx-immediate-sweep-incidents-remediation | cb512d43 | ec01c0ec |
| !211 | MR 5 — senior-dev cleanup bundle (E-M-3 + E-M-6 + E-M-7) | feature/senior-dev-cleanup-bundle | 3bda1354 | 41887532 |
| !212 | MR 6 — csp_approval_gate cancellable polling + retry (E-M-2) | feature/csp-approval-gate-polling-hygiene | 954fd5e5 | d8cf0ba2 |
| !213 | MR 8 — LOW bundle (E-L-1..E-L-4 minus a5e tripwire) | feature/low-severity-bundle | 6a395167 | e71b18a0 |
| !214 | MR 4 — invariants runner defaults (E-M-1 only; E-M-4 punted) | feature/invariants-runner-defaults-e-m-1 | 9c114762 | 2733904e |

## Not shipped this sprint (carried forward)

- **MR 2 — CSP Digest wiring** (CRITICAL, ~350 LOC). Starts paper observation week (ADR §5). Not shipped.
- **MR 7 — Flex-sync freshness watchdog + ADR-FLEX_FRESHNESS_v1** (CRITICAL, ~90 LOC + ADR). Not shipped.
- **E-M-4 half of MR 4** (`__file__` DB_PATH fallback elimination across 4 files + ~15 test updates). Punted. Preflight confirmed no operational impact today (both NSSM services have `AGT_DB_PATH` set).
- **Investigation F — bug-hunt round 2** (3 sub-agents: F.1 telegram_bot LLM loop, F.2 agt_deck FastAPI, F.3 pxo_scanner + vrp_veto + screener + asyncio race trace). Not run.

## Reports written
- `reports/sprint3_mr1_dispatch.md`, `sprint3_mr3_dispatch.md`, `sprint3_mr4_dispatch.md`, `sprint3_mr5_dispatch.md`, `sprint3_mr6_dispatch.md`, `sprint3_mr8_dispatch.md` — per-MR LOC gate fences
- `reports/mr209_ship.md` — MR 1 ship report (full)
- `reports/overnight_sprint_3_rollup.md` — this file

## Cadence notes

Sprint 2 shipped 8 clean MRs overnight. Sprint 3 took significantly longer per-MR due to:
1. Windows CRLF + Unicode escape friction on byte-level edits of the 22k-line `telegram_bot.py` for MR 1 (two f-strings required post-edit LF→`\n` normalization).
2. First-run overhead on the precommit LOC gate YAML fence format — once the `.staged/` + origin-cache pattern was established, subsequent MRs (3, 5, 6, 4, 8) shipped in ~30 min each.
3. MR 1's ~600 LOC across 3 files + 5 new tests was materially larger than the 60 LOC the dispatch estimated (generic helpers + CAS-preserving rewrites expanded scope slightly beyond the per-B estimate).

## URGENT flags for Architect

**None.** No live-capital regressions introduced. No ADR contradictions. No compliance gaps surfaced. All shipped changes are hygiene/observability refactors with existing-test-suite coverage.

**Minor scope expansions worth noting:**
- MR 1 incidentally fixed two DEFERRED-tx write paths in `handle_csp_approval_callback` by routing through `_sync_db_write` (which uses `tx_immediate`). Does NOT make MR 3 redundant — MR 3 still swept `incidents_repo.py`, `remediation.py`, `author_critic.py` as scoped.
- MR 1 also wrapped `cmd_daily`'s circuit_breaker site (not just `_pre_trade_gates`) — same HTTP-heavy pattern, <10 LOC per latitude.
- MR 5 took the WARN-on-invocation path for `_db_enabled` rather than deletion (test infra patches the symbol; deletion would break `tests/test_execution_gate.py`).
- MR 8 punted the a5e atomic_cutover tripwire because `reports/mr201_ship.md` isn't on disk to source the spec from. Follow-on MR needs to reconstruct from MR !201 commit diff.

## Paper observation week status

**Not started.** MR 2 wiring (scheduler job + `/approve_csp_<id>` handlers + allocator persistence) not shipped. ADR §5 observation-week clock has not begun. Phase 3 (live digest activation) remains blocked on that clock.

## Canary expectation (post-merge of shipped MRs)

1. **Paper TRANSMIT round-trip** — tap a staged dynamic exit in Cure Console. Confirm normal 9-step JIT progression. No regression in error messages or approval state machine (MR 1 core test).
2. **`/approve` flow** — approve a staged pending order (paper). Confirm claim/fetch/place works (MR 1 B-4).
3. **`/dex` CANCEL** — cancel an ATTESTED row. Confirm transition to CANCELLED (MR 1 B-5).
4. **Logs** — should NOT show new `DB error` warnings in `handle_csp_approval_callback` or `handle_approve_callback` (MR 1). Should NOT show `execution_gate._db_enabled() invoked` WARNING unless a caller regressed (MR 5 E-M-7 tripwire).
5. **Heartbeat order** (MR 5 E-M-6) — invariant tick now runs BEFORE heartbeat write. `_check_invariants_tick failed` exception log (if any) precedes the heartbeat row, not follows it.

## Next-session recommendation

Start with **MR 2** (CSP Digest wiring). Largest remaining piece and the gate on paper observation week. Then **MR 7** (flex-sync watchdog + ADR). Then E-M-4 as a focused follow-on (lazy-resolve in `agt_equities/db.py` + corresponding test updates). Then Investigation F with three sub-agents.

Estimated follow-on time: 5–7 hours for MR 2 + MR 7 + E-M-4 + Investigation F. The `.staged/commit_mr<N>.py` + dispatch-fence pattern is reusable; origin-cache naming uses `{repo_path.replace("/","__")}` per `scripts/precommit_loc_gate.py`.
