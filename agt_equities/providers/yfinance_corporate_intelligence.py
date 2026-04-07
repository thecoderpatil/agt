"""
agt_equities/providers/yfinance_corporate_intelligence.py — ICorporateIntelligence via yfinance.

COLD PATH ONLY. yfinance is unreliable; cache aggressively.

DEPLOYMENT: Replace this entire class with Reuters Worldwide Fundamentals +
Wall Street Horizon (or equivalent paid feed) at production cutover. All
yfinance calls are marked with # DEPLOYMENT: replace comments.

Phase 3A.5c1.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from agt_equities.market_data_dtos import (
    CorporateCalendarDTO,
    CorporateActionType,
    ConvictionMetricsDTO,
)

logger = logging.getLogger(__name__)


class YFinanceCorporateIntelligenceProvider:
    """ICorporateIntelligence via yfinance with file-based caching.

    Cache: JSON files in cache_dir, one per ticker per data type.
    TTL: max_age_hours (default 24). Returns stale cache on fetch failure.
    """

    def __init__(self, cache_dir: str = "agt_desk_cache/corporate_intel",
                 max_age_hours: float = 24.0):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_age_hours = max_age_hours

    def get_corporate_calendar(
        self, ticker: str,
    ) -> Optional[CorporateCalendarDTO]:
        """Earnings dates, ex-dividend dates, pending corporate actions.

        NOT in any execution hot path. Cached with 24h TTL.
        """
        cached = self._read_cache(ticker, "calendar")
        if cached is not None:
            age = self._cache_age_hours(ticker, "calendar")
            if age < self._max_age_hours:
                return cached

        try:
            # DEPLOYMENT: replace with Wall Street Horizon API
            import yfinance as yf  # noqa: F811
            t = yf.Ticker(ticker)
            info = t.info or {}

            next_earnings = None
            try:
                cal = t.calendar
                if cal is not None and isinstance(cal, dict):
                    earn = cal.get("Earnings Date")
                    if earn and len(earn) > 0:
                        next_earnings = earn[0].date() if hasattr(earn[0], "date") else None
            except Exception:
                pass

            ex_div = None
            div_amount = 0.0
            try:
                ex_div_str = info.get("exDividendDate")
                if ex_div_str:
                    from datetime import datetime as _dt
                    ex_div = _dt.fromtimestamp(ex_div_str).date()
                div_amount = float(info.get("dividendRate", 0) or 0)
            except Exception:
                pass

            now = datetime.now(timezone.utc)
            dto = CorporateCalendarDTO(
                symbol=ticker,
                next_earnings=next_earnings,
                ex_dividend_date=ex_div,
                dividend_amount=div_amount,
                pending_corporate_action=CorporateActionType.NONE,
                data_source="yfinance_temporary",
                cached_at=now,
                cache_age_hours=0.0,
            )
            self._write_cache(ticker, "calendar", dto)
            return dto

        except Exception as exc:
            logger.warning("yfinance calendar fetch failed for %s: %s", ticker, exc)
            if cached is not None:
                return cached  # stale cache better than None
            return None

    def get_conviction_metrics(
        self, ticker: str,
    ) -> Optional[ConvictionMetricsDTO]:
        """Fundamentals for R8 Gate 1 conviction tier.

        NOT in any execution hot path. Cached with 24h TTL.
        """
        cached = self._read_cache_conviction(ticker)
        if cached is not None:
            age = self._cache_age_hours(ticker, "conviction")
            if age < self._max_age_hours:
                return cached

        try:
            # DEPLOYMENT: replace with Reuters Worldwide Fundamentals
            import yfinance as yf  # noqa: F811
            t = yf.Ticker(ticker)
            info = t.info or {}

            eps = info.get("trailingEps")
            eps_positive = eps > 0 if eps is not None else False

            revenue = info.get("totalRevenue")
            revenue_above = revenue is not None and revenue > 0

            rec = info.get("recommendationKey", "")
            has_downgrade = rec in ("sell", "underperform", "strong_sell")

            margin = info.get("operatingMargins")
            op_margin = float(margin) if margin is not None else 0.0

            now = datetime.now(timezone.utc)
            dto = ConvictionMetricsDTO(
                symbol=ticker,
                eps_positive=eps_positive,
                revenue_above_sector_median=revenue_above,
                has_analyst_downgrade=has_downgrade,
                operating_margin=round(op_margin, 4),
                data_source="yfinance_temporary",
                cached_at=now,
                cache_age_hours=0.0,
            )
            self._write_cache_conviction(ticker, dto)
            return dto

        except Exception as exc:
            logger.warning("yfinance conviction fetch failed for %s: %s", ticker, exc)
            if cached is not None:
                return cached
            return None

    # --- Cache helpers ---

    def _cache_path(self, ticker: str, dtype: str) -> Path:
        return self._cache_dir / f"{ticker}_{dtype}.json"

    def _cache_age_hours(self, ticker: str, dtype: str) -> float:
        path = self._cache_path(ticker, dtype)
        if not path.exists():
            return float("inf")
        try:
            data = json.loads(path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            now = datetime.now(timezone.utc)
            return (now - cached_at).total_seconds() / 3600
        except Exception:
            return float("inf")

    def _read_cache(self, ticker: str, dtype: str) -> Optional[CorporateCalendarDTO]:
        path = self._cache_path(ticker, dtype)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            age = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            return CorporateCalendarDTO(
                symbol=data["symbol"],
                next_earnings=date.fromisoformat(data["next_earnings"]) if data.get("next_earnings") else None,
                ex_dividend_date=date.fromisoformat(data["ex_dividend_date"]) if data.get("ex_dividend_date") else None,
                dividend_amount=data.get("dividend_amount", 0.0),
                pending_corporate_action=CorporateActionType(data.get("pending_corporate_action", "none")),
                data_source=data.get("data_source", "yfinance_temporary"),
                cached_at=cached_at,
                cache_age_hours=round(age, 2),
            )
        except Exception:
            return None

    def _write_cache(self, ticker: str, dtype: str, dto: CorporateCalendarDTO) -> None:
        try:
            data = {
                "symbol": dto.symbol,
                "next_earnings": dto.next_earnings.isoformat() if dto.next_earnings else None,
                "ex_dividend_date": dto.ex_dividend_date.isoformat() if dto.ex_dividend_date else None,
                "dividend_amount": dto.dividend_amount,
                "pending_corporate_action": dto.pending_corporate_action.value,
                "data_source": dto.data_source,
                "cached_at": dto.cached_at.isoformat(),
            }
            self._cache_path(ticker, dtype).write_text(json.dumps(data))
        except Exception as exc:
            logger.warning("Cache write failed for %s/%s: %s", ticker, dtype, exc)

    def _read_cache_conviction(self, ticker: str) -> Optional[ConvictionMetricsDTO]:
        path = self._cache_path(ticker, "conviction")
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            age = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            return ConvictionMetricsDTO(
                symbol=data["symbol"],
                eps_positive=data["eps_positive"],
                revenue_above_sector_median=data["revenue_above_sector_median"],
                has_analyst_downgrade=data["has_analyst_downgrade"],
                operating_margin=data["operating_margin"],
                data_source=data.get("data_source", "yfinance_temporary"),
                cached_at=cached_at,
                cache_age_hours=round(age, 2),
            )
        except Exception:
            return None

    def _write_cache_conviction(self, ticker: str, dto: ConvictionMetricsDTO) -> None:
        try:
            data = {
                "symbol": dto.symbol,
                "eps_positive": dto.eps_positive,
                "revenue_above_sector_median": dto.revenue_above_sector_median,
                "has_analyst_downgrade": dto.has_analyst_downgrade,
                "operating_margin": dto.operating_margin,
                "data_source": dto.data_source,
                "cached_at": dto.cached_at.isoformat(),
            }
            self._cache_path(ticker, "conviction").write_text(json.dumps(data))
        except Exception as exc:
            logger.warning("Cache write failed for %s/conviction: %s", ticker, exc)
