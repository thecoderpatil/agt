"""
Sprint B5 — CSP Allocator pre-stage tests.

Covers:
  - v_available_nlv view shape + latest-snapshot semantics + NULL skip.
  - allocate_csp() — mode gate in-loop (Act 60 mixed-mode case),
    NLV-descending traversal, partial allocation, no-snapshot fallback,
    remainder assignment to largest, zero-contract edge.
  - format_allocation_digest() — renders all sections, handles empty
    approved / empty dropped, tolerates None available_nlv.

No ib_async / telegram / FastAPI imports. sprint_a marker so the
targeted runner picks it up.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agt_equities.fa_block_margin import (
    CSPProposal,
    AccountAllocation,
    AllocationDigest,
    MODE_PEACETIME,
    MODE_AMBER,
    MODE_WARTIME,
    STATUS_APPROVED,
    STATUS_INSUFFICIENT_NLV,
    STATUS_MODE_BLOCKED,
    STATUS_NO_SNAPSHOT,
    _contracts_affordable,
    allocate_csp,
    format_allocation_digest,
)
from agt_equities.schema import (
    register_master_log_tables,
    register_operational_tables,
)

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Per-test SQLite DB at tmp_path with operational tables registered."""
    p = tmp_path / "b5_test.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row  # schema.py expects Row on PRAGMA table_info
    try:
        register_operational_tables(conn)
        register_master_log_tables(conn)  # el_snapshots + v_available_nlv view
        conn.commit()
    finally:
        conn.close()
    return p


def _insert_el_snapshot(
    db: Path,
    *,
    account_id: str,
    household: str,
    nlv: float | None,
    excess_liquidity: float | None,
    timestamp: str,
) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO el_snapshots "
            "(account_id, household, nlv, excess_liquidity, buying_power, "
            " source, timestamp) "
            "VALUES (?, ?, ?, ?, NULL, 'test', ?)",
            (account_id, household, nlv, excess_liquidity, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v_available_nlv view tests
# ---------------------------------------------------------------------------


class TestVAvailableNLVView:
    def test_view_exists(self, db_path):
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='view' AND name='v_available_nlv'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_happy_path_available_equals_excess_liquidity(self, db_path):
        _insert_el_snapshot(
            db_path,
            account_id="U1",
            household="H1",
            nlv=100_000.0,
            excess_liquidity=40_000.0,
            timestamp="2026-04-15 09:00:00",
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT account_id, nlv, excess_liquidity, "
                "encumbered_capital, available_nlv "
                "FROM v_available_nlv WHERE account_id='U1'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "U1"
        assert row[1] == 100_000.0
        assert row[2] == 40_000.0
        assert row[3] == 60_000.0   # nlv - excess_liquidity
        assert row[4] == 40_000.0   # available_nlv = excess_liquidity

    def test_latest_snapshot_only(self, db_path):
        # Three snapshots for U1, view must pick the newest.
        for ts, el in [
            ("2026-04-15 08:00:00", 10_000.0),
            ("2026-04-15 09:00:00", 20_000.0),
            ("2026-04-15 08:30:00", 15_000.0),
        ]:
            _insert_el_snapshot(
                db_path,
                account_id="U1",
                household="H1",
                nlv=100_000.0,
                excess_liquidity=el,
                timestamp=ts,
            )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT account_id, available_nlv FROM v_available_nlv"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][1] == 20_000.0

    def test_null_nlv_excluded(self, db_path):
        _insert_el_snapshot(
            db_path,
            account_id="U1",
            household="H1",
            nlv=None,
            excess_liquidity=40_000.0,
            timestamp="2026-04-15 09:00:00",
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT * FROM v_available_nlv WHERE account_id='U1'"
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_null_excess_liquidity_excluded(self, db_path):
        _insert_el_snapshot(
            db_path,
            account_id="U1",
            household="H1",
            nlv=100_000.0,
            excess_liquidity=None,
            timestamp="2026-04-15 09:00:00",
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT * FROM v_available_nlv WHERE account_id='U1'"
            ).fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_null_account_id_excluded(self, db_path):
        _insert_el_snapshot(
            db_path,
            account_id=None,
            household="H1",
            nlv=100_000.0,
            excess_liquidity=40_000.0,
            timestamp="2026-04-15 09:00:00",
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT * FROM v_available_nlv").fetchall()
        finally:
            conn.close()
        assert rows == []

    def test_multiple_accounts_independent_latest(self, db_path):
        _insert_el_snapshot(
            db_path, account_id="U1", household="H1",
            nlv=100_000.0, excess_liquidity=10_000.0,
            timestamp="2026-04-15 08:00:00",
        )
        _insert_el_snapshot(
            db_path, account_id="U1", household="H1",
            nlv=100_000.0, excess_liquidity=30_000.0,
            timestamp="2026-04-15 09:00:00",
        )
        _insert_el_snapshot(
            db_path, account_id="U2", household="H2",
            nlv=50_000.0, excess_liquidity=20_000.0,
            timestamp="2026-04-15 07:00:00",
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT account_id, available_nlv FROM v_available_nlv"
                )
            }
        finally:
            conn.close()
        assert rows == {"U1": 30_000.0, "U2": 20_000.0}


# ---------------------------------------------------------------------------
# _contracts_affordable helper
# ---------------------------------------------------------------------------


class TestContractsAffordable:
    def test_exact_coverage(self):
        assert _contracts_affordable(10_000.0, 100.0) == 1

    def test_partial_floor(self):
        # $500 available at $100 strike = $10,000 per contract → 0.
        assert _contracts_affordable(500.0, 100.0) == 0

    def test_multiple_contracts(self):
        # $50,000 at $50 strike ($5,000 per contract) = 10 contracts.
        assert _contracts_affordable(50_000.0, 50.0) == 10

    def test_negative_available(self):
        assert _contracts_affordable(-1_000.0, 100.0) == 0

    def test_zero_strike(self):
        assert _contracts_affordable(10_000.0, 0.0) == 0


# ---------------------------------------------------------------------------
# allocate_csp — mode gate, NLV sort, partial allocation, digest shape
# ---------------------------------------------------------------------------


def _proposal(
    *,
    ticker: str = "ABC",
    strike: float = 50.0,
    contracts: int = 4,
    accounts: dict[str, str] | None = None,
) -> CSPProposal:
    return CSPProposal(
        household_id="TEST",
        ticker=ticker,
        strike=strike,
        contracts_requested=contracts,
        expiry="20260516",
        mode_gate_accounts=accounts or {},
    )


class TestAllocatorCore:
    def test_all_approved_happy_path(self):
        p = _proposal(
            strike=50.0,  # $5,000 per contract
            contracts=4,
            accounts={
                "U1": MODE_PEACETIME,
                "U2": MODE_PEACETIME,
            },
        )
        # Both amply funded — 2 contracts each ($10k each).
        digest = allocate_csp(
            p,
            available_nlv_override={"U1": 100_000.0, "U2": 100_000.0},
        )
        assert digest.total_contracts_requested == 4
        assert digest.total_contracts_allocated == 4
        assert digest.dropped_accounts == ()
        statuses = {a.account_id: a.margin_check_status for a in digest.allocations}
        assert statuses == {"U1": STATUS_APPROVED, "U2": STATUS_APPROVED}

    def test_wartime_account_blocked_inline(self):
        """Act 60 mixed-mode: one household WARTIME, other PEACETIME."""
        p = _proposal(
            strike=50.0,
            contracts=4,
            accounts={
                "U_PRINCIPAL_WARTIME": MODE_WARTIME,
                "U_ADVISORY_PEACE": MODE_PEACETIME,
            },
        )
        digest = allocate_csp(
            p,
            available_nlv_override={
                "U_PRINCIPAL_WARTIME": 200_000.0,
                "U_ADVISORY_PEACE": 200_000.0,
            },
        )
        # WARTIME account dropped, PEACETIME gets its pro-rata share.
        alloc = {a.account_id: a for a in digest.allocations}
        assert alloc["U_PRINCIPAL_WARTIME"].contracts_allocated == 0
        assert alloc["U_PRINCIPAL_WARTIME"].margin_check_status == STATUS_MODE_BLOCKED
        assert alloc["U_ADVISORY_PEACE"].margin_check_status == STATUS_APPROVED
        # Pro-rata: 4 / 2 = 2, remainder 0. WARTIME doesn't forfeit to peer.
        assert alloc["U_ADVISORY_PEACE"].contracts_allocated == 2
        assert digest.total_contracts_allocated == 2
        assert len(digest.dropped_accounts) == 1

    def test_nlv_descending_traversal_and_remainder(self):
        """Remainder goes to NLV-largest. Input order shouldn't matter."""
        p = _proposal(
            strike=10.0,       # $1k per contract
            contracts=5,       # 5 / 3 = 1 base + 2 remainder to largest
            accounts={
                "SMALL": MODE_PEACETIME,
                "LARGE": MODE_PEACETIME,
                "MID":   MODE_PEACETIME,
            },
        )
        digest = allocate_csp(
            p,
            available_nlv_override={
                "SMALL": 10_000.0,
                "LARGE": 100_000.0,
                "MID":   50_000.0,
            },
        )
        by_acct = {a.account_id: a for a in digest.allocations}
        assert by_acct["LARGE"].contracts_allocated == 3   # 1 base + 2 remainder
        assert by_acct["MID"].contracts_allocated == 1
        assert by_acct["SMALL"].contracts_allocated == 1
        assert digest.total_contracts_allocated == 5
        # Traversal order should be LARGE, MID, SMALL in allocations tuple.
        assert [a.account_id for a in digest.allocations] == ["LARGE", "MID", "SMALL"]

    def test_partial_allocation_insufficient_nlv(self):
        """Account covers only N of its pro-rata share — allocate N, drop status."""
        p = _proposal(
            strike=100.0,   # $10k per contract
            contracts=4,
            accounts={
                "U1": MODE_PEACETIME,
                "U2": MODE_PEACETIME,
            },
        )
        # U1 amply funded ($100k); U2 covers only 1 of 2 pro-rata ($12k).
        digest = allocate_csp(
            p,
            available_nlv_override={"U1": 100_000.0, "U2": 12_000.0},
        )
        by_acct = {a.account_id: a for a in digest.allocations}
        assert by_acct["U1"].margin_check_status == STATUS_APPROVED
        assert by_acct["U1"].contracts_allocated == 2
        assert by_acct["U2"].margin_check_status == STATUS_INSUFFICIENT_NLV
        assert by_acct["U2"].contracts_allocated == 1  # partial, not 0
        assert digest.total_contracts_allocated == 3
        assert ("U2", by_acct["U2"].margin_check_reason) in digest.dropped_accounts

    def test_zero_affordable_dropped(self):
        p = _proposal(
            strike=1000.0,  # $100k per contract
            contracts=2,
            accounts={"U1": MODE_PEACETIME, "U2": MODE_PEACETIME},
        )
        digest = allocate_csp(
            p,
            available_nlv_override={"U1": 5_000.0, "U2": 5_000.0},
        )
        assert digest.total_contracts_allocated == 0
        assert len(digest.dropped_accounts) == 2
        for a in digest.allocations:
            assert a.margin_check_status == STATUS_INSUFFICIENT_NLV

    def test_no_snapshot_account(self):
        p = _proposal(
            strike=10.0,
            contracts=2,
            accounts={"U_KNOWN": MODE_PEACETIME, "U_MISSING": MODE_PEACETIME},
        )
        digest = allocate_csp(
            p,
            available_nlv_override={"U_KNOWN": 100_000.0},
        )
        by_acct = {a.account_id: a for a in digest.allocations}
        assert by_acct["U_MISSING"].margin_check_status == STATUS_NO_SNAPSHOT
        assert by_acct["U_MISSING"].contracts_allocated == 0
        assert by_acct["U_MISSING"].available_nlv is None
        # U_KNOWN takes its own pro-rata (1); doesn't forfeit missing peer's share.
        assert by_acct["U_KNOWN"].contracts_allocated == 1
        assert ("U_MISSING", "no recent el_snapshot in v_available_nlv") in digest.dropped_accounts

    def test_empty_proposal_returns_empty_digest(self):
        p = _proposal(contracts=4, accounts={})
        digest = allocate_csp(p, available_nlv_override={})
        assert digest.allocations == ()
        assert digest.total_contracts_allocated == 0
        assert digest.dropped_accounts == ()

    def test_amber_mode_not_wartime_blocked(self):
        """AMBER blocks NEW CSP entries per 3-mode state machine but
        the allocator-level gate only rejects WARTIME (AMBER gating is
        upstream — caller decides whether to propose at all). Verify
        allocator does not silently block AMBER."""
        p = _proposal(
            strike=50.0,
            contracts=2,
            accounts={"U_AMBER": MODE_AMBER, "U_PEACE": MODE_PEACETIME},
        )
        digest = allocate_csp(
            p,
            available_nlv_override={"U_AMBER": 100_000.0, "U_PEACE": 100_000.0},
        )
        by_acct = {a.account_id: a for a in digest.allocations}
        # Both approved — allocator only hard-gates WARTIME.
        assert by_acct["U_AMBER"].margin_check_status == STATUS_APPROVED
        assert by_acct["U_PEACE"].margin_check_status == STATUS_APPROVED

    def test_integration_against_real_view(self, db_path):
        """End-to-end with the actual v_available_nlv view (no override)."""
        _insert_el_snapshot(
            db_path, account_id="U1", household="H1",
            nlv=100_000.0, excess_liquidity=50_000.0,
            timestamp="2026-04-15 09:00:00",
        )
        _insert_el_snapshot(
            db_path, account_id="U2", household="H1",
            nlv=100_000.0, excess_liquidity=3_000.0,
            timestamp="2026-04-15 09:00:00",
        )
        p = _proposal(
            strike=50.0,  # $5k per contract
            contracts=4,
            accounts={"U1": MODE_PEACETIME, "U2": MODE_PEACETIME},
        )
        digest = allocate_csp(p, db_path=db_path)
        by_acct = {a.account_id: a for a in digest.allocations}
        # U1 covers all 2 pro-rata ($10k of $50k avail) → approved.
        assert by_acct["U1"].margin_check_status == STATUS_APPROVED
        assert by_acct["U1"].contracts_allocated == 2
        # U2 covers 0 of 2 pro-rata ($10k required, $3k avail) → 0 partial.
        assert by_acct["U2"].margin_check_status == STATUS_INSUFFICIENT_NLV
        assert by_acct["U2"].contracts_allocated == 0


# ---------------------------------------------------------------------------
# format_allocation_digest
# ---------------------------------------------------------------------------


class TestFormatAllocationDigest:
    def test_full_render(self):
        p = _proposal(
            ticker="ABC",
            strike=50.0,
            contracts=4,
            accounts={"U1": MODE_PEACETIME, "U2": MODE_PEACETIME},
        )
        digest = allocate_csp(
            p,
            available_nlv_override={"U1": 100_000.0, "U2": 5_000.0},
        )
        text = format_allocation_digest(digest)
        assert "CSP Allocator — TEST/ABC 4x@$50.00 20260516" in text
        assert "Requested: 4" in text
        assert "Approved:" in text
        assert "U1:" in text
        assert "Dropped:" in text
        assert "U2:" in text

    def test_all_dropped_still_renders(self):
        p = _proposal(
            strike=1000.0,
            contracts=2,
            accounts={"U1": MODE_WARTIME, "U2": MODE_WARTIME},
        )
        digest = allocate_csp(
            p,
            available_nlv_override={"U1": 100_000.0, "U2": 100_000.0},
        )
        text = format_allocation_digest(digest)
        assert "Allocated: 0" in text
        assert "Dropped: 2" in text
        assert "Approved:" not in text  # no approved section
        assert "Dropped:" in text
        assert "WARTIME" in text

    def test_all_approved_no_dropped_section(self):
        p = _proposal(
            strike=10.0,
            contracts=2,
            accounts={"U1": MODE_PEACETIME, "U2": MODE_PEACETIME},
        )
        digest = allocate_csp(
            p,
            available_nlv_override={"U1": 100_000.0, "U2": 100_000.0},
        )
        text = format_allocation_digest(digest)
        assert "Approved:" in text
        # No "Dropped:" section header — only the header-line count "Dropped: 0".
        assert "\nDropped:" not in text
        assert "Dropped: 0" in text  # header count still shown

    def test_tolerates_none_available_nlv(self):
        """Renderer must not crash on no-snapshot allocations."""
        p = _proposal(
            strike=10.0,
            contracts=2,
            accounts={"U_MISSING": MODE_PEACETIME},
        )
        digest = allocate_csp(p, available_nlv_override={})
        # Should render cleanly — no-snapshot is a Dropped entry.
        text = format_allocation_digest(digest)
        assert "U_MISSING" in text
        assert "no recent el_snapshot" in text

    def test_empty_allocations(self):
        p = _proposal(contracts=0, accounts={})
        digest = allocate_csp(p, available_nlv_override={})
        text = format_allocation_digest(digest)
        # Header-only digest — still valid.
        assert "CSP Allocator — TEST/ABC" in text
        assert "Requested: 0" in text
        assert "\nApproved:" not in text
        assert "\nDropped:" not in text
