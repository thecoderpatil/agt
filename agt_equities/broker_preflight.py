"""Broker identity pre-flight gate (MR 4b).

Verifies that the IB Gateway the bot connected to matches AGT_BROKER_MODE.
IBKR account IDs start with 'U' for live accounts and 'D' for demo/paper
accounts. A mismatch indicates a misconfigured gateway port or wrong
AGT_BROKER_MODE setting -- both are critical operational errors.

Called once in post_init() after ensure_ib_connected() succeeds.
Raises BrokerIdentityMismatch (a RuntimeError subclass) on mismatch.
The post_init() hook converts this to SystemExit(1) -- fail-closed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# IBKR account ID prefix by mode.
# D = demo/simulation (paper gateway, port 4002)
# U = live/production (live gateway, port 4001)
_EXPECTED_PREFIX: dict[str, str] = {
    "paper": "D",
    "live":  "U",
}


class BrokerIdentityMismatch(RuntimeError):
    """Connected IB accounts don't match AGT_BROKER_MODE declaration.

    Safety invariant: if we think we're on paper but IB gives live accounts
    (or vice versa), the bot must halt. Routing live-capital commands to
    paper accounts silently destroys fill tracking; routing paper commands
    to live accounts places real orders.
    """


async def run_broker_identity_preflight(ib_conn, broker_mode: str) -> None:
    """Verify connected IB accounts prefix matches AGT_BROKER_MODE.

    Two checks, in order:
      1. Static: ACTIVE_ACCOUNTS from config vs expected prefix.
         Catches misconfigured env var without requiring IB round-trip.
      2. Dynamic: accounts IB actually returned on this connection.
         Catches gateway port mismatch (4001 vs 4002) even if config is correct.

    Args:
        ib_conn: Connected ib_async.IB client.
        broker_mode: "paper" | "live" -- from os.environ["AGT_BROKER_MODE"].

    Raises:
        BrokerIdentityMismatch: on any prefix mismatch (static or dynamic).
        Does NOT raise on unknown broker_mode (logs warning, skips check).
    """
    expected_prefix = _EXPECTED_PREFIX.get(broker_mode)
    if expected_prefix is None:
        logger.warning(
            "broker_preflight: unknown broker_mode=%r -- skipping check",
            broker_mode,
        )
        return

    # -- Static check: configured ACTIVE_ACCOUNTS --
    # E-H-5 fix: do NOT downgrade a config-import failure to a logged
    # warning. This static gate exists to catch broker/account drift
    # WITHOUT requiring an IB round-trip; if the import that backs it
    # fails, the gate does not exist and we fail-closed by halting boot.
    try:
        from agt_equities.config import ACTIVE_ACCOUNTS
    except Exception as exc:
        raise BrokerIdentityMismatch(
            f"broker_preflight: agt_equities.config import failed: {exc!r}. "
            f"Cannot verify ACTIVE_ACCOUNTS against AGT_BROKER_MODE="
            f"{broker_mode!r}. Boot halted to avoid the safety net silently "
            f"downgrading to ib-side-only checking."
        ) from exc
    mismatched_config = [
        a for a in ACTIVE_ACCOUNTS if not a.startswith(expected_prefix)
    ]
    if mismatched_config:
        raise BrokerIdentityMismatch(
            f"AGT_BROKER_MODE={broker_mode!r} expects account prefix "
            f"'{expected_prefix}' but ACTIVE_ACCOUNTS contains "
            f"{mismatched_config}. Check config.py or env var."
        )

    # -- Dynamic check: accounts IB returned on this connection --
    try:
        ib_accounts_fn = getattr(ib_conn, "accounts", None)
        if ib_accounts_fn is None:
            logger.debug("broker_preflight: ib_conn.accounts() not available -- dynamic check skipped")
            return

        connected = ib_accounts_fn()  # synchronous in ib_async
        if not connected:
            logger.debug("broker_preflight: ib_conn.accounts() returned empty -- dynamic check skipped")
            return

        mismatched_live = [a for a in connected if not a.startswith(expected_prefix)]
        if mismatched_live:
            raise BrokerIdentityMismatch(
                f"AGT_BROKER_MODE={broker_mode!r} expects account prefix "
                f"'{expected_prefix}' but IB returned accounts {mismatched_live}. "
                f"Gateway port mismatch? (paper=4002, live=4001)"
            )

        logger.info(
            "broker_preflight: OK -- mode=%r accounts=%s",
            broker_mode, connected,
        )

    except BrokerIdentityMismatch:
        raise
    except Exception as exc:
        logger.warning("broker_preflight: dynamic IB check failed (non-fatal): %s", exc)
