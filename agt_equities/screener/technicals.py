"""
agt_equities.screener.technicals — Phase 2: Technical pullback filter.

Takes the Phase 1 survivor list (UniverseTicker), runs a SINGLE batched
yfinance download for all of them, and filters down to tickers in an
active pullback per the spec:

  Current_Price > SMA_200                       (long-term uptrend intact)
  35 <= RSI_14 <= 45                            (oversold but not capitulating)
  Current_Price <= Lower_BBand_20_2 * 1.02      (within 2% of lower band)

Why one batched call: yfinance's `download(tickers, period=...)` accepts
a list of symbols and returns a single DataFrame with MultiIndex columns
(top level = ticker, bottom level = OHLCV). For ~480 tickers this is one
network round-trip and ~10-20 seconds wall-clock total. Compare to per-
ticker `Ticker(t).history()` calls which would burn ~480 calls and risk
yfinance throttling. The batch is the entire reason this phase runs
before fundamentals — it neutralizes the rate-limit risk surface.

Lookback window: `period="14mo"` per Architect ruling 2026-04-10. ~295
trading days guarantees the 200-day SMA produces a non-NaN value at the
most recent close even with a few non-trading-day gaps and a startup
buffer.

Fail-closed: any ticker missing from the yfinance batch result, or
returning NaN for any of (current_price, SMA_200, RSI_14, BBand_lower,
lowest_low_21d), is dropped from the survivor list. NO individual
re-fetches. Per Architect ruling — partial-fill rule is fail-closed.

ISOLATION CONTRACT: imports stdlib + numpy + pandas + (lazily) yfinance
+ agt_equities.screener.{config, types}. No telegram_bot, no ib_async,
no agt_equities.rule_engine. Enforced by tests/test_screener_isolation.py.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

import numpy as np
import pandas as pd

from agt_equities.screener import config
from agt_equities.screener.types import TechnicalCandidate, UniverseTicker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure indicator helpers — operate on a single ticker's OHLCV DataFrame
# ---------------------------------------------------------------------------

def _compute_sma(close: pd.Series, window: int) -> float | None:
    """Return the most recent simple moving average value, or None on NaN."""
    if len(close) < window:
        return None
    sma = close.rolling(window=window, min_periods=window).mean()
    val = sma.iloc[-1]
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return float(val)


def _compute_rsi(close: pd.Series, period: int) -> float | None:
    """Return the most recent N-period RSI, or None on NaN.

    Uses simple moving average over gains/losses (the most common
    library default — matches yfinance/TA-Lib `RSI` and most online
    chart implementations). Wilder's smoothing produces slightly
    different values but is not the spec-canonical method.

    Formula:
        delta = close.diff()
        gain = where(delta > 0, delta, 0).rolling(period).mean()
        loss = where(delta < 0, -delta, 0).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

    Edge cases:
      - All gains in window → loss=0 → rs=inf → rsi=100
      - All losses in window → gain=0 → rs=0 → rsi=0
      - Both zero (flat) → rs=nan → return None (fail-closed)
    """
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=period, min_periods=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period, min_periods=period).mean()

    last_gain = gain.iloc[-1]
    last_loss = loss.iloc[-1]
    if last_gain is None or last_loss is None:
        return None
    if isinstance(last_gain, float) and math.isnan(last_gain):
        return None
    if isinstance(last_loss, float) and math.isnan(last_loss):
        return None

    if last_loss == 0 and last_gain == 0:
        return None  # flat — undefined
    if last_loss == 0:
        return 100.0
    if last_gain == 0:
        return 0.0

    rs = last_gain / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _compute_bbands(
    close: pd.Series, period: int, stdev_mult: float,
) -> tuple[float, float, float] | None:
    """Return (lower, middle, upper) Bollinger band values for the most
    recent bar, or None on NaN.

    Uses population standard deviation (ddof=0), matching the canonical
    Bollinger formulation used by most charting libraries. ddof=1 (sample)
    would slightly widen the bands; for the screener's purpose the
    difference is immaterial.
    """
    if len(close) < period:
        return None
    middle = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)

    last_middle = middle.iloc[-1]
    last_std = std.iloc[-1]
    if last_middle is None or last_std is None:
        return None
    if isinstance(last_middle, float) and math.isnan(last_middle):
        return None
    if isinstance(last_std, float) and math.isnan(last_std):
        return None

    middle_f = float(last_middle)
    std_f = float(last_std)
    upper = middle_f + (stdev_mult * std_f)
    lower = middle_f - (stdev_mult * std_f)
    return (lower, middle_f, upper)


def _compute_lowest_low(low: pd.Series, window: int) -> float | None:
    """Return the lowest low of the trailing N bars, or None on NaN."""
    if len(low) < window:
        return None
    rolling_min = low.rolling(window=window, min_periods=window).min()
    val = rolling_min.iloc[-1]
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return float(val)


# ---------------------------------------------------------------------------
# Pull-back gate — pure function, takes raw indicator values
# ---------------------------------------------------------------------------

def _passes_pullback_gate(
    current_price: float,
    sma_200: float,
    rsi_14: float,
    bband_lower: float,
) -> bool:
    """The Phase 2 pullback predicate.

    All three sub-gates must pass:
      1. Current price above the long-term moving average (uptrend intact).
      2. RSI in the [35, 45] band (oversold but not crashing).
      3. Current price within 2% of the lower Bollinger band (touch or near-touch).
    """
    if current_price <= sma_200:
        return False
    if rsi_14 < config.RSI_MIN or rsi_14 > config.RSI_MAX:
        return False
    if current_price > bband_lower * config.BBAND_PULLBACK_TOLERANCE:
        return False
    return True


# ---------------------------------------------------------------------------
# Batch download wrapper + per-ticker dispatch
# ---------------------------------------------------------------------------

def _default_yf_download(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Production yfinance batch downloader.

    Wraps `yfinance.download(symbols, ...)` and unwraps its quirky
    MultiIndex shape into a flat `{ticker: DataFrame}` dict so the rest
    of the module never has to think about yfinance's API.

    Returns an empty dict if yfinance is not installed or the call fails.
    The screener fails-closed in that case (no Phase 2 survivors), which
    is the correct safety behavior — we'd rather emit zero hits than
    emit hits computed from corrupted data.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; Phase 2 returning empty result")
        return {}

    if not symbols:
        return {}

    try:
        raw = yf.download(
            tickers=symbols,
            period=config.YFINANCE_HISTORY_PERIOD,
            interval=config.YFINANCE_HISTORY_INTERVAL,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning("Phase 2: yfinance batch download failed: %s", exc)
        return {}

    if raw is None or raw.empty:
        return {}

    result: dict[str, pd.DataFrame] = {}

    # yfinance returns a MultiIndex DataFrame for multi-ticker requests,
    # and a flat DataFrame for single-ticker requests. Normalize both.
    if isinstance(raw.columns, pd.MultiIndex):
        # MultiIndex: columns are (ticker, field) tuples
        tickers_in_result = raw.columns.get_level_values(0).unique()
        for tk in tickers_in_result:
            try:
                sub = raw[tk].dropna(how="all")
                if not sub.empty:
                    result[str(tk)] = sub
            except (KeyError, ValueError):
                continue
    else:
        # Flat DataFrame — single ticker case
        if len(symbols) == 1:
            sub = raw.dropna(how="all")
            if not sub.empty:
                result[symbols[0]] = sub

    return result


def _candidate_from_frame(
    upstream: UniverseTicker, frame: pd.DataFrame,
) -> TechnicalCandidate | None:
    """Compute Phase 2 indicators for one ticker. Returns the
    TechnicalCandidate if all gates pass, None on NaN/missing/fail.
    """
    if frame is None or frame.empty:
        return None

    # Required columns: Close, Low (BBand and SMA use Close; lowest_low uses Low)
    if "Close" not in frame.columns or "Low" not in frame.columns:
        return None

    close = frame["Close"]
    low = frame["Low"]

    if len(close) == 0:
        return None

    # Most recent close — last non-NaN value
    last_close = close.iloc[-1]
    if last_close is None or (isinstance(last_close, float) and math.isnan(last_close)):
        return None
    current_price = float(last_close)

    sma_200 = _compute_sma(close, config.SMA_LONG_WINDOW)
    if sma_200 is None:
        return None

    rsi_14 = _compute_rsi(close, config.RSI_PERIOD)
    if rsi_14 is None:
        return None

    bbands = _compute_bbands(close, config.BBAND_PERIOD, config.BBAND_STDEV)
    if bbands is None:
        return None
    bband_lower, bband_middle, bband_upper = bbands

    lowest_low_21d = _compute_lowest_low(low, config.LOWEST_LOW_WINDOW)
    if lowest_low_21d is None:
        return None

    # Apply the gate
    if not _passes_pullback_gate(current_price, sma_200, rsi_14, bband_lower):
        return None

    return TechnicalCandidate.from_universe(
        upstream,
        current_price=current_price,
        sma_200=sma_200,
        rsi_14=rsi_14,
        bband_lower=bband_lower,
        bband_middle=bband_middle,
        bband_upper=bband_upper,
        lowest_low_21d=lowest_low_21d,
    )


# ---------------------------------------------------------------------------
# Phase 2 orchestrator — sync (yfinance has no async API)
# ---------------------------------------------------------------------------

def run_phase_2(
    survivors: list[UniverseTicker],
    *,
    yf_download_fn: Callable[[list[str]], dict[str, pd.DataFrame]] | None = None,
) -> list[TechnicalCandidate]:
    """Execute Phase 2: batched yfinance download + indicator gate.

    Args:
        survivors: Phase 1 output. Empty list returns empty list.
        yf_download_fn: optional injection point for tests. Default is
            _default_yf_download which wraps yfinance.download. Tests
            inject a fixture function returning {ticker: DataFrame}.

    Returns:
        List of TechnicalCandidate survivors. Failed/missing tickers
        are silently dropped (fail-closed per Architect ruling).
    """
    if not survivors:
        logger.info("Phase 2: empty input, returning empty result")
        return []

    download_fn = yf_download_fn if yf_download_fn is not None else _default_yf_download
    symbols = [t.ticker for t in survivors]

    logger.info(
        "Phase 2 (yfinance batch technicals): downloading %d tickers (period=%s)",
        len(symbols), config.YFINANCE_HISTORY_PERIOD,
    )

    download_result = download_fn(symbols)
    if not download_result:
        logger.warning("Phase 2: download returned empty result, no survivors")
        return []

    candidates: list[TechnicalCandidate] = []
    for upstream in survivors:
        frame = download_result.get(upstream.ticker)
        if frame is None:
            continue  # fail-closed: missing from batch
        candidate = _candidate_from_frame(upstream, frame)
        if candidate is not None:
            candidates.append(candidate)

    logger.info(
        "Phase 2 complete: %d/%d tickers in active pullback",
        len(candidates), len(survivors),
    )
    return candidates
