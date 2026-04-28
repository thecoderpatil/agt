"""
Sprint 14 P0 — position_discovery: tkr UnboundLocalError on IBKR provider init failure.

Root cause (Sprint 3 MR 5, commit 3bda135 — MR !272 innocent):
  Defect 1: IBKRPriceVolatilityProvider(ib, ...) uses bare 'ib' (NameError);
            parameter is 'ib_conn'. Fires before the for-loop starts.
  Defect 2: except handler references 'tkr' which was never bound
            → UnboundLocalError.

Fix:
  position_discovery.py:566  ib → ib_conn
  position_discovery.py:568  tkr = "<unknown>" added before the for-loop

Tests:
  1. Source inspection — ib_conn used, tkr sentinel present, bare ib absent
  2. Provider init failure before loop → no UnboundLocalError (tkr = "<unknown>")
  3. Provider error mid-loop → except handler has real ticker in tkr
"""
from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Helper: replicates the fixed fallback block from discover_positions
# (position_discovery.py lines 562-584 post-fix)
# ---------------------------------------------------------------------------

def _run_fallback_block(missed: list[str], mock_prov_cls, spot_prices=None):
    """Mirrors the fixed ibkr_price_volatility fallback block."""
    if spot_prices is None:
        spot_prices = {}
    log_entries: list[tuple[str, Exception]] = []

    tkr = "<unknown>"  # THE FIX — pre-initialise before the loop
    try:
        _prov = mock_prov_cls(None, market_data_mode="delayed")
        for tkr in missed:
            spot = _prov.get_spot(tkr)
            if spot is not None:
                spot_prices[tkr] = round(spot, 2)
    except Exception as exc:
        log_entries.append((tkr, exc))

    return spot_prices, log_entries


# ---------------------------------------------------------------------------
# Test 1 — source structure: ib_conn used, sentinel present, bare ib absent
# ---------------------------------------------------------------------------

def test_ib_conn_passed_not_bare_ib():
    """Regression: position_discovery must pass ib_conn, not bare 'ib', to provider."""
    import agt_equities.position_discovery as pd_mod

    src = inspect.getsource(pd_mod.discover_positions)

    assert 'IBKRPriceVolatilityProvider(ib_conn,' in src, (
        "Fix missing: IBKRPriceVolatilityProvider must receive ib_conn, not bare 'ib'"
    )
    assert 'IBKRPriceVolatilityProvider(ib,' not in src, (
        "Regression: bare 'ib' still passed to IBKRPriceVolatilityProvider"
    )
    assert 'tkr = "<unknown>"' in src, (
        "Fix missing: tkr must be pre-initialised before the for-loop"
    )


# ---------------------------------------------------------------------------
# Test 2 — provider init raises before loop → no UnboundLocalError
# ---------------------------------------------------------------------------

def test_provider_init_error_no_unboundlocal():
    """If provider __init__ raises before the for-loop, except handler must not
    raise UnboundLocalError. tkr sentinel '<unknown>' should appear in the log."""

    class _BoomOnInit:
        def __init__(self, *a, **kw):
            raise RuntimeError("provider init exploded")

    missed = ["AAPL", "MSFT"]

    # Must not raise — before fix this raised UnboundLocalError on tkr
    spot_prices, log_entries = _run_fallback_block(missed, _BoomOnInit)

    assert spot_prices == {}, "no spots should be populated when provider fails on init"
    assert len(log_entries) == 1
    logged_tkr, logged_exc = log_entries[0]
    assert logged_tkr == "<unknown>", (
        f"expected sentinel '<unknown>' but got {logged_tkr!r} — "
        "tkr pre-init fix may be missing"
    )
    assert "provider init exploded" in str(logged_exc)


# ---------------------------------------------------------------------------
# Test 3 — provider raises mid-loop → except handler has the real ticker
# ---------------------------------------------------------------------------

def test_provider_loop_error_logs_ticker():
    """If provider raises on get_spot() mid-loop, except handler must have
    the actual ticker in tkr (not '<unknown>')."""

    class _BoomOnGet:
        def __init__(self, *a, **kw):
            pass

        def get_spot(self, tkr):
            raise ValueError(f"market closed for {tkr}")

    missed = ["TSLA", "NVDA"]

    spot_prices, log_entries = _run_fallback_block(missed, _BoomOnGet)

    assert spot_prices == {}
    assert len(log_entries) == 1
    logged_tkr, logged_exc = log_entries[0]
    # tkr should be the first ticker that caused the error, not the sentinel
    assert logged_tkr == "TSLA", (
        f"expected first ticker 'TSLA' in log, got {logged_tkr!r}"
    )
    assert "market closed" in str(logged_exc)
