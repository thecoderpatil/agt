"""
CLEANUP-5 regression tests — timezone-aware override expiry helpers.

Verifies:
  T1: _parse_override_expiry handles legacy naive datetimes (assumes ET)
  T2: _parse_override_expiry handles new UTC-aware datetimes
  T3: _new_override_expiry generates UTC-aware ISO strings
  T4: DST boundary — naive ET timestamp near spring-forward resolves correctly
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from telegram_bot import _parse_override_expiry, _new_override_expiry


class TestParseOverrideExpiry(unittest.TestCase):

    def test_legacy_naive_assumed_et(self):
        """Naive ISO string like '2026-04-14T19:14:40.806942' should be
        interpreted as US/Eastern and converted to UTC."""
        raw = "2026-04-14T19:14:40.806942"
        result = _parse_override_expiry(raw)
        self.assertIsNotNone(result.tzinfo, "Must return timezone-aware datetime")
        self.assertEqual(result.tzinfo, timezone.utc)
        # 2026-04-14 is EDT (UTC-4), so 19:14 ET = 23:14 UTC
        self.assertEqual(result.hour, 23)
        self.assertEqual(result.minute, 14)

    def test_utc_aware_passthrough(self):
        """Already-UTC ISO string should round-trip without shift."""
        raw = "2026-04-14T23:14:40.806942+00:00"
        result = _parse_override_expiry(raw)
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 23)
        self.assertEqual(result.minute, 14)

    def test_non_utc_aware_converted(self):
        """ISO string with non-UTC timezone should convert to UTC."""
        raw = "2026-04-14T19:14:40-04:00"  # EDT
        result = _parse_override_expiry(raw)
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 23)


class TestNewOverrideExpiry(unittest.TestCase):

    def test_generates_utc_aware(self):
        """_new_override_expiry must produce a UTC-aware ISO string."""
        result = _new_override_expiry(days=7)
        parsed = datetime.fromisoformat(result)
        self.assertIsNotNone(parsed.tzinfo, "Must be timezone-aware")
        self.assertEqual(parsed.utcoffset(), timedelta(0), "Must be UTC")

    def test_days_offset(self):
        """7-day offset should be approximately 7 days from now."""
        result = _new_override_expiry(days=7)
        parsed = datetime.fromisoformat(result)
        diff = parsed - datetime.now(timezone.utc)
        self.assertGreater(diff.total_seconds(), 6 * 86400)
        self.assertLess(diff.total_seconds(), 8 * 86400)

    def test_hours_offset(self):
        """168-hour offset should be approximately 7 days from now."""
        result = _new_override_expiry(hours=168)
        parsed = datetime.fromisoformat(result)
        diff = parsed - datetime.now(timezone.utc)
        self.assertGreater(diff.total_seconds(), 167 * 3600)
        self.assertLess(diff.total_seconds(), 169 * 3600)


class TestDSTBoundary(unittest.TestCase):

    def test_spring_forward_naive_resolves_correctly(self):
        """Naive timestamp at 2:30 AM ET on 2026-03-08 (DST spring-forward day)
        should resolve to the correct UTC time. EST (UTC-5) applies because
        2:30 AM is before the 2:00 AM transition to EDT."""
        raw = "2026-03-08T01:30:00"  # 1:30 AM ET (before spring-forward at 2:00 AM)
        result = _parse_override_expiry(raw)
        # 1:30 AM EST = 6:30 AM UTC
        self.assertEqual(result.hour, 6)
        self.assertEqual(result.minute, 30)


if __name__ == "__main__":
    unittest.main()
