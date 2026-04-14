"""Unit tests for ``agt_equities.ib_conn`` (Decoupling Sprint A Unit A1).

The IBConnector is a thin async wrapper around ``ib_async.IB``. We avoid
touching the real IB Gateway by patching ``ib_async.IB`` with a stub.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.sprint_a

from agt_equities.ib_conn import IBConnConfig, IBConnector


# ---------------------------------------------------------------------------
# Smoke / unit
# ---------------------------------------------------------------------------

def test_import_smoke():
    """Module imports cleanly with zero side effects."""
    import agt_equities.ib_conn as mod
    assert mod.IBConnector is IBConnector


def test_resolve_default_ports_paper(monkeypatch):
    # Force PAPER_MODE re-resolve via env fallback path.
    monkeypatch.setattr(
        "agt_equities.ib_conn._resolve_default_ports",
        lambda: (4002, 7497),
    )
    cfg = IBConnConfig()
    assert cfg.gateway_port == 4002
    assert cfg.tws_fallback_port == 7497


def test_config_explicit_overrides_defaults(monkeypatch):
    cfg = IBConnConfig(
        host="10.0.0.5",
        client_id=2,
        gateway_port=4001,
        tws_fallback_port=7496,
        connect_timeout=5.0,
    )
    assert cfg.host == "10.0.0.5"
    assert cfg.client_id == 2
    assert cfg.gateway_port == 4001
    assert cfg.tws_fallback_port == 7496
    assert cfg.connect_timeout == 5.0


def test_is_connected_false_when_no_handle():
    conn = IBConnector(config=IBConnConfig(client_id=2))
    assert conn.is_connected() is False


def test_is_connected_swallows_exceptions():
    """If the underlying IB.isConnected() raises, we must report False, not blow up."""
    conn = IBConnector(config=IBConnConfig(client_id=2))
    bad = MagicMock()
    bad.isConnected.side_effect = RuntimeError("socket dead")
    conn._ib = bad  # type: ignore[assignment]
    assert conn.is_connected() is False


# ---------------------------------------------------------------------------
# ensure_connected — patch ib_async.IB to avoid real socket.
# ---------------------------------------------------------------------------

class _FakeIB:
    """Minimal ``ib_async.IB`` stand-in for connect-loop tests."""

    def __init__(self):
        self._connected = False
        self.disconnectedEvent = _FakeEvent()
        self.errorEvent = _FakeEvent()
        self.connect_calls: list[tuple] = []

    async def connectAsync(self, host, port, *, clientId, timeout):
        self.connect_calls.append((host, port, clientId, timeout))
        self._connected = True

    def reqMarketDataType(self, *_a, **_k): pass
    def reqPositions(self, *_a, **_k): pass
    def isConnected(self) -> bool: return self._connected
    def disconnect(self):
        self._connected = False


class _FakeEvent:
    def __init__(self):
        self.handlers: list = []
    def __iadd__(self, h):
        self.handlers.append(h); return self
    def __isub__(self, h):
        try:
            self.handlers.remove(h)
        except ValueError:
            pass
        return self


def test_ensure_connected_succeeds_on_gateway(monkeypatch):
    monkeypatch.setattr("agt_equities.ib_conn.ib_async.IB", _FakeIB)
    conn = IBConnector(config=IBConnConfig(
        client_id=2,
        gateway_port=4002,
        tws_fallback_port=7497,
        post_connect_sleep_s=0,
        request_positions_on_connect=False,
    ))
    ib = asyncio.run(conn.ensure_connected())
    assert isinstance(ib, _FakeIB)
    assert ib.connect_calls[0][1] == 4002  # tried Gateway port first
    assert conn.is_connected()


def test_ensure_connected_falls_back_to_tws(monkeypatch):
    """First connect raises, second succeeds."""
    state = {"calls": 0}

    class _FlakyIB(_FakeIB):
        async def connectAsync(self, host, port, *, clientId, timeout):
            state["calls"] += 1
            if port == 4002:
                raise ConnectionRefusedError("gateway down")
            await super().connectAsync(host, port, clientId=clientId, timeout=timeout)

    monkeypatch.setattr("agt_equities.ib_conn.ib_async.IB", _FlakyIB)
    conn = IBConnector(config=IBConnConfig(
        client_id=2,
        gateway_port=4002,
        tws_fallback_port=7497,
        post_connect_sleep_s=0,
        request_positions_on_connect=False,
    ))
    ib = asyncio.run(conn.ensure_connected())
    assert state["calls"] == 2
    assert ib.connect_calls[0][1] == 7497  # fallback succeeded


def test_ensure_connected_raises_when_both_fail(monkeypatch):
    class _DeadIB(_FakeIB):
        async def connectAsync(self, host, port, *, clientId, timeout):
            raise ConnectionRefusedError(f"port {port} dead")

    monkeypatch.setattr("agt_equities.ib_conn.ib_async.IB", _DeadIB)
    conn = IBConnector(config=IBConnConfig(
        client_id=2,
        gateway_port=4002,
        tws_fallback_port=7497,
        post_connect_sleep_s=0,
        request_positions_on_connect=False,
    ))
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(conn.ensure_connected())


def test_ensure_connected_idempotent_when_already_live(monkeypatch):
    monkeypatch.setattr("agt_equities.ib_conn.ib_async.IB", _FakeIB)
    conn = IBConnector(config=IBConnConfig(
        client_id=2,
        post_connect_sleep_s=0,
        request_positions_on_connect=False,
    ))
    ib1 = asyncio.run(conn.ensure_connected())
    ib2 = asyncio.run(conn.ensure_connected())
    assert ib1 is ib2  # same handle, no second connect


def test_post_connect_hook_invoked(monkeypatch):
    monkeypatch.setattr("agt_equities.ib_conn.ib_async.IB", _FakeIB)
    seen: list = []

    async def hook(ib_obj):
        seen.append(ib_obj)

    conn = IBConnector(
        config=IBConnConfig(
            client_id=2,
            post_connect_sleep_s=0,
            request_positions_on_connect=False,
        ),
        post_connect=hook,
    )
    asyncio.run(conn.ensure_connected())
    assert len(seen) == 1


def test_disconnect_clears_handle(monkeypatch):
    monkeypatch.setattr("agt_equities.ib_conn.ib_async.IB", _FakeIB)
    conn = IBConnector(config=IBConnConfig(
        client_id=2,
        post_connect_sleep_s=0,
        request_positions_on_connect=False,
    ))
    asyncio.run(conn.ensure_connected())
    assert conn.is_connected()
    asyncio.run(conn.disconnect())
    assert not conn.is_connected()
    assert conn.ib is None
