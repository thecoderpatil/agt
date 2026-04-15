"""Decoupling Sprint B Unit B1 — pending_order_children schema tests.

Scope:
* Table exists with the documented columns and types.
* All 5 indexes exist with the documented definitions (including the two
  partial indexes for perm_id / order_id).
* Foreign key to pending_orders(id) is declared in the schema (soft FK,
  enforced only when PRAGMA foreign_keys=ON — test asserts declaration).
* With foreign_keys=ON, inserting a child row referencing a non-existent
  parent raises; inserting against a real parent succeeds.

Tests use an in-memory SQLite via agt_equities.schema.register_operational_tables.
No production DB access. No ib_async. Fast.
"""

from __future__ import annotations

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # register_operational_tables creates pending_orders + pending_order_children
    from agt_equities.schema import register_operational_tables

    register_operational_tables(c)
    c.commit()  # close any implicit transaction so PRAGMA foreign_keys=ON sticks
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Table shape
# ---------------------------------------------------------------------------


EXPECTED_COLUMNS = {
    "id": "INTEGER",
    "parent_order_id": "INTEGER",
    "account_id": "TEXT",
    "child_ib_order_id": "INTEGER",
    "child_ib_perm_id": "INTEGER",
    "status": "TEXT",
    "margin_check_status": "TEXT",
    "margin_check_reason": "TEXT",
    "fill_price": "REAL",
    "fill_qty": "INTEGER",
    "fill_commission": "REAL",
    "fill_time": "TIMESTAMP",
    "last_ib_status": "TEXT",
    "status_history": "JSON",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
}

NOT_NULL_COLUMNS = {
    "parent_order_id",
    "account_id",
    "status",
    "created_at",
    "updated_at",
}


def test_pending_order_children_table_exists(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pending_order_children'"
    ).fetchall()
    assert len(rows) == 1


def test_pending_order_children_columns_shape(conn):
    info = conn.execute("PRAGMA table_info(pending_order_children)").fetchall()
    found = {row["name"]: row["type"] for row in info}
    assert found == EXPECTED_COLUMNS, (
        f"column shape mismatch: expected={EXPECTED_COLUMNS}, got={found}"
    )


def test_pending_order_children_not_null_columns(conn):
    info = conn.execute("PRAGMA table_info(pending_order_children)").fetchall()
    not_null = {row["name"] for row in info if row["notnull"]}
    # id is PRIMARY KEY — SQLite reports notnull=0 for PK cols; exclude from check.
    assert NOT_NULL_COLUMNS.issubset(not_null), (
        f"expected NOT NULL columns {NOT_NULL_COLUMNS} missing from {not_null}"
    )


def test_pending_order_children_primary_key_is_id(conn):
    info = conn.execute("PRAGMA table_info(pending_order_children)").fetchall()
    pk_cols = [row["name"] for row in info if row["pk"]]
    assert pk_cols == ["id"]


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


EXPECTED_INDEXES = {
    "idx_poc_parent",
    "idx_poc_perm_id",
    "idx_poc_order_id",
    "idx_poc_account",
    "idx_poc_status",
}


def test_pending_order_children_indexes_present(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='pending_order_children'"
    ).fetchall()
    found = {row["name"] for row in rows}
    assert EXPECTED_INDEXES.issubset(found), (
        f"missing indexes: {EXPECTED_INDEXES - found}"
    )


def test_perm_id_index_is_partial(conn):
    # Partial index: WHERE child_ib_perm_id IS NOT NULL
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_poc_perm_id'"
    ).fetchone()
    assert row is not None
    sql = row["sql"] or ""
    assert "WHERE" in sql.upper()
    assert "child_ib_perm_id" in sql
    assert "NOT NULL" in sql.upper()


def test_order_id_index_is_partial(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='index' AND name='idx_poc_order_id'"
    ).fetchone()
    assert row is not None
    sql = row["sql"] or ""
    assert "WHERE" in sql.upper()
    assert "child_ib_order_id" in sql
    assert "NOT NULL" in sql.upper()


# ---------------------------------------------------------------------------
# Foreign key
# ---------------------------------------------------------------------------


def test_foreign_key_declaration_exists(conn):
    fks = conn.execute(
        "PRAGMA foreign_key_list(pending_order_children)"
    ).fetchall()
    assert len(fks) == 1, f"expected 1 FK, got {len(fks)}: {[dict(r) for r in fks]}"
    fk = fks[0]
    assert fk["table"] == "pending_orders"
    assert fk["from"] == "parent_order_id"
    assert fk["to"] == "id"


def test_fk_enforced_when_pragma_on(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    # Insert child with non-existent parent — should raise IntegrityError.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pending_order_children "
            "(parent_order_id, account_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            (99999, "U21971297", "staged"),
        )


def test_fk_accepts_real_parent(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) "
        "VALUES (?, ?, datetime('now'))",
        ("{}", "staged"),
    )
    parent_id = cur.lastrowid
    conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (parent_id, "U21971297", "staged"),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT parent_order_id, account_id, status FROM pending_order_children"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["parent_order_id"] == parent_id
    assert rows[0]["account_id"] == "U21971297"
    assert rows[0]["status"] == "staged"


# ---------------------------------------------------------------------------
# Nullable invariants (B2 depends on this)
# ---------------------------------------------------------------------------


def test_child_perm_id_and_order_id_are_nullable(conn):
    """B2 relies on nullable child_ib_perm_id/order_id — populated async."""
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "INSERT INTO pending_orders (payload, status, created_at) "
        "VALUES ('{}', 'staged', datetime('now'))"
    )
    parent_id = cur.lastrowid
    conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, status, created_at, updated_at) "
        "VALUES (?, 'U21971297', 'staged', datetime('now'), datetime('now'))",
        (parent_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT child_ib_order_id, child_ib_perm_id "
        "FROM pending_order_children WHERE parent_order_id = ?",
        (parent_id,),
    ).fetchone()
    assert row["child_ib_order_id"] is None
    assert row["child_ib_perm_id"] is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_register_operational_tables_idempotent():
    """Calling twice is a no-op — production startup path relies on this."""
    from agt_equities.schema import register_operational_tables

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row  # schema.py PRAGMA table_info readers use row["name"]
    try:
        register_operational_tables(c)
        register_operational_tables(c)  # must not raise
        rows = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='pending_order_children'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        c.close()
