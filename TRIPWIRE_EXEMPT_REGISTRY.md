# Tripwire Exempt Registry

Tests marked with `@pytest.mark.agt_tripwire_exempt` bypass the
`tests/conftest.py` DB isolation tripwire. This registry tracks every
exemption, its root cause, and the sprint that will fix it. Zero-entry
target: every exemption should be deleted before its fix-by sprint closes.

## Active Exemptions

### Category 1: telegram_bot.py:454 init_db() at import time

Root cause: `telegram_bot.py:454` calls `init_db()` at module level.
`init_db()` → `get_db_connection()` → `sqlite3.connect(DB_PATH)` fires
before any test-level patch() can intercept. Pre-FU-A-02 this silently
mutated production `agt_desk.db` on every test run.

Fix-by sprint: Decoupling Sprint A. When bot becomes a daemon with
explicit startup order, `init_db()` moves from module-level to daemon
boot sequence. All exemptions in this category are deleted then.

#### tests/test_inception_delta_fill.py
- **Surfaced:** FU-A-02 Phase C, 2026-04-14
- **Scope:** 19 tests in the file (module-level `pytestmark`).
- **Delete marker when fixed:** remove `pytestmark = pytest.mark.agt_tripwire_exempt`
  from test_inception_delta_fill.py and delete this registry entry.

#### tests/test_sprint1f.py — TestAGTFormattedBotIsExtBot
- **Surfaced:** FU-A-03c Phase B, 2026-04-14
- **Scope:** 1 test class (TestAGTFormattedBotIsExtBot) inside an
  otherwise-clean test file. 30+ other tests in test_sprint1f.py are
  unaffected and remain under tripwire enforcement.
- **Delete marker when fixed:** remove
  `pytestmark = pytest.mark.agt_tripwire_exempt` from the
  TestAGTFormattedBotIsExtBot class definition and delete this
  registry entry.

### Category 2: agt_deck test fixtures call get_ro_conn() directly

Root cause: Test setUp methods in agt_deck test files construct DB
connections via `get_ro_conn()` (the production read-only connection
helper from `agt_deck/db.py`, which re-exports from `agt_equities/db.py`).
`get_ro_connection()` reads `agt_equities.db.DB_PATH` directly, which is
the tripwire sentinel during test runs. Pre-FU-A-02 this silently hit
production `agt_desk.db` on every test run.

Fix-by sprint: FU-A-04. Add `db_path: str | None = None` kwarg to
`get_db_connection()` and `get_ro_connection()` in `agt_equities/db.py`.
Thread the kwarg through all callers. Migrate the affected tests to pass
`db_path=fixture_db` explicitly. After migration, delete all exemptions
in this category.

#### tests/test_deck_queries.py — TestDeckQueries
- **Surfaced:** FU-A-03c Phase C, 2026-04-14
- **Mechanism:** `setUp` calls `get_ro_conn()` → tripwire sentinel.
- **Scope:** 1 class (TestDeckQueries), 4 test methods. All tests in the
  file are affected (single class, shared setUp).
- **Delete marker when fixed:** remove
  `pytestmark = pytest.mark.agt_tripwire_exempt` from TestDeckQueries
  class, migrate setUp to use `db_path=fixture_db`, and delete this entry.

#### tests/test_deck_auth.py — TestDeckAuth
- **Surfaced:** FU-A-03c Phase C, 2026-04-14
- **Mechanism:** FastAPI TestClient routes to handlers that call
  `get_ro_conn()` internally → tripwire sentinel. Only 1 of 4 tests
  originally failed (`test_correct_token_returns_200`, the only test
  exercising a DB-reading route). Class-level marker applied for safety
  as other tests could fail if they ever test DB-reading routes.
- **Scope:** 1 class (TestDeckAuth), 4 test methods.
- **Delete marker when fixed:** remove
  `pytestmark = pytest.mark.agt_tripwire_exempt` from TestDeckAuth class,
  migrate app fixture to use test DB, and delete this entry.

## Historical Exemptions (resolved)

None yet.
