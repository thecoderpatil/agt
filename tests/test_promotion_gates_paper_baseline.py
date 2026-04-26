"""Sprint 6 Mega-MR 5 — ADR-011 paper-baseline promotion-gate assertions.

Reads the production DB (read-only) + the current
`config/promotion_gates.yaml` thresholds, computes per-engine GateMetrics,
and asserts `evaluate_gates(...).all_pass is True` for each engine.

**These tests are EXPECTED TO FAIL on first run.** The failure summary
IS the deliverable — Architect reads the per-engine blockers report
(`reports/promotion_gate_current_blockers.md`) and queues engine-
readiness work based on which gates fail.

Marked `xfail(strict=False)` so a green gate does not flag unexpected
pass; a gate that turns green is progress, not regression.

Read-only DB access only. Skipped cleanly if AGT_DB_PATH is unset or
points at a missing file (i.e. CI containers without a seeded prod
clone).
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agt_equities.promotion_gates import (
    GateMetrics,
    evaluate_gates,
    load_config,
)

pytestmark = [pytest.mark.sprint_a]


def _prod_db_path() -> Path | None:
    env = os.environ.get("AGT_DB_PATH", "").strip()
    if not env:
        return None
    p = Path(env)
    return p if p.is_file() else None


_PROD_DB = _prod_db_path()
_SKIP_NO_PROD = pytest.mark.skipif(
    _PROD_DB is None,
    reason="Prod DB not available at AGT_DB_PATH (CI runners without seeded clone).",
)


def _ro_connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fourteen_days_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()


def _count_tier0_trips_14d(conn: sqlite3.Connection) -> int:
    """Tier 0 = live-capital severity. Use error_budget_tier if column
    exists (Sprint 6 Mega-MR 4B); else fall back to string severity =
    'critical' as the tier-0 proxy."""
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()
    }
    since = _fourteen_days_ago_iso()
    if "error_budget_tier" in cols:
        row = conn.execute(
            "SELECT COUNT(*) FROM incidents "
            "WHERE error_budget_tier = 0 AND detected_at >= ?",
            (since,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM incidents "
            "WHERE severity = 'critical' AND detected_at >= ?",
            (since,),
        ).fetchone()
    return int(row[0]) if row else 0


def _count_tier1_trips_14d(conn: sqlite3.Connection) -> int:
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()
    }
    since = _fourteen_days_ago_iso()
    if "error_budget_tier" in cols:
        row = conn.execute(
            "SELECT COUNT(*) FROM incidents "
            "WHERE error_budget_tier = 1 AND detected_at >= ?",
            (since,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM incidents "
            "WHERE severity = 'high' AND detected_at >= ?",
            (since,),
        ).fetchone()
    return int(row[0]) if row else 0


def _sample_size_14d(conn: sqlite3.Connection, engine: str) -> int:
    """G3 sample size. pending_orders has engine embedded in payload JSON."""
    since = _fourteen_days_ago_iso()
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_orders "
        "WHERE json_extract(payload, '$.engine') = ? AND created_at >= ?",
        (engine, since),
    ).fetchone()
    return int(row[0]) if row else 0


def _rejection_rate(conn: sqlite3.Connection, engine: str) -> float:
    """G4 rejection rate for this engine in the trailing 14d."""
    since = _fourteen_days_ago_iso()
    total = conn.execute(
        "SELECT COUNT(*) FROM pending_orders "
        "WHERE json_extract(payload, '$.engine') = ? AND created_at >= ?",
        (engine, since),
    ).fetchone()[0]
    if not total:
        return 0.0
    rejected = conn.execute(
        "SELECT COUNT(*) FROM pending_orders "
        "WHERE json_extract(payload, '$.engine') = ? "
        "AND status = 'rejected' AND created_at >= ?",
        (engine, since),
    ).fetchone()[0]
    return float(rejected) / float(total)


def _paper_baseline_metrics(engine: str, conn: sqlite3.Connection) -> GateMetrics:
    """Compose the GateMetrics snapshot for a given engine.

    G1 shadow divergence data is not produced by a live writer yet; we
    report 999.0 bps (clearly above threshold) so the gate correctly
    surfaces as "blocker: shadow_scan data not yet collected".

    G5 operator-override-variance requires `decisions` table rows with
    counterfactual_pnl populated; for engines other than entry it is
    N/A (pvalue=None), and for entry it's None until ADR-012 learning
    loop starts ingesting real data (Sprint 6 Mega-MR 4A shipped the
    decision_outcomes schema but the ingest hooks ship in Sprint 7+).
    """
    t0 = _count_tier0_trips_14d(conn)
    t1 = _count_tier1_trips_14d(conn)
    sample = _sample_size_14d(conn, engine)
    rej = _rejection_rate(conn, engine)
    return GateMetrics(
        engine=engine,  # type: ignore[arg-type]
        shadow_div_bps_mean=999.0,
        shadow_div_bps_p99=999.0,
        tier0_trips_14d=t0,
        tier1_trips_14d=t1,
        sample_size_14d=sample,
        novel=True,  # no engine has live history yet
        rejection_rate=rej,
        operator_override_variance_pvalue=None,
    )


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def prod_conn():
    if _PROD_DB is None:
        pytest.skip("Prod DB unavailable")
    conn = _ro_connect(_PROD_DB)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-engine per-gate assertions. ~20 tests = 4 engines × 5 gates. Each is
# xfail(strict=False) so gates that pass reveal progress; gates that fail
# surface concrete blockers in the test report.
# ---------------------------------------------------------------------------

_ENGINES_IN_SEQUENCE = ["exit", "roll", "harvest", "entry"]


@_SKIP_NO_PROD
@pytest.mark.xfail(
    strict=False,
    reason="G1 blocker: shadow_scan divergence writer not yet producing "
    "paper-baseline metrics to read. Sprint 7+ ingest landing."
)
@pytest.mark.parametrize("engine", _ENGINES_IN_SEQUENCE)
def test_promotion_g1_shadow_divergence(engine, prod_conn, config):
    metrics = _paper_baseline_metrics(engine, prod_conn)
    result = evaluate_gates(metrics, config)
    assert result.g1_shadow_divergence_pass, (
        f"{engine}: G1 blocked — "
        f"mean={metrics.shadow_div_bps_mean}bps p99={metrics.shadow_div_bps_p99}bps"
    )


@_SKIP_NO_PROD
@pytest.mark.xfail(
    strict=False,
    reason="G2 blocker: paper session accumulates Tier-0/1 incidents "
    "routinely pre-observation-week."
)
@pytest.mark.parametrize("engine", _ENGINES_IN_SEQUENCE)
def test_promotion_g2_zero_trip_dry_run(engine, prod_conn, config):
    metrics = _paper_baseline_metrics(engine, prod_conn)
    result = evaluate_gates(metrics, config)
    assert result.g2_zero_trip_dry_run_pass, (
        f"{engine}: G2 blocked — tier0={metrics.tier0_trips_14d} "
        f"tier1={metrics.tier1_trips_14d} (both must be 0)"
    )


@_SKIP_NO_PROD
@pytest.mark.xfail(
    strict=False,
    reason="G3 blocker: novel engine floor 120 not yet met in 14d window."
)
@pytest.mark.parametrize("engine", _ENGINES_IN_SEQUENCE)
def test_promotion_g3_sample_size(engine, prod_conn, config):
    metrics = _paper_baseline_metrics(engine, prod_conn)
    result = evaluate_gates(metrics, config)
    assert result.g3_sample_size_pass, (
        f"{engine}: G3 blocked — sample={metrics.sample_size_14d} "
        f"required>=120 (novel cold-start floor)"
    )


@_SKIP_NO_PROD
@pytest.mark.xfail(
    strict=False,
    reason="G4 blocker: if any paper rejections occur the rate must be "
    "<0.001 across the whole trailing window."
)
@pytest.mark.parametrize("engine", _ENGINES_IN_SEQUENCE)
def test_promotion_g4_rejection_rate(engine, prod_conn, config):
    metrics = _paper_baseline_metrics(engine, prod_conn)
    result = evaluate_gates(metrics, config)
    assert result.g4_broker_rejection_rate_pass, (
        f"{engine}: G4 blocked — rejection_rate={metrics.rejection_rate:.5f} "
        f"threshold<0.001"
    )


@_SKIP_NO_PROD
@pytest.mark.xfail(
    strict=False,
    reason="G5 blocker: entry engine has no decisions-with-counterfactual "
    "data yet to compute operator-override-variance pvalue."
)
@pytest.mark.parametrize("engine", _ENGINES_IN_SEQUENCE)
def test_promotion_g5_operator_override_variance(engine, prod_conn, config):
    metrics = _paper_baseline_metrics(engine, prod_conn)
    result = evaluate_gates(metrics, config)
    assert result.g5_operator_override_variance_pass, (
        f"{engine}: G5 blocked — pvalue={metrics.operator_override_variance_pvalue} "
        f"(entry engine requires significant pvalue >= alpha 0.05)"
    )
