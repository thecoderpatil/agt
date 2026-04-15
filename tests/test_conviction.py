"""Tests for agt_equities.conviction module."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


class TestComputeConvictionTier:
    """Unit tests for compute_conviction_tier."""

    def test_high_conviction(self, monkeypatch):
        """Positive EPS + above-sector revenue + non-downgrade → HIGH."""
        fake_info = {
            "trailingEps": 5.0,
            "forwardEps": 6.0,       # +20% growth
            "revenueGrowth": 0.15,   # above 10%
            "recommendationKey": "buy",
            "operatingMargins": 0.20,
        }
        fake_ticker = MagicMock()
        fake_ticker.info = fake_info

        with patch("agt_equities.conviction.yf") as mock_yf:
            mock_yf.Ticker.return_value = fake_ticker
            from agt_equities.conviction import compute_conviction_tier
            result = compute_conviction_tier("AAPL")

        assert result["tier"] == "HIGH"
        assert result["modifier"] == 0.20
        assert result["inputs"]["eps_revision_trend"] == "POSITIVE"
        assert result["inputs"]["revenue_growth_vs_sector"] == "ABOVE"
        assert result["inputs"]["analyst_consensus_shift"] == "UPGRADE"

    def test_low_conviction_negative_eps(self, monkeypatch):
        """Negative EPS trend → LOW."""
        fake_info = {
            "trailingEps": 5.0,
            "forwardEps": 4.0,       # -20% growth
            "revenueGrowth": 0.05,
            "recommendationKey": "hold",
            "operatingMargins": 0.10,
        }
        fake_ticker = MagicMock()
        fake_ticker.info = fake_info

        with patch("agt_equities.conviction.yf") as mock_yf:
            mock_yf.Ticker.return_value = fake_ticker
            from agt_equities.conviction import compute_conviction_tier
            result = compute_conviction_tier("TSLA")

        assert result["tier"] == "LOW"
        assert result["modifier"] == 0.40

    def test_neutral_conviction(self, monkeypatch):
        """Flat EPS + at-sector revenue + stable → NEUTRAL."""
        fake_info = {
            "trailingEps": 5.0,
            "forwardEps": 5.1,       # ~2% growth (flat)
            "revenueGrowth": 0.05,   # at sector
            "recommendationKey": "hold",
            "operatingMargins": 0.10,
        }
        fake_ticker = MagicMock()
        fake_ticker.info = fake_info

        with patch("agt_equities.conviction.yf") as mock_yf:
            mock_yf.Ticker.return_value = fake_ticker
            from agt_equities.conviction import compute_conviction_tier
            result = compute_conviction_tier("MSFT")

        assert result["tier"] == "NEUTRAL"
        assert result["modifier"] == 0.30

    def test_yfinance_exception_returns_neutral(self, monkeypatch):
        """If yfinance raises, return NEUTRAL with UNAVAILABLE inputs."""
        with patch("agt_equities.conviction.yf") as mock_yf:
            mock_yf.Ticker.side_effect = RuntimeError("network error")
            from agt_equities.conviction import compute_conviction_tier
            result = compute_conviction_tier("BAD")

        assert result["tier"] == "NEUTRAL"
        assert all(v == "UNAVAILABLE" for v in result["inputs"].values())


class TestRefreshConvictionData:
    """Tests for refresh_conviction_data batch orchestrator."""

    def test_refresh_updates_count(self, monkeypatch):
        from agt_equities.conviction import refresh_conviction_data

        computed = []
        def fake_compute(ticker):
            computed.append(ticker)
            return {"tier": "NEUTRAL", "modifier": 0.30, "inputs": {}}

        persisted = []
        def fake_persist(ticker, conviction, **kwargs):
            persisted.append(ticker)

        monkeypatch.setattr("agt_equities.conviction.compute_conviction_tier", fake_compute)
        monkeypatch.setattr("agt_equities.conviction.persist_conviction", fake_persist)

        result = refresh_conviction_data({"AAPL", "MSFT", "GOOG"})
        assert result["updated"] == 3
        assert result["failed"] == 0
        assert result["total"] == 3
        assert result["error"] is None

    def test_refresh_counts_failures(self, monkeypatch):
        from agt_equities.conviction import refresh_conviction_data

        call_count = [0]
        def fake_compute(ticker):
            call_count[0] += 1
            if ticker == "BAD":
                raise RuntimeError("boom")
            return {"tier": "NEUTRAL", "modifier": 0.30, "inputs": {}}

        monkeypatch.setattr("agt_equities.conviction.compute_conviction_tier", fake_compute)
        monkeypatch.setattr("agt_equities.conviction.persist_conviction", lambda *a, **kw: None)

        result = refresh_conviction_data({"AAPL", "BAD"})
        assert result["updated"] == 1
        assert result["failed"] == 1
        assert result["total"] == 2


class TestConvictionAlertFormat:
    """Test CONVICTION_REFRESH format branch in alerts.py."""

    def test_conviction_refresh_format(self):
        from agt_equities.alerts import format_alert_text
        msg = format_alert_text({
            "kind": "CONVICTION_REFRESH",
            "payload": {"updated": 5, "failed": 1, "total": 6},
            "severity": "warn",
        })
        assert "5/6 updated" in msg
        assert "1 failed" in msg
        assert "[WARN]" in msg

    def test_conviction_refresh_no_failures(self):
        from agt_equities.alerts import format_alert_text
        msg = format_alert_text({
            "kind": "CONVICTION_REFRESH",
            "payload": {"updated": 10, "failed": 0, "total": 10},
            "severity": "info",
        })
        assert "10/10 updated" in msg
        assert "failed" not in msg
