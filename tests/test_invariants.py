"""Unit tests for ADR-007 safety invariants.

Every invariant has at least two tests:
    trip_*  - DB state that should violate the invariant
    pass_*  - clean DB state with zero violations

Fixtures use :memory: SQLite so the conftest.py prod-DB tripwire never fires.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agt_equities.invariants.checks import (
    check_no_below_basis_cc,
    check_no_live_in_paper,
    check_no_local_drift,
    check_no_missing_daemon_heartbeat,
    check_no_orphan_children,
    check_no_stale_red_alert,
    check_no_stranded_staged_orders,
    check_no_stuck_processing_order,
    check_no_unapproved_live_csp,
    check_no_zombie_bot_process,
)
from agt_equities.invariants.runner import build_context, load_invariants, run_all
from agt_equities.invariants.types import CheckContext


pytestmark = pytest.mark.sprint_a


NOW = datetime(2026, 4, 16, 22, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> CheckContext:
    return CheckContext(
        now_utc=NOW,
        db_path=":memory:",
        paper_mode=True,
        live_accounts=frozenset({"U21971297", "U22076329"}),
        paper_accounts=frozenset({"DUP751003", "DUP751004", "DUP751005"}),
        expected_daemons=frozenset({"agt_bot"}),
    )


@pytest.fixture
def ctx_live() -> CheckContext:
    return CheckContext(
        now_utc=NOW,
        db_path=":memory:",
        paper_mode=False,
        live_accounts=frozenset({"U21971297", "U22076329"}),
        paper_accounts=frozenset(),
        expected_daemons=frozenset({"agt_bot"}),
    )


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
        CREATE TABLE pending_order_children (
            id INTEGER PRIMARY KEY,
            parent_order_id INTEGER, status TEXT, payload TEXT
        );
        CREATE TABLE daemon_heartbeat (
            daemon_name TEXT PRIMARY KEY, last_beat_utc TEXT,
            pid INTEGER, client_id TEXT, notes TEXT
        );
        CREATE TABLE autonomous_session_log (
            id INTEGER PRIMARY KEY,
            task_name TEXT, run_at TEXT, summary TEXT,
            positions_snapshot TEXT, orders_snapshot TEXT,
            actions_taken TEXT, errors TEXT, metrics TEXT, notes TEXT
        );
        CREATE TABLE cross_daemon_alerts (
            id INTEGER PRIMARY KEY, created_ts TEXT,
            kind TEXT, severity TEXT, payload_json TEXT,
            status TEXT, sent_ts TEXT, attempts INTEGER, last_error TEXT
        );
        CREATE TABLE red_alert_state (
            household TEXT PRIMARY KEY, current_state TEXT,
            activated_at TEXT, activation_reason TEXT,
            conditions_met_count INTEGER, conditions_met_list TEXT,
            last_updated TEXT
        );
    """)
    return c


def _insert_order(
    conn, *, id, status, account_id, ticker,
    action="SELL", right="P", strike=100.0, mode="CSP_ENTRY",
    household="Yash_Household", created_at=None, approval_ref=None, extra=None,
):
    p = {
        "account_id": account_id, "household": household,
        "ticker": ticker, "action": action,
        "right": right, "strike": strike, "mode": mode,
    }
    if approval_ref is not None:
        p["approval_ref"] = approval_ref
    if extra:
        p.update(extra)
    conn.execute(
        "INSERT INTO pending_orders (id, payload, status, created_at, last_ib_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (id, json.dumps(p), status, created_at or NOW.isoformat(), status),
    )


# --- 1. NO_LIVE_IN_PAPER --------------------------------------------------------
def test_no_live_in_paper_trip(conn, ctx):
    _insert_order(conn, id=1, status="staged", account_id="U21971297", ticker="AAPL")
    _insert_order(conn, id=2, status="staged", account_id="DUP751003", ticker="AAPL")
    vios = check_no_live_in_paper(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["account_id"] == "U21971297"
    assert vios[0].severity == "critical"


def test_no_live_in_paper_pass(conn, ctx):
    _insert_order(conn, id=1, status="staged", account_id="DUP751003", ticker="AAPL")
    assert check_no_live_in_paper(conn, ctx) == []


def test_no_live_in_paper_off_when_live_mode(conn):
    ctx_live = CheckContext(
        now_utc=NOW, db_path=":memory:", paper_mode=False,
        live_accounts=frozenset({"U21971297"}), paper_accounts=frozenset(),
        expected_daemons=frozenset(),
    )
    _insert_order(conn, id=1, status="staged", account_id="U21971297", ticker="AAPL")
    assert check_no_live_in_paper(conn, ctx_live) == []


# --- 2. NO_UNAPPROVED_LIVE_CSP --------------------------------------------------
def test_no_unapproved_live_csp_trip(conn, ctx_live):
    _insert_order(conn, id=1, status="sent", account_id="U21971297", ticker="AAPL")
    vios = check_no_unapproved_live_csp(conn, ctx_live)
    assert len(vios) == 1
    assert vios[0].severity == "critical"


def test_no_unapproved_live_csp_pass_with_approval(conn, ctx_live):
    _insert_order(conn, id=1, status="sent", account_id="U21971297",
                  ticker="AAPL", approval_ref="tg:msg:12345")
    assert check_no_unapproved_live_csp(conn, ctx_live) == []


def test_no_unapproved_live_csp_ignores_paper(conn, ctx):
    # Paper mode short-circuits the check (P5a, MR !280).
    _insert_order(conn, id=1, status="sent", account_id="DUP751003", ticker="AAPL")
    assert check_no_unapproved_live_csp(conn, ctx) == []


def test_no_unapproved_live_csp_ignores_non_csp_mode(conn, ctx_live):
    _insert_order(conn, id=1, status="sent", account_id="U21971297",
                  ticker="AAPL", mode="MODE_2_HARVEST")
    assert check_no_unapproved_live_csp(conn, ctx_live) == []


def test_no_unapproved_live_csp_skips_in_paper_mode(conn, ctx):
    """Paper mode short-circuits even when row carries a live account_id and no approval_ref."""
    _insert_order(conn, id=1, status="sent", account_id="U21971297", ticker="AAPL")
    assert check_no_unapproved_live_csp(conn, ctx) == []


# --- 3. NO_BELOW_BASIS_CC -------------------------------------------------------
def test_no_below_basis_cc_empty_db_no_crash(conn, ctx):
    # Check must not blow up on empty DB; result is either clean or degraded
    vios = check_no_below_basis_cc(conn, ctx)
    assert isinstance(vios, list)
    assert all(v.invariant_id == "NO_BELOW_BASIS_CC" for v in vios)


def test_no_below_basis_cc_skips_non_cc(conn, ctx):
    # Puts + buys should never be checked
    _insert_order(conn, id=1, status="sent", account_id="DUP751003",
                  ticker="AAPL", action="SELL", right="P", strike=100.0)
    _insert_order(conn, id=2, status="sent", account_id="DUP751003",
                  ticker="AAPL", action="BUY", right="C", strike=100.0)
    vios = check_no_below_basis_cc(conn, ctx)
    # Either passes clean or degraded; no false positives on non-CC rows
    real_vios = [v for v in vios if not v.evidence.get("degraded")]
    assert real_vios == []


# --- 4. NO_ORPHAN_CHILDREN ------------------------------------------------------
def test_no_orphan_children_trip(conn, ctx):
    _insert_order(conn, id=10, status="filled", account_id="DUP751003", ticker="AAPL")
    conn.execute(
        "INSERT INTO pending_order_children "
        "(id, parent_order_id, status, payload) VALUES (1, 10, 'sent', '{}')"
    )
    vios = check_no_orphan_children(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["parent_status"] == "filled"


def test_no_orphan_children_pass(conn, ctx):
    _insert_order(conn, id=10, status="filled", account_id="DUP751003", ticker="AAPL")
    conn.execute(
        "INSERT INTO pending_order_children "
        "(id, parent_order_id, status, payload) VALUES (1, 10, 'filled', '{}')"
    )
    assert check_no_orphan_children(conn, ctx) == []


def test_no_orphan_children_ignores_active_parent(conn, ctx):
    _insert_order(conn, id=10, status="sent", account_id="DUP751003", ticker="AAPL")
    conn.execute(
        "INSERT INTO pending_order_children "
        "(id, parent_order_id, status, payload) VALUES (1, 10, 'sent', '{}')"
    )
    assert check_no_orphan_children(conn, ctx) == []


# --- 5. NO_STRANDED_STAGED_ORDERS ----------------------------------------------
def test_no_stranded_staged_trip(conn, ctx):
    old = (NOW - timedelta(hours=6)).isoformat()
    _insert_order(conn, id=1, status="staged", account_id="DUP751003",
                  ticker="EXPE", created_at=old)
    vios = check_no_stranded_staged_orders(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["age_hours"] > 1


def test_no_stranded_staged_pass_fresh(conn, ctx):
    recent = (NOW - timedelta(minutes=5)).isoformat()
    _insert_order(conn, id=1, status="staged", account_id="DUP751003",
                  ticker="EXPE", created_at=recent)
    assert check_no_stranded_staged_orders(conn, ctx) == []


def test_no_stranded_staged_ignores_other_statuses(conn, ctx):
    old = (NOW - timedelta(hours=6)).isoformat()
    _insert_order(conn, id=1, status="cancelled", account_id="DUP751003",
                  ticker="EXPE", created_at=old)
    assert check_no_stranded_staged_orders(conn, ctx) == []


# --- 7. NO_ZOMBIE_BOT_PROCESS ---------------------------------------------------
def test_no_zombie_bot_process_pass_in_ci(conn, ctx):
    """In CI no telegram_bot.py is running; result is clean or degraded."""
    vios = check_no_zombie_bot_process(conn, ctx)
    assert isinstance(vios, list)
    # Either empty (no zombies detected) or a single degraded Violation
    assert len(vios) <= 1
    if vios:
        assert vios[0].evidence.get("degraded") or vios[0].evidence.get("pid_count", 0) > 1


# --- 8. NO_STALE_RED_ALERT ------------------------------------------------------
def test_no_stale_red_alert_trip(conn, ctx):
    stale = (NOW - timedelta(days=7)).isoformat()
    conn.execute(
        "INSERT INTO red_alert_state "
        "(household, current_state, activated_at, last_updated) "
        "VALUES ('Yash_Household', 'ON', ?, ?)",
        (stale, stale),
    )
    vios = check_no_stale_red_alert(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["age_hours"] > 48


def test_no_stale_red_alert_pass_recent(conn, ctx):
    recent = (NOW - timedelta(hours=6)).isoformat()
    conn.execute(
        "INSERT INTO red_alert_state "
        "(household, current_state, activated_at, last_updated) "
        "VALUES ('Yash_Household', 'ON', ?, ?)",
        (recent, recent),
    )
    assert check_no_stale_red_alert(conn, ctx) == []


def test_no_stale_red_alert_ignores_off_state(conn, ctx):
    stale = (NOW - timedelta(days=7)).isoformat()
    conn.execute(
        "INSERT INTO red_alert_state "
        "(household, current_state, activated_at, last_updated) "
        "VALUES ('Yash_Household', 'OFF', ?, ?)",
        (stale, stale),
    )
    assert check_no_stale_red_alert(conn, ctx) == []


# --- 9. NO_STUCK_PROCESSING_ORDER ----------------------------------------------
def test_no_stuck_processing_trip(conn, ctx):
    old = (NOW - timedelta(hours=5)).isoformat()
    _insert_order(conn, id=1, status="processing", account_id="U21971297",
                  ticker="UBER", created_at=old)
    vios = check_no_stuck_processing_order(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["pending_order_id"] == 1


def test_no_stuck_processing_pass_recent(conn, ctx):
    recent = (NOW - timedelta(minutes=30)).isoformat()
    _insert_order(conn, id=1, status="processing", account_id="U21971297",
                  ticker="UBER", created_at=recent)
    assert check_no_stuck_processing_order(conn, ctx) == []


# --- 10. NO_MISSING_DAEMON_HEARTBEAT -------------------------------------------
def test_no_missing_heartbeat_trip_missing_row(conn, ctx):
    # No rows at all -> agt_bot missing
    vios = check_no_missing_daemon_heartbeat(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["reason"] == "missing"


def test_no_missing_heartbeat_trip_stale(conn, ctx):
    stale = (NOW - timedelta(minutes=10)).isoformat()
    conn.execute(
        "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid) "
        "VALUES ('agt_bot', ?, 1234)",
        (stale,),
    )
    vios = check_no_missing_daemon_heartbeat(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence["stale_seconds"] > 120


def test_no_missing_heartbeat_pass_fresh(conn, ctx):
    fresh = (NOW - timedelta(seconds=30)).isoformat()
    conn.execute(
        "INSERT INTO daemon_heartbeat (daemon_name, last_beat_utc, pid) "
        "VALUES ('agt_bot', ?, 1234)",
        (fresh,),
    )
    assert check_no_missing_daemon_heartbeat(conn, ctx) == []


# --- 11. NO_LOCAL_DRIFT ----------------------------------------------------------
def test_no_local_drift_no_git_repo_returns_empty(conn, ctx, monkeypatch, tmp_path):
    """When AGT_REPO_PATH points at a non-git dir, check returns [] cleanly."""
    monkeypatch.setenv("AGT_REPO_PATH", str(tmp_path))
    assert check_no_local_drift(conn, ctx) == []


def test_no_local_drift_clean_tree_returns_empty(conn, ctx, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("AGT_REPO_PATH", str(tmp_path))
    import agt_equities.invariants.checks as checks_mod

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*a, **kw):
        return _R()

    monkeypatch.setattr(checks_mod.subprocess, "run", fake_run)
    assert check_no_local_drift(conn, ctx) == []


def test_no_local_drift_exempt_files_filtered(conn, ctx, monkeypatch, tmp_path):
    """All 4 TRIPWIRE_EXEMPT_REGISTRY paths + ignored prefixes must NOT trip."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("AGT_REPO_PATH", str(tmp_path))
    import agt_equities.invariants.checks as checks_mod

    stdout = (
        " M boot_desk.bat\n"
        " M cure_lifecycle.html\n"
        " M cure_smart_friction.html\n"
        " M tests/test_command_prune.py\n"
        "?? reports/foo.log\n"
        "?? tmp/scratch.txt\n"
    )

    class _R:
        returncode = 0
        stderr = ""

    r = _R()
    r.stdout = stdout

    monkeypatch.setattr(checks_mod.subprocess, "run", lambda *a, **kw: r)
    assert check_no_local_drift(conn, ctx) == []


def test_no_local_drift_unexempt_file_trips(conn, ctx, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("AGT_REPO_PATH", str(tmp_path))
    import agt_equities.invariants.checks as checks_mod

    class _R:
        returncode = 0
        stdout = " M agt_equities/trade_repo.py\n"
        stderr = ""

    monkeypatch.setattr(checks_mod.subprocess, "run", lambda *a, **kw: _R())
    vios = check_no_local_drift(conn, ctx)
    assert len(vios) == 1
    assert vios[0].invariant_id == "NO_LOCAL_DRIFT"
    assert vios[0].evidence["drift_count"] == 1
    assert vios[0].evidence["drift_sample"][0]["path"] == "agt_equities/trade_repo.py"


def test_no_local_drift_subprocess_raises_degraded(conn, ctx, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("AGT_REPO_PATH", str(tmp_path))
    import agt_equities.invariants.checks as checks_mod
    import subprocess as _sp

    def boom(*a, **kw):
        raise _sp.SubprocessError("git gone")

    monkeypatch.setattr(checks_mod.subprocess, "run", boom)
    vios = check_no_local_drift(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence.get("degraded") is True
    assert vios[0].severity == "low"


def test_no_local_drift_nonzero_returncode_degraded(conn, ctx, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("AGT_REPO_PATH", str(tmp_path))
    import agt_equities.invariants.checks as checks_mod

    class _R:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(checks_mod.subprocess, "run", lambda *a, **kw: _R())
    vios = check_no_local_drift(conn, ctx)
    assert len(vios) == 1
    assert vios[0].evidence.get("degraded") is True
    assert "fatal" in vios[0].evidence.get("stderr", "")


# --- Runner + manifest integration ---------------------------------------------
def test_load_invariants_yaml_well_formed():
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "agt_equities" / "safety_invariants.yaml"
    manifest = load_invariants(path)
    assert len(manifest) >= 10
    required = {
        "id", "description", "check_fn", "scrutiny_tier",
        "fix_by_sprint", "max_consecutive_violations",
    }
    for entry in manifest:
        assert required <= set(entry.keys()), f"Missing keys in {entry.get('id')}"
    ids = [e["id"] for e in manifest]
    assert len(ids) == len(set(ids)), "Duplicate invariant ids in manifest"


def test_all_manifest_ids_have_registered_check():
    from agt_equities.invariants.checks import CHECK_REGISTRY
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "agt_equities" / "safety_invariants.yaml"
    manifest = load_invariants(path)
    missing = [e["id"] for e in manifest if e["id"] not in CHECK_REGISTRY]
    assert missing == [], f"Manifest entries without registered check: {missing}"


def test_build_context_reads_env(monkeypatch):
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    monkeypatch.setenv("AGT_LIVE_ACCOUNTS", "U12345,U67890")
    monkeypatch.setenv("AGT_EXPECTED_DAEMONS", "agt_bot,agt_scheduler")
    c = build_context(db_path=":memory:")
    assert c.paper_mode is True
    assert "U12345" in c.live_accounts
    assert c.expected_daemons == frozenset({"agt_bot", "agt_scheduler"})


def test_run_all_integration_smoke(conn, ctx, tmp_path):
    yaml_path = tmp_path / "test_inv.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "invariants:\n"
        "  - id: NO_LIVE_IN_PAPER\n"
        "    description: test\n"
        "    check_fn: check_no_live_in_paper\n"
        "    scrutiny_tier: architect_only\n"
        "    fix_by_sprint: existing\n"
        "    max_consecutive_violations: 1\n"
    )
    _insert_order(conn, id=1, status="staged", account_id="U21971297", ticker="AAPL")
    results = run_all(yaml_path=str(yaml_path), db_path=":memory:",
                      ctx=ctx, conn=conn)
    assert "NO_LIVE_IN_PAPER" in results
    assert len(results["NO_LIVE_IN_PAPER"]) == 1
