"""Decoupling Sprint B Unit B2 -- FA-block child permId race retry.

Scope:
* Retry fires up to 3 times with 0.5s delay on lookup miss.
* First-try success: no sleeps, returns value immediately.
* Transient miss followed by hit: returns value, sleep called (attempt-1) times.
* Persistent miss: 3 attempts, 2 sleeps, warning logged, fill still books.

Tests monkeypatch the lookup + time.sleep to avoid real delays and avoid
real DB. Do not exercise _apply_fill_atomically (money-path tests cover
that separately in test_inception_delta_fill.py).
"""

from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.sprint_a


@pytest.fixture()
def bot_module():
    # Avoid side-effect imports by setting the test DB env + skipping init.
    import telegram_bot
    yield telegram_bot


def _make_trade_fill(perm_id: int = 12345, order_id: int = 111, account: str = "U21971297"):
    contract = SimpleNamespace(symbol="AAPL", secType="OPT", right="C")
    order = SimpleNamespace(action="SELL", account=account, permId=perm_id, orderId=order_id)
    execution = SimpleNamespace(
        execId="exec_b2_test", price=2.50, shares=-1,
        acctNumber=account,
    )
    trade = SimpleNamespace(contract=contract, order=order)
    fill = SimpleNamespace(execution=execution)
    return trade, fill


def test_first_try_success_no_sleep(bot_module, monkeypatch):
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    sleeps = []
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: sleeps.append(s))

    lookup_calls = []
    def _fake_lookup(perm_id, client_id):
        lookup_calls.append((perm_id, client_id))
        return 0.25  # inception_delta hit first try

    monkeypatch.setattr(bot_module, "_lookup_inception_delta_from_payload", _fake_lookup)
    monkeypatch.setattr(bot_module, "_apply_fill_atomically", lambda *a, **kw: True)

    trade, fill = _make_trade_fill()
    bot_module._on_cc_fill(trade, fill)

    assert len(lookup_calls) == 1, "expected exactly 1 lookup call on first-try hit"
    assert sleeps == [], f"expected no sleeps on first-try hit, got {sleeps}"


def test_transient_miss_then_hit(bot_module, monkeypatch):
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    sleeps = []
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: sleeps.append(s))

    values = [None, None, 0.32]  # miss, miss, hit on 3rd
    def _fake_lookup(perm_id, client_id):
        return values.pop(0)
    monkeypatch.setattr(bot_module, "_lookup_inception_delta_from_payload", _fake_lookup)

    applied_kwargs = {}
    def _fake_apply(*args, **kw):
        applied_kwargs.update(kw)
        return True
    monkeypatch.setattr(bot_module, "_apply_fill_atomically", _fake_apply)

    trade, fill = _make_trade_fill()
    bot_module._on_cc_fill(trade, fill)

    assert sleeps == [0.5, 0.5], f"expected 2 sleeps of 0.5s, got {sleeps}"
    assert applied_kwargs.get("inception_delta") == 0.32


def test_persistent_miss_exhausts_retries(bot_module, monkeypatch, caplog):
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    sleeps = []
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: sleeps.append(s))

    attempts = [0]
    def _fake_lookup(perm_id, client_id):
        attempts[0] += 1
        return None
    monkeypatch.setattr(bot_module, "_lookup_inception_delta_from_payload", _fake_lookup)

    applied_kwargs = {}
    def _fake_apply(*args, **kw):
        applied_kwargs.update(kw)
        return True
    monkeypatch.setattr(bot_module, "_apply_fill_atomically", _fake_apply)

    # agt_bridge logger is propagate=False + has own handler; flip propagate
    # so caplog (root-attached) captures records.
    logging.getLogger("agt_bridge").propagate = True
    caplog.set_level(logging.WARNING)

    trade, fill = _make_trade_fill()
    bot_module._on_cc_fill(trade, fill)

    assert attempts[0] == 3, f"expected 3 retry attempts, got {attempts[0]}"
    assert sleeps == [0.5, 0.5], f"expected 2 sleeps (between 3 attempts), got {sleeps}"
    # Fill still books with inception_delta=None.
    assert "inception_delta" in applied_kwargs
    assert applied_kwargs["inception_delta"] is None
    # Warning logged.
    assert any("inception_delta lookup miss after 3 retries" in rec.getMessage()
               for rec in caplog.records), "expected retry-exhaustion warning"


def test_retry_guard_does_not_break_non_sell_call(bot_module, monkeypatch):
    """Bail-out guards (non-SELL, non-OPT, non-C) must fire before retry loop."""
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    sleeps = []
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: sleeps.append(s))
    lookup_calls = []
    monkeypatch.setattr(
        bot_module, "_lookup_inception_delta_from_payload",
        lambda p, c: lookup_calls.append((p, c)) or None,
    )
    monkeypatch.setattr(bot_module, "_apply_fill_atomically", lambda *a, **kw: True)

    # Non-SELL -> early return, no lookup, no sleep.
    trade, fill = _make_trade_fill()
    trade.order.action = "BUY"
    bot_module._on_cc_fill(trade, fill)
    assert lookup_calls == []
    assert sleeps == []
