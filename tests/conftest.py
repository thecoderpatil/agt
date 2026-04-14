"""
Test session conftest — DB isolation tripwire.

Enforces that the test suite never accidentally hits the production
database. Works by monkeypatching agt_equities.db.DB_PATH at test
start to a nonexistent sentinel path. Any test that calls a trade_repo
public function without passing an explicit db_path= kwarg will fail
loud with "unable to open database file" instead of silently corrupting
production state.

Tests with pre-existing import-time production DB access (e.g.
telegram_bot.py module-level init_db()) are quarantined via the
@pytest.mark.agt_tripwire_exempt marker. See TRIPWIRE_EXEMPT_REGISTRY.md
for the full list of exemptions, root causes, and fix-by sprints.

Banked in FU-A (2026-04) per DT ruling Q1. See HANDOFF_ARCHITECT_v22 +
DT shot 2026-04-14.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Sentinel path that must not exist on any dev box or CI runner.
# Chosen to be recognizable in error messages if tripwire fires.
_TRIPWIRE_DB = Path("/__agt_test_tripwire_no_prod_db__/agt_desk.db")


def pytest_configure(config):
    """Register the agt_tripwire_exempt marker."""
    config.addinivalue_line(
        "markers",
        "agt_tripwire_exempt: test file is exempt from the DB isolation "
        "tripwire due to pre-existing import-time production DB access. "
        "Exemptions must reference a followup ticket and have a fix-by "
        "sprint. See TRIPWIRE_EXEMPT_REGISTRY.md.",
    )


@pytest.fixture(autouse=True)
def _agt_db_isolation_tripwire(request):
    """Block accidental production DB access during test session.

    Per-test function-scoped autouse. Monkeypatches agt_equities.db.DB_PATH
    to a sentinel nonexistent path for any test NOT marked with
    @pytest.mark.agt_tripwire_exempt. Exempt tests bypass the tripwire and
    run against whatever DB_PATH resolves to at import time (typically
    production — hence the exemption is documented in the handoff).

    Tests that need a real DB without the exemption must pass an explicit
    db_path= kwarg to trade_repo public functions or construct their own
    sqlite3 connection against a fixture path.
    """
    if request.node.get_closest_marker("agt_tripwire_exempt"):
        yield
        return

    try:
        from agt_equities import db as _agt_db
    except ImportError:
        # agt_equities not importable — skip tripwire, let tests fail
        # naturally with a clearer import error.
        yield
        return

    original = _agt_db.DB_PATH
    _agt_db.DB_PATH = _TRIPWIRE_DB
    try:
        yield
    finally:
        _agt_db.DB_PATH = original


@pytest.fixture(scope="session", autouse=True)
def _agt_env_guard():
    """Session-wide env guard — refuse to run against production env.

    If AGT_EXECUTION_ENABLED is true at session start, refuse to run
    tests. This protects against a test run in the live production
    environment accidentally firing live trades through any code path
    that reads the env var.
    """
    if os.getenv("AGT_EXECUTION_ENABLED", "").strip().lower() == "true":
        pytest.exit(
            "REFUSING to run tests with AGT_EXECUTION_ENABLED=true. "
            "Unset the env var before running pytest. This guard exists "
            "to prevent accidental live-trade fires from test runs.",
            returncode=2,
        )
    yield
