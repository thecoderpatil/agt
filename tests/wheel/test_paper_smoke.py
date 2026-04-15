"""tests/wheel/test_paper_smoke.py — WHEEL-6 paper-gateway smoke.

Four stanzas, each exercises one IBKR surface the wheel pipeline depends
on. Every test uses the module-scoped `ib_paper` fixture, so a single
connect happens at module load and teardown disconnects once.

Skip behavior: if the paper gateway isn't up, `ib_paper` raises
pytest.skip and every test in the file is skipped with the same reason.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.paper


def test_connect_roundtrip(ib_paper) -> None:
    """Fixture handed us a live IB — isConnected() must be True."""
    assert ib_paper.isConnected(), "paper fixture handed a dead IB handle"


def test_qualify_spy(ib_paper, event_loop) -> None:
    """Qualify a liquid underlying. conId must resolve > 0."""
    from ib_async import Stock

    contract = Stock("SPY", "SMART", "USD")
    try:
        qualified = event_loop.run_until_complete(
            ib_paper.qualifyContractsAsync(contract)
        )
    except Exception as exc:
        pytest.fail(f"qualifyContractsAsync raised: {exc!r}")
    assert qualified, "qualifyContractsAsync returned empty list"
    assert qualified[0].conId > 0, f"SPY conId not resolved: {qualified[0]}"


def test_account_values_nonempty(ib_paper) -> None:
    """`accountValues()` must return at least NetLiquidation for a live acct."""
    try:
        vals = ib_paper.accountValues()
    except Exception as exc:
        pytest.fail(f"accountValues raised: {exc!r}")
    assert vals, "accountValues returned empty — no account attached to gateway?"
    tags = {v.tag for v in vals}
    assert "NetLiquidation" in tags, (
        f"NetLiquidation missing from accountValues tags: {sorted(tags)[:10]}"
    )


def test_spy_option_chain_params(ib_paper, event_loop) -> None:
    """reqSecDefOptParams on SPY must return at least one SMART-exchange entry.

    The wheel evaluator needs expirations + strikes; this is the minimum
    shape it consumes before qualifying individual option contracts.
    """
    from ib_async import Stock

    spy = Stock("SPY", "SMART", "USD")
    qualified = event_loop.run_until_complete(
        ib_paper.qualifyContractsAsync(spy)
    )
    assert qualified, "SPY did not qualify"
    spy_q = qualified[0]
    try:
        params = event_loop.run_until_complete(
            ib_paper.reqSecDefOptParamsAsync(
                spy_q.symbol, "", spy_q.secType, spy_q.conId
            )
        )
    except Exception as exc:
        pytest.fail(f"reqSecDefOptParamsAsync raised: {exc!r}")
    assert params, "reqSecDefOptParamsAsync returned empty"
    smart = [p for p in params if p.exchange == "SMART"]
    assert smart, f"no SMART exchange in option params: {[p.exchange for p in params]}"
    assert smart[0].expirations, "SMART params have no expirations"
    assert smart[0].strikes, "SMART params have no strikes"
