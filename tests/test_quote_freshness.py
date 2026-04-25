"""ADR-020 §B quote-freshness invariant tests.

Five unit tests covering evaluate_quote_freshness + refresh_limit_price.
No IB dependency — evaluator is pure-functional, takes premium_now as input.
All tests marked @pytest.mark.sprint_a per ADR-020 test convention.
"""
import pytest
from agt_equities.risk.staging_invariants import (
    evaluate_quote_freshness,
    refresh_limit_price,
    FreshnessResult,
    QUOTE_FRESHNESS_DRIFT_THRESHOLD,
    QUOTE_FLOOR_PCT_OF_STAGED,
    QUOTE_ABSOLUTE_FLOOR,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal staged-order payload
# ---------------------------------------------------------------------------

def _payload(premium_at_staging=None, action="SELL"):
    base = {
        "ticker": "AAPL",
        "action": action,
        "right": "P",
        "strike": 185.0,
        "expiry": "2026-05-16",
    }
    if premium_at_staging is not None:
        base["premium_at_staging"] = premium_at_staging
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.sprint_a
def test_refresh_on_mild_drift():
    """Premium drifted 20% above threshold but still above floor -> refresh suggested.

    Setup: premium_at_staging=0.50, premium_now=0.40 (20% drift).
    Floor = max(QUOTE_ABSOLUTE_FLOOR=0.05, 0.50 * QUOTE_FLOOR_PCT_OF_STAGED=0.20) = 0.10.
    0.40 >= 0.10 -> not vetoed. 20% > 10% threshold -> refresh suggested.
    """
    payload = _payload(premium_at_staging=0.50)
    result = evaluate_quote_freshness(payload=payload, premium_now=0.40)

    assert result.passed, f"Expected passed=True, got reason={result.reason!r}"
    assert result.evidence.get("refresh_suggested") is True
    assert result.evidence.get("drift_pct") > QUOTE_FRESHNESS_DRIFT_THRESHOLD * 100
    assert "refreshed_limit_price" in result.evidence
    assert result.evidence["refreshed_limit_price"] == pytest.approx(0.40)

    # refresh_limit_price picks up the suggested price
    final = refresh_limit_price(
        original_limit=0.50,
        freshness_evidence=result.evidence,
    )
    assert final == pytest.approx(0.40)


@pytest.mark.sprint_a
def test_veto_on_premium_collapse():
    """Premium collapsed far below floor -> veto with reason='stale_quote'.

    Setup: premium_at_staging=0.50, premium_now=0.02.
    Floor = max(0.05, 0.50 * 0.20) = 0.10. 0.02 < 0.10 -> dead market -> veto.
    """
    payload = _payload(premium_at_staging=0.50)
    result = evaluate_quote_freshness(payload=payload, premium_now=0.02)

    assert not result.passed
    assert result.reason == "stale_quote"

    ev = result.evidence
    assert ev["premium_now"] == pytest.approx(0.02)
    assert ev["premium_at_staging"] == pytest.approx(0.50)
    expected_floor = max(QUOTE_ABSOLUTE_FLOOR, 0.50 * QUOTE_FLOOR_PCT_OF_STAGED)
    assert ev["floor"] == pytest.approx(expected_floor)
    assert ev["collapse_pct"] > 0

    # Absolute-floor edge: premium just below QUOTE_ABSOLUTE_FLOOR -> still vetoed
    result2 = evaluate_quote_freshness(payload=payload, premium_now=QUOTE_ABSOLUTE_FLOOR - 0.001)
    assert not result2.passed
    assert result2.reason == "stale_quote"


@pytest.mark.sprint_a
def test_fail_closed_on_quote_fetch_failure():
    """premium_now=None (IB fetch failure) -> fail-closed, reason='freshness_check_unavailable'.

    The evaluator never raises; it returns a failed FreshnessResult.
    Gateway code must not proceed to placeOrder on this result.
    """
    payload = _payload(premium_at_staging=0.50)
    result = evaluate_quote_freshness(payload=payload, premium_now=None)

    assert not result.passed
    assert result.reason == "freshness_check_unavailable"
    assert result.evidence.get("premium_now") is None
    assert result.evidence.get("premium_at_staging") == pytest.approx(0.50)


@pytest.mark.sprint_a
def test_legacy_row_proceeds():
    """Payload missing premium_at_staging (legacy row) -> passed=True with legacy_row sentinel.

    Orders staged before ADR-020 have no premium_at_staging field. Evaluator
    logs a warning and proceeds -- the freshness check is skipped, not failed.
    refresh_limit_price returns the original limit unchanged (no refresh_suggested).
    """
    payload = _payload()  # no premium_at_staging key
    result = evaluate_quote_freshness(payload=payload, premium_now=0.35)

    assert result.passed
    assert result.evidence.get("legacy_row") is True
    assert result.reason is None

    # refresh_limit_price must return original when refresh_suggested is absent/False
    final = refresh_limit_price(original_limit=0.48, freshness_evidence=result.evidence)
    assert final == pytest.approx(0.48)


@pytest.mark.sprint_a
def test_drift_within_band_no_refresh():
    """Premium drifted only 8% (below 10% threshold) -> passed=True, no refresh.

    Setup: premium_at_staging=0.50, premium_now=0.46 (8% drift).
    Floor check: 0.46 >= 0.10 -> not vetoed. 8% < 10% threshold -> no refresh suggested.
    """
    payload = _payload(premium_at_staging=0.50)
    result = evaluate_quote_freshness(payload=payload, premium_now=0.46)

    assert result.passed
    assert result.evidence.get("refresh_suggested") is False
    assert result.evidence.get("drift_pct") < QUOTE_FRESHNESS_DRIFT_THRESHOLD * 100

    # refresh_limit_price returns the original when refresh_suggested=False
    final = refresh_limit_price(original_limit=0.50, freshness_evidence=result.evidence)
    assert final == pytest.approx(0.50)
