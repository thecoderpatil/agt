"""
Followup #17 — Empirical verification: _normalize_ibkr_time handles
naive datetimes correctly by applying TWS timezone before UTC conversion.
"""
import sys
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from telegram_bot import _normalize_ibkr_time, _TWS_TZ


def main():
    print(f"_TWS_TZ = {_TWS_TZ}")

    # Test 1: Naive datetime (simulates ib_async Issue #287)
    naive = datetime(2026, 4, 8, 14, 30, 0)
    result = _normalize_ibkr_time(naive)
    assert result.tzinfo is not None, "Must be tz-aware"
    assert result.tzinfo == timezone.utc, f"Must be UTC, got {result.tzinfo}"
    # 14:30 ET (EDT, UTC-4) = 18:30 UTC
    assert result.hour == 18, f"Expected hour=18, got {result.hour}"
    assert result.minute == 30
    print(f"[OK] Naive {naive} -> {result} (ET interpreted, UTC output)")

    # Test 2: Already-aware datetime
    aware = datetime(2026, 4, 8, 14, 30, 0, tzinfo=timezone.utc)
    result2 = _normalize_ibkr_time(aware)
    assert result2 == aware, "Aware datetime must pass through unchanged"
    print(f"[OK] Aware {aware} -> {result2} (passthrough)")

    # Test 3: None passthrough
    assert _normalize_ibkr_time(None) is None
    print("[OK] None -> None (passthrough)")

    # Test 4: EST (winter) — UTC-5
    naive_est = datetime(2026, 1, 15, 14, 30, 0)
    result_est = _normalize_ibkr_time(naive_est)
    # 14:30 EST (UTC-5) = 19:30 UTC
    assert result_est.hour == 19, f"EST: expected hour=19, got {result_est.hour}"
    print(f"[OK] EST naive {naive_est} -> {result_est} (UTC-5 applied)")

    # Test 5: EDT (summer) — UTC-4
    naive_edt = datetime(2026, 7, 15, 14, 30, 0)
    result_edt = _normalize_ibkr_time(naive_edt)
    # 14:30 EDT (UTC-4) = 18:30 UTC
    assert result_edt.hour == 18, f"EDT: expected hour=18, got {result_edt.hour}"
    print(f"[OK] EDT naive {naive_edt} -> {result_edt} (UTC-4 applied)")

    # Test 6: Different timezone aware
    cet = ZoneInfo("Europe/Berlin")
    aware_cet = datetime(2026, 4, 8, 14, 30, 0, tzinfo=cet)
    result_cet = _normalize_ibkr_time(aware_cet)
    assert result_cet.tzinfo == timezone.utc
    # 14:30 CEST (UTC+2) = 12:30 UTC
    assert result_cet.hour == 12, f"CET: expected hour=12, got {result_cet.hour}"
    print(f"[OK] CET {aware_cet} -> {result_cet} (converted to UTC)")

    print("\n=== ALL ASSERTIONS PASSED ===")


if __name__ == "__main__":
    main()
