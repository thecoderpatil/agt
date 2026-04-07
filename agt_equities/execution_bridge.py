"""
agt_equities.execution_bridge — Live fill capture for intraday overlay.

Subscribes to ib_async execution events and:
1. Writes fill metadata to bot_order_log
2. Buffers TradeEvent representations for the overlay path

Replaces legacy fill handlers that write to fill_log / premium_ledger /
cc_cycle_log. See REFACTOR_SPEC_v3.md section 8.
"""
from typing import Optional

from .walker import TradeEvent


class ExecutionBridge:
    """
    Live fill capture bridge between ib_async and the Walker overlay path.

    Phase 3 deliverable — stub only for Phase 0.
    """

    def __init__(self, ib=None):
        """
        Initialize with an ib_async IB instance.
        Subscribes to execDetailsEvent and commissionReportEvent.
        """
        self.ib = ib
        self.intraday_events: list[TradeEvent] = []

    def get_intraday_events(
        self, household: str, ticker: str, since: str
    ) -> list[TradeEvent]:
        """Filter buffered events by (household, ticker) and time."""
        raise NotImplementedError("Phase 3 deliverable")

    def clear_intraday(self, before_date: str) -> None:
        """Clear buffer entries older than before_date."""
        raise NotImplementedError("Phase 3 deliverable")
