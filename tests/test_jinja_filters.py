"""Sprint 1B: Tests for Jinja template filters."""
import unittest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agt_deck.formatters import format_age, el_pct_color, lifecycle_state_classes


class TestFormatAge(unittest.TestCase):

    def test_seconds(self):
        self.assertEqual(format_age(45), "45s ago")

    def test_minutes(self):
        self.assertEqual(format_age(180), "3m ago")

    def test_hours(self):
        self.assertEqual(format_age(7200), "2h 0m ago")

    def test_days(self):
        self.assertEqual(format_age(90000), "1d ago")


class TestElPctColor(unittest.TestCase):

    def test_high_el(self):
        self.assertIn("emerald", el_pct_color(45.0))

    def test_medium_el(self):
        self.assertIn("amber", el_pct_color(30.0))

    def test_low_el(self):
        self.assertIn("orange", el_pct_color(18.0))

    def test_critical_el(self):
        self.assertIn("rose", el_pct_color(10.0))
        self.assertIn("animate-pulse", el_pct_color(10.0))

    def test_none_el(self):
        self.assertIn("slate", el_pct_color(None))


class TestLifecycleStateClasses(unittest.TestCase):

    def test_staged(self):
        self.assertIn("amber", lifecycle_state_classes("STAGED"))

    def test_attested(self):
        self.assertIn("blue", lifecycle_state_classes("ATTESTED"))

    def test_transmitting(self):
        self.assertIn("purple", lifecycle_state_classes("TRANSMITTING"))
        self.assertIn("animate-pulse", lifecycle_state_classes("TRANSMITTING"))

    def test_transmitted(self):
        self.assertIn("emerald", lifecycle_state_classes("TRANSMITTED"))

    def test_orphan(self):
        result = lifecycle_state_classes("TRANSMITTING", is_orphan=True)
        self.assertIn("rose", result)
        self.assertIn("font-bold", result)


if __name__ == "__main__":
    unittest.main()
