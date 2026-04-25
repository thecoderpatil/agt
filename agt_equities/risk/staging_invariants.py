"""ADR-020 §B pre-gateway staging invariants.

Per-order data freshness checks. Distinct from pregateway.py K1-K4
which are engine-wide trip evaluators. These checks veto a single
order; they do not halt the engine.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)

STRIKE_FRESHNESS_DRIFT_THRESHOLD = 0.05  # 5% spot drift → veto


@dataclass(frozen=True)
class FreshnessResult:
    passed: bool
    reason: Optional[str] = None  # None when passed; one of "stale_strike",
                                   # "mode_mismatch", "freshness_check_unavailable"
    evidence: dict[str, Any] = field(default_factory=dict)


def check_mode_match(
    *,
    payload: dict,
    current_broker_mode: str,
) -> FreshnessResult:
    """Verify the ticket was staged under the same broker mode as current runtime."""
    staged_mode = payload.get("broker_mode_at_staging")
    if staged_mode is None:
        # Legacy row — warn + proceed
        logger.warning(
            "Legacy ticket missing broker_mode_at_staging; proceeding with mode-match skip"
        )
        return FreshnessResult(passed=True, evidence={"legacy_row": True})
    if staged_mode != current_broker_mode:
        return FreshnessResult(
            passed=False,
            reason="mode_mismatch",
            evidence={
                "staged_mode": staged_mode,
                "current_mode": current_broker_mode,
            },
        )
    return FreshnessResult(passed=True)


def evaluate_strike_freshness(
    *,
    payload: dict,
    spot_now: Optional[float],
    drift_threshold: float = STRIKE_FRESHNESS_DRIFT_THRESHOLD,
) -> FreshnessResult:
    """Veto a staged order whose underlying spot moved past drift_threshold.

    Per ADR-020 §B invariant 3. Returns:
      - passed=True with evidence {legacy_row: True} if payload missing
        spot_at_staging (legacy row, warn + proceed).
      - passed=False reason="freshness_check_unavailable" if spot_now is None.
      - passed=False reason="stale_strike" if drift exceeds threshold.
      - passed=True otherwise.
    """
    spot_at_staging = payload.get("spot_at_staging")
    if spot_at_staging is None:
        logger.warning(
            "Legacy ticket missing spot_at_staging; skipping strike freshness check"
        )
        return FreshnessResult(passed=True, evidence={"legacy_row": True})
    if spot_now is None:
        return FreshnessResult(
            passed=False,
            reason="freshness_check_unavailable",
            evidence={"spot_at_staging": spot_at_staging, "spot_now": None},
        )
    try:
        drift = abs(float(spot_now) - float(spot_at_staging)) / float(spot_at_staging)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        logger.error("Strike freshness drift compute failed: %s", exc)
        return FreshnessResult(
            passed=False,
            reason="freshness_check_unavailable",
            evidence={"compute_error": str(exc)},
        )
    if drift > drift_threshold:
        return FreshnessResult(
            passed=False,
            reason="stale_strike",
            evidence={
                "drift_pct": round(drift * 100, 4),
                "spot_at_staging": float(spot_at_staging),
                "spot_now": float(spot_now),
                "threshold_pct": drift_threshold * 100,
            },
        )
    return FreshnessResult(
        passed=True,
        evidence={"drift_pct": round(drift * 100, 4)},
    )

QUOTE_FRESHNESS_DRIFT_THRESHOLD = 0.10  # 10% premium drift → refresh
QUOTE_FLOOR_PCT_OF_STAGED = 0.20        # if refreshed premium < 20% of staged,
                                         # veto instead of refresh — dead market
QUOTE_ABSOLUTE_FLOOR = 0.05             # never refresh below $0.05 absolute


def evaluate_quote_freshness(
    *,
    payload: dict,
    premium_now: Optional[float],
    drift_threshold: float = QUOTE_FRESHNESS_DRIFT_THRESHOLD,
) -> FreshnessResult:
    """Veto if premium has collapsed; refresh if drifted within band.

    Per ADR-020 §B invariant 4. Returns:
      - passed=True {legacy_row: True} if payload missing premium_at_staging.
      - passed=False reason="freshness_check_unavailable" if premium_now None.
      - passed=False reason="stale_quote" if premium_now < floor (effectively
        dead market — refreshing would still not fill).
      - passed=True with evidence including suggested refreshed_limit_price
        when drift exceeds threshold but premium is still above floor.
      - passed=True with no refresh suggestion when drift is within band.
    """
    premium_at_staging = payload.get("premium_at_staging")
    if premium_at_staging is None:
        return FreshnessResult(passed=True, evidence={"legacy_row": True})
    if premium_now is None:
        return FreshnessResult(
            passed=False,
            reason="freshness_check_unavailable",
            evidence={"premium_at_staging": premium_at_staging, "premium_now": None},
        )
    try:
        premium_at_staging = float(premium_at_staging)
        premium_now = float(premium_now)
    except (TypeError, ValueError) as exc:
        return FreshnessResult(
            passed=False,
            reason="freshness_check_unavailable",
            evidence={"compute_error": str(exc)},
        )

    floor = max(
        QUOTE_ABSOLUTE_FLOOR,
        premium_at_staging * QUOTE_FLOOR_PCT_OF_STAGED,
    )
    if premium_now < floor:
        return FreshnessResult(
            passed=False,
            reason="stale_quote",
            evidence={
                "premium_at_staging": premium_at_staging,
                "premium_now": premium_now,
                "floor": floor,
                "collapse_pct": round(
                    (premium_at_staging - premium_now) / premium_at_staging * 100, 4
                ),
            },
        )

    drift = abs(premium_now - premium_at_staging) / premium_at_staging
    if drift > drift_threshold:
        return FreshnessResult(
            passed=True,
            evidence={
                "drift_pct": round(drift * 100, 4),
                "refresh_suggested": True,
                "refreshed_limit_price": premium_now,
                "premium_at_staging": premium_at_staging,
                "premium_now": premium_now,
            },
        )

    return FreshnessResult(
        passed=True,
        evidence={
            "drift_pct": round(drift * 100, 4),
            "refresh_suggested": False,
        },
    )


def refresh_limit_price(
    *,
    original_limit: float,
    freshness_evidence: dict,
) -> float:
    """Apply a freshness-suggested refresh to the limit price, or return original.

    Pure function, no side effects. Caller decides when to invoke.
    """
    if freshness_evidence.get("refresh_suggested"):
        return float(freshness_evidence["refreshed_limit_price"])
    return float(original_limit)

