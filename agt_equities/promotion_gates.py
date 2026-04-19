"""ADR-011 promotion-gate evaluator.

Pure function: takes a GateMetrics snapshot + threshold config, returns
a GateResult with per-gate pass/fail and aggregate all_pass. No I/O,
no prod DB reads, no side effects. Paper-baseline extraction is a
separate module (MR-C.1, not yet shipped).

Loader reads ``config/promotion_gates.yaml`` at the repo root by default;
override via ``load_config(path)`` for tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

Engine = Literal["exit", "roll", "harvest", "entry"]

_CONFIG_ANCHOR = Path(__file__).resolve().parent.parent / "config" / "promotion_gates.yaml"
_DEFAULT_CONFIG_PATH = Path(os.environ.get("AGT_PROMOTION_GATES_CONFIG", str(_CONFIG_ANCHOR)))


@dataclass(frozen=True)
class GateMetrics:
    """Inputs to the gate evaluator. All fields are plain numbers; the
    evaluator does not query any DB.

    Attributes:
        engine: which engine these metrics describe.
        shadow_div_bps_mean: G1 mean divergence in bps.
        shadow_div_bps_p99: G1 p99 divergence in bps.
        tier0_trips_14d: G2 Tier-0 invariant trips in trailing 14 days.
        tier1_trips_14d: G2 Tier-1 invariant trips in trailing 14 days.
        sample_size_14d: G3 staged decisions of this engine in trailing 14 days.
        novel: G3 True if engine has no prior live history (cold start).
        rejection_rate: G4 IB rejection rate in [0, 1].
        operator_override_variance_pvalue: G5 one-sided t-test p-value. None = N/A for non-entry engines.
    """

    engine: Engine
    shadow_div_bps_mean: float
    shadow_div_bps_p99: float
    tier0_trips_14d: int
    tier1_trips_14d: int
    sample_size_14d: int
    novel: bool
    rejection_rate: float
    operator_override_variance_pvalue: float | None


@dataclass
class GateResult:
    """Per-gate pass/fail + aggregate."""

    engine: Engine
    g1_shadow_divergence_pass: bool
    g2_zero_trip_dry_run_pass: bool
    g3_sample_size_pass: bool
    g4_broker_rejection_rate_pass: bool
    g5_operator_override_variance_pass: bool
    all_pass: bool
    failing_gates: list[str] = field(default_factory=list)


def load_config(path: str | Path | None = None) -> dict:
    """Load promotion_gates.yaml. Default path is repo-root config/promotion_gates.yaml."""
    resolved = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"promotion_gates config not found at {resolved}. "
            "Either ship config/promotion_gates.yaml or pass explicit path."
        ) from exc
    if not isinstance(cfg, dict) or "gates" not in cfg or "engines" not in cfg:
        raise ValueError(f"promotion_gates config at {resolved} is malformed (missing 'gates' or 'engines').")
    return cfg


def evaluate_gates(metrics: GateMetrics, config: dict) -> GateResult:
    """Evaluate all 5 gates against the supplied metrics.

    Returns a GateResult. Gates evaluated independently — failure of any
    gate does not short-circuit evaluation of the others (all 5 statuses
    are always populated so the failing_gates list is complete).

    G5 is N/A for engines where the config marks g5_applicable=false; we
    set g5 pass=True in that case (N/A counts as not-blocking) but record
    the metric p-value regardless for audit.
    """
    gates_cfg = config["gates"]
    engines_cfg = config["engines"]

    engine_cfg = engines_cfg.get(metrics.engine)
    if engine_cfg is None:
        raise ValueError(f"unknown engine {metrics.engine!r}; expected one of {list(engines_cfg)}")

    g5_applicable = bool(engine_cfg.get("g5_applicable", False))

    # G1 — shadow divergence
    g1_cfg = gates_cfg["g1_shadow_divergence"]
    g1_pass = (
        metrics.shadow_div_bps_mean < g1_cfg["mean_bps_max"]
        and metrics.shadow_div_bps_p99 < g1_cfg["p99_bps_max"]
    )

    # G2 — zero-trip dry run (Tier-0 AND Tier-1 must both be at cap)
    g2_cfg = gates_cfg["g2_zero_trip_dry_run"]
    g2_pass = (
        metrics.tier0_trips_14d <= g2_cfg["tier0_max"]
        and metrics.tier1_trips_14d <= g2_cfg["tier1_max"]
    )

    # G3 — sample size (novel engines use higher floor)
    g3_cfg = gates_cfg["g3_sample_size"]
    floor = g3_cfg["min_decisions_novel"] if metrics.novel else g3_cfg["min_decisions"]
    g3_pass = metrics.sample_size_14d >= floor

    # G4 — broker rejection rate
    g4_cfg = gates_cfg["g4_broker_rejection_rate"]
    g4_pass = metrics.rejection_rate < g4_cfg["max_rate"]

    # G5 — operator override variance (entry only)
    g5_cfg = gates_cfg["g5_operator_override_variance"]
    if not g5_applicable:
        g5_pass = True  # N/A = not blocking
    else:
        pv = metrics.operator_override_variance_pvalue
        if pv is None:
            g5_pass = False  # entry engine with no p-value = cannot attest
        else:
            g5_pass = pv >= g5_cfg["alpha"]

    failing: list[str] = []
    if not g1_pass:
        failing.append("g1_shadow_divergence")
    if not g2_pass:
        failing.append("g2_zero_trip_dry_run")
    if not g3_pass:
        failing.append("g3_sample_size")
    if not g4_pass:
        failing.append("g4_broker_rejection_rate")
    if g5_applicable and not g5_pass:
        failing.append("g5_operator_override_variance")

    all_pass = not failing

    return GateResult(
        engine=metrics.engine,
        g1_shadow_divergence_pass=g1_pass,
        g2_zero_trip_dry_run_pass=g2_pass,
        g3_sample_size_pass=g3_pass,
        g4_broker_rejection_rate_pass=g4_pass,
        g5_operator_override_variance_pass=g5_pass,
        all_pass=all_pass,
        failing_gates=failing,
    )


__all__ = ["Engine", "GateMetrics", "GateResult", "load_config", "evaluate_gates"]
