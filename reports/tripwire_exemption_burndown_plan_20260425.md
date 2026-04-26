# Tripwire Exemption Burndown Plan — 2026-04-25

**Scope:** Recon-only audit of all `agt_tripwire_exempt` markers.
**Author:** Coder A (ADR-020 Phase B support)
**No code changes in this report.**

---

## Background

The `agt_tripwire_exempt` pytest marker bypasses the DB isolation tripwire in
`tests/conftest.py`. The tripwire sets `AGT_DB_PATH` and `DB_PATH` to a
sentinel path (`/__agt_test_tripwire_no_prod_db__/agt_desk.db`) that never
exists on disk, so any test that accidentally opens the prod DB will crash with
a clear error rather than silently corrupting live data.

Exempt markers should be rare and explicitly justified. This audit found
12 exempt locations (across 10 test files), of which 7 are CI-included.

---

## Full Inventory

### CI-INCLUDED (runs in sprint_a_unit_tests CI job)

| # | File | Tests | Classification | Rationale |
|---|------|-------|----------------|-----------|
| 1 | `tests/test_boot_contract.py` | 6 (file-level) | **RETAIN PERMANENTLY** | Tests exercise exact env-var resolution (`AGT_DB_PATH`, `AGT_BROKER_MODE`, `AGT_ENV_FILE`) that the tripwire patches. Removing exemption would break the tests by design. |
| 2 | `tests/test_cached_client.py` | 7 (file-level) | **TRIVIAL REMOVAL** | Defensive/outdated. Fixture uses `tmp_path` + `monkeypatch.setattr(dbmod, "DB_PATH", ...)` — already fully isolated. Exemption added pre-tripwire, never cleaned up. |
| 3 | `tests/test_engine_state.py` | 14 (file-level) | **TRIVIAL REMOVAL** | `seeded_db` fixture creates `tmp_path / "engine_state.db"` + `monkeypatch.setenv("AGT_DB_PATH", ...)` + `monkeypatch.setattr(_agt_db, "DB_PATH", ...)`. Fully isolated already. |
| 4 | `tests/test_incidents_error_budget.py` | 9 (file-level) | **TRIVIAL REMOVAL** | `fresh_db` fixture: `tmp_path / "incidents_mr4b.db"` + same monkeypatch pattern. Fully isolated. |
| 5 | `tests/test_paper_auto_execute.py` | 6 (per-test) | **TRIVIAL REMOVAL** | `staged_db` / `empty_db` fixtures with `tmp_path + monkeypatch.setattr(dbmod, "DB_PATH", ...)`. Per-test comment says "explicit temp DB via monkeypatched DB_PATH". |
| 6 | `tests/test_promotion_gates_paper_baseline.py` | 5 (file-level) | **VESTIGIAL — VERIFY SKIP GUARD** | Reads prod DB via `get_ro_conn()`. Has `skipif(_PROD_DB is None, ...)` guard. In CI, tripwire sets `AGT_DB_PATH` to sentinel path that does not exist → `_prod_db_available()` returns False → all 5 tests skip before any DB touch. Marker is vestigial but benign in CI. |
| 7 | `tests/test_csp_allocator.py` (3 tests) | 3 (per-test) | **VESTIGIAL — VERIFY SKIP GUARD** | Lines 841, 882, 1512. Each has both `@pytest.mark.skipif(not _prod_db_available(), ...)` AND mocks `_fetch_available_nlv` via `patch(...)`. In CI: `_prod_db_available()` → False → skip before execution. Exemption is vestigial. |

**CI-included totals:** ~50 test functions with exempt markers (36 trivial + 6 boot_contract + 8 vestigial/verify).

### CI-EXCLUDED (not in sprint_a_unit_tests CI job)

| # | File | Tests | Classification | Notes |
|---|------|-------|----------------|-------|
| 8 | `tests/test_alerts_gmail_staging.py` | unknown | LOW PRIORITY | Uses `tmp_path`. Staging test, not in CI. Cleanup optional. |
| 9 | `tests/test_bot_heartbeat.py` | unknown | LOW PRIORITY | Tests heartbeat TTL constants + writes. Not in CI. |
| 10 | `tests/test_circuit_breaker_halt_vix.py` | unknown | LOW PRIORITY | Loads `scripts/circuit_breaker.py` via `_load_breaker(monkeypatch, ...)`. Not in CI. |
| 11 | `tests/test_deck_auth.py` | multiple | **COMPLEX — RETAIN** | Documented in `TRIPWIRE_EXEMPT_REGISTRY.md` Category 2. FastAPI DI with `TestClient` requires full app context. Migration needs DI refactor. |
| 12 | `tests/test_deck_queries.py` | multiple | **COMPLEX — RETAIN** | Documented in `TRIPWIRE_EXEMPT_REGISTRY.md` Category 2. Same FastAPI DI constraint as test_deck_auth. |

---

## CI-Included Migration Plan (Priority Order)

### MR-1 (Recommended first MR): Remove 4 defensive markers — TRIVIAL

**Files:** `test_cached_client.py`, `test_engine_state.py`, `test_incidents_error_budget.py`, `test_paper_auto_execute.py`
**Tests affected:** 36 test functions
**Production code changes:** Zero
**Risk:** Negligible — tests already use tmp_path+monkeypatch. Removing the marker means the tripwire fixture also runs, but since monkeypatch sets `DB_PATH` after the tripwire sets it to sentinel, the monkeypatch wins. The tests will still pass.
**Effort:** ~37 LOC deleted (4 file-level pytestmark lines + 6 per-test decorator lines in paper_auto_execute)
**Validation:** `pytest -m sprint_a tests/test_cached_client.py tests/test_engine_state.py tests/test_incidents_error_budget.py tests/test_paper_auto_execute.py` must pass locally before push.

### MR-2: Verify and remove 2 vestigial skip-guarded markers

**Files:** `tests/test_promotion_gates_paper_baseline.py`, `tests/test_csp_allocator.py` (3 tests)
**Tests affected:** 8 test functions
**Production code changes:** Zero
**Risk:** Low — skip guards confirmed working. Removing exemption means tripwire sets AGT_DB_PATH to sentinel → `_prod_db_available()` returns False → tests skip. Same CI outcome, cleaner marker surface.
**Effort:** ~8 LOC deleted
**Prerequisite:** Verify `_prod_db_available()` logic is purely `get_db_path().exists()` and NOT affected by tripwire patching `DB_PATH` module attribute (distinct from `AGT_DB_PATH` env var). Confirm in local run with `AGT_DB_PATH=/__agt_test_tripwire_no_prod_db__/agt_desk.db pytest ...` before push.

### MR-0 (already complete): boot_contract — RETAIN PERMANENTLY

`test_boot_contract.py` marker is by design and must never be removed. The tests are testing the exact mechanism the tripwire uses.

---

## Dependencies

```
MR-1 (trivial 4-file cleanup)
  No prerequisites. Can go immediately.

MR-2 (vestigial skip-guard verification)
  No code prerequisites, but requires local verification run
  (skip-guard behavior under sentinel AGT_DB_PATH).
  Can run in parallel with MR-1 or after.

CI-EXCLUDED files (8, 9, 10)
  Optional cleanup. No CI impact. Low urgency.

COMPLEX (11, 12 — deck_auth/deck_queries)
  Requires FastAPI DI refactor. Out of scope for Phase B.
  Defer to dedicated sprint.
```

---

## Risk Flags

- **test_boot_contract.py** — `RETAIN PERMANENTLY`. Removing exemption breaks tests by design.
- **test_deck_auth.py / test_deck_queries.py** — CI-excluded, complex FastAPI DI migration. Do not touch outside dedicated sprint.
- **test_promotion_gates_paper_baseline.py** — reads real prod DB when it exists. Verify skip guard before removing exemption. Do not remove without local confirmation run.
- **test_csp_allocator.py** 3 tests — both skip guard + mock present. Safe to remove once skip guard verified.

---

## Recommended First MR Scope

**Bundle all 4 trivial removals (test_cached_client, test_engine_state, test_incidents_error_budget, test_paper_auto_execute) into a single MR.**

Rationale:
- Zero production code changes
- All 4 files independently verified as already properly isolated
- 36 test functions gain tripwire coverage (regression protection for DB isolation)
- ~37 LOC delta — STANDARD tier MR (tests/** only)
- Single local pytest run validates all 4 at once before push

This MR closes the most marker surface area (72% of CI-included trivial exemptions) with the lowest risk of any cleanup work in this audit.
