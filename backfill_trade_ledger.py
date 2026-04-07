"""
AGT Equities — IBKR Activity Statement → trade_ledger Backfill
================================================================
Parses consolidated IBKR Activity Statement CSVs (one file covers
all accounts under the advisor umbrella) and populates:
  - trade_ledger:      every option/stock trade, classified
  - dividend_ledger:   every dividend payment
  - nav_snapshots:     per-account NAV + deposits/withdrawals
  - deposit_ledger:    every deposit/withdrawal event

Usage:
    python backfill_trade_ledger.py <path_to_ibkr_csv> [--db agt_desk.db]

The parser detects account boundaries in the consolidated CSV
(each account gets its own repeated set of sections) and tags
every row with the correct account_id and household_id.
"""

import argparse
import csv
import logging
import re
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ── Account → Household mapping ──────────────────────────────────
HOUSEHOLD_MAP = {
    "U21971297": "Yash_Household",   # Individual
    "U22076184": "Yash_Household",   # Trad IRA
    "U22076329": "Yash_Household",   # Roth IRA
    "U22388499": "Vikram_Household", # Vikram IND
}

ACCOUNT_ALIAS = {
    "U21971297": "Individual",
    "U22076184": "Trad",
    "U22076329": "Roth",
    "U22388499": "Vikram IND",
}

# SPX box spreads excluded from all performance calcs (Rule 10)
EXCLUDED_SYMBOLS = {"SPX"}


# ── Schema ────────────────────────────────────────────────────────
def init_dashboard_tables(conn: sqlite3.Connection):
    """Create dashboard-specific tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            household_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            trade_datetime TEXT,
            symbol TEXT NOT NULL,
            underlying TEXT,
            asset_category TEXT NOT NULL,
            trade_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            proceeds REAL NOT NULL,
            realized_pnl REAL DEFAULT 0,
            commission REAL DEFAULT 0,
            return_category TEXT NOT NULL,
            source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, symbol, trade_datetime, quantity, price)
        );

        CREATE INDEX IF NOT EXISTS idx_trade_ledger_date
            ON trade_ledger(trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_ledger_account
            ON trade_ledger(account_id, trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_ledger_category
            ON trade_ledger(return_category, trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_ledger_underlying
            ON trade_ledger(underlying, trade_date);

        CREATE TABLE IF NOT EXISTS dividend_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            household_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            amount REAL NOT NULL,
            div_date TEXT NOT NULL,
            description TEXT,
            source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, symbol, div_date, amount)
        );

        CREATE TABLE IF NOT EXISTS nav_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            household_id TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            nav_total REAL,
            nav_cash REAL,
            nav_stock REAL,
            nav_options REAL,
            net_deposits REAL DEFAULT 0,
            mwr_pct REAL,
            twr_pct REAL,
            source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS deposit_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            household_id TEXT NOT NULL,
            dep_date TEXT NOT NULL,
            amount REAL NOT NULL,
            dep_type TEXT,
            description TEXT,
            source TEXT DEFAULT 'CSV',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, dep_date, amount, description)
        );
    """)
    conn.commit()
    log.info("Dashboard tables initialized.")


# ── CSV Parser: split consolidated statement into per-account blocks ──
def split_by_account(filepath: str) -> list[dict]:
    """
    Parse an IBKR consolidated Activity Statement CSV.
    Returns a list of dicts, one per account, each containing:
      {account_id, alias, sections: {section_name: [rows]}}
    """
    accounts_info = []
    all_rows = []

    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        all_rows = list(reader)

    # Pass 1: find Account Information blocks to get account order
    account_order = []
    for r in all_rows:
        if (len(r) >= 4
                and r[0] == "Account Information"
                and r[1] == "Data"
                and r[2] == "Account"):
            account_order.append(r[3].strip())

    if not account_order:
        log.error("No accounts found in CSV.")
        return []

    log.info("Accounts found: %s", account_order)

    # Pass 2: find Statement Period boundaries (each account starts with one)
    boundaries = []
    for i, r in enumerate(all_rows):
        if (len(r) >= 4
                and r[0].replace("\ufeff", "") == "Statement"
                and r[1] == "Data"
                and r[2] == "Period"):
            boundaries.append(i)

    # In a consolidated statement, some accounts share a single statement
    # header while sections repeat. Use a different approach: track which
    # sections repeat and assign them to accounts in order.
    #
    # Strategy: find all "Trades,Header" lines and map each cluster to
    # the next account in sequence.

    # Pass 3: build per-account section map using section repetition
    # Each account's data is bracketed between consecutive
    # "Account Information,Header" markers.
    acct_header_lines = []
    for i, r in enumerate(all_rows):
        if (len(r) >= 3
                and r[0] == "Account Information"
                and r[1] == "Header"):
            acct_header_lines.append(i)

    # Add end-of-file as final boundary
    acct_header_lines.append(len(all_rows))

    results = []
    for idx in range(len(account_order)):
        start = acct_header_lines[idx]
        end = acct_header_lines[idx + 1] if idx + 1 < len(acct_header_lines) else len(all_rows)
        block = all_rows[start:end]

        acct_id = account_order[idx]
        sections = {}
        for r in block:
            if len(r) >= 2:
                section_name = r[0].strip()
                if section_name not in sections:
                    sections[section_name] = []
                sections[section_name].append(r)

        results.append({
            "account_id": acct_id,
            "alias": ACCOUNT_ALIAS.get(acct_id, acct_id),
            "household_id": HOUSEHOLD_MAP.get(acct_id, "Unknown"),
            "sections": sections,
        })

    return results


# ── Trade classifier ─────────────────────────────────────────────
def parse_option_symbol(symbol: str) -> dict:
    """
    Parse IBKR option symbol like 'ADBE 05DEC25 310 P' or 'META 17APR26 260 C'
    Returns {underlying, expiry, strike, right} or None.
    """
    m = re.match(
        r"^(\w+)\s+(\d{2}[A-Z]{3}\d{2})\s+([\d.]+)\s+([PC])$",
        symbol.strip(),
    )
    if not m:
        return None
    return {
        "underlying": m.group(1),
        "expiry": m.group(2),
        "strike": float(m.group(3)),
        "right": m.group(4),
    }


def classify_trade(asset_cat: str, symbol: str, quantity: float) -> tuple[str, str, str | None]:
    """
    Returns (trade_type, return_category, underlying).
    trade_type:      STO_PUT, BTC_PUT, STO_CALL, BTC_CALL, STOCK_BUY, STOCK_SELL
    return_category: PREMIUM or CAPITAL_GAIN
    underlying:      ticker for options, same as symbol for stocks
    """
    if "Option" in asset_cat:
        parsed = parse_option_symbol(symbol)
        underlying = parsed["underlying"] if parsed else symbol.split()[0]
        right = parsed["right"] if parsed else ("P" if " P" in symbol else "C")

        if quantity < 0:  # SELL
            trade_type = "STO_PUT" if right == "P" else "STO_CALL"
        else:  # BUY
            trade_type = "BTC_PUT" if right == "P" else "BTC_CALL"

        return trade_type, "PREMIUM", underlying

    else:  # Stocks
        trade_type = "STOCK_BUY" if quantity > 0 else "STOCK_SELL"
        return trade_type, "CAPITAL_GAIN", symbol.strip()


def parse_trade_datetime(dt_str: str) -> tuple[str, str]:
    """
    Parse '2025-10-24, 16:20:00' → ('2025-10-24', '2025-10-24 16:20:00')
    """
    dt_str = dt_str.strip().strip('"')
    parts = dt_str.split(",")
    date_part = parts[0].strip()
    time_part = parts[1].strip() if len(parts) > 1 else "00:00:00"
    return date_part, f"{date_part} {time_part}"


# ── Extraction functions ──────────────────────────────────────────
def extract_trades(acct: dict) -> list[dict]:
    """Extract and classify all trades for one account."""
    rows = acct["sections"].get("Trades", [])
    trades = []

    for r in rows:
        if len(r) < 14 or r[1] != "Data" or r[2] != "Order":
            continue

        asset_cat = r[3].strip()
        symbol = r[5].strip()
        qty_str = r[7].strip()
        price_str = r[8].strip()
        proceeds_str = r[10].strip()
        comm_str = r[11].strip()
        realized_pnl_str = r[13].strip()
        dt_str = r[6].strip()

        try:
            quantity = float(qty_str)
            price = float(price_str)
            proceeds = float(proceeds_str)
            commission = float(comm_str)
            realized_pnl = float(realized_pnl_str)
        except (ValueError, IndexError):
            continue

        # Skip SPX box spread trades
        underlying_check = symbol.split()[0] if " " in symbol else symbol
        if underlying_check in EXCLUDED_SYMBOLS:
            continue

        trade_type, return_cat, underlying = classify_trade(asset_cat, symbol, quantity)
        trade_date, trade_datetime = parse_trade_datetime(dt_str)

        trades.append({
            "account_id": acct["account_id"],
            "household_id": acct["household_id"],
            "trade_date": trade_date,
            "trade_datetime": trade_datetime,
            "symbol": symbol,
            "underlying": underlying,
            "asset_category": asset_cat,
            "trade_type": trade_type,
            "quantity": quantity,
            "price": price,
            "proceeds": proceeds,
            "realized_pnl": realized_pnl,
            "commission": commission,
            "return_category": return_cat,
        })

    return trades


def extract_dividends(acct: dict) -> list[dict]:
    """Extract dividend records for one account."""
    rows = acct["sections"].get("Dividends", [])
    divs = []
    for r in rows:
        # Format: Dividends,Data,USD,date,description,amount
        if len(r) < 6 or r[1] != "Data" or r[3].strip() == "":
            continue
        if r[2].strip() == "Total":
            continue
        try:
            amount = float(r[5].strip())
        except (ValueError, IndexError):
            continue

        desc = r[4].strip()
        ticker_match = re.match(r"^(\w+)\(", desc)
        ticker = ticker_match.group(1) if ticker_match else "UNKNOWN"

        divs.append({
            "account_id": acct["account_id"],
            "household_id": acct["household_id"],
            "symbol": ticker,
            "amount": amount,
            "div_date": r[3].strip(),
            "description": desc,
        })
    return divs


def extract_deposits(acct: dict) -> list[dict]:
    """Extract deposit/withdrawal records for one account."""
    rows = acct["sections"].get("Deposits & Withdrawals", [])
    deps = []
    for r in rows:
        # Format: Deposits & Withdrawals,Data,USD,date,description,amount
        if len(r) < 6 or r[1] != "Data":
            continue
        if r[2].strip() == "Total" or r[3].strip() == "":
            continue
        try:
            amount = float(r[5].strip())
        except (ValueError, IndexError):
            continue
        deps.append({
            "account_id": acct["account_id"],
            "household_id": acct["household_id"],
            "dep_date": r[3].strip(),
            "amount": amount,
            "dep_type": "DEPOSIT" if amount > 0 else "WITHDRAWAL",
            "description": r[4].strip() if len(r) > 4 else "",
        })
    return deps


def extract_nav(acct: dict) -> dict:
    """Extract NAV and Change in NAV for one account."""
    nav_data = {"account_id": acct["account_id"], "household_id": acct["household_id"]}

    # NAV in Base — format: section,Data,AssetClass,PriorTotal,CurLong,CurShort,CurTotal,Change
    nav_rows = acct["sections"].get("Net Asset Value", [])
    for r in nav_rows:
        if r[1] == "Data":
            # TWR line: ['Net Asset Value', 'Data', '-23.769551373%']
            if len(r) >= 3 and "%" in r[2] and len(r) < 5:
                try:
                    pct_str = r[2].strip().replace("%", "")
                    nav_data["twr_pct"] = float(pct_str)
                except ValueError:
                    pass
                continue
            if len(r) >= 7:
                field = r[2].strip().lower()
                try:
                    val = float(r[6].strip())  # Current Total column
                except (ValueError, IndexError):
                    continue
                if "cash" in field:
                    nav_data["nav_cash"] = val
                elif field == "stock":
                    nav_data["nav_stock"] = val
                elif field == "options":
                    nav_data["nav_options"] = val
                elif field == "total":
                    nav_data["nav_total"] = val

    # Change in NAV — format: section,Data,FieldName,Value
    change_rows = acct["sections"].get("Change in NAV", [])
    for r in change_rows:
        if len(r) >= 4 and r[1] == "Data":
            field = r[2].strip()
            try:
                val = float(r[3].strip())
            except (ValueError, IndexError):
                continue
            if field == "Deposits & Withdrawals":
                nav_data["net_deposits"] = val
            elif field == "Ending Value":
                nav_data["ending_value"] = val
            elif field == "Starting Value":
                nav_data["starting_value"] = val

    # Account Summary for MWR % — format: ...,AccountID,Alias,...,StartingValue,EndingValue,ReturnPct
    summary_rows = acct["sections"].get("Account Summary", [])
    for r in summary_rows:
        if len(r) >= 9 and r[1] == "Data" and r[3].strip() == acct["account_id"]:
            try:
                pct_str = r[8].strip().replace("%", "")
                nav_data["mwr_pct"] = float(pct_str)
            except (ValueError, IndexError):
                pass

    return nav_data


# ── Insert functions ──────────────────────────────────────────────
def insert_trades(conn: sqlite3.Connection, trades: list[dict]) -> int:
    """Insert trades, skip duplicates. Returns count inserted."""
    inserted = 0
    for t in trades:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO trade_ledger
                    (account_id, household_id, trade_date, trade_datetime,
                     symbol, underlying, asset_category, trade_type,
                     quantity, price, proceeds, realized_pnl, commission,
                     return_category, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CSV')
            """, (
                t["account_id"], t["household_id"], t["trade_date"],
                t["trade_datetime"], t["symbol"], t["underlying"],
                t["asset_category"], t["trade_type"], t["quantity"],
                t["price"], t["proceeds"], t["realized_pnl"],
                t["commission"], t["return_category"],
            ))
            if conn.total_changes:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def insert_dividends(conn: sqlite3.Connection, divs: list[dict]) -> int:
    inserted = 0
    for d in divs:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO dividend_ledger
                    (account_id, household_id, symbol, amount, div_date,
                     description, source)
                VALUES (?, ?, ?, ?, ?, ?, 'CSV')
            """, (
                d["account_id"], d["household_id"], d["symbol"],
                d["amount"], d["div_date"], d["description"],
            ))
            inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def insert_deposits(conn: sqlite3.Connection, deps: list[dict]) -> int:
    inserted = 0
    for d in deps:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO deposit_ledger
                    (account_id, household_id, dep_date, amount,
                     dep_type, description, source)
                VALUES (?, ?, ?, ?, ?, ?, 'CSV')
            """, (
                d["account_id"], d["household_id"], d["dep_date"],
                d["amount"], d["dep_type"], d["description"],
            ))
            inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def insert_nav(conn: sqlite3.Connection, nav: dict, snapshot_date: str) -> None:
    try:
        conn.execute("""
            INSERT OR REPLACE INTO nav_snapshots
                (account_id, household_id, snapshot_date,
                 nav_total, nav_cash, nav_stock, nav_options,
                 net_deposits, mwr_pct, twr_pct, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CSV')
        """, (
            nav["account_id"], nav["household_id"], snapshot_date,
            nav.get("nav_total"), nav.get("nav_cash"),
            nav.get("nav_stock"), nav.get("nav_options"),
            nav.get("net_deposits", 0), nav.get("mwr_pct"),
            nav.get("twr_pct"),
        ))
    except sqlite3.Error as e:
        log.warning("NAV insert failed for %s: %s", nav["account_id"], e)


# ── Validation ────────────────────────────────────────────────────
def validate(conn: sqlite3.Connection):
    """Print summary stats for validation against known totals."""
    print("\n" + "=" * 70)
    print("VALIDATION REPORT")
    print("=" * 70)

    for acct_id, alias in ACCOUNT_ALIAS.items():
        print(f"\n── {alias} ({acct_id}) ──")

        # Stock capital gains
        row = conn.execute("""
            SELECT COALESCE(SUM(realized_pnl), 0)
            FROM trade_ledger
            WHERE account_id = ? AND return_category = 'CAPITAL_GAIN'
        """, (acct_id,)).fetchone()
        cap_gains = row[0]

        # Net option premium (proceeds from all option trades)
        row = conn.execute("""
            SELECT COALESCE(SUM(realized_pnl), 0)
            FROM trade_ledger
            WHERE account_id = ? AND return_category = 'PREMIUM'
        """, (acct_id,)).fetchone()
        net_premium = row[0]

        # Trade count
        row = conn.execute("""
            SELECT COUNT(*)
            FROM trade_ledger
            WHERE account_id = ?
        """, (acct_id,)).fetchone()
        trade_count = row[0]

        # Dividends
        row = conn.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM dividend_ledger
            WHERE account_id = ?
        """, (acct_id,)).fetchone()
        divs = row[0]

        total = cap_gains + net_premium + divs
        print(f"  Trades:         {trade_count:>6}")
        print(f"  Capital Gains:  ${cap_gains:>12,.2f}")
        print(f"  Net Premium:    ${net_premium:>12,.2f}")
        print(f"  Dividends:      ${divs:>12,.2f}")
        print(f"  TOTAL RETURN:   ${total:>12,.2f}")

    # Household totals
    print(f"\n── HOUSEHOLD TOTALS ──")
    for hh in ("Yash_Household", "Vikram_Household"):
        row = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN return_category='CAPITAL_GAIN'
                             THEN realized_pnl ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN return_category='PREMIUM'
                             THEN realized_pnl ELSE 0 END), 0)
            FROM trade_ledger WHERE household_id = ?
        """, (hh,)).fetchone()
        div_row = conn.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM dividend_ledger WHERE household_id = ?
        """, (hh,)).fetchone()
        cg, prem, dv = row[0], row[1], div_row[0]
        print(f"  {hh}: Cap ${cg:,.2f} + Prem ${prem:,.2f} + Div ${dv:,.2f} = ${cg+prem+dv:,.2f}")

    # Time breakdowns
    print(f"\n── TIME BREAKDOWN (All Accounts) ──")
    for label, where in [
        ("2025", "trade_date BETWEEN '2025-01-01' AND '2025-12-31'"),
        ("2026 YTD", "trade_date >= '2026-01-01'"),
    ]:
        row = conn.execute(f"""
            SELECT
                COALESCE(SUM(CASE WHEN return_category='CAPITAL_GAIN'
                             THEN realized_pnl ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN return_category='PREMIUM'
                             THEN realized_pnl ELSE 0 END), 0)
            FROM trade_ledger WHERE {where}
        """).fetchone()
        div_row = conn.execute(f"""
            SELECT COALESCE(SUM(amount), 0)
            FROM dividend_ledger WHERE div_date >= '{'2025-01-01' if '2025' in label else '2026-01-01'}'
                AND div_date <= '{'2025-12-31' if '2025' in label else '2026-12-31'}'
        """).fetchone()
        cg, prem, dv = row[0], row[1], div_row[0]
        print(f"  {label}: Cap ${cg:,.2f} + Prem ${prem:,.2f} + Div ${dv:,.2f} = ${cg+prem+dv:,.2f}")


# ── Main ──────────────────────────────────────────────────────────
def backfill(csv_path: str, db_path: str):
    """Main backfill entry point."""
    log.info("Parsing: %s", csv_path)
    log.info("Database: %s", db_path)

    accounts = split_by_account(csv_path)
    if not accounts:
        log.error("No accounts parsed. Aborting.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_dashboard_tables(conn)

    total_trades = 0
    total_divs = 0
    total_deps = 0

    for acct in accounts:
        acct_id = acct["account_id"]
        alias = acct["alias"]
        log.info("Processing %s (%s)...", alias, acct_id)

        # Trades
        trades = extract_trades(acct)
        n = insert_trades(conn, trades)
        total_trades += len(trades)
        log.info("  Trades: %d parsed, %d new", len(trades), n)

        # Dividends
        divs = extract_dividends(acct)
        n = insert_dividends(conn, divs)
        total_divs += len(divs)
        log.info("  Dividends: %d parsed", len(divs))

        # Deposits
        deps = extract_deposits(acct)
        n = insert_deposits(conn, deps)
        total_deps += len(deps)
        log.info("  Deposits/Withdrawals: %d parsed", len(deps))

        # NAV snapshot (use statement end date, fallback to latest trade date)
        nav = extract_nav(acct)
        # Extract period end date from Statement section
        stmt_rows = acct["sections"].get("Statement", [])
        snapshot_date = None
        for r in stmt_rows:
            if len(r) >= 4 and r[2] == "Period":
                period = r[3].strip()
                # "September 16, 2025 - April 2, 2026"
                parts = period.split(" - ")
                if len(parts) == 2:
                    try:
                        end_dt = datetime.strptime(parts[1].strip(), "%B %d, %Y")
                        snapshot_date = end_dt.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

        # Fallback: use latest trade date if no Statement section
        if not snapshot_date and trades:
            snapshot_date = max(t["trade_date"] for t in trades)

        # Second fallback: use today
        if not snapshot_date:
            snapshot_date = datetime.now().strftime("%Y-%m-%d")

        if nav.get("nav_total") is not None:
            insert_nav(conn, nav, snapshot_date)
            log.info("  NAV: $%.2f | TWR: %s | Deposits: $%.2f",
                     nav.get("nav_total", 0),
                     f"{nav.get('twr_pct', 'N/A')}%",
                     nav.get("net_deposits", 0))

    conn.commit()
    log.info("\nBackfill complete: %d trades, %d dividends, %d deposits",
             total_trades, total_divs, total_deps)

    validate(conn)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill trade_ledger from IBKR CSV")
    parser.add_argument("csv_path", help="Path to IBKR Activity Statement CSV")
    parser.add_argument("--db", default="agt_desk.db", help="SQLite database path")
    args = parser.parse_args()

    backfill(args.csv_path, args.db)
