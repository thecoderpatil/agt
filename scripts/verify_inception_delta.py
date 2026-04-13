#!/usr/bin/env python3
"""
Sprint-1.4 verification harness for inception_delta pipeline.

Inspects the most recent CC fill in fill_log and verifies all
five sprint-1.4 conditions:
  1. Real fill (not test data)
  2. inception_delta IS NOT NULL
  3. inception_delta in [0.15, 0.40]
  4. Matching pending_orders.payload contains same value
  5. Matching cc_cycle_log row exists

Usage:
  python scripts/verify_inception_delta.py
  python scripts/verify_inception_delta.py --tail   # follow mode
  python scripts/verify_inception_delta.py --exec-id <id>

Exit codes:
  0 — verification passed
  1 — verification failed (one or more conditions not met)
  2 — no eligible fill found
"""

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

INCEPTION_DELTA_MIN = 0.15
INCEPTION_DELTA_MAX = 0.40


def fetch_latest_cc_fill(conn, exec_id=None):
    """Fetch the most recent SELL_CALL fill from fill_log."""
    if exec_id:
        cursor = conn.execute(
            "SELECT exec_id, ticker, action, quantity, price, "
            "premium_delta, account_id, household_id, "
            "inception_delta "
            "FROM fill_log WHERE exec_id = ?",
            (exec_id,),
        )
    else:
        cursor = conn.execute(
            "SELECT exec_id, ticker, action, quantity, price, "
            "premium_delta, account_id, household_id, "
            "inception_delta "
            "FROM fill_log "
            "WHERE action = 'SELL_CALL' "
            "ORDER BY rowid DESC LIMIT 1"
        )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "exec_id": row[0],
        "ticker": row[1],
        "action": row[2],
        "quantity": row[3],
        "price": row[4],
        "premium_delta": row[5],
        "account_id": row[6],
        "household_id": row[7],
        "inception_delta": row[8],
    }


def fetch_matching_pending_order(conn, ticker):
    """Fetch the most recent pending_orders row matching the ticker,
    with parsed payload. Used for condition 4."""
    cursor = conn.execute(
        "SELECT id, payload, ib_perm_id "
        "FROM pending_orders "
        "WHERE payload LIKE ? "
        "ORDER BY id DESC LIMIT 1",
        (f'%"ticker": "{ticker}"%',),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except json.JSONDecodeError:
        return {"id": row[0], "payload": None, "ib_perm_id": row[2]}
    return {"id": row[0], "payload": payload, "ib_perm_id": row[2]}


def fetch_matching_cc_cycle_log(conn, ticker, household):
    """Fetch the most recent cc_cycle_log row for the
    (ticker, household) pair. Used for condition 5."""
    cursor = conn.execute(
        "SELECT ticker, household, strike, expiry, mode, flag "
        "FROM cc_cycle_log "
        "WHERE ticker = ? AND household = ? "
        "ORDER BY rowid DESC LIMIT 1",
        (ticker, household),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "ticker": row[0],
        "household": row[1],
        "strike": row[2],
        "expiry": row[3],
        "mode": row[4],
        "flag": row[5],
    }


def verify_fill(conn, fill):
    """Run all five verification conditions against a fill row.
    Returns (passed: bool, results: list of (condition, status, detail))."""
    results = []

    # Condition 1: real fill (heuristic — exec_id starts with
    # IBKR's normal prefix, not a synthetic test prefix)
    is_real = (
        fill["exec_id"]
        and not fill["exec_id"].startswith("test_")
        and not fill["exec_id"].startswith("synth_")
    )
    results.append((
        "1. Real fill (not test data)",
        "PASS" if is_real else "FAIL",
        f"exec_id={fill['exec_id']}",
    ))

    # Condition 2: inception_delta IS NOT NULL
    has_value = fill["inception_delta"] is not None
    results.append((
        "2. inception_delta IS NOT NULL",
        "PASS" if has_value else "FAIL",
        f"inception_delta={fill['inception_delta']}",
    ))

    # Condition 3: inception_delta in expected range
    in_range = (
        has_value
        and INCEPTION_DELTA_MIN <= fill["inception_delta"] <= INCEPTION_DELTA_MAX
    )
    results.append((
        "3. inception_delta in [0.15, 0.40]",
        "PASS" if in_range else "FAIL",
        f"value={fill['inception_delta']} expected [{INCEPTION_DELTA_MIN}, {INCEPTION_DELTA_MAX}]",
    ))

    # Condition 4: matching pending_orders.payload has same value
    pending = fetch_matching_pending_order(conn, fill["ticker"])
    if pending and pending["payload"]:
        payload_value = pending["payload"].get("inception_delta")
        matches = (
            payload_value is not None
            and has_value
            and abs(payload_value - fill["inception_delta"]) < 1e-9
        )
        results.append((
            "4. pending_orders.payload matches",
            "PASS" if matches else "FAIL",
            f"payload_value={payload_value} fill_value={fill['inception_delta']}",
        ))
    else:
        results.append((
            "4. pending_orders.payload matches",
            "FAIL",
            "no matching pending_orders row found",
        ))

    # Condition 5: matching cc_cycle_log row exists
    cycle = fetch_matching_cc_cycle_log(
        conn, fill["ticker"], fill["household_id"]
    )
    results.append((
        "5. cc_cycle_log row exists",
        "PASS" if cycle is not None else "FAIL",
        f"row={cycle}" if cycle else "no row found",
    ))

    passed = all(status == "PASS" for _, status, _ in results)
    return passed, results


def print_report(fill, passed, results):
    print("=" * 70)
    print("INCEPTION_DELTA VERIFICATION REPORT - Sprint 1.4")
    print("=" * 70)
    print(f"Fill: {fill['ticker']} {fill['action']} "
          f"qty={fill['quantity']} price={fill['price']}")
    print(f"Account: {fill['account_id']} "
          f"Household: {fill['household_id']}")
    print(f"Exec ID: {fill['exec_id']}")
    print("-" * 70)
    for condition, status, detail in results:
        marker = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {marker} {condition}")
        print(f"         {detail}")
    print("-" * 70)
    overall = "VERIFIED" if passed else "FAILED"
    print(f"OVERALL: {overall}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Sprint-1.4 inception_delta verification harness"
    )
    parser.add_argument("--tail", action="store_true",
                        help="Follow mode: poll for new fills")
    parser.add_argument("--exec-id", type=str, default=None,
                        help="Verify a specific exec_id")
    parser.add_argument("--db", type=str, default="agt_desk.db",
                        help="Path to AGT SQLite DB")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(2)

    if args.tail:
        print(f"Tail mode: polling {db_path} for new CC fills...")
        last_seen_exec_id = None
        try:
            while True:
                with closing(sqlite3.connect(str(db_path))) as conn:
                    fill = fetch_latest_cc_fill(conn)
                    if fill and fill["exec_id"] != last_seen_exec_id:
                        passed, results = verify_fill(conn, fill)
                        print_report(fill, passed, results)
                        last_seen_exec_id = fill["exec_id"]
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nTail mode stopped.")
            sys.exit(0)

    with closing(sqlite3.connect(str(db_path))) as conn:
        fill = fetch_latest_cc_fill(conn, exec_id=args.exec_id)
        if fill is None:
            print("No CC fills found in fill_log.", file=sys.stderr)
            sys.exit(2)
        passed, results = verify_fill(conn, fill)
        print_report(fill, passed, results)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
