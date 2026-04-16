"""MR #2 — circuit_breaker halt semantics + check_vix tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.agt_tripwire_exempt


def _load_breaker(monkeypatch, project_root: Path):
    """Load scripts/circuit_breaker.py fresh with a temp cwd + DB path.

    circuit_breaker chdirs to its parent.parent in module scope, which
    would point at the real project. We monkeypatch DB_PATH before using
    it, so the chdir is harmless for logic tests.
    """
    scripts_dir = project_root / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        if "circuit_breaker" in sys.modules:
            del sys.modules["circuit_breaker"]
        mod = importlib.import_module("circuit_breaker")
        return mod
    finally:
        sys.path.remove(str(scripts_dir))


def test_check_daily_order_limit_halts(monkeypatch, tmp_path):
    import sqlite3
    from agt_equities.schema import register_operational_tables

    db_file = tmp_path / "cb.db"
    conn = sqlite3.connect(db_file)
    register_operational_tables(conn)
    # Insert > MAX_DAILY_ORDERS rows with today's date.
    for i in range(35):
        conn.execute(
            "INSERT INTO pending_orders(id, ticker, status, payload, created_at) "
            "VALUES (?, ?, 'staged', '{}', datetime('now'))",
            (1000 + i, "FAKE"),
        )
    conn.commit()
    conn.close()

    project_root = Path(__file__).resolve().parent.parent
    cb = _load_breaker(monkeypatch, project_root)
    monkeypatch.setattr(cb, "DB_PATH", str(db_file))

    result = cb.check_daily_order_limit()
    assert result["ok"] is False
    assert result.get("halted") is True, "MR #2: daily_orders must halt"


def test_check_vix_soft_fails_when_yfinance_broken(monkeypatch):
    """check_vix must not raise or halt when yfinance import fails."""
    project_root = Path(__file__).resolve().parent.parent
    cb = _load_breaker(monkeypatch, project_root)

    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "yfinance":
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    result = cb.check_vix()
    assert result["ok"] is True
    assert "warning" in result or "vix" not in result


def test_check_vix_halts_above_threshold(monkeypatch):
    """When yfinance returns VIX >= VIX_HALT_THRESHOLD, halted=True."""
    project_root = Path(__file__).resolve().parent.parent
    cb = _load_breaker(monkeypatch, project_root)

    class FakeFastInfo:
        last_price = 99.0  # well above threshold

    class FakeTicker:
        def __init__(self, sym):
            self.fast_info = FakeFastInfo()

    class FakeYF:
        Ticker = FakeTicker

    monkeypatch.setitem(sys.modules, "yfinance", FakeYF)
    result = cb.check_vix()
    assert result["ok"] is False
    assert result["halted"] is True
    assert result["vix"] >= cb.VIX_HALT_THRESHOLD


def test_run_all_checks_includes_vix(monkeypatch):
    project_root = Path(__file__).resolve().parent.parent
    cb = _load_breaker(monkeypatch, project_root)

    # Stub the individual checks so run_all_checks composition runs quickly.
    monkeypatch.setattr(cb, "check_daily_order_limit",  lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_daily_notional",     lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_consecutive_errors", lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_nlv_drop",           lambda: {"ok": True})
    monkeypatch.setattr(cb, "check_vix",                lambda: {"ok": True, "vix": 15.0})
    monkeypatch.setattr(cb, "check_directive_freshness", lambda: {"ok": True})

    verdict = cb.run_all_checks()
    assert "vix" in verdict["checks"]
    assert verdict["ok"] is True
    assert verdict["halted"] is False
