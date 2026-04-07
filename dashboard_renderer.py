"""
AGT Equities — Dashboard Renderer
===================================
Generates polished dark-theme dashboard images from trade_ledger data.
Designed to be called from the Telegram bot's /dashboard command.

Outputs 2 PNG images:
  1. Account Performance Card — return $ and % by period
  2. Active Positions Grid — per-ticker detail with adjusted basis
"""

import logging
import sqlite3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import FancyBboxPatch
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict

_logger = logging.getLogger(__name__)

# ── Theme ─────────────────────────────────────────────────────────
BG_COLOR      = "#0d1117"
CARD_BG       = "#161b22"
HEADER_BG     = "#1c2333"
TEXT_COLOR     = "#e6edf3"
TEXT_DIM       = "#7d8590"
GREEN          = "#3fb950"
RED            = "#f85149"
AMBER          = "#d29922"
CYAN           = "#58a6ff"
PURPLE         = "#bc8cff"
BORDER         = "#30363d"
WHITE          = "#ffffff"

ACCOUNT_ALIAS = {
    "U21971297": "Individual",
    "U22076184": "Trad",
    "U22076329": "Roth",
    "U22388499": "Vikram IND",
}

HOUSEHOLD_MAP = {
    "U21971297": "Yash_Household",
    "U22076184": "Yash_Household",
    "U22076329": "Yash_Household",
    "U22388499": "Vikram_Household",
}

# Accounts to show on dashboard (skip Trad — negligible)
DISPLAY_ACCOUNTS = ["U21971297", "U22076329", "U22388499"]


def _get_period_bounds() -> dict:
    """Return date boundaries for each period."""
    today = date.today()
    return {
        "Today": (today.isoformat(), today.isoformat()),
        "YTD": (f"{today.year}-01-01", today.isoformat()),
        "2025": ("2025-01-01", "2025-12-31"),
        "Inception": ("2025-09-01", today.isoformat()),
    }


def _query_returns(conn: sqlite3.Connection, account_id: str,
                   start: str, end: str, use_master_log: bool = False) -> dict:
    """Query realized returns for an account in a date range."""
    if use_master_log:
        try:
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN asset_category='OPT'
                                 THEN CAST(fifo_pnl_realized AS REAL) ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN asset_category='STK'
                                 THEN CAST(fifo_pnl_realized AS REAL) ELSE 0 END), 0)
                FROM master_log_trades
                WHERE account_id = ? AND trade_date BETWEEN ? AND ?
            """, (account_id, start.replace('-', ''), end.replace('-', ''))).fetchone()

            div_row = conn.execute("""
                SELECT COALESCE(SUM(CAST(amount AS REAL)), 0)
                FROM master_log_statement_of_funds
                WHERE account_id = ? AND activity_code = 'DIV'
                AND date BETWEEN ? AND ?
            """, (account_id, start.replace('-', ''), end.replace('-', ''))).fetchone()

            premium = row[0] if row else 0
            cap_gains = row[1] if row else 0
            dividends = div_row[0] if div_row else 0
            return {
                "premium": round(premium, 2),
                "cap_gains": round(cap_gains, 2),
                "dividends": round(dividends, 2),
                "total": round(premium + cap_gains + dividends, 2),
            }
        except Exception as exc:
            _logger.warning("master_log _query_returns fallback: %s", exc)

    # Legacy
    row = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN return_category='PREMIUM'
                         THEN realized_pnl ELSE 0 END), 0) as premium,
            COALESCE(SUM(CASE WHEN return_category='CAPITAL_GAIN'
                         THEN realized_pnl ELSE 0 END), 0) as cap_gains
        FROM trade_ledger
        WHERE account_id = ? AND trade_date BETWEEN ? AND ?
    """, (account_id, start, end)).fetchone()

    div_row = conn.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM dividend_ledger
        WHERE account_id = ? AND div_date BETWEEN ? AND ?
    """, (account_id, start, end)).fetchone()

    premium = row[0] if row else 0
    cap_gains = row[1] if row else 0
    dividends = div_row[0] if div_row else 0

    return {
        "premium": round(premium, 2),
        "cap_gains": round(cap_gains, 2),
        "dividends": round(dividends, 2),
        "total": round(premium + cap_gains + dividends, 2),
    }


def _query_household_returns(conn: sqlite3.Connection, household_id: str,
                              start: str, end: str, use_master_log: bool = False) -> dict:
    """Query realized returns for an entire household."""
    if use_master_log:
        try:
            accts = [a for a, h in HOUSEHOLD_MAP.items() if h == household_id]
            if accts:
                ph = ','.join('?' * len(accts))
                row = conn.execute(f"""
                    SELECT
                        COALESCE(SUM(CASE WHEN asset_category='OPT'
                                     THEN CAST(fifo_pnl_realized AS REAL) ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN asset_category='STK'
                                     THEN CAST(fifo_pnl_realized AS REAL) ELSE 0 END), 0)
                    FROM master_log_trades
                    WHERE account_id IN ({ph}) AND trade_date BETWEEN ? AND ?
                """, (*accts, start.replace('-', ''), end.replace('-', ''))).fetchone()

                div_row = conn.execute(f"""
                    SELECT COALESCE(SUM(CAST(amount AS REAL)), 0)
                    FROM master_log_statement_of_funds
                    WHERE account_id IN ({ph}) AND activity_code = 'DIV'
                    AND date BETWEEN ? AND ?
                """, (*accts, start.replace('-', ''), end.replace('-', ''))).fetchone()

                p, c, d = row[0], row[1], div_row[0]
                return {
                    "premium": round(p, 2),
                    "cap_gains": round(c, 2),
                    "dividends": round(d, 2),
                    "total": round(p + c + d, 2),
                }
        except Exception as exc:
            _logger.warning("master_log _query_household_returns fallback: %s", exc)

    # Legacy
    row = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN return_category='PREMIUM'
                         THEN realized_pnl ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN return_category='CAPITAL_GAIN'
                         THEN realized_pnl ELSE 0 END), 0)
        FROM trade_ledger
        WHERE household_id = ? AND trade_date BETWEEN ? AND ?
    """, (household_id, start, end)).fetchone()

    div_row = conn.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM dividend_ledger
        WHERE household_id = ? AND div_date BETWEEN ? AND ?
    """, (household_id, start, end)).fetchone()

    p, c, d = row[0], row[1], div_row[0]
    return {
        "premium": round(p, 2),
        "cap_gains": round(c, 2),
        "dividends": round(d, 2),
        "total": round(p + c + d, 2),
    }


def _get_nav_and_deposits(conn: sqlite3.Connection, account_id: str,
                          use_master_log: bool = False) -> dict:
    """Get latest NAV, TWR, and total deposits for return % calc."""
    if use_master_log:
        try:
            nav_row = conn.execute("""
                SELECT CAST(total AS REAL) as nav_total
                FROM master_log_nav
                WHERE account_id = ?
                ORDER BY report_date DESC LIMIT 1
            """, (account_id,)).fetchone()

            # TWR from ChangeInNAV; mwr_pct dropped per Yash decision
            twr_row = conn.execute("""
                SELECT CAST(twr AS REAL), CAST(deposits_withdrawals AS REAL)
                FROM master_log_change_in_nav
                WHERE account_id = ?
            """, (account_id,)).fetchone()

            # Period P&L % = (ending - starting) / starting
            pnl_row = conn.execute("""
                SELECT CAST(starting_value AS REAL), CAST(ending_value AS REAL)
                FROM master_log_change_in_nav
                WHERE account_id = ?
            """, (account_id,)).fetchone()

            nav = nav_row[0] if nav_row else None
            twr = twr_row[0] if twr_row else None
            deposits = twr_row[1] if twr_row else 0

            return {
                "nav": nav,
                "net_deposits": deposits,
                "mwr_pct": None,  # dropped per decision
                "twr_pct": round(twr * 100, 2) if twr else None,
                "total_deposits": deposits,
            }
        except Exception as exc:
            _logger.warning("master_log _get_nav_and_deposits fallback: %s", exc)

    # Legacy
    nav_row = conn.execute("""
        SELECT nav_total, net_deposits, mwr_pct, twr_pct
        FROM nav_snapshots
        WHERE account_id = ?
        ORDER BY snapshot_date DESC LIMIT 1
    """, (account_id,)).fetchone()

    dep_row = conn.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM deposit_ledger
        WHERE account_id = ?
    """, (account_id,)).fetchone()

    return {
        "nav": nav_row[0] if nav_row else None,
        "net_deposits": nav_row[1] if nav_row else (dep_row[0] if dep_row else 0),
        "mwr_pct": nav_row[2] if nav_row and nav_row[2] else None,
        "twr_pct": nav_row[3] if nav_row and nav_row[3] else None,
        "total_deposits": dep_row[0] if dep_row else 0,
    }


def _get_historical_offset(conn, account_id, period_name):
    """Get pre-IBKR historical offset for a period (e.g. '2025')."""
    try:
        row = conn.execute("""
            SELECT premium_offset, capgains_offset, dividend_offset, total_offset
            FROM historical_offsets
            WHERE account_id = ? AND period = ?
        """, (account_id, period_name)).fetchone()
        if row:
            return {
                "premium": float(row[0] or 0),
                "cap_gains": float(row[1] or 0),
                "dividends": float(row[2] or 0),
                "total": float(row[3] or 0),
            }
    except Exception:
        pass
    return {"premium": 0, "cap_gains": 0, "dividends": 0, "total": 0}


def _get_inception_config(conn):
    """Load inception configuration values."""
    config = {}
    try:
        for row in conn.execute("SELECT key, value FROM inception_config"):
            config[row[0]] = float(row[1])
    except Exception:
        pass
    return config


def _get_ibkr_net_external(conn):
    """Sum external IBKR deposits (excluding ACATS transfers and internal moves)."""
    try:
        row = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM deposit_ledger
            WHERE description NOT LIKE '%ACATS%'
            AND description NOT LIKE '%Internal Transfer%'
            AND description NOT LIKE '%ADJUSTMENT%'
        """).fetchone()
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def _fmt_dollar(v: float) -> str:
    """Format dollar value with color hint."""
    if v == 0:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}${abs(v):,.0f}"


def _fmt_pct(v: float) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def _color_for(v: float) -> str:
    if v > 0:
        return GREEN
    elif v < 0:
        return RED
    return TEXT_DIM


# ── Panel 1: Account Performance Card ────────────────────────────
def render_performance_card(conn: sqlite3.Connection, output_path: str,
                            use_master_log: bool = False) -> str:
    """Render the account performance dashboard image (portrait, mobile-first)."""
    periods = _get_period_bounds()
    period_names = list(periods.keys())

    # Gather data — include Trad (U22076184) for offsets even though not displayed
    ALL_ACCOUNTS = DISPLAY_ACCOUNTS + ["U22076184"]

    rows_data = []
    for acct_id in DISPLAY_ACCOUNTS:
        alias = ACCOUNT_ALIAS[acct_id]
        nav_info = _get_nav_and_deposits(conn, acct_id, use_master_log=use_master_log)
        acct_returns = {}
        for pname, (start, end) in periods.items():
            acct_returns[pname] = _query_returns(conn, acct_id, start, end,
                                                  use_master_log=use_master_log)
            # Add Fidelity offsets for 2025 and Inception
            if pname in ("2025", "Inception"):
                offset = _get_historical_offset(conn, acct_id, "2025")
                acct_returns[pname]["premium"] += offset["premium"]
                acct_returns[pname]["cap_gains"] += offset["cap_gains"]
                acct_returns[pname]["dividends"] += offset["dividends"]
                acct_returns[pname]["total"] += offset["total"]
        rows_data.append({
            "alias": alias,
            "account_id": acct_id,
            "nav": nav_info,
            "returns": acct_returns,
        })

    # Household totals (include Trad offsets in household sum)
    hh_returns = {}
    for pname, (start, end) in periods.items():
        yash = _query_household_returns(conn, "Yash_Household", start, end,
                                          use_master_log=use_master_log)
        vik = _query_household_returns(conn, "Vikram_Household", start, end,
                                        use_master_log=use_master_log)
        hh_returns[pname] = {
            "premium": yash["premium"] + vik["premium"],
            "cap_gains": yash["cap_gains"] + vik["cap_gains"],
            "dividends": yash["dividends"] + vik["dividends"],
            "total": yash["total"] + vik["total"],
        }
        # Add Fidelity offsets for all accounts (including Trad)
        if pname in ("2025", "Inception"):
            for acct_id in ALL_ACCOUNTS:
                offset = _get_historical_offset(conn, acct_id, "2025")
                hh_returns[pname]["premium"] += offset["premium"]
                hh_returns[pname]["cap_gains"] += offset["cap_gains"]
                hh_returns[pname]["dividends"] += offset["dividends"]
                hh_returns[pname]["total"] += offset["total"]

    # Total NAV + total deposits for household return %
    total_nav = sum(r["nav"]["nav"] for r in rows_data if r["nav"]["nav"])
    total_deposits = sum(r["nav"].get("net_deposits", 0) for r in rows_data
                         if r["nav"].get("net_deposits"))

    # ── True inception return (capital-base approach) ──
    ic = _get_inception_config(conn)
    starting_capital = ic.get("starting_capital", 0)
    fidelity_remaining = ic.get("fidelity_remaining", 0)
    fidelity_net_external = ic.get("fidelity_net_external", 0)
    ibkr_net_external = _get_ibkr_net_external(conn)
    total_capital_base = starting_capital + fidelity_net_external + ibkr_net_external
    current_value = total_nav + fidelity_remaining
    inception_return_pct = ((current_value - total_capital_base) / total_capital_base * 100
                            if total_capital_base > 0 else None)

    # ── Column x-positions (portrait layout) ──
    col_label = 0.3
    col_vals = {"Today": 4.0, "YTD": 5.8, "2025": 7.6, "Inception": 9.5}

    # ── Draw ──
    fig, ax = plt.subplots(figsize=(8, 14))
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 26)
    ax.axis("off")

    # ── Title block ──
    ax.text(5, 25.3, "AGT EQUITIES \u2014 PERFORMANCE",
            ha="center", va="center", fontsize=18, fontweight="bold",
            color=CYAN, fontfamily="monospace")

    ax.text(5, 24.6,
            f"NAV: ${total_nav:,.0f}  |  {date.today().strftime('%b %d, %Y')}",
            ha="center", va="center", fontsize=11, color=TEXT_DIM,
            fontfamily="monospace")

    # ── Column headers ──
    y = 23.2
    ax.axhline(y=y - 0.3, color=BORDER, linewidth=0.5, xmin=0.02, xmax=0.98)
    ax.text(col_label, y, "ACCOUNT", fontsize=11, fontweight="bold",
            color=TEXT_DIM, fontfamily="monospace", va="center")
    for pname, x in col_vals.items():
        ax.text(x, y, pname, fontsize=11, fontweight="bold",
                color=TEXT_DIM, fontfamily="monospace", va="center", ha="right")

    y = 22.2

    # ── Per-account rows ──
    for row in rows_data:
        alias = row["alias"]
        nav_val = row["nav"]["nav"]
        twr = row["nav"].get("twr_pct")
        dep = row["nav"].get("net_deposits", 0)
        nav_str = f"${nav_val:,.0f}" if nav_val else "\u2014"

        # Inception realized return % for this account
        acct_inception = row["returns"]["Inception"]["total"]
        acct_realized_pct = (acct_inception / dep * 100) if dep and dep > 0 else None

        # Account header bar
        ax.add_patch(FancyBboxPatch((0.1, y - 0.35), 9.8, 0.7,
                                     boxstyle="round,pad=0.05",
                                     facecolor=HEADER_BG, edgecolor=BORDER,
                                     linewidth=0.5))
        ax.text(col_label, y, f"\u25a0 {alias}  ({nav_str})",
                fontsize=12, fontweight="bold", color=WHITE,
                fontfamily="monospace", va="center")

        y -= 0.65

        # Sub-rows: Premium, Cap Gains, Total, Return %
        for label, key, color in [
            ("  Premium", "premium", PURPLE),
            ("  Cap Gains", "cap_gains", CYAN),
            ("  TOTAL", "total", WHITE),
        ]:
            if key == "total":
                ax.axhline(y=y + 0.25, color=BORDER, linewidth=0.3,
                           xmin=0.02, xmax=0.98)

            is_total = key == "total"
            ax.text(col_label, y, label, fontsize=11, color=color,
                    fontfamily="monospace", va="center",
                    fontweight="bold" if is_total else "normal")

            for pname, x in col_vals.items():
                val = row["returns"][pname][key]
                ax.text(x, y, _fmt_dollar(val), fontsize=11,
                        color=_color_for(val) if is_total else color,
                        fontfamily="monospace", va="center", ha="right",
                        fontweight="bold" if is_total else "normal")
            y -= 0.5

        # RETURN row — realized % for sub-periods
        if dep and dep > 0:
            ax.text(col_label, y, "  RETURN", fontsize=11, color=WHITE,
                    fontfamily="monospace", va="center", fontweight="bold")
            for pname, x in col_vals.items():
                period_total = row["returns"][pname]["total"]
                pct = period_total / dep * 100
                ax.text(x, y, _fmt_pct(pct), fontsize=11,
                        color=_color_for(pct),
                        fontfamily="monospace", va="center", ha="right")
            y -= 0.5

        y -= 0.35  # gap

    # ── Household total section ──
    ax.axhline(y=y + 0.15, color=CYAN, linewidth=1.5, xmin=0.02, xmax=0.98)
    y -= 0.15

    ax.add_patch(FancyBboxPatch((0.1, y - 0.35), 9.8, 0.7,
                                 boxstyle="round,pad=0.05",
                                 facecolor="#1a2332", edgecolor=CYAN,
                                 linewidth=1))
    ax.text(col_label, y, f"\u25b2 HOUSEHOLD TOTAL  (${total_nav:,.0f})",
            fontsize=12, fontweight="bold", color=CYAN,
            fontfamily="monospace", va="center")

    y -= 0.65

    for label, key, color in [
        ("  Premium", "premium", PURPLE),
        ("  Cap Gains", "cap_gains", CYAN),
        ("  Dividends", "dividends", GREEN),
        ("  TOTAL", "total", WHITE),
    ]:
        if key == "total":
            ax.axhline(y=y + 0.25, color=BORDER, linewidth=0.3,
                       xmin=0.02, xmax=0.98)

        is_total = key == "total"
        ax.text(col_label, y, label, fontsize=11, color=color,
                fontfamily="monospace", va="center",
                fontweight="bold" if is_total else "normal")

        for pname, x in col_vals.items():
            val = hh_returns[pname][key]
            ax.text(x, y, _fmt_dollar(val), fontsize=11,
                    color=_color_for(val) if is_total else color,
                    fontfamily="monospace", va="center", ha="right",
                    fontweight="bold" if is_total else "normal")
        y -= 0.5

    # Household RETURN row — true inception return for Inception column
    if total_deposits and total_deposits > 0:
        ax.text(col_label, y, "  RETURN", fontsize=11, color=WHITE,
                fontfamily="monospace", va="center", fontweight="bold")
        for pname, x in col_vals.items():
            if pname == "Inception" and inception_return_pct is not None:
                pct = inception_return_pct
            else:
                period_total = hh_returns[pname]["total"]
                pct = period_total / total_deposits * 100
            ax.text(x, y, _fmt_pct(pct), fontsize=11,
                    color=_color_for(pct),
                    fontfamily="monospace", va="center", ha="right")
        y -= 0.5

    plt.tight_layout(pad=0.5)
    fig.savefig(output_path, dpi=150, facecolor=BG_COLOR,
                bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return output_path


# ── Panel 2: Active Positions Grid ────────────────────────────────
def render_positions_grid(conn: sqlite3.Connection,
                          positions: list[dict],
                          output_path: str,
                          use_master_log: bool = False) -> str:
    """
    Render active positions grid.
    `positions` is a list of dicts from ib_async or the trade_ledger,
    each with: ticker, shares, account_id, cost_basis, current_price,
               unrealized_pnl, active_option (str or None)
    """
    if not positions:
        # Fallback: build from trade_ledger aggregate
        positions = _positions_from_ledger(conn)

    n_pos = len(positions)
    if n_pos == 0:
        return None

    fig_width = 8
    row_height = 0.8
    fig_height = max(6, 2.5 + n_pos * row_height)

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, n_pos + 3)
    ax.axis("off")

    # Title
    ax.text(5, n_pos + 2.2, "ACTIVE POSITIONS",
            ha="center", fontsize=16, fontweight="bold",
            color=CYAN, fontfamily="monospace")

    # Column headers (rescaled for x 0-10)
    cols = [
        (0.1, "TICKER", "left"),
        (1.5, "SHARES", "right"),
        (3.0, "BASIS", "right"),
        (4.3, "PREM", "right"),
        (5.7, "ADJ", "right"),
        (7.0, "SPOT", "right"),
        (7.8, "M", "center"),
        (8.8, "P&L", "right"),
        (9.9, "OPTION", "right"),
    ]

    y = n_pos + 1.2
    for x, label, ha in cols:
        ax.text(x, y, label, fontsize=10, fontweight="bold",
                color=TEXT_DIM, fontfamily="monospace", ha=ha, va="center")

    ax.axhline(y=y - 0.3, color=BORDER, linewidth=0.5, xmin=0.01, xmax=0.99)

    # Position rows
    for i, pos in enumerate(sorted(positions, key=lambda p: p.get("ticker", ""))):
        y = n_pos - i + 0.2
        ticker = pos.get("ticker", "?")
        shares = pos.get("shares") or 0
        basis = pos.get("cost_basis") or 0
        premium = pos.get("total_premium") or 0
        adj_basis = basis - (premium / shares) if shares > 0 and premium else basis
        spot = pos.get("current_price") or 0
        unrl = pos.get("unrealized_pnl")
        if unrl is None:
            unrl = (spot - basis) * shares if spot and basis else 0
        option = pos.get("active_option") or "\u2014"

        # Mode
        if spot and adj_basis and spot > 0 and adj_basis > 0 and spot >= adj_basis:
            mode = "M2"
            mode_color = GREEN
        else:
            mode = "M1"
            mode_color = AMBER

        # Alternating row bg
        if i % 2 == 0:
            ax.add_patch(plt.Rectangle((0, y - 0.3), 10, 0.6,
                                        facecolor="#12171e", edgecolor="none"))

        vals = [
            (0.1, ticker, "left", WHITE, True),
            (1.5, f"{int(shares)}", "right", TEXT_COLOR, False),
            (3.0, f"${basis:,.0f}" if basis else "\u2014", "right", TEXT_COLOR, False),
            (4.3, f"${premium:,.0f}" if premium else "\u2014", "right", PURPLE, False),
            (5.7, f"${adj_basis:,.0f}" if adj_basis else "\u2014", "right", CYAN, False),
            (7.0, f"${spot:,.0f}" if spot else "\u2014", "right", TEXT_COLOR, False),
            (7.8, mode, "center", mode_color, True),
            (8.8, _fmt_dollar(unrl), "right", _color_for(unrl), False),
            (9.9, option or "\u2014", "right", TEXT_DIM, False),
        ]

        for x, text, ha, color, bold in vals:
            ax.text(x, y, text, fontsize=10.5, fontfamily="monospace",
                    color=color, ha=ha, va="center",
                    fontweight="bold" if bold else "normal")

    plt.tight_layout(pad=0.5)
    fig.savefig(output_path, dpi=150, facecolor=BG_COLOR,
                bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return output_path


def _positions_from_ledger(conn: sqlite3.Connection) -> list[dict]:
    """Build positions from premium_ledger as fallback."""
    rows = conn.execute("""
        SELECT household_id, ticker, initial_basis, total_premium_collected, shares_owned
        FROM premium_ledger
        WHERE shares_owned > 0
    """).fetchall()

    positions = []
    for r in rows:
        household = r[0]
        # Map household to primary account
        if household == "Yash_Household":
            acct_id = "U21971297"
        elif household == "Vikram_Household":
            acct_id = "U22388499"
        else:
            acct_id = "UNKNOWN"

        positions.append({
            "ticker": r[1],
            "account_id": acct_id,
            "shares": r[4],
            "cost_basis": r[2],
            "total_premium": r[3],
            "current_price": None,  # Will be filled by ib_async
            "unrealized_pnl": None,
            "active_option": None,
        })
    return positions


# ── Convenience: generate all panels ─────────────────────────────
def generate_dashboard(db_path: str, output_dir: str,
                       live_positions: list[dict] | None = None) -> list[str]:
    """
    Generate all dashboard images. Returns list of file paths.
    
    Args:
        db_path: path to agt_desk.db
        output_dir: directory to write PNG files
        live_positions: optional list from ib_async with real-time data
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    paths = []

    # Panel 1: Performance Card
    p1 = str(output_dir / "dashboard_performance.png")
    try:
        render_performance_card(conn, p1)
        paths.append(p1)
    except Exception as e:
        print(f"Error rendering performance card: {e}")

    # Panel 2: Positions Grid
    p2 = str(output_dir / "dashboard_positions.png")
    try:
        render_positions_grid(conn, live_positions or [], p2)
        if p2:
            paths.append(p2)
    except Exception as e:
        print(f"Error rendering positions grid: {e}")

    conn.close()
    return paths


# ── CLI test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "test_dashboard.db"
    paths = generate_dashboard(db, "/tmp/dashboard_output")
    for p in paths:
        print(f"Generated: {p}")
