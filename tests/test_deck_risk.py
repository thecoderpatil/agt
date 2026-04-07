"""Test risk module VIX→EL table."""
import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agt_deck import risk
from agt_deck.risk import vix_required_el_pct


class TestVixElTableV8(unittest.TestCase):
    """Tests for v8 (legacy) Rule 2 table."""

    def setUp(self):
        risk._VIX_EL_TABLE = risk._VIX_EL_TABLE_V8

    def tearDown(self):
        risk._VIX_EL_TABLE = risk._VIX_EL_TABLE_V9  # restore v9 (active)

    def test_low_vix(self):
        self.assertEqual(vix_required_el_pct(12.0), 0.80)

    def test_boundary_19_99(self):
        self.assertEqual(vix_required_el_pct(19.99), 0.80)

    def test_boundary_20_0(self):
        self.assertEqual(vix_required_el_pct(20.0), 0.85)

    def test_high_vix(self):
        self.assertEqual(vix_required_el_pct(50.0), 1.00)

    def test_extreme_vix(self):
        self.assertEqual(vix_required_el_pct(999.0), 1.00)


class TestVixElTableV9(unittest.TestCase):
    """Tests for v9 (proposed) Rule 2 table with 60% deploy cap."""

    def setUp(self):
        # Temporarily switch to v9
        risk._VIX_EL_TABLE = risk._VIX_EL_TABLE_V9

    def tearDown(self):
        # Restore v8
        risk._VIX_EL_TABLE = risk._VIX_EL_TABLE_V8

    def test_v9_low_vix(self):
        self.assertEqual(vix_required_el_pct(15.0), 0.80)

    def test_v9_boundary_20(self):
        self.assertEqual(vix_required_el_pct(20.0), 0.70)

    def test_v9_boundary_25(self):
        self.assertEqual(vix_required_el_pct(25.0), 0.60)

    def test_v9_boundary_30(self):
        self.assertEqual(vix_required_el_pct(30.0), 0.50)

    def test_v9_boundary_40(self):
        """40% retain = 60% max deploy — the survival bunker floor."""
        self.assertEqual(vix_required_el_pct(40.0), 0.40)

    def test_v9_extreme_vix(self):
        """VIX 80+ still capped at 40% retain / 60% deploy."""
        self.assertEqual(vix_required_el_pct(80.0), 0.40)

    def test_v9_never_exceeds_60_deploy(self):
        """No VIX level allows more than 60% deployment."""
        for vix in [0, 10, 20, 30, 40, 50, 60, 80, 100, 200]:
            retain = vix_required_el_pct(float(vix))
            deploy = 1.0 - retain
            self.assertLessEqual(deploy, 0.60,
                f"VIX {vix}: deploy {deploy:.0%} exceeds 60% cap")


class TestRule11Leverage(unittest.TestCase):
    """Tests for Rule 11 beta-weighted leverage circuit breaker."""

    def setUp(self):
        risk._leverage_breached.clear()

    def test_under_threshold(self):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.status = 'ACTIVE'
        c.shares_held = 100
        c.household_id = 'Yash_Household'
        c.ticker = 'TEST'
        result = risk.gross_beta_leverage(
            cycles=[c], spots={'TEST': 100}, betas={'TEST': 1.0},
            household_nlv={'Yash_Household': 100000},
        )
        lev, status = result['Yash']
        self.assertAlmostEqual(lev, 0.10, delta=0.01)
        self.assertEqual(status, 'OK')

    def test_at_threshold(self):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.status = 'ACTIVE'
        c.shares_held = 1500
        c.household_id = 'Yash_Household'
        c.ticker = 'TEST'
        result = risk.gross_beta_leverage(
            cycles=[c], spots={'TEST': 100}, betas={'TEST': 1.0},
            household_nlv={'Yash_Household': 100000},
        )
        lev, status = result['Yash']
        self.assertAlmostEqual(lev, 1.50, delta=0.01)
        self.assertEqual(status, 'BREACHED')

    def test_hysteresis_stays_breached(self):
        """Once breached, stays breached until below release threshold (1.40x)."""
        from unittest.mock import MagicMock

        # First: breach at 1.60x
        c = MagicMock()
        c.status = 'ACTIVE'
        c.shares_held = 1600
        c.household_id = 'Yash_Household'
        c.ticker = 'TEST'
        risk.gross_beta_leverage(
            cycles=[c], spots={'TEST': 100}, betas={'TEST': 1.0},
            household_nlv={'Yash_Household': 100000},
        )

        # Now drop to 1.42x — still breached (hysteresis)
        c.shares_held = 1420
        result = risk.gross_beta_leverage(
            cycles=[c], spots={'TEST': 100}, betas={'TEST': 1.0},
            household_nlv={'Yash_Household': 100000},
        )
        _, status = result['Yash']
        self.assertEqual(status, 'BREACHED')

        # Drop to 1.39x — released
        c.shares_held = 1390
        result = risk.gross_beta_leverage(
            cycles=[c], spots={'TEST': 100}, betas={'TEST': 1.0},
            household_nlv={'Yash_Household': 100000},
        )
        _, status = result['Yash']
        self.assertEqual(status, 'AMBER')  # 1.39 > 1.30 = AMBER


class TestDynamicExitThreshold(unittest.TestCase):
    """Tests for W3.8 dynamic exit threshold formula."""

    def test_regime_1_near_basis(self):
        """Low redeploy yield, short wait → small threshold (freeze + CC)."""
        d = risk.dynamic_exit_threshold(
            redeploy_yield=0.10, wait_months=3, cc_yield=0.05, recovery_prob=0.9
        )
        self.assertLess(d, 0.05, f"Regime 1: d*={d:.4f} should be < 5%")

    def test_regime_2_moderate(self):
        """Moderate redeploy yield, medium wait → mid-range threshold."""
        d = risk.dynamic_exit_threshold(
            redeploy_yield=0.20, wait_months=6, cc_yield=0.05, recovery_prob=0.7
        )
        self.assertGreater(d, 0.05, f"Regime 2: d*={d:.4f} should be > 5%")
        self.assertLess(d, 0.30, f"Regime 2: d*={d:.4f} should be < 30%")

    def test_regime_3_deep_drawdown(self):
        """High redeploy yield, long wait, low recovery → large threshold (exit)."""
        d = risk.dynamic_exit_threshold(
            redeploy_yield=0.30, wait_months=12, cc_yield=0.03, recovery_prob=0.3
        )
        self.assertGreater(d, 0.25, f"Regime 3: d*={d:.4f} should be > 25%")


if __name__ == '__main__':
    unittest.main()
