"""
Sprint 14 P1.1 — STC liquidate KeyError fix + ADR-020 enrichment.

Tests:
  1. STC MKT ticket has no limit_price; .get() fallback is safe (not KeyError)
  2. _build_liquidate_tickets injects engine/run_id/gate_verdicts into all tickets
  3. BTC + STC from same invocation share run_id; different invocations differ
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.sprint_a

from telegram_bot import _build_liquidate_tickets


def _liq_payload(*, shares: int = 400, contracts: int = 4) -> dict:
    return {
        "ticker": "AAPL",
        "account_id": "DUP751004",
        "contracts": contracts,
        "shares": shares,
        "btc_limit": 0.85,
        "strike": 190.0,
        "expiry": "2026-05-02",
        "reason": "OPPORTUNITY_COST spot=266.74 net=247.49>basis=213.50",
    }


def test_stc_market_order_no_keyerror():
    """STC MKT ticket must not carry limit_price; .get() fallback returns 0."""
    tickets = _build_liquidate_tickets(_liq_payload())
    stc = next(t for t in tickets if t["sec_type"] == "STK")
    assert stc["order_type"] == "MKT"
    assert "limit_price" not in stc
    # Fixed _place_single_order uses payload.get("limit_price", bid) — must not raise.
    assert stc.get("limit_price", 0) == 0


def test_liquidate_tickets_have_adr020_keys():
    """All tickets from _build_liquidate_tickets must carry ADR-020 enrichment."""
    tickets = _build_liquidate_tickets(_liq_payload())
    assert len(tickets) == 2
    for t in tickets:
        assert t.get("engine") == "v2_router_liquidate", f"engine missing in {t['sec_type']} ticket"
        run_id = t.get("run_id")
        assert run_id is not None and len(run_id) == 32, f"run_id invalid in {t['sec_type']} ticket"
        gv = t.get("gate_verdicts", {})
        assert gv.get("v2_router") is True
        assert gv.get("liquidate") is True


def test_liquidate_btc_stc_share_run_id():
    """BTC + STC from the same invocation share run_id; separate calls differ."""
    tickets = _build_liquidate_tickets(_liq_payload())
    btc = next(t for t in tickets if t["sec_type"] == "OPT")
    stc = next(t for t in tickets if t["sec_type"] == "STK")
    assert btc["run_id"] == stc["run_id"]
    tickets2 = _build_liquidate_tickets(_liq_payload())
    assert tickets2[0]["run_id"] != btc["run_id"]
