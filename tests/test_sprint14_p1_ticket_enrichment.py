"""
Sprint 14 P1 — CSP ticket enrichment: inception_delta + digest fields.

Tests:
  1. _tickets_from_digest includes inception_delta equal to candidate.delta
  2. _tickets_from_digest includes delta, otm_pct, spot from candidate
  3. build_digest_payload.premium_dollars falls back to limit_price*100
  4. build_digest_payload.spot reads from the 'spot' key
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Helpers — lightweight stubs for AllocationDigest + ScanCandidate
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from agt_equities.fa_block_margin import AccountAllocation, AllocationDigest, CSPProposal, STATUS_APPROVED
from agt_equities.scan_bridge import ScanCandidate
from agt_equities.csp_allocator import _tickets_from_digest


def _make_digest(account_id: str, contracts: int = 3) -> AllocationDigest:
    proposal = CSPProposal(
        household_id="Yash_Household",
        ticker="ARM",
        strike=210.0,
        contracts_requested=contracts,
        expiry="20260501",
        account_ids=[account_id],
    )
    alloc = AccountAllocation(
        account_id=account_id,
        contracts_allocated=contracts,
        margin_check_status=STATUS_APPROVED,
        margin_check_reason="test",
        available_nlv=None,
    )
    return AllocationDigest(
        proposal=proposal,
        allocations=(alloc,),
        total_contracts_requested=contracts,
        total_contracts_allocated=contracts,
    )


def _make_candidate() -> ScanCandidate:
    return ScanCandidate(
        ticker="ARM",
        strike=210.0,
        mid=2.98,
        expiry="2026-05-01",
        annualized_yield=129.49,
        delta=0.18,
        otm_pct=5.57,
        current_price=222.38,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ticket_has_inception_delta(monkeypatch):
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    candidate = _make_candidate()
    digest = _make_digest("DUP751003")
    hh = {"household": "Yash_Household"}
    tickets = _tickets_from_digest(digest, hh, candidate)
    assert len(tickets) == 1
    assert tickets[0]["inception_delta"] == pytest.approx(0.18)


def test_ticket_has_delta_otm_spot(monkeypatch):
    monkeypatch.setenv("AGT_BROKER_MODE", "paper")
    candidate = _make_candidate()
    digest = _make_digest("DUP751003")
    hh = {"household": "Yash_Household"}
    tickets = _tickets_from_digest(digest, hh, candidate)
    t = tickets[0]
    assert t["delta"] == pytest.approx(0.18)
    assert t["otm_pct"] == pytest.approx(5.57)
    assert t["spot"] == pytest.approx(222.38)


def test_digest_premium_uses_limit_price():
    from csp_digest_runner import build_digest_payload
    latest = {
        "staged": [{"ticker": "ARM", "strike": 210.0, "expiry": "20260501",
                    "annualized_yield": 129.49, "limit_price": 2.98,
                    "account_id": "DUP751003", "quantity": 3}],
        "rejected": [],
        "run_id": "test-run",
        "trade_date": "2026-04-27",
        "created_at": "2026-04-27T13:00:00+00:00",
    }
    payload = build_digest_payload(latest=latest, commentaries={})
    assert len(payload.candidates) == 1
    assert payload.candidates[0].premium_dollars == pytest.approx(298.0)


def test_digest_spot_uses_spot_key():
    from csp_digest_runner import build_digest_payload
    latest = {
        "staged": [{"ticker": "ARM", "strike": 210.0, "expiry": "20260501",
                    "annualized_yield": 129.49, "spot": 222.38,
                    "account_id": "DUP751003", "quantity": 3}],
        "rejected": [],
        "run_id": "test-run",
        "trade_date": "2026-04-27",
        "created_at": "2026-04-27T13:00:00+00:00",
    }
    payload = build_digest_payload(latest=latest, commentaries={})
    assert payload.candidates[0].spot == pytest.approx(222.38)
