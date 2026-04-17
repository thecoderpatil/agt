"""MR 1 plumbing tests for ADR-008 Shadow Scan.

Covers:
- RunContext frozen-ness + Protocol isinstance
- CollectorOrderSink / CollectorDecisionSink thread-safety + contract
- SQLiteOrderSink / SQLiteDecisionSink forward-to-callable
- NullDecisionSink no-op
- clone_sqlite_db_with_wal round-trip + isolation + error path
- build_shadow_ctx invariant refusals
- shadow_scan.main() smoke (empty digest JSON artifact)
- NO_SHADOW_ON_PROD_DB invariant (steady-state + fake offender)
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import sys
import threading
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.sprint_a

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agt_equities.runtime import (  # noqa: E402
    PROD_DB_PATH,
    DecisionSink,
    OrderSink,
    RunContext,
    RunMode,
    clone_sqlite_db_with_wal,
)
from agt_equities.sinks import (  # noqa: E402
    CollectorDecisionSink,
    CollectorOrderSink,
    NullDecisionSink,
    ShadowDecision,
    ShadowOrder,
    SQLiteDecisionSink,
    SQLiteOrderSink,
)


def _make_check_ctx(tmp_path: Path):
    """Build a minimal CheckContext matching agt_equities.invariants.types."""
    from agt_equities.invariants.types import CheckContext

    return CheckContext(
        now_utc=datetime.now(tz=timezone.utc),
        db_path=str(tmp_path / "nothing.db"),
        paper_mode=True,
        live_accounts=frozenset(),
        paper_accounts=frozenset({"DUPAPER1"}),
        expected_daemons=frozenset({"telegram_bot", "agt_scheduler"}),
    )


# -------------------------- RunContext -------------------------- #


def _make_ctx(mode: RunMode = RunMode.SHADOW, db_path: str | None = None) -> RunContext:
    return RunContext(
        mode=mode,
        run_id=uuid.uuid4().hex,
        order_sink=CollectorOrderSink(),
        decision_sink=CollectorDecisionSink(),
        db_path=db_path,
    )


def test_runcontext_is_frozen():
    ctx = _make_ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.mode = RunMode.LIVE  # type: ignore[misc]


def test_runcontext_mode_helpers():
    live = _make_ctx(mode=RunMode.LIVE)
    shadow = _make_ctx(mode=RunMode.SHADOW)
    assert live.is_live and not live.is_shadow
    assert shadow.is_shadow and not shadow.is_live


def test_sinks_satisfy_protocol():
    ctx = _make_ctx()
    assert isinstance(ctx.order_sink, OrderSink)
    assert isinstance(ctx.decision_sink, DecisionSink)


# -------------------------- CollectorOrderSink -------------------------- #


def test_collector_order_sink_stage_drain_peek():
    sink = CollectorOrderSink()
    tickets = [
        {"ticker": "AAPL", "right": "P", "strike": 180.0, "qty": 1, "limit": 1.25},
        {"ticker": "MSFT", "right": "C", "strike": 410.0, "qty": 2, "limit": 3.10},
    ]
    sink.stage(tickets, engine="csp_allocator", run_id="rid-0")
    assert len(sink) == 2
    peek = sink.peek()
    assert len(peek) == 2
    assert peek[0].ticker == "AAPL"
    # peek must not mutate
    assert len(sink) == 2
    drained = sink.drain()
    assert len(drained) == 2
    assert all(isinstance(o, ShadowOrder) for o in drained)
    assert len(sink) == 0


def test_collector_order_sink_captures_parse_error():
    sink = CollectorOrderSink()
    # non-numeric strike raises ValueError inside float() -> sink must NOT raise;
    # must record with shadow_parse_error meta instead.
    bad = [{"ticker": "AAPL", "right": "P", "strike": "not-a-float", "qty": 1}]
    sink.stage(bad, engine="csp_allocator", run_id="rid-bad")
    drained = sink.drain()
    assert len(drained) == 1
    assert "shadow_parse_error" in drained[0].meta


def test_collector_order_sink_thread_safety():
    sink = CollectorOrderSink()
    N_PRODUCERS = 8
    PER_PRODUCER = 100
    barrier = threading.Barrier(N_PRODUCERS)

    def produce(idx: int) -> None:
        barrier.wait()
        batch = [
            {
                "ticker": f"T{idx}",
                "right": "P",
                "strike": float(100 + j),
                "qty": 1,
                "limit": 0.5,
            }
            for j in range(PER_PRODUCER)
        ]
        sink.stage(batch, engine=f"eng{idx}", run_id=f"rid-{idx}")

    threads = [threading.Thread(target=produce, args=(i,)) for i in range(N_PRODUCERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    drained = sink.drain()
    assert len(drained) == N_PRODUCERS * PER_PRODUCER
    # no cross-contamination: engine-ticker pairing preserved
    for o in drained:
        assert o.ticker == f"T{o.engine[3:]}"
    # sink is now empty
    assert sink.drain() == []


# -------------------------- CollectorDecisionSink -------------------------- #


def test_collector_decision_sink_records():
    sink = CollectorDecisionSink()
    sink.record_cc_cycle([{"ticker": "AAPL", "cycle_id": 42}], run_id="rid-cc")
    sink.record_dynamic_exit([{"ticker": "MSFT", "rule": "R3"}], run_id="rid-exit")
    drained = sink.drain()
    kinds = sorted(d.kind for d in drained)
    assert kinds == ["cc_cycle", "dynamic_exit"]
    assert all(isinstance(d, ShadowDecision) for d in drained)


def test_null_decision_sink_noop():
    sink = NullDecisionSink()
    sink.record_cc_cycle([{"x": 1}], run_id="r")
    sink.record_dynamic_exit([{"y": 2}], run_id="r")
    # no API for reading back — just assert it didn't raise


# -------------------------- SQLite sinks (forward to callable) -------------------------- #


def test_sqlite_order_sink_forwards():
    captured: list[list] = []

    def fake_stage(tickets):
        captured.append(list(tickets))

    sink = SQLiteOrderSink(staging_fn=fake_stage, supersede_fn=None)
    sink.stage(
        [{"ticker": "AAPL", "right": "P", "strike": 180.0, "qty": 1, "limit": 1.0}],
        engine="csp",
        run_id="rid",
    )
    assert len(captured) == 1
    assert captured[0][0]["ticker"] == "AAPL"


def test_sqlite_decision_sink_forwards():
    cc: list = []
    de: list = []

    sink = SQLiteDecisionSink(
        record_cc_cycle_fn=lambda entries: cc.append(list(entries)),
        record_dynamic_exit_fn=lambda entries: de.append(list(entries)),
    )
    sink.record_cc_cycle([{"ticker": "AAPL"}], run_id="rid-cc")
    sink.record_dynamic_exit([{"ticker": "MSFT"}], run_id="rid-exit")
    assert cc == [[{"ticker": "AAPL"}]]
    assert de == [[{"ticker": "MSFT"}]]


# -------------------------- clone_sqlite_db_with_wal -------------------------- #


def test_clone_sqlite_db_round_trip(tmp_path: Path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); "
        "INSERT INTO t(v) VALUES ('a'), ('b'), ('c');"
    )
    conn.commit()
    conn.close()

    clone_path = clone_sqlite_db_with_wal(str(src))
    try:
        assert os.path.exists(clone_path)
        c2 = sqlite3.connect(clone_path)
        rows = c2.execute("SELECT v FROM t ORDER BY id").fetchall()
        c2.close()
        assert [r[0] for r in rows] == ["a", "b", "c"]

        # isolation: writing to clone does not mutate source
        c3 = sqlite3.connect(clone_path)
        c3.execute("INSERT INTO t(v) VALUES ('d')")
        c3.commit()
        c3.close()

        c4 = sqlite3.connect(str(src))
        src_rows = c4.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        c4.close()
        assert src_rows == 3, "clone writes bled into source"
    finally:
        import shutil
        d = os.path.dirname(clone_path)
        if d and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)


def test_clone_sqlite_db_missing_source_raises(tmp_path: Path):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        clone_sqlite_db_with_wal(str(missing))


# -------------------------- shadow_scan CLI -------------------------- #


def test_build_shadow_ctx_refuses_prod_db():
    import scripts.shadow_scan as ss

    with pytest.raises(RuntimeError, match="NO_SHADOW_ON_PROD_DB"):
        ss.build_shadow_ctx(PROD_DB_PATH)


def test_build_shadow_ctx_returns_shadow_mode(tmp_path: Path):
    import scripts.shadow_scan as ss

    fake_clone = str(tmp_path / "clone.db")
    Path(fake_clone).write_bytes(b"")
    ctx = ss.build_shadow_ctx(fake_clone)
    assert ctx.mode is RunMode.SHADOW
    assert ctx.is_shadow
    assert ctx.db_path == fake_clone


def test_shadow_scan_main_writes_empty_digest(tmp_path: Path, monkeypatch):
    import scripts.shadow_scan as ss

    # isolate reports and clone parent so shadow_scan's post-run rmtree
    # of the clone's parent does not also wipe the reports directory
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    clone_parent = tmp_path / "clone_parent"
    clone_parent.mkdir()

    monkeypatch.setattr(ss, "REPORTS_DIR", reports_dir)

    def fake_clone(src, dest_dir=None):
        p = clone_parent / "fake_clone.db"
        sqlite3.connect(str(p)).close()
        return str(p)

    monkeypatch.setattr(ss, "clone_sqlite_db_with_wal", fake_clone)

    argv = ["shadow_scan", "--emit", "json"]
    monkeypatch.setattr(sys, "argv", argv)
    rc = ss.main()
    assert rc == 0

    artifacts = list(reports_dir.glob("shadow_scan_*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["mode"] == "shadow"
    assert payload["orders"] == []
    assert payload["decisions"] == []


# -------------------------- NO_SHADOW_ON_PROD_DB invariant -------------------------- #


def test_check_registry_has_no_shadow_invariant():
    from agt_equities.invariants.checks import CHECK_REGISTRY

    assert "NO_SHADOW_ON_PROD_DB" in CHECK_REGISTRY


def test_no_shadow_on_prod_db_steady_state(monkeypatch, tmp_path: Path):
    from agt_equities.invariants import checks as checks_mod

    fake_psutil = types.ModuleType("psutil")

    def _iter(attrs=None):
        return iter([])

    fake_psutil.process_iter = _iter  # type: ignore[attr-defined]
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})  # type: ignore[attr-defined]
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})  # type: ignore[attr-defined]
    fake_psutil.ZombieProcess = type("ZombieProcess", (Exception,), {})  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    ctx = _make_check_ctx(tmp_path)
    # conn arg is unused by this check but signature demands it
    conn = sqlite3.connect(":memory:")
    try:
        violations = checks_mod.check_no_shadow_on_prod_db(conn, ctx)
    finally:
        conn.close()

    # steady-state: either empty (no offenders) or a degraded stub if runtime import/psutil
    # weren't in the stubbed sys.modules yet — but never a real high-severity breach.
    for v in violations:
        assert "degraded" in (v.stable_key or "").lower() or v.severity in ("low", "info")


def test_no_shadow_on_prod_db_flags_offender(monkeypatch, tmp_path: Path):
    from agt_equities.invariants import checks as checks_mod

    class _Proc:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "cmdline": cmdline}

    offender = _Proc(
        9999,
        [
            "python.exe",
            "scripts\\shadow_scan.py",
            "--db-clone",
            PROD_DB_PATH,
        ],
    )

    fake_psutil = types.ModuleType("psutil")

    def _iter(attrs=None):
        return iter([offender])

    fake_psutil.process_iter = _iter  # type: ignore[attr-defined]
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})  # type: ignore[attr-defined]
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})  # type: ignore[attr-defined]
    fake_psutil.ZombieProcess = type("ZombieProcess", (Exception,), {})  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    ctx = _make_check_ctx(tmp_path)
    conn = sqlite3.connect(":memory:")
    try:
        violations = checks_mod.check_no_shadow_on_prod_db(conn, ctx)
    finally:
        conn.close()

    real = [v for v in violations if "degraded" not in (v.stable_key or "").lower()]
    assert real, "expected a real NO_SHADOW_ON_PROD_DB violation, got none"
    v = real[0]
    assert v.invariant_id == "NO_SHADOW_ON_PROD_DB"
    assert v.severity == "high"
    offs = v.evidence.get("offenders", [])
    assert any(o.get("pid") == 9999 for o in offs)
