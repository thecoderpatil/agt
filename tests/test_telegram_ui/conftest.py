"""
tests/test_telegram_ui/conftest.py

Telegram UI offline test harness. Provides:
  - In-memory SQLite seeded with pending_orders schema
  - Forged telegram.Update objects for commands and callbacks
  - Patched bot.send_message / edit_message_text with mock capture

Design: PTB Update Forgery pattern (Gemini peer review 2026-04-16).
No live Telegram, no live IBKR. Pure message-format + DB-transition tests.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# CI gate
pytestmark = pytest.mark.sprint_a


# ---------------------------------------------------------------------------
# In-memory SQLite with pending_orders schema
# ---------------------------------------------------------------------------

PENDING_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS pending_orders (
    id INTEGER PRIMARY KEY,
    payload JSON NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_orders_status
    ON pending_orders(status, created_at, id);
"""

PENDING_ORDER_CHILDREN_DDL = """
CREATE TABLE IF NOT EXISTS pending_order_children (
    id INTEGER PRIMARY KEY,
    parent_order_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    child_ib_order_id INTEGER,
    child_ib_perm_id INTEGER,
    status TEXT NOT NULL,
    margin_check_status TEXT,
    margin_check_reason TEXT,
    FOREIGN KEY (parent_order_id) REFERENCES pending_orders(id)
);
"""


@pytest.fixture
def mem_db():
    """In-memory SQLite with pending_orders + pending_order_children tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(PENDING_ORDERS_DDL)
    conn.executescript(PENDING_ORDER_CHILDREN_DDL)
    yield conn
    conn.close()


def seed_staged_orders(conn: sqlite3.Connection, orders: list[dict]) -> list[int]:
    """Insert staged orders into pending_orders, return list of IDs."""
    ids = []
    for order in orders:
        payload = json.dumps(order, default=str)
        cur = conn.execute(
            "INSERT INTO pending_orders (payload, status, created_at) VALUES (?, 'staged', ?)",
            (payload, datetime.now().isoformat()),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def get_orders_by_status(conn: sqlite3.Connection, status: str) -> list[dict]:
    """Fetch all pending_orders with given status."""
    rows = conn.execute(
        "SELECT id, payload, status FROM pending_orders WHERE status = ?",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Forged Telegram objects
# ---------------------------------------------------------------------------

FAKE_USER_ID = 123456789
FAKE_CHAT_ID = 123456789


def forge_update_command(command: str, args: str = "", update_id: int = 1) -> MagicMock:
    """Forge a telegram.Update for a /command message.

    Returns a MagicMock shaped like telegram.Update with:
      - update.message.text = "/command args"
      - update.message.from_user.id = FAKE_USER_ID
      - update.message.chat.id = FAKE_CHAT_ID
      - update.message.reply_text = AsyncMock (captures calls)
    """
    text = f"/{command}" + (f" {args}" if args else "")

    user = MagicMock()
    user.id = FAKE_USER_ID
    user.first_name = "TestUser"
    user.is_bot = False

    chat = MagicMock()
    chat.id = FAKE_CHAT_ID
    chat.type = "private"

    message = MagicMock()
    message.text = text
    message.from_user = user
    message.chat = chat
    message.chat_id = FAKE_CHAT_ID
    message.reply_text = AsyncMock()
    message.reply_html = AsyncMock()
    message.message_id = update_id * 100

    update = MagicMock()
    update.update_id = update_id
    update.message = message
    update.callback_query = None
    update.effective_user = user
    update.effective_chat = chat

    return update


def forge_callback_query(
    callback_data: str,
    message_id: int = 100,
    update_id: int = 2,
) -> MagicMock:
    """Forge a telegram.Update for a callback query (inline button tap).

    Returns a MagicMock shaped like telegram.Update with:
      - update.callback_query.data = callback_data
      - update.callback_query.answer = AsyncMock
      - update.callback_query.edit_message_text = AsyncMock
      - update.callback_query.from_user.id = FAKE_USER_ID

    IMPORTANT: callback_data must be <64 bytes (Telegram API limit).
    """
    assert len(callback_data.encode('utf-8')) <= 64, (
        f"callback_data exceeds 64-byte Telegram limit: "
        f"{len(callback_data.encode('utf-8'))} bytes: {callback_data!r}"
    )

    user = MagicMock()
    user.id = FAKE_USER_ID
    user.first_name = "TestUser"

    message = MagicMock()
    message.message_id = message_id
    message.chat_id = FAKE_CHAT_ID

    query = MagicMock()
    query.data = callback_data
    query.from_user = user
    query.message = message
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.update_id = update_id
    update.message = None
    update.callback_query = query
    update.effective_user = user

    return update


def make_mock_context() -> MagicMock:
    """Create a mock ContextTypes.DEFAULT_TYPE for command handlers."""
    context = MagicMock()
    context.args = []
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot_data = {}
    context.user_data = {}
    context.chat_data = {}
    return context
