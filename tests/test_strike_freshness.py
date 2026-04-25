"""ADR-020 §B strike-freshness invariant tests."""
import pytest
from agt_equities.risk.staging_invariants import (
    check_mode_match,
    evaluate_strike_freshness,
    FreshnessResult,
    STRIKE_FRESHNESS_DRIFT_THRESHOLD,
)


@pytest.mark.sprint_a
def test_veto_on_drift_above_5pct():
    payload = {"spot_at_staging": 100.0, "broker_mode_at_staging": "paper"}
    result = evaluate_strike_freshness(payload=payload, spot_now=137.52)
    assert not result.passed
    assert result.reason == "stale_strike"
    assert result.evidence["drift_pct"] > 5.0


@pytest.mark.sprint_a
def test_fail_closed_on_spot_fetch_failure():
    payload = {"spot_at_staging": 100.0}
    result = evaluate_strike_freshness(payload=payload, spot_now=None)
    assert not result.passed
    assert result.reason == "freshness_check_unavailable"


@pytest.mark.sprint_a
def test_fail_closed_on_mode_mismatch():
    payload = {"broker_mode_at_staging": "paper"}
    result = check_mode_match(payload=payload, current_broker_mode="live")
    assert not result.passed
    assert result.reason == "mode_mismatch"


@pytest.mark.sprint_a
def test_legacy_row_warns_and_proceeds():
    payload = {}  # legacy row — no spot_at_staging, no broker_mode_at_staging
    strike_result = evaluate_strike_freshness(payload=payload, spot_now=137.52)
    mode_result = check_mode_match(payload=payload, current_broker_mode="live")
    assert strike_result.passed
    assert strike_result.evidence.get("legacy_row") is True
    assert mode_result.passed
    assert mode_result.evidence.get("legacy_row") is True


@pytest.mark.sprint_a
def test_drift_below_5pct_proceeds():
    payload = {"spot_at_staging": 100.0}
    result = evaluate_strike_freshness(payload=payload, spot_now=104.5)  # 4.5% drift
    assert result.passed
    assert result.evidence["drift_pct"] < 5.0
