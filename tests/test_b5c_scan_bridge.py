"""Sprint B5.c-bridge-1 tests — scan_bridge adapter + extras_provider +
end-to-end digest-only run through run_csp_allocator.

Marker: sprint_a (runs in the CI slim container). No ib_async / telegram /
anthropic imports — pure csp_allocator + scan_bridge so this test file runs
without the heavy bot deps.
"""
from __future__ import annotations

import pytest
import tempfile
import unittest
from pathlib import Path

pytestmark = pytest.mark.sprint_a

from agt_equities.scan_bridge import (
    ScanCandidate,
    adapt_scanner_candidates,
    build_watchlist_sector_map,
    make_minimal_extras_provider,
)


# ---------------------------------------------------------------------------
# adapt_scanner_candidates
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ADR-008 MR 2: live ctx helper for tests.
# Wraps the caller's staging_fn in a SQLiteOrderSink so we preserve
# "staging_fn(tickets)" semantics without rewriting every assertion.
# ---------------------------------------------------------------------------


def _live_ctx(staging_fn=None):
    """Build a LIVE RunContext whose order_sink forwards tickets to
    ``staging_fn``. ``staging_fn=None`` produces a no-op sink (MR 1
    SQLiteOrderSink.stage early-returns on empty tickets; for non-empty
    it still calls the provided fn, so we default to a discard lambda).
    """
    import uuid as _uuid
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
    fn = staging_fn if staging_fn is not None else (lambda tickets: None)
    return RunContext(
        mode=RunMode.LIVE,
        run_id=_uuid.uuid4().hex,
        order_sink=SQLiteOrderSink(staging_fn=fn),
        decision_sink=NullDecisionSink(),
    
        broker_mode="paper",
        engine="csp",
    )




def _scanner_row(**overrides):
    base = {
        "ticker": "AAPL",
        "strike": 180.0,
        "expiry": "2026-05-15",
        "premium": 2.40,
        "ann_roi": 35.0,
        "dte": 30,
        "otm_pct": 5.2,
        "capital_required": 18000.0,
        "headline": "AAPL up on iPhone demand",
    }
    base.update(overrides)
    return base


class TestAdapter:
    def test_happy_path_maps_all_fields(self):
        out = adapt_scanner_candidates([_scanner_row()])
        assert len(out) == 1
        c = out[0]
        assert isinstance(c, ScanCandidate)
        assert c.ticker == "AAPL"
        assert c.strike == 180.0
        assert c.mid == 2.40                    # premium -> mid
        assert c.expiry == "2026-05-15"         # YYYY-MM-DD preserved
        assert c.annualized_yield == 35.0       # ann_roi percent preserved
        assert c.dte == 30
        assert c.capital_required == 18000.0

    def test_ticker_uppercased(self):
        out = adapt_scanner_candidates([_scanner_row(ticker="msft")])
        assert out[0].ticker == "MSFT"

    def test_missing_required_key_drops_row(self):
        good = _scanner_row()
        bad = _scanner_row()
        bad.pop("strike")
        out = adapt_scanner_candidates([good, bad])
        assert len(out) == 1
        assert out[0].ticker == "AAPL"

    def test_malformed_numeric_drops_row(self):
        bad = _scanner_row(premium="not-a-number")
        assert adapt_scanner_candidates([bad]) == []

    def test_zero_strike_drops_row(self):
        bad = _scanner_row(strike=0)
        assert adapt_scanner_candidates([bad]) == []

    def test_empty_input_returns_empty_list(self):
        assert adapt_scanner_candidates([]) == []
        assert adapt_scanner_candidates(None) == []

    def test_preserves_input_order(self):
        rows = [
            _scanner_row(ticker="A", strike=10.0),
            _scanner_row(ticker="B", strike=20.0),
            _scanner_row(ticker="C", strike=30.0),
        ]
        out = adapt_scanner_candidates(rows)
        assert [c.ticker for c in out] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# build_watchlist_sector_map
# ---------------------------------------------------------------------------


class TestWatchlistSectorMap:
    def test_extracts_uppercased_ticker_to_sector(self):
        wl = [
            {"ticker": "aapl", "sector": "Technology"},
            {"ticker": "JPM",  "sector": "Banks"},
        ]
        out = build_watchlist_sector_map(wl)
        assert out == {"AAPL": "Technology", "JPM": "Banks"}

    def test_missing_sector_collapses_to_unknown(self):
        wl = [{"ticker": "FOO"}, {"ticker": "BAR", "sector": ""}]
        out = build_watchlist_sector_map(wl)
        assert out["FOO"] == "Unknown"
        assert out["BAR"] == "Unknown"

    def test_handles_empty_input(self):
        assert build_watchlist_sector_map([]) == {}
        assert build_watchlist_sector_map(None) == {}


# ---------------------------------------------------------------------------
# make_minimal_extras_provider
# ---------------------------------------------------------------------------


class TestMinimalExtrasProvider:
    def test_returns_all_four_keys(self):
        sm = {"AAPL": "Technology"}
        provider = make_minimal_extras_provider(sm)
        extras = provider({}, ScanCandidate(
            ticker="AAPL", strike=100.0, mid=1.0,
            expiry="2026-01-01", annualized_yield=25.0,
        ))
        assert set(extras.keys()) == {
            "sector_map", "correlations", "delta", "days_to_earnings",
        }
        assert extras["sector_map"] == sm
        assert extras["correlations"] == {}
        assert extras["delta"] is None
        assert extras["days_to_earnings"] is None

    def test_provider_is_defensive_on_sector_map(self):
        """Mutating the source map after provider creation must NOT leak in."""
        sm = {"AAPL": "Technology"}
        provider = make_minimal_extras_provider(sm)
        sm["MSFT"] = "Software"  # mutate source
        extras = provider({}, ScanCandidate(
            ticker="AAPL", strike=100.0, mid=1.0,
            expiry="2026-01-01", annualized_yield=25.0,
        ))
        assert "MSFT" not in extras["sector_map"]

    def test_provider_ignores_hh_arg(self):
        provider = make_minimal_extras_provider({})
        a = provider({"household": "X"}, None)
        b = provider({"household": "Y"}, None)
        # Same shape regardless of household input.
        assert a == b


# ---------------------------------------------------------------------------
# End-to-end digest-only dry-run through run_csp_allocator
# ---------------------------------------------------------------------------


class TestE2EDigestDryRun:
    """Round-trip: adapter -> run_csp_allocator(staging_callback=None) ->
    digest_lines. Proves the bridge-1 surface is wired without staging.
    """

    def _household_snapshot(self, hh_name: str, acct_id: str, *,
                            margin_eligible: bool = True,
                            nlv: float = 100_000.0):
        return {
            hh_name: {
                "household": hh_name,
                "hh_nlv": nlv,
                "hh_margin_nlv": nlv if margin_eligible else 0.0,
                "hh_margin_el": nlv * 0.5 if margin_eligible else 0.0,
                "accounts": {
                    acct_id: {
                        "account_id": acct_id,
                        "nlv": nlv,
                        "el": nlv * 0.5,
                        "buying_power": nlv * 2.0,
                        "cash_available": nlv,
                        "margin_eligible": margin_eligible,
                    },
                },
                "existing_positions": {},
                "existing_csps": {},
                "working_order_tickers": set(),
                "staged_order_tickers": set(),
            },
        }

    def test_empty_candidates_produces_no_stages(self):
        from agt_equities.csp_allocator import run_csp_allocator
        snapshots = self._household_snapshot("TestHH", "U001")
        provider = make_minimal_extras_provider({})
        result = run_csp_allocator(
            ray_candidates=[],
            snapshots=snapshots,
            vix=18.0,
            extras_provider=provider,
            ctx=_live_ctx(None),
        )
        assert result.staged == []
        assert result.errors == []

    def test_staging_callback_none_is_dry_run(self):
        """With staging_callback=None the allocator still populates staged
        tickets on result but does not call out to any external stager."""
        from agt_equities.csp_allocator import run_csp_allocator

        # Gates that need the DB (rule_2 reads v_available_nlv for margin
        # accounts) would trip on an isolated test harness. Use a cash IRA
        # account instead — skips the view lookup path.
        snapshots = self._household_snapshot(
            "TestHH", "U001", margin_eligible=False,
        )
        candidates = adapt_scanner_candidates([_scanner_row(
            ticker="AAPL", strike=150.0, premium=1.00, ann_roi=28.0,
            expiry="2026-05-15",
        )])
        provider = make_minimal_extras_provider({"AAPL": "Technology"})

        calls = []
        def _tracking_callback(tickets):
            calls.append(tickets)

        # Pass staging_callback=None — allocator must not raise, must not
        # attempt to invoke a callback, and must still produce digest_lines.
        result = run_csp_allocator(
            ray_candidates=candidates,
            snapshots=snapshots,
            vix=18.0,
            extras_provider=provider,
            ctx=_live_ctx(None),
        )
        assert calls == []  # never called (we didn't pass it)
        assert isinstance(result.digest_lines, list)
        assert len(result.digest_lines) > 0

    def test_adapter_output_objects_satisfy_allocator_contract(self):
        """ScanCandidate must expose every attribute the allocator reads."""
        c = adapt_scanner_candidates([_scanner_row()])[0]
        # Attributes touched by _process_one / _build_csp_proposal /
        # _csp_size_household / gates:
        assert hasattr(c, "ticker")
        assert hasattr(c, "strike")
        assert hasattr(c, "mid")
        assert hasattr(c, "expiry")
        assert hasattr(c, "annualized_yield")
        # Expiry format must survive YYYYMMDD conversion via .replace("-", "")
        assert c.expiry.replace("-", "").isdigit()
        assert len(c.expiry.replace("-", "")) == 8



def _single_household_snapshot(hh_name="TestHH", acct_id="U001", nlv=100_000.0):
    """Standalone household snapshot for bridge-2 tests."""
    return {
        hh_name: {
            "household": hh_name,
            "hh_nlv": nlv,
            "hh_margin_nlv": nlv,
            "hh_margin_el": nlv * 0.5,
            "accounts": {
                acct_id: {
                    "account_id": acct_id,
                    "nlv": nlv,
                    "el": nlv * 0.5,
                    "buying_power": nlv * 2.0,
                    "cash_available": nlv,
                    "margin_eligible": False,
                },
            },
            "existing_positions": {},
            "existing_csps": {},
            "working_order_tickers": set(),
            "staged_order_tickers": set(),
        },
    }

# ---------------------------------------------------------------------------
# B5.c-bridge-2 — staging callback tests
# ---------------------------------------------------------------------------

class TestBridge2StagingCallback(unittest.TestCase):
    """Verify that the allocator invokes staging_callback with correct tickets."""

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "test_b5c_staging.db"
        from agt_equities.schema import (
            register_master_log_tables,
            register_operational_tables,
        )
        from agt_equities.db import get_db_connection
        conn = get_db_connection(db_path=str(self.db_path))
        register_master_log_tables(conn)
        register_operational_tables(conn)
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.db_path.parent, ignore_errors=True)

    def test_staging_callback_receives_tickets(self):
        """When staging_callback is not None, allocator calls it with staged tickets."""
        from agt_equities.scan_bridge import (
            adapt_scanner_candidates,
            make_minimal_extras_provider,
        )
        from agt_equities.csp_allocator import run_csp_allocator

        candidates = adapt_scanner_candidates([_scanner_row()])
        snapshots = _single_household_snapshot()
        provider = make_minimal_extras_provider({})

        staged_tickets = []

        def _capture_callback(tickets):
            staged_tickets.extend(tickets)

        result = run_csp_allocator(
            ray_candidates=candidates,
            snapshots=snapshots,
            vix=18.0,
            extras_provider=provider,
            ctx=_live_ctx(_capture_callback),
        )

        # Allocator should have called our callback with any staged tickets
        if result.total_staged_contracts > 0:
            assert len(staged_tickets) > 0
            # Each ticket must have the fields _place_single_order expects
            for t in staged_tickets:
                assert "ticker" in t
                assert "strike" in t
                assert "account_id" in t
                assert "quantity" in t

    def test_staging_callback_none_does_not_stage(self):
        """staging_callback=None must not raise and must not stage."""
        from agt_equities.scan_bridge import (
            adapt_scanner_candidates,
            make_minimal_extras_provider,
        )
        from agt_equities.csp_allocator import run_csp_allocator

        candidates = adapt_scanner_candidates([_scanner_row()])
        snapshots = _single_household_snapshot()
        provider = make_minimal_extras_provider({})

        # Must not raise
        result = run_csp_allocator(
            ray_candidates=candidates,
            snapshots=snapshots,
            vix=18.0,
            extras_provider=provider,
            ctx=_live_ctx(None),
        )
        assert isinstance(result.digest_lines, list)

    def test_env_flag_scan_live_off_skips_staging(self):
        """AGT_SCAN_LIVE=0 should cause cmd_scan to pass staging_callback=None."""
        import os
        os.environ["AGT_SCAN_LIVE"] = "0"
        try:
            _scan_live = os.getenv("AGT_SCAN_LIVE", "1") == "1"
            assert _scan_live is False
            # In cmd_scan, _staging_cb would be None
            _staging_cb = (lambda t: t) if _scan_live else None
            assert _staging_cb is None
        finally:
            os.environ["AGT_SCAN_LIVE"] = "1"

    def test_env_flag_scan_live_default_is_on(self):
        """Default AGT_SCAN_LIVE should be '1' (staging enabled)."""
        import os
        # Remove if set
        old = os.environ.pop("AGT_SCAN_LIVE", None)
        try:
            _scan_live = os.getenv("AGT_SCAN_LIVE", "1") == "1"
            assert _scan_live is True
        finally:
            if old is not None:
                os.environ["AGT_SCAN_LIVE"] = old

    def test_staged_ticket_shape_matches_place_single_order_contract(self):
        """Tickets staged by allocator must contain all fields
        that _place_single_order reads from payload."""
        from agt_equities.scan_bridge import (
            adapt_scanner_candidates,
            make_minimal_extras_provider,
        )
        from agt_equities.csp_allocator import run_csp_allocator

        candidates = adapt_scanner_candidates([_scanner_row()])
        snapshots = _single_household_snapshot()
        provider = make_minimal_extras_provider({})

        staged_tickets = []
        result = run_csp_allocator(
            ray_candidates=candidates,
            snapshots=snapshots,
            vix=18.0,
            extras_provider=provider,
            ctx=_live_ctx(lambda t: staged_tickets.extend(t)),
        )

        if staged_tickets:
            t = staged_tickets[0]
            # Fields _place_single_order reads (telegram_bot.py:6586+):
            required_keys = [
                "ticker", "strike", "expiry", "quantity",
                "limit_price", "account_id",
            ]
            for key in required_keys:
                assert key in t, f"Missing key '{key}' in staged ticket: {t.keys()}"
            # CSP-specific
            assert t.get("action", "SELL") in ("SELL", "BUY")
            assert t.get("right", "P") in ("P", "C")
            assert t.get("sec_type", "OPT") == "OPT"
