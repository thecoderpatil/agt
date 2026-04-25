"""agt_equities.scan_bridge — Sprint B5.c bridge layer (bridge-1 + bridge-2).

Bridges ``pxo_scanner.scan_csp_candidates`` (emits dicts) to
``csp_allocator.run_csp_allocator`` (expects objects with attribute access
and an injected ``extras_provider``).

This module is intentionally pure: no IB calls, no DB, no Telegram imports,
no yfinance. The live ``/scan`` command in ``telegram_bot.py`` composes it
with ``_discover_positions`` + ``_fetch_household_buying_power_snapshot`` +
``run_csp_allocator`` (staging_callback=None → dry-run).

Unit conventions (verified against csp_allocator + pxo_scanner as of
2026-04-15 origin/main):

* Scanner's ``ann_roi`` is a percent (e.g. ``32.5`` for 32.5% annualised).
  Allocator's ``_format_digest`` prints ``annualized_yield`` with a ``% ann``
  suffix (line 1022 of csp_allocator.py), treating it as a percent. So we
  map ``annualized_yield ← ann_roi`` with no unit change.
* Scanner's ``premium`` is the per-contract option premium in dollars.
  Allocator uses ``.mid`` for the limit-price field in staged tickets
  (``_tickets_from_digest``). Map ``mid ← premium`` direct.
* Scanner's ``expiry`` is ``YYYY-MM-DD``. Allocator's ``_build_csp_proposal``
  does ``candidate.expiry.replace("-", "")`` to get ``YYYYMMDD``, so we
  preserve the dash form here.

Gate tolerance (verified against CSP_GATE_REGISTRY):

* ``rule_7_csp_procedure`` tolerates ``extras['delta'] is None`` (only
  vetos when delta is non-None AND abs > 0.25).
* ``rule_7`` also tolerates ``extras['days_to_earnings'] is None``.
* ``rule_3_sector`` and ``rule_4_correlation`` gracefully no-op when
  ``sector_map`` / ``correlations`` are missing or empty.

So a minimal extras_provider that supplies only ``sector_map`` is
acceptable for bridge-1 (dry-run digest). The bridge-2 provider supplies
real delta, days_to_earnings, and correlations so Rules 3, 4, and 7
actually enforce their gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# ScanCandidate adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanCandidate:
    """Attribute-access wrapper over a ``pxo_scanner`` dict row.

    Fields required by ``csp_allocator``:
        ticker, strike, mid, expiry (YYYY-MM-DD), annualized_yield.
    Convenience fields (preserved for digest / downstream consumers):
        dte, otm_pct, capital_required, headline, sector, delta.
    """

    ticker: str
    strike: float
    mid: float
    expiry: str
    annualized_yield: float
    dte: int = 0
    otm_pct: float = 0.0
    capital_required: float = 0.0
    headline: str = ""
    sector: str = "Unknown"
    delta: float = 0.0
    current_price: float = 0.0
    household_id: str = ""


def adapt_scanner_candidates(rows: list[dict]) -> list[ScanCandidate]:
    """Convert pxo_scanner output dicts to ScanCandidate objects.

    Silently drops rows missing any of the five allocator-required keys
    (``ticker``, ``strike``, ``expiry``, ``premium``, ``ann_roi``) — a
    malformed row should never abort the full scan.

    Args:
        rows: Output of ``pxo_scanner.scan_csp_candidates``.

    Returns:
        List of ScanCandidate, preserving input order.
    """
    out: list[ScanCandidate] = []
    for row in rows or []:
        try:
            ticker = str(row["ticker"]).upper()
            strike = float(row["strike"])
            premium = float(row["premium"])
            expiry = str(row["expiry"])
            ann_roi = float(row["ann_roi"])
        except (KeyError, TypeError, ValueError):
            continue
        if not ticker or strike <= 0 or premium < 0 or not expiry:
            continue
        # Delta: scanner emits abs(delta) as float; 0.0 fallback if missing.
        raw_delta = row.get("delta")
        try:
            delta = abs(float(raw_delta)) if raw_delta is not None else 0.0
        except (TypeError, ValueError):
            delta = 0.0
        _cp_raw = row.get("current_price") or row.get("underlying_price")
        current_price = float(_cp_raw) if _cp_raw is not None else 0.0
        out.append(
            ScanCandidate(
                ticker=ticker,
                strike=strike,
                mid=premium,
                expiry=expiry,
                annualized_yield=ann_roi,
                dte=int(row.get("dte", 0) or 0),
                otm_pct=float(row.get("otm_pct", 0.0) or 0.0),
                capital_required=float(row.get("capital_required", 0.0) or 0.0),
                headline=str(row.get("headline", "") or ""),
                sector=str(row.get("sector", "Unknown") or "Unknown"),
                delta=delta,
                current_price=current_price,
            )
        )
    return out


# ---------------------------------------------------------------------------
# extras_provider factories
# ---------------------------------------------------------------------------


def build_watchlist_sector_map(watchlist: list[dict]) -> dict[str, str]:
    """Extract {TICKER: sector} from a scanner watchlist.

    Missing / falsy sectors collapse to ``"Unknown"``.
    """
    out: dict[str, str] = {}
    for row in watchlist or []:
        try:
            ticker = str(row["ticker"]).upper()
        except (KeyError, TypeError):
            continue
        if not ticker:
            continue
        sector = row.get("sector") or "Unknown"
        out[ticker] = str(sector)
    return out


def make_minimal_extras_provider(
    sector_map: dict[str, str],
) -> Callable[[dict, Any], dict]:
    """Return an extras_provider with only sector_map populated.

    delta / days_to_earnings / correlations default to None/empty — rule_7
    tolerates ``None`` delta + None earnings; rule_4 no-ops on empty
    correlations. This is deliberately lossy: it's the bridge-1 scope.
    The staging-capable bridge-2 provider MUST supply real delta +
    correlations + earnings before any capital-touching path is wired.

    The returned callable is pure and snapshot-free: it closes over
    sector_map but ignores its ``hh`` arg. Safe to reuse across a full
    scanner run.
    """
    frozen_map = dict(sector_map)  # defensive copy

    def _provider(hh: dict, candidate: Any) -> dict:
        return {
            "sector_map": frozen_map,
            "correlations": {},
            "delta": None,
            "days_to_earnings": None,
        }

    return _provider


def make_bridge2_extras_provider(
    sector_map: dict[str, str],
    earnings_map: dict[str, int | None],
    correlation_pairs: dict[tuple[str, str], float],
) -> Callable[[dict, Any], dict]:
    """Return a bridge-2 extras_provider with real gate data.

    Unlike the bridge-1 minimal provider, this supplies:
      - ``delta``: read from ``candidate.delta`` (carried through from
        pxo_scanner via ScanCandidate).
      - ``days_to_earnings``: looked up from ``earnings_map`` by ticker.
      - ``correlations``: the full pre-computed pairwise dict.
      - ``sector_map``: same as bridge-1.

    All four fields are populated so Rules 3, 4, and 7 actually enforce
    their gates instead of fail-opening on None/empty.

    The returned callable is pure and snapshot-free: it closes over the
    injected maps and reads candidate.delta per call. Safe to reuse
    across a full allocator run.

    Args:
        sector_map: {TICKER: GICS_sector} from the watchlist.
        earnings_map: {TICKER: days_to_next_earnings or None}.
            Built by ``scan_extras.fetch_earnings_map()``.
        correlation_pairs: {(ticker_a, ticker_b): correlation}.
            Built by ``scan_extras.build_correlation_pairs()``.
            Order-independent — rule_4 checks both (a,b) and (b,a).
    """
    frozen_sectors = dict(sector_map)
    frozen_earnings = dict(earnings_map)
    frozen_corr = dict(correlation_pairs)

    def _provider(hh: dict, candidate: Any) -> dict:
        ticker = getattr(candidate, "ticker", "").upper()
        return {
            "sector_map": frozen_sectors,
            "correlations": frozen_corr,
            "delta": getattr(candidate, "delta", None),
            "days_to_earnings": frozen_earnings.get(ticker),
        }

    return _provider
