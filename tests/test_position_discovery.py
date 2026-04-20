"""Tests for agt_equities.position_discovery.

Uses mock ib_conn -- no IB connection required. Covers:
  - empty positions returns well-formed empty dict
  - IB failure returns error dict (never raises)
  - household_filter parameter accepted
  - include_staged=False path executes without error
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture()
def mock_ib():
    ib = MagicMock()
    ib.reqPositionsAsync = AsyncMock(return_value=[])
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ib.reqPnLSingle = MagicMock(return_value=MagicMock(unrealizedPnL=None))
    return ib


@pytest.fixture()
def empty_margin_stats():
    return {
        "accounts": {},
        "agg_margin_nlv": 0.0,
        "agg_margin_el": 0.0,
        "agg_el_pct": 0.0,
        "all_book_nlv": 0.0,
        "error": None,
    }


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_empty_positions_returns_well_formed_dict(mock_ib, empty_margin_stats):
    """No IB positions -> households dict is empty, all_book_nlv present."""
    from agt_equities.position_discovery import discover_positions

    result = await discover_positions(mock_ib, empty_margin_stats)
    assert "households" in result
    assert "all_book_nlv" in result
    assert result["households"] == {}
    assert "error" not in result or result.get("error") is None


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_ib_failure_returns_error_dict_not_raises(empty_margin_stats):
    """reqPositionsAsync raising must return error dict, not propagate exception."""
    from agt_equities.position_discovery import discover_positions

    bad_ib = MagicMock()
    bad_ib.reqPositionsAsync = AsyncMock(side_effect=RuntimeError("IB down"))

    result = await discover_positions(bad_ib, empty_margin_stats)
    assert "error" in result
    assert result["households"] == {}
    assert result["all_book_nlv"] == 0.0


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_household_filter_accepted(mock_ib, empty_margin_stats):
    """household_filter parameter accepted without error on empty positions."""
    from agt_equities.position_discovery import discover_positions

    result = await discover_positions(
        mock_ib, empty_margin_stats, household_filter="Yash_Household"
    )
    assert "households" in result


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_include_staged_false_accepted(mock_ib, empty_margin_stats):
    """include_staged=False path executes without error (skips DB staged query)."""
    from agt_equities.position_discovery import discover_positions

    result = await discover_positions(mock_ib, empty_margin_stats, include_staged=False)
    assert "households" in result
