"""Tests for agt_equities.approval_policy — 6-way policy matrix."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agt_equities.approval_policy import (
    needs_csp_approval,
    needs_liquidate_approval,
    needs_roll_approval,
)


def _ctx(broker_mode: str, engine: str):
    """Minimal RunContext stand-in — only broker_mode and engine fields needed."""
    ctx = MagicMock()
    ctx.broker_mode = broker_mode
    ctx.engine = engine
    return ctx


@pytest.mark.sprint_a
class TestNeedsCspApproval:
    def test_live_csp_requires_approval(self):
        assert needs_csp_approval(_ctx("live", "csp")) is True

    def test_paper_csp_no_approval(self):
        assert needs_csp_approval(_ctx("paper", "csp")) is False

    def test_live_cc_no_approval(self):
        assert needs_csp_approval(_ctx("live", "cc")) is False


@pytest.mark.sprint_a
class TestNeedsLiquidateApproval:
    def test_live_requires_approval(self):
        assert needs_liquidate_approval(_ctx("live", "roll")) is True

    def test_paper_no_approval(self):
        assert needs_liquidate_approval(_ctx("paper", "roll")) is False


@pytest.mark.sprint_a
class TestNeedsRollApproval:
    def test_always_false_paper(self):
        assert needs_roll_approval(_ctx("paper", "roll")) is False

    def test_always_false_live(self):
        assert needs_roll_approval(_ctx("live", "roll")) is False
