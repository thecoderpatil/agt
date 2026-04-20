"""
Sprint B5.b — M1.x csp_allocator ↔ fa_block_margin integration tests.

Covers the helper seam inserted into csp_allocator.py:
  _build_csp_proposal, _build_and_allocate, _tickets_from_digest.

Does NOT exercise the full run_csp_allocator orchestrator (that requires
a fully-stubbed RAYCandidate + extras_provider + staging_callback
harness — out of scope for pure-library tests). Instead asserts the
helper functions produce the correct shape and semantics from an M1.x
hh_snapshot dict.
"""
from __future__ import annotations

import types

import pytest

from agt_equities.csp_allocator import (
    _build_and_allocate,
    _build_csp_proposal,
    _tickets_from_digest,
)
from agt_equities.fa_block_margin import (
    AllocationDigest,
    CSPProposal,
    STATUS_APPROVED,
)

pytestmark = pytest.mark.sprint_a


def _make_candidate(
    *, ticker: str = "ABC", strike: float = 50.0, mid: float = 1.25,
    expiry: str = "2026-05-16", annualized_yield: float = 42.0,
) -> types.SimpleNamespace:
    """Minimal RAYCandidate stand-in for M1.x helper tests."""
    return types.SimpleNamespace(
        ticker=ticker, strike=strike, mid=mid, expiry=expiry,
        annualized_yield=annualized_yield, dte=30,
    )


def _hh_snapshot(accounts: dict, household: str = "H1") -> dict:
    """Minimal M1.x-shaped snapshot."""
    return {
        "household": household,
        "hh_nlv": 500_000.0,
        "hh_margin_nlv": 400_000.0,
        "hh_margin_el": 200_000.0,
        "accounts": accounts,
        "existing_positions": {},
        "existing_csps": {},
    }


class TestBuildCspProposal:
    def test_shape_and_flag_lift(self):
        hh = _hh_snapshot({
            "U_IRA": {
                "account_id": "U_IRA", "margin_eligible": False,
                "cash_available": 10_000.0, "buying_power": 0.0,
            },
            "U_M1": {
                "account_id": "U_M1", "margin_eligible": True,
                "cash_available": 0.0, "buying_power": 100_000.0,
            },
        })
        candidate = _make_candidate(ticker="abc", strike=50.0, mid=1.25)
        proposal = _build_csp_proposal(3, hh, candidate)
        assert isinstance(proposal, CSPProposal)
        assert proposal.household_id == "H1"
        assert proposal.ticker == "ABC"           # uppercase canonicalization
        assert proposal.expiry == "20260516"      # YYYYMMDD conversion
        assert proposal.contracts_requested == 3
        assert set(proposal.account_ids) == {"U_IRA", "U_M1"}
        assert proposal.margin_eligible == {"U_IRA": False, "U_M1": True}
        assert proposal.limit_price == 1.25
        assert proposal.annualized_yield == 42.0

    def test_account_ids_list_populated(self):
        """account_ids list carries all account keys from snapshot."""
        hh = _hh_snapshot({
            "U_A": {
                "account_id": "U_A", "margin_eligible": True,
                "cash_available": 0.0, "buying_power": 50_000.0,
            },
        })
        proposal = _build_csp_proposal(1, hh, _make_candidate())
        assert "U_A" in proposal.account_ids


class TestBuildAndAllocate:
    def test_round_trip_mixed_cash_margin(self):
        """Full round-trip: hh_snapshot → allocate_csp → AllocationDigest."""
        hh = _hh_snapshot({
            "U_IRA": {
                "account_id": "U_IRA", "margin_eligible": False,
                "cash_available": 10_000.0, "buying_power": 0.0,
            },
            "U_M1": {
                "account_id": "U_M1", "margin_eligible": True,
                "cash_available": 0.0, "buying_power": 100_000.0,
            },
            "U_M2": {
                "account_id": "U_M2", "margin_eligible": True,
                "cash_available": 0.0, "buying_power": 100_000.0,
            },
        })
        candidate = _make_candidate(strike=50.0)  # $5k per contract

        # Inject NLV via monkeypatch of the fetch helper
        import agt_equities.fa_block_margin as fam
        orig = fam._fetch_available_nlv
        fam._fetch_available_nlv = lambda ids, **kw: {
            "U_M1": 100_000.0, "U_M2": 100_000.0,
        }
        try:
            digest = _build_and_allocate(6, hh, candidate)
        finally:
            fam._fetch_available_nlv = orig

        assert isinstance(digest, AllocationDigest)
        by_acct = {a.account_id: a for a in digest.allocations}
        # IRA covers 2 (10k/5k). Residual=4. M1/M2 get 2 each.
        assert by_acct["U_IRA"].contracts_allocated == 2
        assert by_acct["U_M1"].contracts_allocated == 2
        assert by_acct["U_M2"].contracts_allocated == 2
        assert digest.total_contracts_allocated == 6

    def test_no_snapshot_account_not_forfeited_to_peer(self):
        """DT Q4 invariant — no-snapshot acct doesn't give share to peer."""
        hh = _hh_snapshot({
            "U_PRINCIPAL": {
                "account_id": "U_PRINCIPAL", "margin_eligible": True,
                "cash_available": 0.0, "buying_power": 100_000.0,
            },
            "U_ADVISORY": {
                "account_id": "U_ADVISORY", "margin_eligible": True,
                "cash_available": 0.0, "buying_power": 100_000.0,
            },
        })
        candidate = _make_candidate(strike=50.0)
        import agt_equities.fa_block_margin as fam
        orig = fam._fetch_available_nlv
        # U_PRINCIPAL has no NLV snapshot — should not forfeit share to U_ADVISORY
        fam._fetch_available_nlv = lambda ids, **kw: {
            "U_ADVISORY": 100_000.0,
        }
        try:
            digest = _build_and_allocate(4, hh, candidate)
        finally:
            fam._fetch_available_nlv = orig
        by_acct = {a.account_id: a for a in digest.allocations}
        assert by_acct["U_ADVISORY"].contracts_allocated == 2  # 4/2 pro-rata
        assert digest.total_contracts_allocated == 2           # no-snapshot forfeit not redistributed


class TestTicketsFromDigest:
    def test_only_approved_become_tickets(self):
        p = CSPProposal(
            household_id="H1", ticker="ABC", strike=50.0,
            contracts_requested=4, expiry="20260516",
            account_ids=["U_OK", "U_DROP"],
            margin_eligible={"U_OK": True, "U_DROP": True},
        )
        import agt_equities.fa_block_margin as fam
        # U_DROP has no NLV snapshot → no_snapshot → 0 contracts, not redistributed
        digest = fam.allocate_csp(
            p, available_nlv_override={"U_OK": 100_000.0},
        )
        candidate = _make_candidate(ticker="abc", strike=50.0, mid=1.25,
                                    expiry="2026-05-16", annualized_yield=42.0)
        hh = _hh_snapshot({
            "U_OK": {"account_id": "U_OK", "margin_eligible": True},
            "U_DROP": {"account_id": "U_DROP", "margin_eligible": True},
        })
        tickets = _tickets_from_digest(digest, hh, candidate)
        assert len(tickets) == 1
        t = tickets[0]
        assert t["account_id"] == "U_OK"
        # U_OK is the only approved, takes pro-rata base_share=2 (4/2=2,
        # since no-snapshot peer gets 0, not forfeited).
        assert t["quantity"] == 2
        assert t["ticker"] == "ABC"
        assert t["strike"] == 50.0
        assert t["expiry"] == "20260516"
        assert t["action"] == "SELL"
        assert t["right"] == "P"
        assert t["mode"] == "CSP_ENTRY"
        assert t["status"] == "staged"
        assert t["limit_price"] == 1.25
        assert t["annualized_yield"] == 42.0

    def test_empty_digest_empty_tickets(self):
        p = CSPProposal(
            household_id="H1", ticker="ABC", strike=50.0,
            contracts_requested=0, expiry="20260516",
            account_ids=[],
        )
        import agt_equities.fa_block_margin as fam
        digest = fam.allocate_csp(p)
        assert _tickets_from_digest(digest, {"household": "H1"}, _make_candidate()) == []
