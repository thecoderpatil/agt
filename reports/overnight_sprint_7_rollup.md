# Sprint 7 Rollup — ADR-017 §9 First Sub-Dispatch

## Pre-sprint gate
- Tip entering: `8badd612` (Sprint 6 final)
- Services healthy: yes (inherited from Sprint 6 close; fresh verification deferred to end-of-sprint deploy)
- Gate report: `reports/sprint7_pre_sprint_gate.md`

## Shipped MRs

| MR  | Branch                                       | Squash     | Merge      | LOC  | Notes |
|-----|----------------------------------------------|------------|------------|------|-------|
| !232 | feature/observability-digest-helper          | `3ae9a73a` | `95c10dd2` | ~450 | Mega-MR A.1 — snapshot+render library + tests (5 new) |
| !234 | feature/observability-digest-scheduled       | `2d2b6f3f` | `121509d8` | ~210 | Mega-MR A.2 — PTB JobQueue 18:35 ET Mon-Fri (3 new) |
| !236 | feature/observability-thresholds-v3          | `6ad72beb` | `6e973a3`  | ~495 | Mega-MR B — hybrid absolute+relative threshold engine (7 new) |
| !237 | feature/observability-oversight-status-cmd   | `007cbda1` | `f2ce289c` | ~175 | Mega-MR C — /oversight_status command + registry parity (3 new + 1 updated) |

(!233 and !235 were earlier B ships that were closed+deleted due to
.gitlab-ci.yml line-level conflicts — see `mr236_ship.md` §Notes for
root cause + fix.)

**Final tip:** `f2ce289c`.

Four code MRs. Zero infrastructure work. Zero LLM code anywhere per
ADR-017 §4 non-goal. Zero csp_digest imports per ADR-017 §6 prohibition.

## Reports written
- `reports/sprint7_pre_sprint_gate.md`
- `reports/mr232_ship.md` (A.1)
- `reports/mr234_ship.md` (A.2)
- `reports/mr236_ship.md` (B)
- `reports/mr<iid>_ship.md` (C — filled on merge)
- `reports/overnight_sprint_7_rollup.md` (this)
- `reports/sprint7_first_fire_observation.md` (post first 18:35 ET fire,
  next trading day 2026-04-24 or 04-27 depending on deploy window)

## Key architectural decisions held

- **Separate package** from `agt_equities/csp_digest/`. Zero imports of
  CSP-typed classes (`DigestCandidate`, `approval_gate.py`,
  `formatter.py`). Verified via grep on `agt_equities/observability/*.py`.
- **Read-only across all five upstream tables** — `incidents`,
  `daemon_heartbeat`, `cross_daemon_alerts`, `master_log_sync` (via
  `flex_sync_watchdog.query_latest_sync` which uses `get_ro_connection`),
  `decisions` (via `paper_baseline.evaluate_all`). No write paths
  anywhere in observability code.
- **Native severity semantics preserved per section.** `scrutiny_tier`,
  `error_budget_tier`, heartbeat `fresh/warn/stale`, flex `stale + zero_row_suspicion`,
  promotion-gate `green/red/insufficient_data/not yet instrumented` all
  render in distinct visual formats.
- **G1/G3/G4 rendered as `not yet instrumented`** (regardless of upstream
  paper_baseline status) — explicit MVP acknowledgement that those gates
  are schema-gap-stubs (ADR-011 §2 pending additions).
- **Thresholds hybrid + cold-start.** Absolute triggers fire regardless
  of baseline; relative `max(5, 3× 7d median)` skips if <3 prior days.
  Canonical column is `error_budget_tier` (ADR-013), not legacy
  `severity_tier`.

## Deferred / blocked

- **Structured logging migration** — per ADR-017 §8 prerequisite for
  Phase 2 LLM eligibility. Not in Sprint 7 scope; separate future ADR
  when scoped.
- **icontract runtime assertions + PSScriptAnalyzer CI + Z-score migration**
  — Sprint 8+ conditional hardening per ADR-017 §7.
- **Sprint 6 add-on boot smoke MR** (task #35) — unchanged status;
  awaiting explicit go-ahead.

## Deploy verification

_(To be filled post-`deploy.ps1` run at end-of-sprint.)_

## ADR-017 Phase 2 observation window

- **Day 1 of 10:** first scheduled fire next trading day 18:35 ET
  post-deploy. Target: Friday 2026-04-24 18:35 ET if deploy lands by
  mid-afternoon; else Monday 2026-04-27 18:35 ET.
- **Window closes:** Day 1 + 10 trading days.
- **Missed-incident count:** 0 (baseline; Architect tracks).

Architect owns the Phase 2 eligibility decision at window close per
ADR-017 §8 four-condition gate.

## URGENT flags

(none expected this sprint)

## Next architect actions

- Review per-MR ship reports.
- Observation-window kickoff tracking daily.
- Post-observation-window (Monday 2026-05-10 approx): draft Phase 2
  eligibility decision per ADR-017 §8.
- Sprint 8 planning post-observation-window close.
