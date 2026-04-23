"""Sprint 6 Mega-MR 5 — pre-gateway skeleton shape tests.

The pre-gateway evaluators ship as SKELETONS in this MR (each raises
NotImplementedError). This test file locks the public contracts:
TripResult shape, evaluator signatures, and the stable module
attribute surface. Bodies land in Sprint 7+ and the tests there will
supplement these with behavioral assertions.

These tests do NOT assert ANY evaluator body works — they only assert
the shape is stable.
"""
from __future__ import annotations

import inspect
from dataclasses import is_dataclass, fields

import pytest

pytestmark = pytest.mark.sprint_a


def test_module_exposes_required_surface():
    from agt_equities.risk import pregateway
    expected = {
        "Engine",
        "TripResult",
        "evaluate_k1_session_drawdown",
        "evaluate_k2_consecutive_rejections",
        "evaluate_k3_latency",
        "evaluate_k4_correlation_drift",
        "evaluate_order",
    }
    actual = set(pregateway.__all__)
    missing = expected - actual
    assert not missing, f"pregateway missing: {missing}"


def test_trip_result_is_frozen_dataclass_with_expected_fields():
    from agt_equities.risk.pregateway import TripResult
    assert is_dataclass(TripResult)
    names = {f.name for f in fields(TripResult)}
    expected = {
        "tripped",
        "k1_session_drawdown_tripped",
        "k2_consecutive_rejections_tripped",
        "k3_latency_tripped",
        "k4_correlation_drift_tripped",
        "reason",
        "evidence",
    }
    assert names == expected


def test_evaluate_k1_signature_is_stable():
    from agt_equities.risk.pregateway import evaluate_k1_session_drawdown
    sig = inspect.signature(evaluate_k1_session_drawdown)
    params = set(sig.parameters)
    assert params == {"engine", "session_nav", "pre_open_nav", "threshold_pct"}


def test_evaluate_k2_signature_is_stable():
    from agt_equities.risk.pregateway import evaluate_k2_consecutive_rejections
    sig = inspect.signature(evaluate_k2_consecutive_rejections)
    params = set(sig.parameters)
    assert params == {
        "engine", "recent_rejections", "threshold_count", "window_seconds",
    }


def test_evaluate_k3_signature_is_stable():
    from agt_equities.risk.pregateway import evaluate_k3_latency
    sig = inspect.signature(evaluate_k3_latency)
    params = set(sig.parameters)
    assert params == {"engine", "recent_latencies_ms", "threshold_p95_ms"}


def test_evaluate_k4_signature_is_stable():
    from agt_equities.risk.pregateway import evaluate_k4_correlation_drift
    sig = inspect.signature(evaluate_k4_correlation_drift)
    params = set(sig.parameters)
    assert params == {
        "engine", "live_decisions", "paper_decisions", "threshold_correlation",
    }


def test_evaluate_order_signature_is_stable():
    from agt_equities.risk.pregateway import evaluate_order
    sig = inspect.signature(evaluate_order)
    params = set(sig.parameters)
    assert params == {"engine", "order_payload"}


def test_evaluators_raise_not_implemented_not_other_errors():
    """The skeletons must raise NotImplementedError specifically — not
    AttributeError / TypeError / KeyError. Sprint 7 will replace the
    bodies; until then, callers must see a clean NIE."""
    from agt_equities.risk import pregateway

    with pytest.raises(NotImplementedError):
        pregateway.evaluate_k1_session_drawdown(
            engine="exit", session_nav=0.0, pre_open_nav=0.0, threshold_pct=0.05,
        )
    with pytest.raises(NotImplementedError):
        pregateway.evaluate_k2_consecutive_rejections(
            engine="exit", recent_rejections=[], threshold_count=3, window_seconds=60,
        )
    with pytest.raises(NotImplementedError):
        pregateway.evaluate_k3_latency(
            engine="exit", recent_latencies_ms=[], threshold_p95_ms=500,
        )
    with pytest.raises(NotImplementedError):
        pregateway.evaluate_k4_correlation_drift(
            engine="exit", live_decisions=[], paper_decisions=[],
            threshold_correlation=0.95,
        )
    with pytest.raises(NotImplementedError):
        pregateway.evaluate_order(engine="exit", order_payload={})
