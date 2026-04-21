"""Tests for agt_equities.broker_preflight.

No IB connection required -- ib_conn is mocked.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agt_equities.broker_preflight import (
    BrokerIdentityMismatch,
    run_broker_identity_preflight,
)


def _make_ib(accounts: list[str]):
    ib = MagicMock()
    ib.accounts = MagicMock(return_value=accounts)
    return ib


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_paper_accounts_pass(monkeypatch):
    """D-prefix accounts with broker_mode='paper' -- no exception."""
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", ["DU123456", "DU789012"])
    ib = _make_ib(["DU123456", "DU789012"])
    await run_broker_identity_preflight(ib, "paper")


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_live_accounts_pass(monkeypatch):
    """U-prefix accounts with broker_mode='live' -- no exception."""
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", ["U21971297", "U22388499"])
    ib = _make_ib(["U21971297", "U22388499"])
    await run_broker_identity_preflight(ib, "live")


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_live_accounts_with_paper_mode_raises(monkeypatch):
    """U-prefix accounts declared as paper -- BrokerIdentityMismatch."""
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", ["U21971297"])
    ib = _make_ib(["U21971297"])
    with pytest.raises(BrokerIdentityMismatch, match="U21971297"):
        await run_broker_identity_preflight(ib, "paper")


@pytest.mark.sprint_a
@pytest.mark.asyncio
async def test_ib_api_failure_is_non_fatal(monkeypatch):
    """ib_conn.accounts() raising must not propagate -- dynamic check is non-fatal."""
    monkeypatch.setattr("agt_equities.config.ACTIVE_ACCOUNTS", ["U21971297"])
    ib = MagicMock()
    ib.accounts = MagicMock(side_effect=RuntimeError("IB API error"))
    try:
        await run_broker_identity_preflight(ib, "live")
    except BrokerIdentityMismatch:
        pass  # acceptable -- static check already passed with U prefix
    except Exception as exc:
        pytest.fail(f"Non-BrokerIdentityMismatch exception propagated: {exc!r}")
