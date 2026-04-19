"""Synthetic-metrics tests for agt_equities.promotion_gates.

Paper-baseline tests (read-only prod DB) ship in MR-C.1. This file is
pure logic under synthetic inputs; runs in CI without DB access.
"""

from __future__ import annotations

import pytest

from agt_equities.promotion_gates import (
    GateMetrics,
    GateResult,
    evaluate_gates,
    load_config,
)

pytestmark = pytest.mark.sprint_a


@pytest.fixture
def config():
    return load_config()


def _exit_healthy() -> GateMetrics:
    return GateMetrics(
        engine="exit",
        shadow_div_bps_mean=2.0,
        shadow_div_bps_p99=4.0,
        tier0_trips_14d=0,
        tier1_trips_14d=0,
        sample_size_14d=70,
        novel=False,
        rejection_rate=0.0005,
        operator_override_variance_pvalue=None,  # N/A for defensive engines
    )


def _entry_healthy() -> GateMetrics:
    return GateMetrics(
        engine="entry",
        shadow_div_bps_mean=2.0,
        shadow_div_bps_p99=4.0,
        tier0_trips_14d=0,
        tier1_trips_14d=0,
        sample_size_14d=70,
        novel=False,
        rejection_rate=0.0005,
        operator_override_variance_pvalue=0.30,  # overrides NOT significantly better
    )


def test_healthy_exit_all_pass(config):
    result = evaluate_gates(_exit_healthy(), config)
    assert result.all_pass is True
    assert result.failing_gates == []
    # G5 is N/A for exit → pass=True by convention
    assert result.g5_operator_override_variance_pass is True


def test_healthy_entry_all_pass(config):
    result = evaluate_gates(_entry_healthy(), config)
    assert result.all_pass is True
    assert result.failing_gates == []


def test_g1_mean_above_threshold_fails(config):
    m = _exit_healthy()
    bad = GateMetrics(**{**m.__dict__, "shadow_div_bps_mean": 3.5})
    result = evaluate_gates(bad, config)
    assert result.all_pass is False
    assert "g1_shadow_divergence" in result.failing_gates


def test_g1_p99_above_threshold_fails(config):
    m = _exit_healthy()
    bad = GateMetrics(**{**m.__dict__, "shadow_div_bps_p99": 6.0})
    result = evaluate_gates(bad, config)
    assert result.g1_shadow_divergence_pass is False
    assert "g1_shadow_divergence" in result.failing_gates


def test_g2_single_tier0_trip_fails(config):
    m = _exit_healthy()
    bad = GateMetrics(**{**m.__dict__, "tier0_trips_14d": 1})
    result = evaluate_gates(bad, config)
    assert result.g2_zero_trip_dry_run_pass is False
    assert "g2_zero_trip_dry_run" in result.failing_gates


def test_g3_undersized_sample_fails(config):
    m = _exit_healthy()
    bad = GateMetrics(**{**m.__dict__, "sample_size_14d": 50})
    result = evaluate_gates(bad, config)
    assert result.g3_sample_size_pass is False
    assert "g3_sample_size" in result.failing_gates


def test_g3_novel_engine_higher_floor(config):
    m = _exit_healthy()
    # 70 decisions is fine for non-novel (floor=60) but fails for novel (floor=120)
    novel = GateMetrics(**{**m.__dict__, "novel": True})
    result = evaluate_gates(novel, config)
    assert result.g3_sample_size_pass is False
    assert "g3_sample_size" in result.failing_gates


def test_g4_rejection_rate_above_threshold_fails(config):
    m = _exit_healthy()
    bad = GateMetrics(**{**m.__dict__, "rejection_rate": 0.002})
    result = evaluate_gates(bad, config)
    assert result.g4_broker_rejection_rate_pass is False
    assert "g4_broker_rejection_rate" in result.failing_gates


def test_g5_entry_missing_pvalue_fails(config):
    m = _entry_healthy()
    bad = GateMetrics(**{**m.__dict__, "operator_override_variance_pvalue": None})
    result = evaluate_gates(bad, config)
    assert result.g5_operator_override_variance_pass is False
    assert "g5_operator_override_variance" in result.failing_gates


def test_g5_entry_significant_override_win_fails(config):
    m = _entry_healthy()
    # p < 0.05 means operator overrides ARE significantly better = engine not ready
    bad = GateMetrics(**{**m.__dict__, "operator_override_variance_pvalue": 0.02})
    result = evaluate_gates(bad, config)
    assert result.g5_operator_override_variance_pass is False


def test_unknown_engine_raises(config):
    m = GateMetrics(
        engine="bogus",  # type: ignore[arg-type]
        shadow_div_bps_mean=1.0,
        shadow_div_bps_p99=1.0,
        tier0_trips_14d=0,
        tier1_trips_14d=0,
        sample_size_14d=100,
        novel=False,
        rejection_rate=0.0,
        operator_override_variance_pvalue=None,
    )
    with pytest.raises(ValueError, match="unknown engine"):
        evaluate_gates(m, config)


def test_config_has_expected_shape(config):
    # Structural smoke — catches accidental yaml typos.
    assert set(config["gates"].keys()) == {
        "g1_shadow_divergence",
        "g2_zero_trip_dry_run",
        "g3_sample_size",
        "g4_broker_rejection_rate",
        "g5_operator_override_variance",
    }
    assert set(config["engines"].keys()) == {"exit", "roll", "harvest", "entry"}
    assert config["engines"]["entry"]["g5_applicable"] is True
    assert config["engines"]["exit"]["g5_applicable"] is False
    assert config["canary_ramp"]["c4"]["min_sessions"] is None  # permanent
