"""Decoupling Sprint B Unit B4 -- FA-block-aware inception_delta reader.

Scope:
* _lookup_inception_delta three-stage resolver:
    Stage 1: pending_order_children.child_ib_perm_id  -> parent_order_id -> payload
    Stage 2: pending_order_children.child_ib_order_id -> parent_order_id -> payload
    Stage 3: legacy flat pending_orders.ib_perm_id / ib_order_id
* Orphaned child falls through to stage 3 with ORPHANED_CHILD_ROW warning.
* Feature flag USE_FA_BLOCK_CHILDREN_READER=0 routes to legacy resolver.
* Shim _lookup_inception_delta_from_payload preserves existing monkeypatch
  compatibility (defaults to new resolver).
* _on_csp_premium_fill has B2-parity bounded retry + INCEPTION_DELTA_MISS
  alert push on exhaustion.
* format_alert_text renders INCEPTION_DELTA_MISS kind.

Tests seed an in-memory SQLite with agt_equities.schema.register_operational_tables
and monkeypatch telegram_bot._get_db_connection to return that connection.
No production DB, no ib_async, no real time.sleep.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# B4 tests live-import telegram_bot for resolver + CSP retry coverage, which
# pulls heavy deps (anthropic, telegram, ib_async) intentionally not in
# requirements-ci.txt. importorskip mirrors test_b2 / test_inception_delta_fill:
# collected in CI but skipped when heavy deps absent; local smoke (uv py312)
# is canonical signal per feedback_run_tests_via_gitlab_ci.
pytest.importorskip("anthropic")
pytest.importorskip("telegram")
pytest.importorskip("ib_async")

pytestmark = pytest.mark.sprint_a


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from agt_equities.schema import register_operational_tables, _extend_pending_orders
    register_operational_tables(c)
    _extend_pending_orders(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def bot_module(conn, monkeypatch):
    """Import telegram_bot and monkeypatch _get_db_connection to return the
    shared in-memory conn. A thin no-op close wrapper prevents contextlib.closing
    from destroying the fixture-owned connection after the resolver returns."""
    import telegram_bot

    class _ConnProxy:
        def __init__(self, c):
            self._c = c
        def execute(self, *a, **kw):
            return self._c.execute(*a, **kw)
        def commit(self):
            return self._c.commit()
        def close(self):
            pass  # fixture owns lifecycle

    monkeypatch.setattr(telegram_bot, "_get_db_connection", lambda: _ConnProxy(conn))
    # Default flag ON; individual tests override.
    monkeypatch.setenv("USE_FA_BLOCK_CHILDREN_READER", "1")
    yield telegram_bot


def _seed_parent(conn, payload: dict, ib_perm_id: int = 0, ib_order_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO pending_orders (payload, status, ib_perm_id, ib_order_id, created_at) "
        "VALUES (?, 'sent', ?, ?, datetime('now'))",
        (json.dumps(payload), ib_perm_id, ib_order_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_child(conn, parent_id: int, account_id: str = "U21971297",
                child_ib_perm_id: int | None = None,
                child_ib_order_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, child_ib_order_id, child_ib_perm_id, "
        " status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'sent', datetime('now'), datetime('now'))",
        (parent_id, account_id, child_ib_order_id, child_ib_perm_id),
    )
    conn.commit()
    return int(cur.lastrowid)


# --------------------------------------------------------------------------
# Stage 1: child permId resolution
# --------------------------------------------------------------------------

def test_stage1_child_perm_id_resolves_to_parent_payload(bot_module, conn):
    parent_id = _seed_parent(conn, {"inception_delta": -0.28})
    _seed_child(conn, parent_id, account_id="U21971297", child_ib_perm_id=99999)

    result = bot_module._lookup_inception_delta(perm_id=99999, client_id=0)
    assert result == pytest.approx(-0.28)


def test_stage1_child_perm_id_one_parent_many_children_all_resolve(bot_module, conn):
    """Parent-wide inception_delta semantics (Yash 2026-04-15 lock):
    every child under the same parent returns the same value."""
    parent_id = _seed_parent(conn, {"inception_delta": -0.35})
    _seed_child(conn, parent_id, "U21971297", child_ib_perm_id=111)
    _seed_child(conn, parent_id, "U22076329", child_ib_perm_id=222)
    _seed_child(conn, parent_id, "U22388499", child_ib_perm_id=333)

    for pid in (111, 222, 333):
        assert bot_module._lookup_inception_delta(perm_id=pid) == pytest.approx(-0.35)


# --------------------------------------------------------------------------
# Stage 2: child orderId fallback
# --------------------------------------------------------------------------

def test_stage2_child_order_id_fallback(bot_module, conn):
    parent_id = _seed_parent(conn, {"inception_delta": -0.41})
    _seed_child(conn, parent_id, child_ib_perm_id=None, child_ib_order_id=77)

    assert bot_module._lookup_inception_delta(perm_id=0, client_id=77) == pytest.approx(-0.41)


def test_stage2_child_order_id_when_perm_id_misses(bot_module, conn):
    parent_id = _seed_parent(conn, {"inception_delta": -0.15})
    _seed_child(conn, parent_id, child_ib_perm_id=8888, child_ib_order_id=42)

    # Wrong perm_id but valid order_id -> stage 2 wins.
    assert bot_module._lookup_inception_delta(perm_id=7777, client_id=42) == pytest.approx(-0.15)


# --------------------------------------------------------------------------
# Stage 3: legacy flat path (non-FA single-account orders)
# --------------------------------------------------------------------------

def test_stage3_flat_perm_id_path(bot_module, conn):
    _seed_parent(conn, {"inception_delta": -0.22}, ib_perm_id=55555)
    assert bot_module._lookup_inception_delta(perm_id=55555) == pytest.approx(-0.22)


def test_stage3_flat_order_id_path(bot_module, conn):
    _seed_parent(conn, {"inception_delta": -0.19}, ib_order_id=12)
    assert bot_module._lookup_inception_delta(perm_id=0, client_id=12) == pytest.approx(-0.19)


# --------------------------------------------------------------------------
# Miss + edge cases
# --------------------------------------------------------------------------

def test_total_miss_returns_none(bot_module, conn):
    # Empty DB.
    assert bot_module._lookup_inception_delta(perm_id=1, client_id=2) is None


def test_both_perm_and_client_zero_returns_none(bot_module, conn):
    assert bot_module._lookup_inception_delta(perm_id=0, client_id=0) is None


def test_orphaned_child_falls_through_to_flat_path(bot_module, conn, caplog):
    """Child row references parent_order_id that doesn't exist; resolver logs
    ORPHANED_CHILD_ROW and falls through to stage 3 (which also misses here).
    """
    import logging as _lg
    _lg.getLogger("agt_bridge").propagate = True
    caplog.set_level(_lg.WARNING)
    import logging
    conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, child_ib_perm_id, "
        " status, created_at, updated_at) "
        "VALUES (99999, 'U21971297', 7777, 'sent', datetime('now'), datetime('now'))"
    )
    conn.commit()

    with caplog.at_level(logging.WARNING, logger="telegram_bot"):
        result = bot_module._lookup_inception_delta(perm_id=7777)
    assert result is None
    assert any("ORPHANED_CHILD_ROW" in rec.getMessage() for rec in caplog.records)


def test_orphaned_child_fallthrough_hits_flat_path(bot_module, conn):
    """Orphaned child exists with perm_id=7777; a separate flat-path row also
    has ib_perm_id=7777. Resolver falls through to stage 3 and resolves it."""
    conn.execute(
        "INSERT INTO pending_order_children "
        "(parent_order_id, account_id, child_ib_perm_id, "
        " status, created_at, updated_at) "
        "VALUES (99999, 'U21971297', 7777, 'sent', datetime('now'), datetime('now'))"
    )
    _seed_parent(conn, {"inception_delta": -0.99}, ib_perm_id=7777)
    assert bot_module._lookup_inception_delta(perm_id=7777) == pytest.approx(-0.99)


def test_payload_missing_inception_delta_returns_none(bot_module, conn):
    parent_id = _seed_parent(conn, {"some_other_key": 1})
    _seed_child(conn, parent_id, child_ib_perm_id=100)
    assert bot_module._lookup_inception_delta(perm_id=100) is None


def test_null_inception_delta_returns_none(bot_module, conn):
    parent_id = _seed_parent(conn, {"inception_delta": None})
    _seed_child(conn, parent_id, child_ib_perm_id=200)
    assert bot_module._lookup_inception_delta(perm_id=200) is None


# --------------------------------------------------------------------------
# Shim + feature flag
# --------------------------------------------------------------------------

def test_shim_defaults_to_new_resolver(bot_module, conn, monkeypatch):
    monkeypatch.delenv("USE_FA_BLOCK_CHILDREN_READER", raising=False)
    parent_id = _seed_parent(conn, {"inception_delta": -0.10})
    _seed_child(conn, parent_id, child_ib_perm_id=501)
    # New resolver hits child; legacy would miss (no flat row).
    assert bot_module._lookup_inception_delta_from_payload(perm_id=501) == pytest.approx(-0.10)


def test_shim_flag_zero_routes_to_legacy(bot_module, conn, monkeypatch):
    monkeypatch.setenv("USE_FA_BLOCK_CHILDREN_READER", "0")
    parent_id = _seed_parent(conn, {"inception_delta": -0.10})
    _seed_child(conn, parent_id, child_ib_perm_id=502)
    # Legacy path ignores children; no flat row -> miss.
    assert bot_module._lookup_inception_delta_from_payload(perm_id=502) is None


def test_shim_flag_invalid_falls_back_to_new_resolver(bot_module, conn, monkeypatch):
    """Strict flag semantics: only the literal "0" disables. Anything else
    (empty, "false", "maybe") keeps the safer new resolver."""
    for bogus in ("", "false", "no", "maybe", "00", "nope"):
        monkeypatch.setenv("USE_FA_BLOCK_CHILDREN_READER", bogus)
        parent_id = _seed_parent(conn, {"inception_delta": -0.77})
        perm = 10000 + hash(bogus) % 5000
        _seed_child(conn, parent_id, child_ib_perm_id=perm)
        assert bot_module._lookup_inception_delta_from_payload(perm_id=perm) == pytest.approx(-0.77), \
            f"bogus flag value {bogus!r} should not disable new resolver"


def test_legacy_resolver_ignores_pending_order_children(bot_module, conn):
    """Even seeded with a child row, legacy resolver should miss because it
    only queries pending_orders flat columns."""
    parent_id = _seed_parent(conn, {"inception_delta": -0.10})  # no flat ids
    _seed_child(conn, parent_id, child_ib_perm_id=601, child_ib_order_id=602)
    assert bot_module._lookup_inception_delta_legacy(perm_id=601, client_id=602) is None


# --------------------------------------------------------------------------
# _on_csp_premium_fill retry parity (mirrors test_b2_cc_fill_permid_retry)
# --------------------------------------------------------------------------

def _make_csp_trade_fill(perm_id=12345, order_id=111, account="U21971297"):
    contract = SimpleNamespace(symbol="AAPL", secType="OPT", right="P")
    order = SimpleNamespace(action="SELL", account=account, permId=perm_id, orderId=order_id)
    execution = SimpleNamespace(
        execId="exec_b4_csp", price=1.25, shares=-1,
        acctNumber=account,
    )
    trade = SimpleNamespace(contract=contract, order=order)
    fill = SimpleNamespace(execution=execution)
    return trade, fill


def test_csp_first_try_success_no_sleep(bot_module, monkeypatch):
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    sleeps = []
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: sleeps.append(s))

    lookup_calls = []
    def _fake_lookup(perm_id, client_id):
        lookup_calls.append((perm_id, client_id))
        return 0.18

    monkeypatch.setattr(bot_module, "_lookup_inception_delta_from_payload", _fake_lookup)

    captured = {}
    def _fake_apply(*args, **kw):
        captured["args"] = args
        captured["kw"] = kw
        return True
    monkeypatch.setattr(bot_module, "_apply_fill_atomically", _fake_apply)

    trade, fill = _make_csp_trade_fill()
    bot_module._on_csp_premium_fill(trade, fill)

    assert len(lookup_calls) == 1
    assert sleeps == []
    assert captured["kw"].get("inception_delta") == 0.18


def test_csp_transient_miss_then_hit(bot_module, monkeypatch):
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    sleeps = []
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: sleeps.append(s))

    values = [None, None, 0.22]
    monkeypatch.setattr(
        bot_module, "_lookup_inception_delta_from_payload",
        lambda p, c: values.pop(0),
    )

    applied_kwargs = {}
    monkeypatch.setattr(
        bot_module, "_apply_fill_atomically",
        lambda *a, **kw: applied_kwargs.update(kw) or True,
    )

    trade, fill = _make_csp_trade_fill()
    bot_module._on_csp_premium_fill(trade, fill)

    assert sleeps == [0.5, 0.5]
    assert applied_kwargs.get("inception_delta") == 0.22


def test_csp_persistent_miss_exhausts_and_enqueues_alert(bot_module, monkeypatch, caplog):

    import logging as _lg
    _lg.getLogger("agt_bridge").propagate = True
    caplog.set_level(_lg.WARNING)
    import logging
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())

    monkeypatch.setattr(bot_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        bot_module, "_lookup_inception_delta_from_payload",
        lambda p, c: None,
    )

    applied_kwargs = {}
    monkeypatch.setattr(
        bot_module, "_apply_fill_atomically",
        lambda *a, **kw: applied_kwargs.update(kw) or True,
    )

    enqueued = []
    monkeypatch.setattr(
        bot_module, "_enqueue_inception_delta_miss",
        lambda *a, **kw: enqueued.append((a, kw)),
    )

    trade, fill = _make_csp_trade_fill()
    with caplog.at_level(logging.WARNING, logger="telegram_bot"):
        bot_module._on_csp_premium_fill(trade, fill)

    assert applied_kwargs.get("inception_delta") is None
    assert any("inception_delta lookup miss" in rec.getMessage() for rec in caplog.records)
    assert len(enqueued) == 1, "INCEPTION_DELTA_MISS alert must fire exactly once on exhaustion"


def test_cc_persistent_miss_also_enqueues_alert(bot_module, monkeypatch):
    """Regression: confirm the B4 miss-alert push is wired in _on_cc_fill too."""
    monkeypatch.setitem(bot_module.ACCOUNT_TO_HOUSEHOLD, "U21971297", "Yash_Household")
    monkeypatch.setattr(bot_module, "EXCLUDED_TICKERS", set())
    monkeypatch.setattr(bot_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        bot_module, "_lookup_inception_delta_from_payload",
        lambda p, c: None,
    )
    monkeypatch.setattr(bot_module, "_apply_fill_atomically", lambda *a, **kw: True)

    enqueued = []
    monkeypatch.setattr(
        bot_module, "_enqueue_inception_delta_miss",
        lambda *a, **kw: enqueued.append((a, kw)),
    )

    contract = SimpleNamespace(symbol="AAPL", secType="OPT", right="C")
    order = SimpleNamespace(action="SELL", account="U21971297", permId=5, orderId=6)
    execution = SimpleNamespace(execId="x", price=1.0, shares=-1, acctNumber="U21971297")
    trade = SimpleNamespace(contract=contract, order=order)
    fill = SimpleNamespace(execution=execution)
    bot_module._on_cc_fill(trade, fill)

    assert len(enqueued) == 1


# --------------------------------------------------------------------------
# format_alert_text INCEPTION_DELTA_MISS branch
# --------------------------------------------------------------------------

def test_format_alert_inception_delta_miss():
    from agt_equities.alerts import format_alert_text
    alert = {
        "kind": "INCEPTION_DELTA_MISS",
        "severity": "info",
        "payload": {
            "household": "Yash_Household",
            "ticker": "AAPL",
            "acct_id": "U21971297",
            "perm_id": 99999,
            "client_id": 42,
            "exec_id": "exec_abc",
        },
    }
    text = format_alert_text(alert)
    assert "[INFO]" in text
    assert "inception_delta miss" in text
    assert "Yash_Household/AAPL" in text
    assert "acct=U21971297" in text
    assert "permId=99999" in text


def test_format_alert_inception_delta_miss_tolerant_of_missing_keys():
    from agt_equities.alerts import format_alert_text
    text = format_alert_text({
        "kind": "INCEPTION_DELTA_MISS",
        "severity": "warn",
        "payload": {},
    })
    assert "[WARN]" in text
    assert "inception_delta miss" in text
    assert "?" in text  # defaults used for missing fields


# --------------------------------------------------------------------------
# _enqueue_inception_delta_miss fire-and-forget semantics
# --------------------------------------------------------------------------

def test_enqueue_miss_survives_alert_module_failure(bot_module, monkeypatch, caplog):
    """If enqueue_alert raises, the helper must swallow it + log WARNING.
    Fill callbacks cannot crash on alert-bus failures."""
    import logging as _lg
    _lg.getLogger("agt_bridge").propagate = True
    caplog.set_level(_lg.WARNING)
    import logging

    def _boom(*a, **kw):
        raise RuntimeError("simulated bus failure")

    import agt_equities.alerts as alerts_mod
    monkeypatch.setattr(alerts_mod, "enqueue_alert", _boom)

    with caplog.at_level(logging.WARNING, logger="telegram_bot"):
        # Should NOT raise.
        bot_module._enqueue_inception_delta_miss(
            "Yash_Household", "AAPL", 1, 2, "U21971297", "exec_test",
        )

    assert any("INCEPTION_DELTA_MISS alert enqueue failed" in rec.getMessage()
               for rec in caplog.records)
