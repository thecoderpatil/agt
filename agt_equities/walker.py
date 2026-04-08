"""
agt_equities/walker.py — Equities Master Log Walker.

Pure function that derives wheel cycles from a chronologically sorted
event stream. Zero I/O, fully unit-testable, no database or ib_async
dependencies.

Reference: REFACTOR_SPEC_v3.md section 5.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WalkerWarning dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkerWarning:
    """Structured warning emitted during walk_cycles()."""
    code: str                                     # e.g. COUNTER_GUARD, UNKNOWN_ACCT, ORPHAN_TRANSFER, EXCLUDED_SKIP
    severity: Literal["INFO", "WARN", "ERROR"]
    ticker: str | None
    household: str | None
    account: str | None
    message: str
    context: dict = field(default_factory=dict)

# ---------------------------------------------------------------------------
# Household mapping (account → household)
# ---------------------------------------------------------------------------

HOUSEHOLD_MAP: dict[str, str] = {
    "U21971297": "Yash_Household",      # Yash Individual
    "U22076329": "Yash_Household",      # Yash Roth IRA
    # U22076184 (Trad IRA) dormant — retained for Walker historical reconstruction of pre-dormancy trades in master_log_trades
    "U22076184": "Yash_Household",      # Yash Trad IRA
    "U22388499": "Vikram_Household",    # Vikram Individual
}


def household_for(account_id: str) -> str:
    try:
        return HOUSEHOLD_MAP[account_id]
    except KeyError:
        raise ValueError(
            f"Unknown account_id {account_id!r} — add to HOUSEHOLD_MAP"
        )


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class UnknownEventError(Exception):
    """Raised when classify_event encounters a trade row it cannot map
    to any known EventType. The walker must fail closed — never silently
    skip an event."""
    pass


# ---------------------------------------------------------------------------
# TradeEvent (immutable input record)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeEvent:
    """One normalised trade / corporate-action / carry-in row."""
    source:              str          # 'FLEX_TRADE', 'FLEX_CORP_ACTION', 'INCEPTION_CARRYIN'
    account_id:          str
    household_id:        str
    ticker:              str
    trade_date:          str          # YYYYMMDD
    date_time:           str          # YYYYMMDD;HHMMSS
    ib_order_id:         int | None
    transaction_id:      str | None
    asset_category:      str          # 'STK', 'OPT'
    right:               str | None   # 'P', 'C', or None
    strike:              float | None
    expiry:              str | None   # YYYYMMDD
    buy_sell:            str          # 'BUY', 'SELL'
    open_close:          str | None   # 'O', 'C', or None
    quantity:            float        # always positive
    trade_price:         float
    net_cash:            float        # signed
    fifo_pnl_realized:   float        # signed
    transaction_type:    str          # 'ExchTrade', 'BookTrade', 'CorpAction', 'InceptionCarryin'
    notes:               str
    currency:            str          # MUST be 'USD'
    raw:                 dict         # full source row


# ---------------------------------------------------------------------------
# EventType enum (16 canonical event categories)
# ---------------------------------------------------------------------------

class EventType(Enum):
    CSP_OPEN            = 'csp_open'
    CSP_CLOSE           = 'csp_close'
    CC_OPEN             = 'cc_open'
    CC_CLOSE            = 'cc_close'
    LONG_OPT_OPEN       = 'long_opt_open'
    LONG_OPT_CLOSE      = 'long_opt_close'
    STK_BUY_DIRECT      = 'stk_buy_direct'
    STK_SELL_DIRECT     = 'stk_sell_direct'
    ASSIGN_STK_LEG      = 'assign_stk_leg'
    ASSIGN_OPT_LEG      = 'assign_opt_leg'
    EXPIRE_WORTHLESS    = 'expire_worthless'
    EXERCISE_STK_LEG    = 'exercise_stk_leg'
    EXERCISE_OPT_LEG    = 'exercise_opt_leg'
    CORP_ACTION         = 'corp_action'
    CARRYIN_STK         = 'carryin_stk'
    CARRYIN_OPT         = 'carryin_opt'
    TRANSFER_OUT        = 'transfer_out'
    TRANSFER_IN         = 'transfer_in'


# ---------------------------------------------------------------------------
# Cycle (mutable state accumulated during the walk)
# ---------------------------------------------------------------------------

@dataclass
class Cycle:
    """A single strategic wheel cycle for one ticker within one household."""
    household_id:        str
    ticker:              str
    cycle_seq:           int
    status:              str               # 'ACTIVE' or 'CLOSED'
    cycle_type:          str               # 'WHEEL' or 'SATELLITE'
    opened_at:           str
    closed_at:           str | None
    shares_held:         float
    open_short_puts:     int
    open_short_calls:    int
    open_long_puts:      int
    open_long_calls:     int
    _paper_basis_by_account: dict          # {account_id: (total_cost, total_shares)}
    premium_total:       float
    stock_cash_flow:     float
    realized_pnl:        float
    events:              list              # list[TradeEvent]
    event_types:         list              # list[EventType]

    @property
    def open_short_options(self) -> int:
        """Total open short options (puts + calls). Read-only compat property."""
        return self.open_short_puts + self.open_short_calls

    @property
    def paper_basis(self) -> float | None:
        """Household-aggregate paper_basis (weighted avg across accounts).

        Backward-compatible property. For per-account comparison with IBKR
        costBasisPrice, use paper_basis_for_account().
        """
        total_cost = sum(c for c, _ in self._paper_basis_by_account.values())
        total_shares = sum(s for _, s in self._paper_basis_by_account.values())
        if total_shares <= 0:
            return None
        return total_cost / total_shares

    def paper_basis_for_account(self, account_id: str) -> float | None:
        """Per-account IRS cost basis. Should match IBKR costBasisPrice."""
        try:
            cost, shares = self._paper_basis_by_account[account_id]
        except KeyError:
            return self.paper_basis  # fallback to aggregate
        if shares <= 0:
            return None
        return cost / shares

    @property
    def adjusted_basis(self) -> float | None:
        """Strategy basis: paper_basis reduced by total OPT premium per share.

        Intentionally differs from IBKR's costBasisPrice (IRS wash-sale rules).
        """
        if self.shares_held <= 0 or self.paper_basis is None:
            return None
        return self.paper_basis - (self.premium_total / self.shares_held)


# ---------------------------------------------------------------------------
# classify_event — pure dispatch
# ---------------------------------------------------------------------------

def classify_event(ev: TradeEvent) -> EventType:
    """Map a TradeEvent to its canonical EventType.

    Raises UnknownEventError on any unmapped event. Never silently skips.
    """
    if ev.currency != 'USD':
        raise UnknownEventError(f"Non-USD event: {ev.currency} on {ev.ticker}")

    if ev.source == 'INCEPTION_CARRYIN':
        return EventType.CARRYIN_STK if ev.asset_category == 'STK' else EventType.CARRYIN_OPT

    if ev.source == 'FLEX_CORP_ACTION':
        return EventType.CORP_ACTION

    if ev.source == 'FLEX_TRANSFER':
        if ev.open_close == 'OUT':
            return EventType.TRANSFER_OUT
        else:
            return EventType.TRANSFER_IN

    if ev.transaction_type == 'BookTrade':
        if ev.notes == 'A':
            return EventType.ASSIGN_STK_LEG if ev.asset_category == 'STK' else EventType.ASSIGN_OPT_LEG
        elif ev.notes == 'Ep':
            if ev.asset_category != 'OPT':
                raise UnknownEventError(f"Unexpected Ep on non-OPT: {ev}")
            return EventType.EXPIRE_WORTHLESS
        elif ev.notes == 'Ex':
            return EventType.EXERCISE_STK_LEG if ev.asset_category == 'STK' else EventType.EXERCISE_OPT_LEG
        else:
            raise UnknownEventError(
                f"Unmapped BookTrade notes: {ev.notes!r} on {ev.ticker} "
                f"(tid={ev.transaction_id}, dt={ev.date_time})"
            )

    elif ev.transaction_type == 'ExchTrade':
        if ev.asset_category == 'STK':
            return EventType.STK_BUY_DIRECT if ev.buy_sell == 'BUY' else EventType.STK_SELL_DIRECT
        elif ev.asset_category == 'OPT':
            if ev.buy_sell == 'SELL' and ev.open_close == 'O':
                return EventType.CSP_OPEN if ev.right == 'P' else EventType.CC_OPEN
            elif ev.buy_sell == 'BUY' and ev.open_close == 'C':
                return EventType.CSP_CLOSE if ev.right == 'P' else EventType.CC_CLOSE
            elif ev.buy_sell == 'BUY' and ev.open_close == 'O':
                return EventType.LONG_OPT_OPEN
            elif ev.buy_sell == 'SELL' and ev.open_close == 'C':
                return EventType.LONG_OPT_CLOSE
            else:
                raise UnknownEventError(
                    f"Unclassifiable ExchTrade OPT: {ev.buy_sell}/{ev.open_close} "
                    f"on {ev.ticker} (tid={ev.transaction_id})"
                )
        else:
            raise UnknownEventError(f"Unknown asset_category: {ev.asset_category}")

    else:
        raise UnknownEventError(f"Unknown transaction_type: {ev.transaction_type}")


# ---------------------------------------------------------------------------
# canonical_sort_key
# ---------------------------------------------------------------------------

def canonical_sort_key(ev: TradeEvent) -> tuple:
    """Sort key for resolving same-timestamp ambiguities."""
    if ev.transaction_type == 'BookTrade':
        if ev.asset_category == 'OPT':
            leg_priority = 0
        else:
            leg_priority = 1
    elif ev.source == 'INCEPTION_CARRYIN':
        leg_priority = 0
    else:
        if ev.asset_category == 'OPT' and ev.open_close == 'C':
            leg_priority = 0
        elif ev.asset_category == 'STK':
            leg_priority = 1
        else:
            leg_priority = 2
    return (ev.date_time, leg_priority, ev.ib_order_id or 0, ev.transaction_id or '')


# ---------------------------------------------------------------------------
# walk_cycles — the core state machine
# ---------------------------------------------------------------------------

def _new_cycle(
    household_id: str, ticker: str, seq: int, opened_at: str,
    cycle_type: str = 'WHEEL',
) -> Cycle:
    return Cycle(
        household_id=household_id,
        ticker=ticker,
        cycle_seq=seq,
        status='ACTIVE',
        cycle_type=cycle_type,
        opened_at=opened_at,
        closed_at=None,
        shares_held=0.0,
        open_short_puts=0,
        open_short_calls=0,
        open_long_puts=0,
        open_long_calls=0,
        _paper_basis_by_account={},
        premium_total=0.0,
        stock_cash_flow=0.0,
        realized_pnl=0.0,
        events=[],
        event_types=[],
    )


def _update_paper_basis(
    cycle: Cycle, delta_shares: float, price_per_share: float, account_id: str,
) -> None:
    """Per-account weighted average update for stock acquisitions."""
    if delta_shares <= 0:
        return
    old_cost, old_shares = cycle._paper_basis_by_account.get(account_id, (0.0, 0.0))
    new_cost = old_cost + (price_per_share * delta_shares)
    new_shares = old_shares + delta_shares
    cycle._paper_basis_by_account[account_id] = (new_cost, new_shares)


# Module-level warnings buffer (populated per walk_cycles call)
_walker_warnings: list[WalkerWarning] = []


def _guard_decrement(current: int | float, delta: int | float,
                     counter_name: str, ev) -> int | float:
    """Guard a counter decrement: emit warning and clamp to 0 if it would go negative."""
    result = current - delta
    if result < 0:
        _walker_warnings.append(WalkerWarning(
            code="COUNTER_GUARD",
            severity="WARN",
            ticker=ev.ticker,
            household=getattr(ev, 'household_id', None),
            account=ev.account_id,
            message=(f"Non-negative guard: {counter_name} would go to {result} "
                     f"(current={current}, delta={delta}) on {ev.ticker} "
                     f"{ev.date_time} tid={ev.transaction_id}"),
            context={"counter": counter_name, "current": current, "delta": delta,
                     "transaction_id": ev.transaction_id},
        ))
        return 0
    return result


def _reduce_paper_basis(cycle: Cycle, delta_shares: float, account_id: str) -> None:
    """Reduce per-account shares on stock sell (preserves per-share basis)."""
    old_cost, old_shares = cycle._paper_basis_by_account.get(account_id, (0.0, 0.0))
    if old_shares > 0:
        per_share = old_cost / old_shares
        new_shares = old_shares - delta_shares
        new_cost = per_share * max(new_shares, 0)
        cycle._paper_basis_by_account[account_id] = (new_cost, max(new_shares, 0))


def _apply_event(cycle: Cycle, ev: TradeEvent, et: EventType) -> None:
    """Apply a single classified event to a cycle's running state."""
    cycle.events.append(ev)
    cycle.event_types.append(et)
    cycle.realized_pnl += ev.fifo_pnl_realized

    if et == EventType.CSP_OPEN:
        cycle.open_short_puts += int(ev.quantity)
        cycle.premium_total += ev.net_cash

    elif et == EventType.CSP_CLOSE:
        cycle.open_short_puts = _guard_decrement(cycle.open_short_puts, int(ev.quantity), 'open_short_puts', ev)
        cycle.premium_total += ev.net_cash

    elif et == EventType.CC_OPEN:
        cycle.open_short_calls += int(ev.quantity)
        cycle.premium_total += ev.net_cash

    elif et == EventType.CC_CLOSE:
        cycle.open_short_calls = _guard_decrement(cycle.open_short_calls, int(ev.quantity), 'open_short_calls', ev)
        cycle.premium_total += ev.net_cash

    elif et == EventType.LONG_OPT_OPEN:
        if ev.right == 'C':
            cycle.open_long_calls += int(ev.quantity)
        else:
            cycle.open_long_puts += int(ev.quantity)
        cycle.premium_total += ev.net_cash

    elif et == EventType.LONG_OPT_CLOSE:
        if ev.right == 'C':
            cycle.open_long_calls = _guard_decrement(cycle.open_long_calls, int(ev.quantity), 'open_long_calls', ev)
        else:
            cycle.open_long_puts = _guard_decrement(cycle.open_long_puts, int(ev.quantity), 'open_long_puts', ev)
        cycle.premium_total += ev.net_cash

    elif et == EventType.EXPIRE_WORTHLESS:
        # Determine if this is a SHORT or LONG option expiring.
        # Only decrement the short counter if the originating open was short.
        is_long = False
        try:
            for prev_ev, prev_et in zip(reversed(cycle.events[:-1]),
                                         reversed(cycle.event_types[:-1])):
                if (prev_ev.account_id == ev.account_id
                        and prev_ev.strike == ev.strike
                        and prev_ev.expiry == ev.expiry
                        and prev_ev.right == ev.right):
                    if prev_et == EventType.LONG_OPT_OPEN:
                        is_long = True
                    break  # found the most recent matching event
        except Exception:
            pass  # fallback: treat as short (conservative)

        if is_long:
            if ev.right == 'C':
                cycle.open_long_calls = _guard_decrement(cycle.open_long_calls, int(ev.quantity), 'open_long_calls', ev)
            else:
                cycle.open_long_puts = _guard_decrement(cycle.open_long_puts, int(ev.quantity), 'open_long_puts', ev)
        else:
            if ev.right == 'C':
                cycle.open_short_calls = _guard_decrement(cycle.open_short_calls, int(ev.quantity), 'open_short_calls', ev)
            else:
                cycle.open_short_puts = _guard_decrement(cycle.open_short_puts, int(ev.quantity), 'open_short_puts', ev)
        cycle.premium_total += ev.net_cash

    elif et == EventType.ASSIGN_OPT_LEG:
        if ev.right == 'C':
            cycle.open_short_calls = _guard_decrement(cycle.open_short_calls, int(ev.quantity), 'open_short_calls', ev)
        else:
            cycle.open_short_puts = _guard_decrement(cycle.open_short_puts, int(ev.quantity), 'open_short_puts', ev)
        cycle.premium_total += ev.net_cash

    elif et == EventType.ASSIGN_STK_LEG:
        delta = ev.quantity
        if ev.buy_sell == 'BUY':
            # IRS paper_basis: strike minus assigned-put premium per share
            irs_basis = ev.trade_price  # fallback: raw strike
            try:
                # Find the ASSIGN_OPT_LEG matching by date+account+right+strike.
                # Stock trade_price == put strike for put assignments.
                matched_opt = None
                for prev_ev, prev_et in zip(
                    reversed(cycle.events[:-1]), reversed(cycle.event_types[:-1])
                ):
                    if (prev_et == EventType.ASSIGN_OPT_LEG
                            and prev_ev.trade_date == ev.trade_date
                            and prev_ev.account_id == ev.account_id
                            and prev_ev.right == 'P'
                            and prev_ev.strike == ev.trade_price):
                        matched_opt = prev_ev
                        break

                # Fallback: match without strike (for edge cases)
                if matched_opt is None:
                    for prev_ev, prev_et in zip(
                        reversed(cycle.events[:-1]), reversed(cycle.event_types[:-1])
                    ):
                        if (prev_et == EventType.ASSIGN_OPT_LEG
                                and prev_ev.trade_date == ev.trade_date
                                and prev_ev.account_id == ev.account_id
                                and prev_ev.right == 'P'):
                            matched_opt = prev_ev
                            logger.warning(
                                "Walker: ASSIGN_STK_LEG strike-match fallback for "
                                "%s/%s/%s/strike=%s (matched opt strike=%s)",
                                ev.ticker, ev.account_id, ev.trade_date,
                                ev.trade_price, prev_ev.strike,
                            )
                            break

                if matched_opt is not None:
                    # Find originating CSP_OPEN for this put
                    best_open = None
                    for o_ev, o_et in zip(cycle.events, cycle.event_types):
                        if (o_et == EventType.CSP_OPEN
                                and o_ev.account_id == ev.account_id
                                and o_ev.strike == matched_opt.strike
                                and o_ev.expiry == matched_opt.expiry):
                            best_open = o_ev
                    if best_open is not None:
                        put_prem_per_share = best_open.net_cash / (best_open.quantity * 100.0)
                        irs_basis = ev.trade_price - put_prem_per_share
            except Exception:
                pass  # fallback to raw strike on any error
            _update_paper_basis(cycle, delta, irs_basis, ev.account_id)
            cycle.shares_held += delta
        else:
            # Called away (short call assigned): SELL stock
            _reduce_paper_basis(cycle, delta, ev.account_id)
            cycle.shares_held = _guard_decrement(cycle.shares_held, delta, 'shares_held', ev)
        cycle.stock_cash_flow += ev.net_cash

    elif et == EventType.STK_BUY_DIRECT:
        _update_paper_basis(cycle, ev.quantity, ev.trade_price, ev.account_id)
        cycle.shares_held += ev.quantity
        cycle.stock_cash_flow += ev.net_cash

    elif et == EventType.STK_SELL_DIRECT:
        _reduce_paper_basis(cycle, ev.quantity, ev.account_id)
        cycle.shares_held = _guard_decrement(cycle.shares_held, ev.quantity, 'shares_held', ev)
        cycle.stock_cash_flow += ev.net_cash

    elif et == EventType.EXERCISE_STK_LEG:
        if ev.buy_sell == 'BUY':
            _update_paper_basis(cycle, ev.quantity, ev.trade_price, ev.account_id)
            cycle.shares_held += ev.quantity
        else:
            _reduce_paper_basis(cycle, ev.quantity, ev.account_id)
            cycle.shares_held = _guard_decrement(cycle.shares_held, ev.quantity, 'shares_held', ev)
        cycle.stock_cash_flow += ev.net_cash

    elif et == EventType.EXERCISE_OPT_LEG:
        if ev.right == 'C':
            cycle.open_short_calls = _guard_decrement(cycle.open_short_calls, int(ev.quantity), 'open_short_calls', ev)
        else:
            cycle.open_short_puts = _guard_decrement(cycle.open_short_puts, int(ev.quantity), 'open_short_puts', ev)
        cycle.premium_total += ev.net_cash

    elif et == EventType.CARRYIN_STK:
        cycle.shares_held += ev.quantity
        cycle._paper_basis_by_account[ev.account_id] = (
            ev.trade_price * ev.quantity, ev.quantity
        )

    elif et == EventType.CARRYIN_OPT:
        if ev.right == 'C':
            cycle.open_short_calls += int(ev.quantity)
        else:
            cycle.open_short_puts += int(ev.quantity)

    elif et == EventType.CORP_ACTION:
        # Dispatch on the corp action type stored in ev.notes or ev.raw.get('type')
        ca_type = (ev.raw.get('type') or ev.notes or '').upper().strip()
        try:
            if ca_type in ('FS', 'RS'):
                # Forward split (FS) or reverse split (RS)
                # ev.quantity = new shares received (FS) or shares removed (RS)
                # Adjust shares and per-share basis
                if ev.quantity != 0 and cycle.shares_held > 0:
                    ratio = (cycle.shares_held + ev.quantity) / cycle.shares_held
                    cycle.shares_held += ev.quantity
                    # Adjust per-account basis: total cost unchanged, shares change
                    for acct, (cost, shares) in list(cycle._paper_basis_by_account.items()):
                        if shares > 0:
                            new_shares = shares * ratio
                            cycle._paper_basis_by_account[acct] = (cost, new_shares)
            elif ca_type in ('TC', 'IC'):
                # Ticker change (TC) or CUSIP change (IC) — no economic effect
                # The cycle's ticker is already set; IBKR may send new conid
                pass
            elif ca_type == 'SD':
                # Special dividend (return of capital) — reduce basis
                if ev.net_cash is not None and cycle.shares_held > 0:
                    per_share = abs(float(ev.net_cash)) / cycle.shares_held
                    for acct, (cost, shares) in list(cycle._paper_basis_by_account.items()):
                        if shares > 0:
                            cycle._paper_basis_by_account[acct] = (cost - per_share * shares, shares)
            elif ca_type in ('SO', 'DW'):
                # Spinoff (SO) or demerger (DW) — complex, log warning
                logger.warning("Walker: corp action %s on %s — spinoff handling TBD", ca_type, ev.ticker)
            elif ca_type in ('CM', 'TM'):
                # Cash merger (CM) or tender offer merger (TM)
                proceeds = ev.raw.get('proceeds') or ev.net_cash
                if proceeds:
                    cycle.stock_cash_flow += float(proceeds)
                cycle.shares_held = 0  # position closed by merger
            else:
                logger.warning("Walker: unhandled corp action type %r on %s", ca_type, ev.ticker)
        except Exception as exc:
            logger.warning("Walker: corp action handler error for %s: %s", ev.ticker, exc)

    elif et == EventType.TRANSFER_OUT:
        # Intra-household transfer OUT: position moves to another account in
        # the same household. Since the Walker groups by (household, ticker),
        # the cycle's aggregate position is unchanged — the option/stock just
        # moves between account lanes. Only update per-account paper_basis
        # tracking for STK transfers.
        if ev.asset_category == 'STK':
            _reduce_paper_basis(cycle, ev.quantity, ev.account_id)
            cycle.shares_held = _guard_decrement(cycle.shares_held, ev.quantity, 'shares_held', ev)

    elif et == EventType.TRANSFER_IN:
        # Intra-household transfer IN: position arrives from another account.
        if ev.asset_category == 'STK':
            _update_paper_basis(cycle, ev.quantity, ev.trade_price, ev.account_id)
            cycle.shares_held += ev.quantity


def walk_cycles(
    events: list[TradeEvent],
    excluded_tickers: set[str] = frozenset({'SPX', 'VIX', 'NDX', 'RUT', 'XSP'}),
) -> list[Cycle]:
    """Derive wheel cycles from a chronologically sorted event stream.

    Preconditions:
    - All events share the same household_id and ticker.
    - Events are sorted by canonical_sort_key.
    - ticker not in excluded_tickers.

    Returns a list of Cycle objects. At most one has status='ACTIVE'.
    """
    # Clear module-level warnings buffer for this walk
    global _walker_warnings
    _walker_warnings = []

    if not events:
        return []

    hh = events[0].household_id
    tk = events[0].ticker

    # W3.5: Input validation — all events must share household and ticker
    for i, ev in enumerate(events):
        if ev.household_id != hh:
            raise ValueError(
                f"Mixed household in walk_cycles: event[{i}] has "
                f"household_id={ev.household_id!r}, expected {hh!r} "
                f"(tid={ev.transaction_id})"
            )
        if ev.ticker != tk:
            raise ValueError(
                f"Mixed ticker in walk_cycles: event[{i}] has "
                f"ticker={ev.ticker!r}, expected {tk!r} "
                f"(tid={ev.transaction_id})"
            )
        if ev.account_id not in HOUSEHOLD_MAP:
            _walker_warnings.append(WalkerWarning(
                code="UNKNOWN_ACCT",
                severity="ERROR",
                ticker=tk,
                household=hh,
                account=ev.account_id,
                message=(f"Unknown account_id {ev.account_id!r} in event[{i}] "
                         f"for {tk} (tid={ev.transaction_id})"),
                context={"event_index": i, "transaction_id": ev.transaction_id},
            ))
        elif HOUSEHOLD_MAP[ev.account_id] != hh:
            raise ValueError(
                f"Cross-household account in walk_cycles: event[{i}] "
                f"account_id={ev.account_id!r} maps to "
                f"{HOUSEHOLD_MAP[ev.account_id]!r}, expected {hh!r}"
            )

    if tk in excluded_tickers:
        _walker_warnings.append(WalkerWarning(
            code="EXCLUDED_SKIP",
            severity="INFO",
            ticker=tk,
            household=hh,
            account=None,
            message=f"Excluded ticker {tk} skipped ({len(events)} events)",
            context={"event_count": len(events)},
        ))
        return []

    sorted_events = sorted(events, key=canonical_sort_key)

    cycles: list[Cycle] = []
    current: Cycle | None = None
    seq = 0

    from itertools import groupby
    days = groupby(sorted_events, key=lambda e: e.trade_date)

    for trade_date, day_events_iter in days:
        day_events = list(day_events_iter)

        for ev in day_events:
            et = classify_event(ev)

            # Promote satellite to wheel if a position-holding event arrives
            if (current is not None
                    and current.cycle_type == 'SATELLITE'
                    and et in (EventType.CSP_OPEN, EventType.CC_OPEN,
                               EventType.ASSIGN_STK_LEG, EventType.ASSIGN_OPT_LEG,
                               EventType.STK_BUY_DIRECT, EventType.CARRYIN_STK,
                               EventType.CARRYIN_OPT)):
                current.cycle_type = 'WHEEL'

            # Cycle origination
            if current is None:
                if et == EventType.CSP_OPEN:
                    seq += 1
                    current = _new_cycle(hh, tk, seq, ev.trade_date, 'WHEEL')
                elif et == EventType.CARRYIN_OPT:
                    seq += 1
                    current = _new_cycle(hh, tk, seq, ev.trade_date, 'WHEEL')
                elif et == EventType.CARRYIN_STK:
                    seq += 1
                    current = _new_cycle(hh, tk, seq, ev.trade_date, 'WHEEL')
                elif et == EventType.TRANSFER_IN:
                    # Per Codex I13: TRANSFER_IN must NOT originate a cycle.
                    # Emit warning and skip — the position should have been
                    # received into an existing cycle or handled by carry-in.
                    _walker_warnings.append(WalkerWarning(
                        code="ORPHAN_TRANSFER",
                        severity="WARN",
                        ticker=tk,
                        household=hh,
                        account=ev.account_id,
                        message=(f"Orphan TRANSFER_IN for {tk} on {ev.trade_date} "
                                 f"(acct={ev.account_id}, tid={ev.transaction_id}). "
                                 f"Position not attached to any cycle."),
                        context={"trade_date": ev.trade_date, "transaction_id": ev.transaction_id},
                    ))
                    continue
                elif et in (EventType.LONG_OPT_OPEN, EventType.LONG_OPT_CLOSE):
                    # Satellite: long-option-only activity outside a wheel cycle
                    seq += 1
                    current = _new_cycle(hh, tk, seq, ev.trade_date, 'SATELLITE')
                elif et == EventType.EXPIRE_WORTHLESS:
                    # Could be a long option expiring outside a cycle
                    # (e.g., bought pre-window, expiring in-window)
                    seq += 1
                    current = _new_cycle(hh, tk, seq, ev.trade_date, 'SATELLITE')
                else:
                    raise UnknownEventError(
                        f"ORPHAN_EVENT: {et.value} for {tk} on {ev.trade_date} "
                        f"with no active cycle (tid={ev.transaction_id})"
                    )

            _apply_event(current, ev, et)

        # EOD closure check — ALL position counters must be zero
        if current is not None:
            all_flat = (
                current.shares_held == 0
                and current.open_short_puts == 0
                and current.open_short_calls == 0
                and current.open_long_puts == 0
                and current.open_long_calls == 0
            )
            if all_flat:
                current.status = 'CLOSED'
                current.closed_at = trade_date
                cycles.append(current)
                current = None

    if current is not None:
        cycles.append(current)

    # Log any warnings emitted during this walk
    if _walker_warnings:
        for w in _walker_warnings:
            logger.warning("Walker [%s/%s]: %s", w.code, w.severity, w.message)

    return cycles


def get_walker_warnings() -> list[WalkerWarning]:
    """Return warnings from the most recent walk_cycles() call."""
    return list(_walker_warnings)


# ---------------------------------------------------------------------------
# Walk-away P&L — pure computation for Rule 8 Dynamic Exit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkAwayResult:
    """Result of walk-away P&L computation for a candidate exit."""
    walk_away_pnl_per_share: float   # positive = profitable, negative = loss
    walk_away_pnl_total: float       # per_share * 100 * contracts (or * shares)
    adjusted_cost_basis: float
    is_profitable: bool              # P&L >= 0


def compute_walk_away_pnl(
    adjusted_cost_basis: float,
    proposed_exit_strike: float,
    proposed_exit_premium: float,
    quantity: int,
    multiplier: int = 100,
) -> WalkAwayResult:
    """Pure function. Computes walk-away P&L for a candidate exit.

    Walk-Away P&L per Share = Strike + Premium - Adjusted Cost Basis

    Per v10 Rule 8: "Walk-Away P&L > 0: profitable exit. Walk-Away P&L < 0:
    capital liberation trade."

    Walker stays pure: no I/O, no DB writes, no side effects.

    Args:
        adjusted_cost_basis: paper_basis - (accumulated_premium / shares)
        proposed_exit_strike: option strike price
        proposed_exit_premium: option premium (mid or bid)
        quantity: number of contracts (for CC) or shares (for STK_SELL)
        multiplier: 100 for options, 1 for stock
    """
    pnl_per_share = proposed_exit_strike + proposed_exit_premium - adjusted_cost_basis
    pnl_total = pnl_per_share * multiplier * quantity
    return WalkAwayResult(
        walk_away_pnl_per_share=round(pnl_per_share, 4),
        walk_away_pnl_total=round(pnl_total, 2),
        adjusted_cost_basis=adjusted_cost_basis,
        is_profitable=pnl_per_share >= 0,
    )
