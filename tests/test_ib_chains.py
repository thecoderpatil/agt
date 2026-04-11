"""
tests/test_ib_chains.py

Unit tests for agt_equities.ib_chains NaN-safe coercion helpers and
the _build_chain_rows pure function. Created as part of C6.2 after
the 2026-04-11 paper run surfaced a "cannot convert float NaN to
integer" crash in get_chain_for_expiry when the IBKR OPRA subscription
was missing.

This is the FIRST unit test file for agt_equities.ib_chains. Prior
to C6.2, ib_chains was only exercised via mocked-boundary tests in
tests/test_screener_chain_walker.py (which monkeypatched the ib_chains
public API and therefore never reached the internals).

Test structure: six tests per the C6.2 dispatch spec. Five test the
helpers (_safe_int, _safe_float) with direct calls. The sixth is a
regression test that exercises _build_chain_rows with a
types.SimpleNamespace fake that simulates the NaN-returning IBKR
BarData shape. No FakeIB, no ib_async monkeypatching, no snapshot
timing simulation — the refactor extracted the coercion loop into
a pure function so the test surface is trivial.

ISOLATION: this test file imports only stdlib + pytest + the
private helpers from agt_equities.ib_chains. It does NOT touch
any screener package module. The screener is shielded from the
C6.2 core infra change by the ib_chains public API.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from agt_equities.ib_chains import (
    _build_chain_rows,
    _safe_float,
    _safe_int,
)


# ---------------------------------------------------------------------------
# 1-5. _safe_int / _safe_float unit tests
# ---------------------------------------------------------------------------

def test_safe_int_on_none_returns_default():
    """None input returns the default (0 by default, caller-overridable)."""
    assert _safe_int(None) == 0
    assert _safe_int(None, default=42) == 42
    assert _safe_int(None, default=-1) == -1


def test_safe_int_on_nan_returns_default():
    """NaN float input returns the default. This is the load-bearing
    regression guard for the 2026-04-11 paper run crash — bare
    int(float('nan')) raises ValueError."""
    assert _safe_int(float("nan")) == 0
    assert _safe_int(float("nan"), default=99) == 99
    # Verify the bare int() would have raised (documents the original bug)
    with pytest.raises(ValueError):
        int(float("nan"))


def test_safe_int_on_valid_number_converts():
    """Valid numeric inputs coerce normally."""
    assert _safe_int(42) == 42
    assert _safe_int(42.7) == 42            # truncates like int()
    assert _safe_int("15") == 15            # string coercion
    assert _safe_int(0) == 0
    assert _safe_int(-5) == -5              # negative ints pass through
    # Uncoercible garbage falls back to default
    assert _safe_int("not a number") == 0
    assert _safe_int([1, 2, 3]) == 0


def test_safe_float_on_none_returns_default():
    """None input returns the default (0.0 by default)."""
    assert _safe_float(None) == 0.0
    assert _safe_float(None, default=1.5) == 1.5
    assert _safe_float(None, default=-0.5) == -0.5


def test_safe_float_on_nan_returns_default():
    """NaN input returns the default. Guards the two NaN paths:
    (a) input is already a NaN float, (b) input is something else
    that float() coerces into NaN.
    """
    assert _safe_float(float("nan")) == 0.0
    assert _safe_float(float("nan"), default=2.5) == 2.5
    # Sanity: valid floats pass through
    assert _safe_float(3.14) == pytest.approx(3.14)
    assert _safe_float(0.0) == 0.0
    assert _safe_float(-1.5) == -1.5        # negatives pass through (clamping
                                            # is the caller's responsibility)
    # String coercion + uncoercible fallback
    assert _safe_float("2.718") == pytest.approx(2.718)
    assert _safe_float("garbage") == 0.0
    assert _safe_float([1.0]) == 0.0


# ---------------------------------------------------------------------------
# 6. _build_chain_rows — regression test for the 2026-04-11 paper run
# ---------------------------------------------------------------------------

def test_build_chain_rows_with_nan_fields_does_not_crash():
    """Regression test for the 2026-04-11 paper run GD/HSY/MPC failures.

    When IBKR's OPRA subscription is missing (or a strike is illiquid
    enough that the snapshot hasn't fully populated), reqMktData
    returns BarData-like objects with NaN in volume, openInterest,
    and sometimes impliedVolatility. The pre-C6.2 coercion loop
    crashed with `ValueError: cannot convert float NaN to integer`
    on the `int(vol)` / `int(oi)` line, aborting the entire chain
    fetch for that expiry.

    This test builds a types.SimpleNamespace fake (duck-typed against
    ib_async BarData) with NaN volume and openInterest, passes it
    through _build_chain_rows, and asserts:
      1. The function does NOT crash
      2. The resulting row has volume=0 and openInterest=0 (defaults)
      3. Valid fields (bid, ask, last, impliedVol) are preserved
      4. The strike key is carried through as a float
    """
    fake_td = SimpleNamespace(
        bid=1.25,
        ask=1.35,
        last=1.30,
        volume=float("nan"),
        openInterest=float("nan"),
        impliedVolatility=0.28,
    )
    rows = _build_chain_rows({150.0: fake_td})

    assert len(rows) == 1
    row = rows[0]

    # Strike carried through
    assert row["strike"] == 150.0
    assert isinstance(row["strike"], float)

    # NaN coerced to safe defaults
    assert row["volume"] == 0
    assert row["openInterest"] == 0
    assert isinstance(row["volume"], int)
    assert isinstance(row["openInterest"], int)

    # Valid fields preserved
    assert row["bid"] == pytest.approx(1.25)
    assert row["ask"] == pytest.approx(1.35)
    assert row["last"] == pytest.approx(1.30)
    assert row["impliedVol"] == pytest.approx(0.28)


# ---------------------------------------------------------------------------
# Additional coverage — _build_chain_rows behavior on edge cases
# (not strictly in the dispatch spec but closes related gaps)
# ---------------------------------------------------------------------------

def test_build_chain_rows_valid_data_bit_identical():
    """Regression guard for the refactor: pre-C6.2 behavior on VALID
    (non-NaN, non-negative) data must be bit-identical post-C6.2.

    A candidate with clean numeric data should produce a row whose
    fields match what the old inline coercion loop would have
    produced — modulo the output shape which has always been
    {strike, bid, ask, last, volume, openInterest, impliedVol}.
    """
    fake_td = SimpleNamespace(
        bid=2.10, ask=2.15, last=2.12,
        volume=350, openInterest=1200, impliedVolatility=0.325,
    )
    rows = _build_chain_rows({145.5: fake_td})
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "strike": 145.5,
        "bid": 2.10,
        "ask": 2.15,
        "last": 2.12,
        "volume": 350,
        "openInterest": 1200,
        "impliedVol": 0.325,
    }


def test_build_chain_rows_negative_prices_clamped_to_zero():
    """Pre-C6.2 semantic preserved: valid-but-negative price fields
    are clamped to 0.0. The old inline code used `v if v > 0 else 0.0`
    for bid/ask/last. C6.2's _build_chain_rows applies the same clamp
    via explicit `if bid < 0: bid = 0.0` pass after _safe_float.
    """
    fake_td = SimpleNamespace(
        bid=-0.01, ask=1.05, last=-5.0,
        volume=100, openInterest=500, impliedVolatility=-0.1,
    )
    rows = _build_chain_rows({100.0: fake_td})
    assert len(rows) == 1
    row = rows[0]
    assert row["bid"] == 0.0      # clamped from -0.01
    assert row["last"] == 0.0     # clamped from -5.0
    assert row["impliedVol"] == 0.0  # clamped from -0.1
    # Positive ask passes through unchanged
    assert row["ask"] == pytest.approx(1.05)


def test_build_chain_rows_empty_dict_returns_empty_list():
    """Defensive: empty input returns empty output, no crash."""
    assert _build_chain_rows({}) == []


def test_build_chain_rows_multiple_strikes_sorted():
    """Output is sorted by strike ascending (pre-C6.2 behavior
    via `sorted(tickers_data.items())`)."""
    td_template = SimpleNamespace(
        bid=1.0, ask=1.1, last=1.05,
        volume=100, openInterest=500, impliedVolatility=0.3,
    )
    tickers_data = {
        155.0: td_template,
        145.0: td_template,
        150.0: td_template,
    }
    rows = _build_chain_rows(tickers_data)
    assert len(rows) == 3
    assert [r["strike"] for r in rows] == [145.0, 150.0, 155.0]
