"""Tests for Underwater Positions panel (formerly Needs Attention)."""
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_cycle(ticker, household, unreal_pct, unreal_dollar, nearest_dte=None,
                open_short_calls=0, open_short_puts=0):
    """Build a cycle dict matching build_cycles_table() output."""
    return {
        "ticker": ticker,
        "household": household,
        "shares": 100,
        "paper_basis": 100.0,
        "spot": 100.0 * (1 + unreal_pct / 100) if unreal_pct else 100.0,
        "unreal_dollar": unreal_dollar,
        "unreal_pct": unreal_pct,
        "nearest_dte": nearest_dte,
        "leg": f"{open_short_calls}C" if open_short_calls else "flat",
        "cycle_seq": 1,
        "premium_total": 0,
        "realized_pnl": 0,
        "adjusted_basis": 100.0,
        "open_short_puts": open_short_puts,
        "open_short_calls": open_short_calls,
        "event_count": 0,
    }


def _filter_attention(cycles):
    """Replicate the attention filter + CC indicator + sort from main.py."""
    ATTENTION_MIN_LOSS_DOLLAR = 1500
    attention = [
        r for r in cycles
        if (r["nearest_dte"] is not None and r["nearest_dte"] <= 5)
        or (r["unreal_pct"] is not None and r["unreal_pct"] < -15
            and r["unreal_dollar"] is not None and abs(r["unreal_dollar"]) >= ATTENTION_MIN_LOSS_DOLLAR)
    ]
    for a in attention:
        a["has_cc"] = a.get("open_short_calls", 0) > 0
    attention.sort(key=lambda r: (
        r["unreal_pct"] if r["unreal_pct"] is not None else 0,
        r.get("household", ""),
    ))
    return attention


class TestUnderwaterPanel(unittest.TestCase):

    def test_shows_household_badge(self):
        """Two households with same ticker → 2 distinct rows with different badges."""
        cycles = [
            _make_cycle("ADBE", "Yash", -20.0, -4000),
            _make_cycle("ADBE", "Vikram", -18.0, -2700),
        ]
        result = _filter_attention(cycles)
        self.assertEqual(len(result), 2)
        households = {r["household"] for r in result}
        self.assertEqual(households, {"Yash", "Vikram"})

    def test_cc_aware_indicator(self):
        """One covered + one uncovered position → icons differ."""
        cycles = [
            _make_cycle("MSFT", "Yash", -22.0, -5000, open_short_calls=2),
            _make_cycle("PYPL", "Yash", -25.0, -3000, open_short_calls=0),
        ]
        result = _filter_attention(cycles)
        self.assertEqual(len(result), 2)
        msft = next(r for r in result if r["ticker"] == "MSFT")
        pypl = next(r for r in result if r["ticker"] == "PYPL")
        self.assertTrue(msft["has_cc"])
        self.assertFalse(pypl["has_cc"])

    def test_sorted_by_loss_desc(self):
        """Biggest loss (most negative %) first, household alpha tiebreak."""
        cycles = [
            _make_cycle("AAPL", "Yash", -16.0, -2000),
            _make_cycle("MSFT", "Vikram", -30.0, -6000),
            _make_cycle("ADBE", "Yash", -22.0, -4400),
            _make_cycle("ADBE", "Vikram", -22.0, -3300),
        ]
        result = _filter_attention(cycles)
        # Expected order: MSFT(-30%), ADBE/Vikram(-22%), ADBE/Yash(-22%), AAPL(-16%)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["ticker"], "MSFT")
        self.assertAlmostEqual(result[0]["unreal_pct"], -30.0)
        self.assertEqual(result[1]["ticker"], "ADBE")
        self.assertEqual(result[1]["household"], "Vikram")
        self.assertEqual(result[2]["ticker"], "ADBE")
        self.assertEqual(result[2]["household"], "Yash")
        self.assertEqual(result[3]["ticker"], "AAPL")


if __name__ == "__main__":
    unittest.main()
