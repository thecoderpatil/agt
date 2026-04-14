"""Shared IBKR connector — lean wrapper for both ``agt_bot`` and ``agt_scheduler``.

Decoupling Sprint A Unit A1.

Design intent
-------------
* Both daemons need: connectAsync with Gateway→TWS port fallback, retry loop,
  market-data-type pin, disconnect callback hook.
* They DIVERGE on: post-connect side effects (fill listeners, Telegram alerts,
  orphan scans). Those stay in their respective daemons.
* This module exposes :class:`IBConnector` with optional callback hooks so each
  daemon can wire the divergent surface without monkey-patching.

NOT extracted from telegram_bot.py in this sprint
-------------------------------------------------
* ``_handle_1101_data_lost`` / ``_alert_1102`` — bot-specific Telegram alerts.
* Fill event listeners (``execDetailsEvent``) — registered only on the bot's
  ``clientId=1`` connection because the scheduler's ``clientId=2`` is data-only.
* ``_auto_reconnect`` orphan scan — bot-only reconciliation surface.

The bot continues to use its existing in-module IB connect path; once Sprint A
ships and the scheduler is proven stable, a follow-up can converge the bot
onto :class:`IBConnector` (banked as ``FU-BOT-IB-CONN-CONVERGE`` post-Sprint-A).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import ib_async


logger = logging.getLogger("agt_equities.ib_conn")


# ---------------------------------------------------------------------------
# Defaults — read from agt_equities.config.PAPER_MODE if available, else env.
# ---------------------------------------------------------------------------

def _resolve_default_ports() -> tuple[int, int]:
    """Return (gateway_port, tws_fallback_port) for the active mode."""
    try:
        from agt_equities.config import PAPER_MODE
    except Exception:
        PAPER_MODE = os.environ.get("PAPER_MODE", "1") == "1"
    gateway = 4002 if PAPER_MODE else 4001
    tws = 7497 if PAPER_MODE else 7496
    return gateway, tws


@dataclass
class IBConnConfig:
    """Connection parameters. Defaults preserve existing bot behavior."""

    host: str = "127.0.0.1"
    client_id: int = 1
    gateway_port: int | None = None
    tws_fallback_port: int | None = None
    connect_timeout: float = 10.0
    market_data_type: int = 4  # 4 = delayed-frozen; matches bot default
    post_connect_sleep_s: float = 2.0  # mirrors bot wait between connect + reqPositions
    request_positions_on_connect: bool = True

    def __post_init__(self) -> None:
        if self.gateway_port is None or self.tws_fallback_port is None:
            g, t = _resolve_default_ports()
            if self.gateway_port is None:
                self.gateway_port = g
            if self.tws_fallback_port is None:
                self.tws_fallback_port = t


# Type aliases for callbacks.
DisconnectCallback = Callable[[], None]
ErrorCallback = Callable[[int, int, str, object], None]
PostConnectHook = Callable[[ib_async.IB], Awaitable[None]]


@dataclass
class IBConnector:
    """Connect-with-retry wrapper around ``ib_async.IB``.

    Thread-safety: all coroutine entrypoints serialize behind ``_lock``. Safe
    for concurrent ``ensure_connected()`` calls from multiple coroutines on the
    same event loop.
    """

    config: IBConnConfig = field(default_factory=IBConnConfig)
    on_disconnect: DisconnectCallback | None = None
    on_error: ErrorCallback | None = None
    post_connect: PostConnectHook | None = None

    _ib: ib_async.IB | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def ib(self) -> ib_async.IB | None:
        return self._ib

    def is_connected(self) -> bool:
        try:
            return self._ib is not None and self._ib.isConnected()
        except Exception:
            return False

    async def ensure_connected(self) -> ib_async.IB:
        """Return a live connection, reconnecting if necessary.

        Tries Gateway port first, then TWS fallback. Raises the last
        exception if both fail.
        """
        async with self._lock:
            if self.is_connected():
                assert self._ib is not None
                return self._ib

            # Drop stale handle before retry.
            if self._ib is not None:
                try:
                    self._ib.disconnect()
                except Exception:
                    pass
                self._ib = None

            cfg = self.config
            last_exc: Exception | None = None
            for port, label in (
                (cfg.gateway_port, "Gateway"),
                (cfg.tws_fallback_port, "TWS"),
            ):
                candidate = ib_async.IB()
                try:
                    logger.info(
                        "Connecting to %s:%s (%s) clientId=%s …",
                        cfg.host, port, label, cfg.client_id,
                    )
                    if self.on_disconnect is not None:
                        candidate.disconnectedEvent += self.on_disconnect
                    if self.on_error is not None:
                        candidate.errorEvent += self.on_error
                    await candidate.connectAsync(
                        cfg.host, port,
                        clientId=cfg.client_id,
                        timeout=cfg.connect_timeout,
                    )
                    candidate.reqMarketDataType(cfg.market_data_type)
                    if cfg.post_connect_sleep_s > 0:
                        await asyncio.sleep(cfg.post_connect_sleep_s)
                    if cfg.request_positions_on_connect:
                        try:
                            candidate.reqPositions()
                            await asyncio.sleep(1)
                        except Exception as exc:
                            logger.warning("reqPositions failed post-connect: %s", exc)
                    self._ib = candidate
                    if self.post_connect is not None:
                        try:
                            await self.post_connect(candidate)
                        except Exception:
                            logger.exception("post_connect hook raised")
                    logger.info(
                        "IB connected via %s clientId=%s",
                        label, cfg.client_id,
                    )
                    return candidate
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Connect to %s:%s failed: %s", cfg.host, port, exc,
                    )
                    try:
                        candidate.disconnect()
                    except Exception:
                        pass

            assert last_exc is not None
            raise last_exc

    async def disconnect(self) -> None:
        async with self._lock:
            if self._ib is None:
                return
            try:
                if self._ib.isConnected():
                    self._ib.disconnect()
            except Exception as exc:
                logger.warning("disconnect failed: %s", exc)
            self._ib = None
