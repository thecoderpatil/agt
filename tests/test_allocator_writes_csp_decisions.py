"""Integration: allocator writes to csp_decisions for every candidate.

Fixture helpers are inlined (not imported from test_csp_allocator_shadow_mode)
because the shadow-mode fixtures use private ``_`` names and lack the
``sector`` attribute needed by rule_3b. See dispatch MR !100 open risk note.

The ``extras_provider`` returns a sector_map so rule_3b can classify each
candidate by sector. AAPL / Technology passes; MRNA / Biotechnology is
hard-rejected at rule_3b_excluded_sector.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from agt_equities import csp_decisions_repo
from agt_equities.csp_allocator import run_csp_allocator
from agt_equities.runtime import RunContext, RunMode
from agt_equities.sinks import CollectorOrderSink, NullDecisionSink

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Inline fixture helpers
# ---------------------------------------------------------------------------

def _make_candidate(ticker: str, sector: str = "Technology", strike: float = 150.0) -> Any:
    class _C:
        pass
    c = _C()
    c.ticker = ticker
    c.sector = sector
    c.strike = float(strike)
    c.mid = 2.50
    c.expiry = "2026-05-15"
    c.annualized_yield = 18.5
    return c


def _make_hh_snapshot() -> dict:
    """Canonical shape: $261K NLV / $109K margin NLV = $109K EL (zero margin used)."""
    return {
        "household": "Yash_Household",
        "hh_nlv": 261_000.0,
        "hh_margin_nlv": 109_000.0,
        "hh_margin_el": 109_000.0,
        "accounts": {
            "U21971297": {
                "account_id": "U21971297",
                "nlv": 109_000.0,
                "el": 109_000.0,
                "buying_power": 200_000.0,
                "cash_available": 200_000.0,
                "margin_eligible": True,
            },
            "U22076329": {
                "account_id": "U22076329",
                "nlv": 152_000.0,
                "el": 0.0,
                "buying_power": 0.0,
                "cash_available": 152_000.0,
                "margin_eligible": False,
            },
        },
        "existing_positions": {},
        "existing_csps": {},
        "working_order_tickers": set(),
        "staged_order_tickers": set(),
    }


def _make_ctx(*, run_id: str, db_path: Path) -> RunContext:
    sink = CollectorOrderSink()
    return RunContext(
        mode=RunMode.SHADOW,
        run_id=run_id,
        order_sink=sink,
        decision_sink=NullDecisionSink(),
        db_path=str(db_path),
    
        broker_mode="paper",
        engine="csp",
    )


def _sector_extras(hh, candidate) -> dict:
    """Supply sector_map so rule_3b can classify the candidate."""
    return {"sector_map": {candidate.ticker.upper(): getattr(candidate, "sector", "Technology")}}


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "alloc_decisions.db"
    csp_decisions_repo.ensure_schema(db_path=db)
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_allocator_writes_one_row_per_candidate(tmp_db: Path):
    """Every candidate evaluated (staged or rejected) gets a csp_decisions row."""
    run_id = "alloc-t1"
    ctx = _make_ctx(run_id=run_id, db_path=tmp_db)
    snapshots = {"Yash_Household": _make_hh_snapshot()}

    run_csp_allocator(
        [_make_candidate("AAPL", "Technology"), _make_candidate("MRNA", "Biotechnology")],
        snapshots,
        18.0,
        _sector_extras,
        ctx=ctx,
    )

    rows = csp_decisions_repo.list_by_run(run_id, db_path=tmp_db)
    tickers = {r["ticker"] for r in rows}
    assert "AAPL" in tickers
    assert "MRNA" in tickers
    assert len(rows) == 2


def test_rejected_candidate_captures_gate_reason(tmp_db: Path):
    """MRNA is hard-rejected at rule_3b; the csp_decisions row records the gate."""
    run_id = "alloc-t2"
    ctx = _make_ctx(run_id=run_id, db_path=tmp_db)
    snapshots = {"Yash_Household": _make_hh_snapshot()}

    run_csp_allocator(
        [_make_candidate("MRNA", "Biotechnology")],
        snapshots,
        18.0,
        _sector_extras,
        ctx=ctx,
    )

    rows = csp_decisions_repo.list_by_ticker("MRNA", db_path=tmp_db)
    assert len(rows) == 1
    assert rows[0]["final_outcome"].startswith("rejected_by_")
    # Rule 3b is the biotech hard-exclude (MR !99)
    assert "rule_3b" in rows[0]["final_outcome"] or "excluded_sector" in rows[0]["final_outcome"]
    # Gate verdict list should include rule_3b with ok=False
    failed = [v for v in rows[0]["gate_verdicts"] if not v["ok"]]
    assert len(failed) >= 1


def test_staged_candidate_has_gate_verdicts_in_ticket_payload(tmp_db: Path):
    """Staged tickets carry gate_verdicts in the ticket dict (pending_orders payload)."""
    run_id = "alloc-t3"
    sink = CollectorOrderSink()
    ctx = RunContext(
        mode=RunMode.SHADOW,
        run_id=run_id,
        order_sink=sink,
        decision_sink=NullDecisionSink(),
        db_path=str(tmp_db),
    
        broker_mode="paper",
        engine="csp",
    )
    snapshots = {"Yash_Household": _make_hh_snapshot()}

    result = run_csp_allocator(
        [_make_candidate("AAPL", "Technology")],
        snapshots,
        18.0,
        _sector_extras,
        ctx=ctx,
    )

    # result.staged is a list of ticket dicts
    assert len(result.staged) >= 1, (
        f"Expected AAPL to be staged; got skipped={result.skipped} errors={result.errors}"
    )
    ticket = result.staged[0]
    assert isinstance(ticket, dict)
    assert "gate_verdicts" in ticket, f"gate_verdicts missing from staged ticket: {ticket.keys()}"
    assert isinstance(ticket["gate_verdicts"], list)
    assert len(ticket["gate_verdicts"]) >= 1
    # All verdicts should be ok=True for a staged candidate
    assert all(v["ok"] for v in ticket["gate_verdicts"])
