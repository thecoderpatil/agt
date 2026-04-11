import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestV2StateRouter(unittest.TestCase):

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
    @patch("telegram_bot.append_pending_tickets")
    @patch("telegram_bot._load_premium_ledger_snapshot", return_value={
        "initial_basis": 120.0,
        "adjusted_basis": 110.0,
    })
    @patch("telegram_bot._ibkr_get_spot", new_callable=AsyncMock, return_value=80.0)
    def test_router_does_not_assign_otm_microstructure_false_positive(
        self,
        mock_spot,
        mock_ledger,
        mock_append,
        mock_mode,
    ):
        import telegram_bot

        today = telegram_bot._date.today()
        expiry = (today + telegram_bot._timedelta(days=30)).strftime("%Y%m%d")
        current_contract = SimpleNamespace(
            symbol="PYPL",
            strike=100.0,
            secType="OPT",
            right="C",
            lastTradeDateOrContractMonth=expiry,
            conId=111,
        )
        pos = SimpleNamespace(
            account="U1",
            position=-1,
            avgCost=-20.0,
            contract=current_contract,
        )

        ib_conn = MagicMock()
        ib_conn.reqPositionsAsync = AsyncMock(return_value=[pos])
        ib_conn.reqMarketDataType = MagicMock()
        ib_conn.reqMktData = MagicMock(return_value=SimpleNamespace(
            ask=0.01,
            bid=0.00,
            modelGreeks=SimpleNamespace(delta=0.05),
            bidGreeks=None,
        ))
        ib_conn.cancelMktData = MagicMock()

        async def _qualify(contract):
            return [SimpleNamespace(conId=getattr(contract, "conId", 0) or 222)]

        ib_conn.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

        alerts = _run(telegram_bot._scan_and_stage_defensive_rolls(ib_conn))

        self.assertNotIn(
            "[ASSIGN] PYPL Extrinsic exhausted. Parity breached. Defense standing down.",
            alerts,
        )
        self.assertEqual(
            alerts,
            [
                "━━ V2 Router [mode=PEACETIME] ━━",
                "[HARVEST] PYPL Capital dead. Staging BTC.",
            ],
        )
        mock_append.assert_called_once()

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
    @patch("telegram_bot.append_pending_tickets")
    @patch("telegram_bot._load_premium_ledger_snapshot", return_value={
        "initial_basis": 120.0,
        "adjusted_basis": 110.0,
    })
    @patch("telegram_bot._ibkr_get_spot", new_callable=AsyncMock, return_value=95.0)
    def test_router_stages_state2_harvest_btc(
        self,
        mock_spot,
        mock_ledger,
        mock_append,
        mock_mode,
    ):
        import telegram_bot

        today = telegram_bot._date.today()
        expiry = (today + telegram_bot._timedelta(days=30)).strftime("%Y%m%d")
        current_contract = SimpleNamespace(
            symbol="AAPL",
            strike=100.0,
            secType="OPT",
            right="C",
            lastTradeDateOrContractMonth=expiry,
            conId=111,
        )
        pos = SimpleNamespace(
            account="U1",
            position=-1,
            avgCost=-150.0,
            contract=current_contract,
        )

        ib_conn = MagicMock()
        ib_conn.reqPositionsAsync = AsyncMock(return_value=[pos])
        ib_conn.reqMarketDataType = MagicMock()
        ib_conn.reqMktData = MagicMock(return_value=SimpleNamespace(
            ask=0.20,
            bid=0.15,
            modelGreeks=SimpleNamespace(delta=0.20),
            bidGreeks=None,
        ))
        ib_conn.cancelMktData = MagicMock()

        async def _qualify(contract):
            return [SimpleNamespace(conId=getattr(contract, "conId", 0) or 222)]

        ib_conn.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

        alerts = _run(telegram_bot._scan_and_stage_defensive_rolls(ib_conn))

        self.assertEqual(
            alerts,
            [
                "━━ V2 Router [mode=PEACETIME] ━━",
                "[HARVEST] AAPL Capital dead. Staging BTC.",
            ],
        )
        mock_append.assert_called_once()
        ticket = mock_append.call_args.args[0][0]
        self.assertEqual(ticket["sec_type"], "OPT")
        self.assertEqual(ticket["action"], "BUY")
        self.assertEqual(ticket["ticker"], "AAPL")
        self.assertEqual(ticket["limit_price"], 0.20)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
    @patch("telegram_bot.append_pending_tickets")
    @patch("telegram_bot._load_premium_ledger_snapshot", return_value={
        "initial_basis": 600.0,
        "adjusted_basis": 550.0,
    })
    @patch("telegram_bot._ibkr_get_spot", new_callable=AsyncMock, return_value=450.0)
    def test_router_harvests_massive_winner_even_with_penny_ask(
        self,
        mock_spot,
        mock_ledger,
        mock_append,
        mock_mode,
    ):
        import telegram_bot

        today = telegram_bot._date.today()
        expiry = (today + telegram_bot._timedelta(days=14)).strftime("%Y%m%d")
        current_contract = SimpleNamespace(
            symbol="ADBE",
            strike=500.0,
            secType="OPT",
            right="C",
            lastTradeDateOrContractMonth=expiry,
            conId=111,
        )
        pos = SimpleNamespace(
            account="U1",
            position=-1,
            avgCost=-1.00,
            contract=current_contract,
        )

        ib_conn = MagicMock()
        ib_conn.reqPositionsAsync = AsyncMock(return_value=[pos])
        ib_conn.reqMarketDataType = MagicMock()
        ib_conn.reqMktData = MagicMock(return_value=SimpleNamespace(
            ask=0.01,
            bid=0.00,
            modelGreeks=SimpleNamespace(delta=0.03),
            bidGreeks=None,
        ))
        ib_conn.cancelMktData = MagicMock()

        async def _qualify(contract):
            return [SimpleNamespace(conId=getattr(contract, "conId", 0) or 222)]

        ib_conn.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

        alerts = _run(telegram_bot._scan_and_stage_defensive_rolls(ib_conn))

        self.assertEqual(
            alerts,
            [
                "━━ V2 Router [mode=PEACETIME] ━━",
                "[HARVEST] ADBE Capital dead. Staging BTC.",
            ],
        )
        mock_append.assert_called_once()
        ticket = mock_append.call_args.args[0][0]
        self.assertEqual(ticket["action"], "BUY")
        self.assertEqual(ticket["limit_price"], 0.01)

    @patch("telegram_bot._get_current_desk_mode", return_value="PEACETIME")
    @patch("telegram_bot.ACCOUNT_TO_HOUSEHOLD", {"U1": "Yash_Household"})
    @patch("telegram_bot.append_pending_tickets")
    @patch("telegram_bot._ibkr_get_chain", new_callable=AsyncMock)
    @patch("telegram_bot._ibkr_get_expirations", new_callable=AsyncMock)
    @patch("telegram_bot._load_premium_ledger_snapshot", return_value={
        "initial_basis": 120.0,
        "adjusted_basis": 110.0,
    })
    @patch("telegram_bot._ibkr_get_spot", new_callable=AsyncMock, return_value=90.0)
    def test_router_stages_state3_defend_roll(
        self,
        mock_spot,
        mock_ledger,
        mock_expirations,
        mock_chain,
        mock_append,
        mock_mode,
    ):
        import telegram_bot

        today = telegram_bot._date.today()
        current_expiry = (today + telegram_bot._timedelta(days=35)).strftime("%Y%m%d")
        future_expiry = (today + telegram_bot._timedelta(days=70)).isoformat()
        mock_expirations.return_value = [future_expiry]
        mock_chain.return_value = [
            {"strike": 110.0, "bid": 1.50, "ask": 1.70},
            {"strike": 112.0, "bid": 1.00, "ask": 1.20},
        ]

        current_contract = SimpleNamespace(
            symbol="AAPL",
            strike=100.0,
            secType="OPT",
            right="C",
            lastTradeDateOrContractMonth=current_expiry,
            conId=111,
        )
        pos = SimpleNamespace(
            account="U1",
            position=-1,
            avgCost=-500.0,
            contract=current_contract,
        )

        ib_conn = MagicMock()
        ib_conn.reqPositionsAsync = AsyncMock(return_value=[pos])
        ib_conn.reqMarketDataType = MagicMock()
        ib_conn.reqMktData = MagicMock(return_value=SimpleNamespace(
            ask=4.00,
            bid=3.80,
            modelGreeks=SimpleNamespace(delta=0.45),
            bidGreeks=None,
        ))
        ib_conn.cancelMktData = MagicMock()

        async def _qualify(contract):
            con_id = getattr(contract, "conId", 0) or 222
            return [SimpleNamespace(conId=con_id)]

        ib_conn.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

        alerts = _run(telegram_bot._scan_and_stage_defensive_rolls(ib_conn))

        self.assertEqual(
            alerts,
            [
                "━━ V2 Router [mode=PEACETIME] ━━",
                "[DEFEND] AAPL EV-Accretive Roll staged.",
            ],
        )
        mock_append.assert_called_once()
        ticket = mock_append.call_args.args[0][0]
        self.assertEqual(ticket["sec_type"], "BAG")
        self.assertEqual(ticket["strike"], 112.0)
        self.assertEqual(ticket["limit_price"], 3.0)
        self.assertEqual(ticket["combo_legs"][0]["action"], "BUY")
        self.assertEqual(ticket["combo_legs"][1]["action"], "SELL")


class TestV2ChainWalkers(unittest.TestCase):

    @patch("telegram_bot._ibkr_get_chain", new_callable=AsyncMock)
    @patch("telegram_bot._ibkr_get_expirations", new_callable=AsyncMock)
    def test_mode1_chain_walks_down_from_acb_buffer(
        self,
        mock_expirations,
        mock_chain,
    ):
        import telegram_bot

        expiry = (telegram_bot._date.today() + telegram_bot._timedelta(days=21)).isoformat()
        mock_expirations.return_value = [expiry]
        mock_chain.return_value = [
            {"strike": 95.0, "bid": 0.70, "ask": 0.80},
            {"strike": 100.0, "bid": 0.30, "ask": 0.40},
            {"strike": 105.0, "bid": 0.45, "ask": 0.55},
            {"strike": 108.0, "bid": 0.25, "ask": 0.35},
            {"strike": 110.0, "bid": 0.80, "ask": 0.90},
        ]

        result = _run(telegram_bot._walk_mode1_chain("AAPL", 80.0, 100.0, (14, 30)))

        self.assertIsNotNone(result)
        self.assertEqual(result["strike"], 105.0)

    @patch("telegram_bot._ibkr_get_chain", new_callable=AsyncMock)
    @patch("telegram_bot._ibkr_get_expirations", new_callable=AsyncMock)
    def test_harvest_chain_selects_highest_strike_in_band(
        self,
        mock_expirations,
        mock_chain,
    ):
        import telegram_bot

        expiry = (telegram_bot._date.today() + telegram_bot._timedelta(days=21)).isoformat()
        mock_expirations.return_value = [expiry]
        mock_chain.return_value = [
            {"strike": 100.0, "bid": 2.00, "ask": 2.20},
            {"strike": 105.0, "bid": 1.80, "ask": 2.00},
            {"strike": 110.0, "bid": 1.50, "ask": 1.70},
        ]

        result = _run(telegram_bot._walk_harvest_chain("AAPL", 102.0, 100.0, (14, 30)))

        self.assertIsNotNone(result)
        self.assertEqual(result["strike"], 105.0)


if __name__ == "__main__":
    unittest.main()
