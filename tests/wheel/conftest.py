"""tests/wheel/conftest.py — paper-port test harness fixtures (WHEEL-6).

These fixtures back the @pytest.mark.paper tests. They establish a single
module-scoped connection to IB Gateway (4002) → TWS (7497) paper with
clientId=99 so a live harness run doesn't collide with the bot (clientId=1)
or the scheduler (clientId=2). If no gateway is reachable, every paper test
in the module skips with a single clean reason.

NOT collected by CI — .gitlab-ci.yml's sprint_a_unit_tests script uses an
explicit file list that excludes tests/wheel/. Operator-run only:
    pytest -m paper tests/wheel/
Paper tests intentionally stay OUT of `pytest -m sprint_a` and the full
CI baseline.
"""
from __future__ import annotations

import asyncio
import os
from typing import Iterator

import pytest

from agt_equities.ib_conn import IBConnConfig, IBConnector


PAPER_HARNESS_CLIENT_ID = int(os.environ.get("AGT_PAPER_HARNESS_CLIENT_ID", "99"))
PAPER_HARNESS_HOST = os.environ.get("AGT_PAPER_HARNESS_HOST", "127.0.0.1")


def pytest_configure(config: pytest.Config) -> None:
    """Register the `paper` marker local to this subtree."""
    config.addinivalue_line(
        "markers",
        "paper: test requires a live IBKR paper Gateway/TWS on 4002/7497. "
        "Skips if no gateway reachable. Not collected by CI.",
    )


@pytest.fixture(scope="module")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Module-scoped loop so the IB connection survives across tests.

    Each test function would otherwise get a fresh loop under
    pytest-asyncio defaults, tearing down the IB socket between tests.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        try:
            loop.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def ib_paper(event_loop: asyncio.AbstractEventLoop):
    """Live paper-gateway connection. Skips cleanly if unreachable.

    Uses clientId=99 to avoid colliding with bot (1) or scheduler (2).
    request_positions_on_connect=False — we don't want the harness
    triggering position-reconciliation side effects on the paper acct.
    """
    cfg = IBConnConfig(
        host=PAPER_HARNESS_HOST,
        client_id=PAPER_HARNESS_CLIENT_ID,
        gateway_port=4002,
        tws_fallback_port=7497,
        connect_timeout=5.0,
        post_connect_sleep_s=0.5,
        request_positions_on_connect=False,
        market_data_type=4,  # delayed-frozen — paper often lacks RT entitlements
    )
    conn = IBConnector(config=cfg)
    try:
        ib = event_loop.run_until_complete(conn.ensure_connected())
    except Exception as exc:
        pytest.skip(f"paper gateway unreachable on {PAPER_HARNESS_HOST}:4002/7497: {exc}")
    try:
        yield ib
    finally:
        try:
            event_loop.run_until_complete(conn.disconnect())
        except Exception:
            pass
