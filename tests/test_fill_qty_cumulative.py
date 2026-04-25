"""Regression tests for fill_qty / fill_price cumulative accounting.

Verifies that _r5_on_exec_details stores execution.cumQty (not execution.shares)
and execution.avgPrice (not execution.price) in pending_orders.  Pre-fix the
overwrite pattern with incremental shares left fill_qty = last-callback quantity,
not the total filled.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import telegram_bot

pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class _ConnProxy:
    def __init__(self, inner):
        self._c = inner

    def execute(self, *a, **kw):
        return self._c.execute(*a, **kw)

    def executemany(self, *a, **kw):
        return self._c.executemany(*a, **kw)

    def commit(self):
        return self._c.commit()

    def rollback(self):
        return self._c.rollback()

    def close(self):
        pass  # fixture owns connection lifecycle


def _build_db():
    from agt_equities.schema import register_operational_tables, _extend_pending_orders
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    register_operational_tables(c)
    _extend_pending_orders(c)
    c.commit()
    return c


def _seed_order(c, perm_id):
    cur = c.execute(
        "INSERT INTO pending_orders (payload, status, ib_perm_id, created_at) "
        "VALUES ('{}', 'sent', ?, datetime('now'))",
        (perm_id,),
    )
    c.commit()
    return int(cur.lastrowid)


def _make_trade(perm_id, remaining=1):
    return SimpleNamespace(
        order=SimpleNamespace(permId=perm_id, orderId=0, orderRef=""),
        orderStatus=SimpleNamespace(remaining=remaining),
    )


def _make_fill(exec_id, shares, cum_qty, price, avg_price,
               time_str="20260425 14:30:00 ET"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            shares=float(shares),
            cumQty=float(cum_qty),
            price=float(price),
            avgPrice=float(avg_price),
            time=time_str,
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFillQtyCumulative:

    def test_three_callbacks_persist_cumulative_fill_qty(self, monkeypatch):
        """Three partial-fill callbacks must leave fill_qty=cumQty of the final
        callback (400), not the incremental shares of the last execution (100)."""
        c = _build_db()
        _seed_order(c, perm_id=42)
        monkeypatch.setattr(telegram_bot, "_get_db_connection", lambda: _ConnProxy(c))

        trade = _make_trade(perm_id=42, remaining=1)

        # 1st execution: 143 shares @ 270.10, cumQty=143
        telegram_bot._r5_on_exec_details(
            trade, _make_fill("e001", shares=143, cum_qty=143, price=270.10,
                              avg_price=270.10, time_str="20260425 09:30:00 ET")
        )
        # 2nd execution: 157 shares @ 270.05, cumQty=300
        telegram_bot._r5_on_exec_details(
            trade, _make_fill("e002", shares=157, cum_qty=300, price=270.05,
                              avg_price=270.05, time_str="20260425 10:00:00 ET")
        )
        # 3rd execution: 100 shares @ 270.00, cumQty=400
        telegram_bot._r5_on_exec_details(
            trade, _make_fill("e003", shares=100, cum_qty=400, price=270.00,
                              avg_price=270.00, time_str="20260425 11:00:00 ET")
        )

        row = c.execute(
            "SELECT fill_qty, fill_price FROM pending_orders WHERE ib_perm_id = 42"
        ).fetchone()
        assert row is not None
        assert row["fill_qty"] == 400, (
            f"fill_qty should be cumQty=400, got {row['fill_qty']}"
        )
        assert row["fill_price"] == pytest.approx(270.00), (
            f"fill_price should be avgPrice=270.00, got {row['fill_price']}"
        )
        c.close()

    def test_single_callback_unchanged_behavior(self, monkeypatch):
        """A single fill callback (cumQty == shares) must still persist correctly."""
        c = _build_db()
        _seed_order(c, perm_id=43)
        monkeypatch.setattr(telegram_bot, "_get_db_connection", lambda: _ConnProxy(c))

        trade = _make_trade(perm_id=43, remaining=0)
        telegram_bot._r5_on_exec_details(
            trade, _make_fill("e001", shares=400, cum_qty=400, price=270.05,
                              avg_price=270.05)
        )

        row = c.execute(
            "SELECT fill_qty, fill_price FROM pending_orders WHERE ib_perm_id = 43"
        ).fetchone()
        assert row is not None
        assert row["fill_qty"] == 400
        assert row["fill_price"] == pytest.approx(270.05)
        c.close()
