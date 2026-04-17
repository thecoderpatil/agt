"""MR !84 triage-driven bundle tests.

Covers the five scope items from `project_mr84_bundle_scoped.md`:

    Fix 1: check_no_live_in_paper filters out terminal-status rows
           (superseded / rejected / cancelled / failed / filled). Keeps
           partially_filled in scope. Kills 297 false positives that
           scanned resolved historical orders.

    Fix 2: NO_STALE_RED_ALERT incident_key stabilizes to a per-household
           slug via ``Violation.stable_key`` so repeat ticks bump
           consecutive_breaches rather than INSERTing a fresh row every
           60s (observed growth rate: ~2 rows/min pre-fix).

    Fix 3: requirements-runtime.txt manifest exists + parses (dep-drift
           CI job enforcement; see also test_requirements_runtime.py).

    Fix 4: .gitignore covers the 67-file Cowork/ops-scratch drift set so
           NO_LOCAL_DRIFT stops flagging operational noise.

    Fix 5: (this file + test_requirements_runtime.py) exists and is wired
           into the sprint_a CI file list. Meta-check: the sprint_a
           marker at module level + the explicit file-list entry are
           both required or CI silently skips the file.

Each test is deliberately compact; the goal is one verifiable assertion
per fix surface, not exhaustive coverage of adjacent behavior.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a


NOW = datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# Shared fixtures (local to this file -- avoid coupling to
# test_invariants.py fixtures so a rename there never silent-breaks us)
# ----------------------------------------------------------------------

@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE pending_orders (
            id INTEGER PRIMARY KEY,
            payload TEXT, status TEXT, created_at TEXT,
            ib_order_id INTEGER, ib_perm_id INTEGER, status_history TEXT,
            fill_price REAL, fill_qty INTEGER, fill_commission REAL,
            fill_time TEXT, last_ib_status TEXT, client_id TEXT
        );
        CREATE TABLE red_alert_state (
            household TEXT PRIMARY KEY, current_state TEXT,
            activated_at TEXT, activation_reason TEXT,
            conditions_met_count INTEGER, conditions_met_list TEXT,
            last_updated TEXT
        );
    """)
    return c


@pytest.fixture
def paper_ctx():
    from agt_equities.invariants.types import CheckContext
    return CheckContext(
        now_utc=NOW,
        db_path=":memory:",
        paper_mode=True,
        live_accounts=frozenset({"U21971297", "U22076329"}),
        paper_accounts=frozenset({"DUP751003"}),
        expected_daemons=frozenset({"agt_bot"}),
    )


def _stage_live_order(conn, *, id: int, status: str, account_id: str = "U21971297"):
    p = {"account_id": account_id, "ticker": "AAPL",
         "action": "SELL", "right": "P", "strike": 100.0,
         "mode": "CSP_ENTRY", "household": "Yash_Household"}
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at, last_ib_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (id, json.dumps(p), status, NOW.isoformat(), status),
    )


# ======================================================================
# Fix 1: check_no_live_in_paper terminal-status filter
# ======================================================================

TERMINAL_STATUSES = ("superseded", "rejected", "cancelled", "failed", "filled")


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_no_live_in_paper_excludes_terminal_status(conn, paper_ctx, status):
    """Rows in any of the 5 terminal statuses must NOT trip NO_LIVE_IN_PAPER.

    Pre-MR-!84 this scanned every row regardless of status, producing 297
    false positives against resolved historical orders (147 superseded,
    90 rejected, 50 cancelled, 9 failed, 1 filled) on 2026-04-17 01:35.
    """
    from agt_equities.invariants.checks import check_no_live_in_paper
    _stage_live_order(conn, id=1, status=status, account_id="U21971297")
    assert check_no_live_in_paper(conn, paper_ctx) == []


def test_no_live_in_paper_partially_filled_still_trips(conn, paper_ctx):
    """partially_filled is NOT terminal -- more routing is possible, so
    a live account in a partially_filled row still needs to flag."""
    from agt_equities.invariants.checks import check_no_live_in_paper
    _stage_live_order(conn, id=1, status="partially_filled", account_id="U21971297")
    vios = check_no_live_in_paper(conn, paper_ctx)
    assert len(vios) == 1
    assert vios[0].evidence["status"] == "partially_filled"
    assert vios[0].evidence["account_id"] == "U21971297"


def test_no_live_in_paper_non_terminal_statuses_still_trip(conn, paper_ctx):
    """The non-terminal statuses (staged, sent, processing) must still trip."""
    from agt_equities.invariants.checks import check_no_live_in_paper
    for i, status in enumerate(("staged", "sent", "processing"), start=1):
        _stage_live_order(conn, id=i, status=status, account_id="U21971297")
    vios = check_no_live_in_paper(conn, paper_ctx)
    assert len(vios) == 3
    assert {v.evidence["status"] for v in vios} == {"staged", "sent", "processing"}


def test_no_live_in_paper_terminal_filter_keeps_paper_clean(conn, paper_ctx):
    """Sanity: mixed terminal+paper accounts, no violations."""
    from agt_equities.invariants.checks import check_no_live_in_paper
    for i, (status, acct) in enumerate([
        ("filled", "U21971297"),       # terminal live -> filter out
        ("cancelled", "U22076329"),    # terminal live -> filter out
        ("staged", "DUP751003"),       # non-terminal paper -> not live
    ], start=1):
        _stage_live_order(conn, id=i, status=status, account_id=acct)
    assert check_no_live_in_paper(conn, paper_ctx) == []


# ======================================================================
# Fix 2: NO_STALE_RED_ALERT stable_key
# ======================================================================

def test_no_stale_red_alert_violation_carries_stable_key(conn, paper_ctx):
    """The Violation returned must set ``stable_key`` to
    ``NO_STALE_RED_ALERT:<household>`` so the tick layer can dedup
    regardless of time-varying evidence fields."""
    from agt_equities.invariants.checks import check_no_stale_red_alert
    stale = (NOW - timedelta(days=8)).isoformat()
    conn.execute(
        "INSERT INTO red_alert_state (household, current_state, activated_at, last_updated) "
        "VALUES ('Yash_Household', 'ON', ?, ?)",
        (stale, stale),
    )
    vios = check_no_stale_red_alert(conn, paper_ctx)
    assert len(vios) == 1
    assert vios[0].stable_key == "NO_STALE_RED_ALERT:Yash_Household"


def test_no_stale_red_alert_stable_key_per_household(conn, paper_ctx):
    """Two stale households must produce two distinct stable_keys."""
    from agt_equities.invariants.checks import check_no_stale_red_alert
    stale = (NOW - timedelta(days=8)).isoformat()
    for hh in ("Yash_Household", "Vikram_Household"):
        conn.execute(
            "INSERT INTO red_alert_state (household, current_state, activated_at, last_updated) "
            "VALUES (?, 'ON', ?, ?)",
            (hh, stale, stale),
        )
    vios = check_no_stale_red_alert(conn, paper_ctx)
    keys = sorted(v.stable_key for v in vios)
    assert keys == ["NO_STALE_RED_ALERT:Vikram_Household",
                    "NO_STALE_RED_ALERT:Yash_Household"]


def test_no_stale_red_alert_stable_key_steady_across_ticks(conn, paper_ctx):
    """Simulate two consecutive ticks (different now_utc) on the same
    stale alert -- stable_key must match. This is what lets
    ``incidents_repo.register`` bump ``consecutive_breaches`` instead of
    INSERTing a second row."""
    from agt_equities.invariants.checks import check_no_stale_red_alert
    from agt_equities.invariants.types import CheckContext
    stale = (NOW - timedelta(days=8)).isoformat()
    conn.execute(
        "INSERT INTO red_alert_state (household, current_state, activated_at, last_updated) "
        "VALUES ('Yash_Household', 'ON', ?, ?)",
        (stale, stale),
    )
    t1 = NOW
    t2 = NOW + timedelta(minutes=1)
    ctx1 = CheckContext(
        now_utc=t1, db_path=":memory:", paper_mode=True,
        live_accounts=frozenset(), paper_accounts=frozenset(),
        expected_daemons=frozenset(),
    )
    ctx2 = CheckContext(
        now_utc=t2, db_path=":memory:", paper_mode=True,
        live_accounts=frozenset(), paper_accounts=frozenset(),
        expected_daemons=frozenset(),
    )
    v1 = check_no_stale_red_alert(conn, ctx1)[0]
    v2 = check_no_stale_red_alert(conn, ctx2)[0]
    assert v1.stable_key == v2.stable_key == "NO_STALE_RED_ALERT:Yash_Household"
    # age_hours differs between ticks -- that's the whole point: evidence
    # varies, stable_key doesn't.
    assert v1.evidence["age_hours"] != v2.evidence["age_hours"]


# ======================================================================
# Fix 2b: tick.py honors stable_key
# ======================================================================

def test_tick_uses_stable_key_when_present(monkeypatch):
    """When a Violation has stable_key set, the tick layer must use it
    verbatim as the incident_key rather than hashing evidence."""
    import agt_equities.incidents_repo as ir_mod
    import agt_equities.invariants as inv_mod
    from agt_equities.invariants.tick import check_invariants_tick
    from agt_equities.invariants.types import Violation

    calls = []

    def fake_register(incident_key, **kwargs):
        calls.append({"incident_key": incident_key, **kwargs})
        return {"incident_key": incident_key, "id": len(calls)}

    def fake_load():
        return [{"id": "NO_STALE_RED_ALERT", "description": "stub",
                 "check_fn": "check_no_stale_red_alert",
                 "scrutiny_tier": "low", "fix_by_sprint": "mr84",
                 "max_consecutive_violations": 1,
                 "severity_floor": "medium"}]

    v = Violation(
        invariant_id="NO_STALE_RED_ALERT",
        description="stale",
        evidence={"household": "Yash_Household", "age_hours": 182.3},
        severity="medium",
        stable_key="NO_STALE_RED_ALERT:Yash_Household",
    )

    monkeypatch.setattr(inv_mod, "load_invariants", lambda *a, **k: fake_load())
    monkeypatch.setattr(inv_mod, "run_all", lambda *a, **k: {"NO_STALE_RED_ALERT": [v]})
    monkeypatch.setattr(ir_mod, "register", fake_register)

    n = check_invariants_tick()
    assert n == 1
    assert calls[0]["incident_key"] == "NO_STALE_RED_ALERT:Yash_Household"


def test_tick_dedups_stable_key_across_ticks(monkeypatch):
    """Two ticks of the same stable-key Violation with time-varying evidence
    must produce identical incident_keys (dedup behavior)."""
    import agt_equities.incidents_repo as ir_mod
    import agt_equities.invariants as inv_mod
    from agt_equities.invariants.tick import check_invariants_tick
    from agt_equities.invariants.types import Violation

    calls = []

    def fake_register(incident_key, **kwargs):
        calls.append(incident_key)
        return {"incident_key": incident_key, "id": len(calls)}

    monkeypatch.setattr(
        inv_mod, "load_invariants",
        lambda *a, **k: [{
            "id": "NO_STALE_RED_ALERT", "description": "stub",
            "check_fn": "check_no_stale_red_alert",
            "scrutiny_tier": "low", "fix_by_sprint": "mr84",
            "max_consecutive_violations": 1, "severity_floor": "medium",
        }],
    )
    monkeypatch.setattr(ir_mod, "register", fake_register)

    for age in (181.0, 181.1, 181.25):
        v = Violation(
            invariant_id="NO_STALE_RED_ALERT",
            description=f"stale ({age}h)",
            evidence={"household": "Vikram_Household", "age_hours": age},
            severity="medium",
            stable_key="NO_STALE_RED_ALERT:Vikram_Household",
        )
        monkeypatch.setattr(
            inv_mod, "run_all",
            lambda *a, v=v, **k: {"NO_STALE_RED_ALERT": [v]},
        )
        check_invariants_tick()
    assert calls == ["NO_STALE_RED_ALERT:Vikram_Household"] * 3


def test_tick_falls_back_to_fingerprint_when_stable_key_missing(monkeypatch):
    """Violations without stable_key must still use the evidence fingerprint
    -- the stable_key field is opt-in, existing checks untouched."""
    import agt_equities.incidents_repo as ir_mod
    import agt_equities.invariants as inv_mod
    from agt_equities.invariants.tick import _evidence_fingerprint, check_invariants_tick
    from agt_equities.invariants.types import Violation

    calls = []

    def fake_register(incident_key, **kwargs):
        calls.append(incident_key)
        return {"incident_key": incident_key, "id": len(calls)}

    monkeypatch.setattr(
        inv_mod, "load_invariants",
        lambda *a, **k: [{
            "id": "NO_BELOW_BASIS_CC", "description": "stub",
            "check_fn": "check_no_below_basis_cc",
            "scrutiny_tier": "low", "fix_by_sprint": "wheel",
            "max_consecutive_violations": 1, "severity_floor": "high",
        }],
    )
    monkeypatch.setattr(ir_mod, "register", fake_register)

    ev = {"pending_order_id": 247, "account_id": "U22076329", "ticker": "UBER"}
    v = Violation(
        invariant_id="NO_BELOW_BASIS_CC",
        description="cc below basis",
        evidence=ev,
        severity="high",
        # stable_key deliberately omitted
    )
    monkeypatch.setattr(inv_mod, "run_all", lambda *a, **k: {"NO_BELOW_BASIS_CC": [v]})
    check_invariants_tick()
    expected = f"NO_BELOW_BASIS_CC:{_evidence_fingerprint(ev)}"
    assert calls == [expected]


# ======================================================================
# Fix 3: requirements-runtime.txt manifest presence + shape
# ======================================================================

def test_requirements_runtime_file_exists():
    path = REPO_ROOT / "requirements-runtime.txt"
    assert path.is_file(), f"requirements-runtime.txt missing at {path}"


def test_requirements_runtime_pins_pyyaml():
    """The pyyaml incident is the reason this manifest exists. Pin it
    explicitly -- regression guard on the lesson."""
    path = REPO_ROOT / "requirements-runtime.txt"
    text = path.read_text()
    names = [
        re.split(r"[=<>!~]", line, 1)[0].strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "pyyaml" in names


def test_requirements_runtime_includes_core_bot_deps():
    """Every dep the bot/scheduler import at module level must be here.
    If you added a top-level third-party import to agt_scheduler.py,
    telegram_bot.py, or agt_equities/** and didn't add it here, production
    breaks silently. This is the static guard; dep_drift_check CI is the
    runtime guard."""
    path = REPO_ROOT / "requirements-runtime.txt"
    text = path.read_text().lower()
    required = [
        "pyyaml", "ib_async", "apscheduler", "python-telegram-bot",
        "python-dotenv", "anthropic", "finnhub-python", "yfinance",
        "pandas", "pytz", "psutil", "requests",
    ]
    missing = [r for r in required if r not in text]
    assert missing == [], f"requirements-runtime.txt missing: {missing}"


# ======================================================================
# Fix 4: .gitignore covers the drift set
# ======================================================================

def test_gitignore_covers_cowork_drift_set():
    """Patterns must exist for the session/Cowork/ops scratch that
    NO_LOCAL_DRIFT flagged on 2026-04-17. If you add a new kind of
    Cowork/ops scratch, add a .gitignore pattern AND extend this list."""
    path = REPO_ROOT / ".gitignore"
    text = path.read_text()
    required_patterns = [
        ".bot.pid",
        ".claude-cowork-notes.md",
        ".gitlab-token",
        "gitlab-recovery-codes.txt",
        "agt_desk_cache/",
        "hardening/",
        "reports/",
        "/_*.md",
    ]
    missing = [p for p in required_patterns if p not in text]
    assert missing == [], f".gitignore missing patterns: {missing}"
