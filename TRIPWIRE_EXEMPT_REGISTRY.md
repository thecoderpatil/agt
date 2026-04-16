# Tripwire Exempt Registry

Tests marked with `@pytest.mark.agt_tripwire_exempt` bypass the
`tests/conftest.py` DB isolation tripwire. This registry tracks every
exemption, its root cause, and the sprint that will fix it. Zero-entry
target: every exemption should be deleted before its fix-by sprint closes.

## Active Exemptions

### Category 2: agt_deck test fixtures call get_ro_conn() directly

Root cause: Test setUp methods in agt_deck test files construct DB
connections via `get_ro_conn()` (the production read-only connection
helper from `agt_deck/db.py`, which re-exports from `agt_equities/db.py`).
`get_ro_connection()` reads `agt_equities.db.DB_PATH` directly, which is
the tripwire sentinel during test runs. Pre-FU-A-02 this silently hit
production `agt_desk.db` on every test run.

Status (post-FU-A-04): The shared module API surface is now ready for
migration — `get_db_connection()` and `get_ro_connection()` accept a
`db_path: str | Path | None = None` kwarg as of FU-A-04 Phase B. The
test migrations themselves were deferred because the FU-A-04a survey
revealed the migration scope is materially larger than originally
estimated. Each affected test class has a different blocker (see
per-entry notes below) and a different fix-by sprint. The shared module
kwarg expansion was preserved as preventive infrastructure so the future
sprints don't need to re-enter `agt_equities/db.py` to do the same change.

#### tests/test_deck_queries.py — TestDeckQueries
- **Surfaced:** FU-A-03c Phase C, 2026-04-14
- **Mechanism:** `setUp` calls `get_ro_conn()` → tripwire sentinel.
- **Scope:** 1 class (TestDeckQueries), 4 test methods. All tests in the
  file are affected (single class, shared setUp).
- **Why FU-A-04 deferred migration:** TestDeckQueries depends on
  production DB contents for assertion shapes — the 4 tests assert
  against real NAV values, fill counts, and recon summaries that only
  exist in production. Migrating to a fixture DB requires building seed
  data that matches the assertion shapes AND maintaining that fixture as
  production data evolves. That's a 2-4 hour focused refactor with
  non-trivial design questions (seed data scope, schema-drift
  maintenance, possibly a shared test-data builder helper). FU-A-04 is
  the trade_repo.DB_PATH deletion sprint, not the integration-test
  architecture sprint. Forcing a rushed migration risks breaking
  integration test signal that currently works.
- **Fix-by sprint:** `FU-INTEGRATION-TEST-FIXTURES` (currently
  unscheduled, banked in HANDOFF_ARCHITECT_v23 backlog inventory).
  Earliest viable scheduling is post-Decoupling-Sprint-B because
  Sprint B may change pending_orders schema, which would invalidate any
  fixture data built earlier.
- **Delete marker when fixed:** remove
  `pytestmark = pytest.mark.agt_tripwire_exempt` from TestDeckQueries
  class, migrate setUp to use `get_ro_conn(db_path=fixture_db)` against
  a properly-seeded fixture DB, and delete this entry.

#### tests/test_deck_auth.py — TestDeckAuth
- **Surfaced:** FU-A-03c Phase C, 2026-04-14
- **Mechanism:** FastAPI TestClient routes to handlers that call
  `get_ro_conn()` internally → tripwire sentinel. Only 1 of 4 tests
  originally failed (`test_correct_token_returns_200`, the only test
  exercising a DB-reading route). Class-level marker applied for safety
  as other tests could fail if they ever test DB-reading routes.
- **Scope:** 1 class (TestDeckAuth), 4 test methods.
- **Why FU-A-04 deferred migration:** TestDeckAuth's tripwire fires via
  `TestClient(app)` → FastAPI route handlers calling `get_ro_conn()`
  internally. Migration requires either (a) FastAPI dependency injection
  refactor of `agt_deck/main.py` to use `Depends(get_ro_conn)` instead
  of direct function calls (touching ~10 call sites + adding dependency
  wiring), or (b) a conftest fixture that monkeypatches
  `agt_equities.db.DB_PATH` before `TestClient(app)` construction (which
  is structurally equivalent to the current tripwire exemption in
  reverse). Option (a) is the architecturally clean path but expands
  FU-A-04 scope into FastAPI app architecture refactoring. Option (b)
  doesn't materially improve over the current exemption.
- **Related followup:** `FU-DECK-AUTH-INTERMITTENT` — TestDeckAuth's
  `test_correct_token_returns_200` showed an intermittent 401 vs 200
  result when run in isolation during FU-A-03c Phase C. Pre-existing
  test ordering bug masked by the tripwire. Low-priority cleanup.
- **Fix-by sprint:** `FU-AGT-DECK-DI-REFACTOR` (currently unscheduled,
  banked in HANDOFF_ARCHITECT_v23 backlog inventory). Can run in parallel
  with other sprints since it doesn't depend on Decoupling Sprint A or B.
- **Delete marker when fixed:** remove
  `pytestmark = pytest.mark.agt_tripwire_exempt` from TestDeckAuth class,
  refactor `agt_deck/main.py` to use FastAPI dependency injection for
  `get_ro_conn`, override the dependency in the test fixture to point at
  a test DB, and delete this entry.

## Historical Exemptions (resolved)

### Category 1: telegram_bot.py module-level init_db() — CLOSED by A4

**Root cause:** `telegram_bot.py:454` called `init_db()` at module scope.
`init_db()` → `get_db_connection()` → `sqlite3.connect(DB_PATH)` fired
before any test-level patch could intercept.

**Resolution:** Decoupling Sprint A unit A4 (2026-04-14). The bare
`init_db()` call at module scope was deleted; an `init_db()` invocation
was added as the first statement of `def main()` so the daemon boot
path remains identical while `import telegram_bot` becomes
side-effect-free for the on-disk database.

**Tests un-exempted:**
- `tests/test_inception_delta_fill.py` — module-level `pytestmark` removed.
- `tests/test_sprint1f.py::TestAGTFormattedBotIsExtBot` — class-level
  `pytestmark` removed.

**Regression guard:** `tests/test_a4_init_db_lazy.py` (sprint_a marker)
asserts via AST that `telegram_bot.py` contains zero module-level
`init_db()` calls and that `def main` still calls `init_db()` in its
body.


## Category 3 — csp_allocator routing tests → allocate_csp → get_ro_connection

**Signature:** `sqlite3.OperationalError: unable to open database file`
in `fa_block_margin.allocate_csp` → `_fetch_available_nlv` → `get_ro_connection(db_path=None)`.

**Affected tests (3):**
- `tests/test_csp_allocator.py::test_route_partial_when_household_cannot_fit_all`
- `tests/test_csp_allocator.py::test_route_spills_from_ira_to_margin_when_ira_full`
- `tests/test_csp_allocator.py::test_orchestrator_mutates_snapshot_between_candidates`

**Root cause:** `_csp_route_to_accounts` → `_build_and_allocate` → `allocate_csp`
opens a read-only DB connection to query `v_available_nlv` (margin NLV view).
When called in CI without a production DB, the tripwire path fires.

**Fix-by:** FU-INTEGRATION-TEST-FIXTURES — inject a temp SQLite with
`el_snapshots` + `v_available_nlv` view, or pass `available_nlv_override`
kwarg through the call chain. Moderate effort (4 sites to thread).
