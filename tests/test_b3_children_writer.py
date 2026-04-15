"""Decoupling Sprint B Unit B3 -- pending_order_children writer + openOrder handler.

Scope:
* children_writer_enabled() reads AGT_B3_CHILDREN_WRITER env on every call.
* insert_pending_order_child inserts a row with the right columns.
* insert_pending_order_child is idempotent on (parent_order_id, account_id).
* insert_pending_order_child COALESCE-upserts NULL ib ids on re-call.
* update_child_ib_ids populates NULL ids, preserves non-NULL ids.
* update_child_ib_ids returns False when the child row doesn't exist.
* _on_open_order_write_child resolves parent via ib_order_id and updates
  the matching child row.
* _on_open_order_write_child short-circuits when kill switch is off.
* _on_open_order_write_child no-ops on orphan events.

Tests use an in-memory SQLite via agt_equities.schema.register_operational_tables.
No production DB access. No ib_async. Fast.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.sprint_a


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from agt_equities.schema import register_operational_tables
    register_operational_tables(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def parent_order_id(conn):
    cur = conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) "
        "VALUES ('{}', 'sent', datetime('now'))"
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture(autouse=True)
def _b3_writer_on(monkeypatch):
    monkeypatch.setenv("AGT_B3_CHILDREN_WRITER", "1")


# Feature flag -------------------------------------------------------------

def test_children_writer_enabled_default_on(monkeypatch):
    from agt_equities.order_state import children_writer_enabled
    monkeypatch.delenv("AGT_B3_CHILDREN_WRITER", raising=False)
    assert children_writer_enabled() is True


def test_children_writer_enabled_kill_switch(monkeypatch):
    from agt_equities.order_state import children_writer_enabled
    monkeypatch.setenv("AGT_B3_CHILDREN_WRITER", "0")
    assert children_writer_enabled() is False


def test_children_writer_enabled_reads_env_each_call(monkeypatch):
    from agt_equities.order_state import children_writer_enabled
    monkeypatch.setenv("AGT_B3_CHILDREN_WRITER", "1")
    assert children_writer_enabled() is True
    monkeypatch.setenv("AGT_B3_CHILDREN_WRITER", "0")
    assert children_writer_enabled() is False
    monkeypatch.setenv("AGT_B3_CHILDREN_WRITER", "1")
    assert children_writer_enabled() is True


# insert_pending_order_child -----------------------------------------------

def test_insert_creates_row_with_status_and_ids(conn, parent_order_id):
    import json
    from agt_equities.order_state import insert_pending_order_child

    child_id = insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=4242, child_ib_perm_id=9999,
    )
    assert child_id > 0
    row = conn.execute(
        "SELECT parent_order_id, account_id, status, "
        "child_ib_order_id, child_ib_perm_id, status_history "
        "FROM pending_order_children WHERE id = ?",
        (child_id,),
    ).fetchone()
    assert row["parent_order_id"] == parent_order_id
    assert row["account_id"] == "U21971297"
    assert row["status"] == "sent"
    assert row["child_ib_order_id"] == 4242
    assert row["child_ib_perm_id"] == 9999
    hist = json.loads(row["status_history"])
    assert len(hist) == 1
    assert hist[0]["status"] == "sent"
    assert hist[0]["by"] == "b3_writer"


def test_insert_is_idempotent_on_parent_plus_account(conn, parent_order_id):
    from agt_equities.order_state import insert_pending_order_child
    id1 = insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=1, child_ib_perm_id=1,
    )
    id2 = insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=2, child_ib_perm_id=2,
    )
    assert id1 == id2
    count = conn.execute(
        "SELECT COUNT(*) FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ?",
        (parent_order_id, "U21971297"),
    ).fetchone()[0]
    assert count == 1


def test_insert_coalesce_preserves_non_null_ids_on_recall(conn, parent_order_id):
    from agt_equities.order_state import insert_pending_order_child
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=4242, child_ib_perm_id=9999,
    )
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=1111, child_ib_perm_id=2222,
    )
    row = conn.execute(
        "SELECT child_ib_order_id, child_ib_perm_id FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ?",
        (parent_order_id, "U21971297"),
    ).fetchone()
    assert row["child_ib_order_id"] == 4242
    assert row["child_ib_perm_id"] == 9999


def test_insert_fills_null_ids_on_recall(conn, parent_order_id):
    from agt_equities.order_state import insert_pending_order_child
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="staged",
    )
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=7777, child_ib_perm_id=8888,
    )
    row = conn.execute(
        "SELECT child_ib_order_id, child_ib_perm_id FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ?",
        (parent_order_id, "U21971297"),
    ).fetchone()
    assert row["child_ib_order_id"] == 7777
    assert row["child_ib_perm_id"] == 8888


def test_insert_distinct_accounts_produce_distinct_rows(conn, parent_order_id):
    from agt_equities.order_state import insert_pending_order_child
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=1, child_ib_perm_id=1,
    )
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U22076329",
        status="sent", child_ib_order_id=2, child_ib_perm_id=2,
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM pending_order_children "
        "WHERE parent_order_id = ?",
        (parent_order_id,),
    ).fetchone()[0]
    assert count == 2


# update_child_ib_ids ------------------------------------------------------

def test_update_populates_null_ids(conn, parent_order_id):
    from agt_equities.order_state import (
        insert_pending_order_child, update_child_ib_ids,
    )
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="staged",
    )
    updated = update_child_ib_ids(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        child_ib_order_id=5555, child_ib_perm_id=6666,
    )
    assert updated is True
    row = conn.execute(
        "SELECT child_ib_order_id, child_ib_perm_id FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ?",
        (parent_order_id, "U21971297"),
    ).fetchone()
    assert row["child_ib_order_id"] == 5555
    assert row["child_ib_perm_id"] == 6666


def test_update_preserves_non_null_ids(conn, parent_order_id):
    from agt_equities.order_state import (
        insert_pending_order_child, update_child_ib_ids,
    )
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="sent", child_ib_order_id=100, child_ib_perm_id=200,
    )
    update_child_ib_ids(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        child_ib_order_id=999, child_ib_perm_id=999,
    )
    row = conn.execute(
        "SELECT child_ib_order_id, child_ib_perm_id FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ?",
        (parent_order_id, "U21971297"),
    ).fetchone()
    assert row["child_ib_order_id"] == 100
    assert row["child_ib_perm_id"] == 200


def test_update_returns_false_when_no_child_row(conn, parent_order_id):
    from agt_equities.order_state import update_child_ib_ids
    updated = update_child_ib_ids(
        conn, parent_order_id=parent_order_id, account_id="U99999999",
        child_ib_order_id=1, child_ib_perm_id=1,
    )
    assert updated is False


def test_update_noop_when_both_ids_none(conn, parent_order_id):
    from agt_equities.order_state import (
        insert_pending_order_child, update_child_ib_ids,
    )
    insert_pending_order_child(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
        status="staged",
    )
    updated = update_child_ib_ids(
        conn, parent_order_id=parent_order_id, account_id="U21971297",
    )
    assert updated is False


# _on_open_order_write_child handler --------------------------------------

def _make_open_order_trade(
    *, order_id: int = 4242, perm_id: int = 0, account: str = "U21971297",
):
    order = SimpleNamespace(orderId=order_id, permId=perm_id, account=account)
    return SimpleNamespace(order=order)


@pytest.fixture()
def bot_module():
    # CI deps (requirements-ci.txt) intentionally omit telegram/fastapi/etc.
    # Skip handler tests when the production bot module cannot import --
    # local dev + production both have the full stack, so this still runs
    # everywhere it matters. The 12 order_state helper tests above do NOT
    # require this fixture and cover the B3 writer contract by themselves.
    return pytest.importorskip("telegram_bot")


def test_open_order_handler_populates_perm_id(bot_module, monkeypatch):
    import sqlite3 as _sq
    from agt_equities.schema import register_operational_tables
    from agt_equities.order_state import insert_pending_order_child

    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    register_operational_tables(c)
    from agt_equities.schema import _extend_pending_orders
    _extend_pending_orders(c)
    cur = c.execute(
        "INSERT INTO pending_orders (payload, status, ib_order_id, created_at) "
        "VALUES ('{}', 'sent', 4242, datetime('now'))"
    )
    parent_id = int(cur.lastrowid)
    insert_pending_order_child(
        c, parent_order_id=parent_id, account_id="U21971297",
        status="sent", child_ib_order_id=4242,
    )
    c.commit()

    class _ConnProxy:
        def __init__(self, inner): self._c = inner
        def execute(self, *a, **kw): return self._c.execute(*a, **kw)
        def executemany(self, *a, **kw): return self._c.executemany(*a, **kw)
        def commit(self): return self._c.commit()
        def rollback(self): return self._c.rollback()
        def close(self): pass
    monkeypatch.setattr(bot_module, "_get_db_connection", lambda: _ConnProxy(c))
    trade = _make_open_order_trade(order_id=4242, perm_id=55555, account="U21971297")
    bot_module._on_open_order_write_child(trade)

    row = c.execute(
        "SELECT child_ib_order_id, child_ib_perm_id FROM pending_order_children "
        "WHERE parent_order_id = ? AND account_id = ?",
        (parent_id, "U21971297"),
    ).fetchone()
    assert row["child_ib_order_id"] == 4242
    assert row["child_ib_perm_id"] == 55555
    c.close()


def test_open_order_handler_kill_switch_short_circuits(bot_module, monkeypatch):
    monkeypatch.setenv("AGT_B3_CHILDREN_WRITER", "0")

    def _should_not_be_called():
        raise AssertionError("_get_db_connection must not be called when kill switch is off")

    monkeypatch.setattr(bot_module, "_get_db_connection", _should_not_be_called)
    trade = _make_open_order_trade()
    bot_module._on_open_order_write_child(trade)


def test_open_order_handler_noop_on_orphan(bot_module, monkeypatch):
    import sqlite3 as _sq
    from agt_equities.schema import register_operational_tables, _extend_pending_orders

    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    register_operational_tables(c)
    _extend_pending_orders(c)
    c.commit()

    class _ConnProxy:
        def __init__(self, inner): self._c = inner
        def execute(self, *a, **kw): return self._c.execute(*a, **kw)
        def executemany(self, *a, **kw): return self._c.executemany(*a, **kw)
        def commit(self): return self._c.commit()
        def rollback(self): return self._c.rollback()
        def close(self): pass
    monkeypatch.setattr(bot_module, "_get_db_connection", lambda: _ConnProxy(c))
    trade = _make_open_order_trade(order_id=999999, perm_id=888888, account="U21971297")
    bot_module._on_open_order_write_child(trade)

    count = c.execute("SELECT COUNT(*) FROM pending_order_children").fetchone()[0]
    assert count == 0
    c.close()


def test_open_order_handler_swallows_exceptions(bot_module, monkeypatch, caplog):
    import logging

    # agt_bridge logger is propagate=False + has its own handler, so default
    # caplog (attached to root) misses records. Flip propagate for the test.
    logging.getLogger("agt_bridge").propagate = True
    caplog.set_level(logging.WARNING)

    def _boom():
        raise RuntimeError("db dead")

    monkeypatch.setattr(bot_module, "_get_db_connection", _boom)
    trade = _make_open_order_trade()
    bot_module._on_open_order_write_child(trade)
    assert any(
        "B3 openOrderEvent handler error" in rec.getMessage()
        for rec in caplog.records
    )
