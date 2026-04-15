"""Conviction-tier computation and persistence.

Extracted from ``telegram_bot.py`` during Decoupling Sprint A (A5e).
Pure library module — no IB, no Telegram, no asyncio.

Public API
----------
* ``compute_conviction_tier(ticker)`` — yfinance-based tier.
* ``persist_conviction(ticker, conviction, *, db_path=None)`` — UPDATE ticker_universe.
* ``get_effective_conviction(ticker, *, db_path=None)`` — CIO-override aware.
* ``refresh_conviction_data(held_tickers, *, db_path=None)`` — batch refresh.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime as _datetime, timezone as _timezone
from zoneinfo import ZoneInfo

import yfinance as yf

from agt_equities.db import get_db_connection, tx_immediate

logger = logging.getLogger("agt_bridge.conviction")

# ── Constants (mirrored from telegram_bot.py) ──────────────────────────
CONVICTION_TIERS: dict[str, float] = {
    "HIGH":    0.20,
    "NEUTRAL": 0.30,
    "LOW":     0.40,
}

CONVICTION_OVERRIDE_EXPIRY_DAYS = 90

# Tickers excluded from conviction refresh (non-equity / internal).
EXCLUDED_TICKERS: frozenset[str] = frozenset(
    {"IBKR", "TRAW.CVR", "SPX", "SLS", "GTLB"}
)

_LEGACY_OVERRIDE_TZ = ZoneInfo("America/New_York")


# ── Helpers ────────────────────────────────────────────────────────────

def _parse_override_expiry(raw: str) -> _datetime:
    """Parse override expiry; handles legacy naive (assume ET) and UTC-aware."""
    dt = _datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LEGACY_OVERRIDE_TZ)
    return dt.astimezone(_timezone.utc)


# ── Core functions ─────────────────────────────────────────────────────

def compute_conviction_tier(ticker: str) -> dict:
    """Compute conviction tier from yfinance fundamentals.

    Returns ``{"tier": str, "modifier": float, "inputs": {...}}``.
    """
    try:
        yf_tkr = yf.Ticker(ticker)
        info = yf_tkr.info or {}

        # EPS revision trend
        trailing_eps = info.get("trailingEps")
        forward_eps = info.get("forwardEps")
        if trailing_eps and forward_eps and abs(trailing_eps) > 0:
            eps_growth = (forward_eps - trailing_eps) / abs(trailing_eps)
            if eps_growth > 0.05:
                eps_trend = "POSITIVE"
            elif eps_growth < -0.05:
                eps_trend = "NEGATIVE"
            else:
                eps_trend = "FLAT"
        else:
            eps_trend = "UNAVAILABLE"

        # Revenue growth
        revenue_growth = info.get("revenueGrowth")
        if revenue_growth is not None:
            if revenue_growth > 0.10:
                rev_vs_sector = "ABOVE"
            elif revenue_growth > 0.0:
                rev_vs_sector = "AT"
            else:
                rev_vs_sector = "BELOW"
        else:
            rev_vs_sector = "UNAVAILABLE"

        # Analyst consensus
        rec_key = info.get("recommendationKey", "").lower()
        if rec_key in ("strong_buy", "buy"):
            analyst_shift = "UPGRADE"
        elif rec_key in ("sell", "strong_sell", "underperform"):
            analyst_shift = "DOWNGRADE"
        else:
            analyst_shift = "STABLE"

        # Margin level (point-in-time, not trend)
        op_margin = info.get("operatingMargins")
        if op_margin is not None:
            if op_margin > 0.15:
                margin_trend = "HIGH_MARGIN"
            elif op_margin > 0.05:
                margin_trend = "MID_MARGIN"
            else:
                margin_trend = "LOW_MARGIN"
        else:
            margin_trend = "UNAVAILABLE"

        # Tier assignment
        high_qual = (
            eps_trend == "POSITIVE"
            and rev_vs_sector in ("ABOVE", "AT")
            and analyst_shift != "DOWNGRADE"
        )
        low_qual = (
            eps_trend == "NEGATIVE"
            or rev_vs_sector == "BELOW"
            or margin_trend == "LOW_MARGIN"
            or analyst_shift == "DOWNGRADE"
        )

        if high_qual:
            tier = "HIGH"
        elif low_qual:
            tier = "LOW"
        else:
            tier = "NEUTRAL"

        return {
            "tier": tier,
            "modifier": CONVICTION_TIERS[tier],
            "inputs": {
                "eps_revision_trend": eps_trend,
                "revenue_growth_vs_sector": rev_vs_sector,
                "analyst_consensus_shift": analyst_shift,
                "margin_trend": margin_trend,
            },
        }
    except Exception as exc:
        logger.warning("compute_conviction_tier failed for %s: %s", ticker, exc)
        return {
            "tier": "NEUTRAL",
            "modifier": CONVICTION_TIERS["NEUTRAL"],
            "inputs": {
                "eps_revision_trend": "UNAVAILABLE",
                "revenue_growth_vs_sector": "UNAVAILABLE",
                "analyst_consensus_shift": "UNAVAILABLE",
                "margin_trend": "UNAVAILABLE",
            },
        }


def persist_conviction(
    ticker: str,
    conviction: dict,
    *,
    db_path: str | None = None,
) -> None:
    """Save computed conviction to ``ticker_universe``."""
    try:
        inputs = conviction.get("inputs", {})
        with closing(get_db_connection(db_path=db_path)) as conn:
            with tx_immediate(conn):
                conn.execute(
                    """
                    UPDATE ticker_universe
                    SET conviction_tier = ?,
                        eps_revision_trend = ?,
                        revenue_growth_vs_sector = ?,
                        analyst_consensus_shift = ?,
                        margin_trend = ?,
                        conviction_updated_at = ?
                    WHERE ticker = ?
                    """,
                    (
                        conviction["tier"],
                        inputs.get("eps_revision_trend"),
                        inputs.get("revenue_growth_vs_sector"),
                        inputs.get("analyst_consensus_shift"),
                        inputs.get("margin_trend"),
                        _datetime.now().isoformat(),
                        ticker,
                    ),
                )
    except Exception as exc:
        logger.warning("persist_conviction failed for %s: %s", ticker, exc)


def get_effective_conviction(
    ticker: str,
    *,
    db_path: str | None = None,
) -> dict:
    """Get conviction tier, checking for active CIO overrides first.

    Override expires after ``CONVICTION_OVERRIDE_EXPIRY_DAYS``.
    """
    try:
        with closing(get_db_connection(db_path=db_path)) as conn:
            override = conn.execute(
                """
                SELECT overridden_tier, justification, expires_at
                FROM conviction_overrides
                WHERE ticker = ? AND active = 1
                ORDER BY created_at DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()

            if override:
                try:
                    expires = _parse_override_expiry(override["expires_at"])
                    if _datetime.now(_timezone.utc) > expires:
                        conn.execute(
                            "UPDATE conviction_overrides SET active = 0 WHERE ticker = ? AND active = 1",
                            (ticker,),
                        )
                    else:
                        tier = override["overridden_tier"]
                        return {
                            "tier": tier,
                            "modifier": CONVICTION_TIERS.get(tier, 0.30),
                            "source": "CIO_OVERRIDE",
                            "justification": override["justification"],
                            "expires": override["expires_at"][:10],
                        }
                except (ValueError, TypeError):
                    pass

        # No active override — compute from fundamentals
        computed = compute_conviction_tier(ticker)
        computed["source"] = "COMPUTED"
        return computed

    except Exception as exc:
        logger.warning("get_effective_conviction failed for %s: %s", ticker, exc)
        return {
            "tier": "NEUTRAL",
            "modifier": 0.30,
            "source": "DEFAULT",
        }


def refresh_conviction_data(
    held_tickers: set[str],
    *,
    db_path: str | None = None,
) -> dict:
    """Batch-refresh conviction tiers for *held_tickers*.

    This is the IB-decoupled orchestrator.  The caller is responsible for
    fetching positions via ``reqPositionsAsync`` and filtering to
    non-zero STK positions excluding :data:`EXCLUDED_TICKERS`.

    Returns ``{"updated": int, "failed": int, "total": int, "error": str|None}``.
    """
    updated = 0
    failed = 0
    for tkr in held_tickers:
        try:
            c = compute_conviction_tier(tkr)
            persist_conviction(tkr, c, db_path=db_path)
            updated += 1
        except Exception as tkr_exc:
            logger.warning("Conviction refresh failed for %s: %s", tkr, tkr_exc)
            failed += 1
    return {"updated": updated, "failed": failed, "total": len(held_tickers), "error": None}
