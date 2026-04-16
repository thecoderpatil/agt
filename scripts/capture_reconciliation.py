#!/usr/bin/env python3
"""
capture_reconciliation.py — Phase 1 Reconciliation Harness

Captures 4 data views at a single point in time and freezes them as JSON
fixtures per (account, ticker) for offline comparison:

  1. Walker active cycles (trade_repo.get_active_cycles)
  2. Walker per-account basis (cycle.paper_basis_for_account / adjusted_basis_for_account)
  3. _discover_positions output (what the bot sees)
  4. _load_premium_ledger_snapshot per-account (ADR-006 path)

Usage (from C:\\AGT_Telegram_Bridge, DB must be accessible):
  python scripts/capture_reconciliation.py

Output: reports/reconciliation_snapshot_<timestamp>.json

This script does NOT require IB connection — it reads from the local
agt_desk.db only (walker cycles, premium_ledger). The _discover_positions
view (which needs IB) is captured separately via a bot command or can be
supplied as an input JSON.

NOTE: This script is read-only. It opens the DB in WAL read-only mode.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("READ_FROM_MASTER_LOG", "1")


def _capture_walker_cycles() -> list[dict]:
    """Capture all active walker cycles with per-account breakdown."""
    from agt_equities import trade_repo
    from agt_equities.config import ACCOUNT_TO_HOUSEHOLD
    # ACCOUNT_LABELS removed from config; fall back to acct_id as label.

    cycles = trade_repo.get_active_cycles()
    results = []

    for c in cycles:
        if c.cycle_type != "WHEEL":
            continue

        cycle_rec = {
            "household_id": c.household_id,
            "ticker": c.ticker,
            "cycle_type": c.cycle_type,
            "shares_held": int(c.shares_held),
            "paper_basis_household": round(c.paper_basis, 4) if c.paper_basis else None,
            "adjusted_basis_household": round(c.adjusted_basis, 4) if c.adjusted_basis else None,
            "premium_total": round(c.premium_total, 4),
            "per_account": {},
        }

        # Per-account breakdown via ADR-006 methods
        if hasattr(c, "_paper_basis_by_account"):
            for acct_id in c._paper_basis_by_account:
                paper = c.paper_basis_for_account(acct_id)
                adj = c.adjusted_basis_for_account(acct_id)
                prem = c.premium_for_account(acct_id) if hasattr(c, "premium_for_account") else None
                basis_data, shares = c._paper_basis_by_account.get(acct_id, (None, 0))

                cycle_rec["per_account"][acct_id] = {
                    "account_label": acct_id,
                    "household": ACCOUNT_TO_HOUSEHOLD.get(acct_id, "unknown"),
                    "shares": int(shares) if shares else 0,
                    "paper_basis": round(paper, 4) if paper is not None else None,
                    "adjusted_basis": round(adj, 4) if adj is not None else None,
                    "premium_collected": round(prem, 4) if prem is not None else None,
                }

        results.append(cycle_rec)

    return results


def _capture_ledger_snapshots() -> list[dict]:
    """Capture _load_premium_ledger_snapshot for every (household, ticker, account) combo."""
    from agt_equities.config import ACCOUNT_TO_HOUSEHOLD, HOUSEHOLD_MAP

    # Import the function from telegram_bot — heavy import but we're a script
    try:
        from telegram_bot import _load_premium_ledger_snapshot
    except ImportError:
        return [{"error": "Could not import _load_premium_ledger_snapshot from telegram_bot"}]

    results = []
    seen = set()

    for hh_name, acct_list in HOUSEHOLD_MAP.items():
        # Get all tickers for this household from walker
        from agt_equities import trade_repo
        cycles = trade_repo.get_active_cycles()
        tickers = {c.ticker for c in cycles if c.household_id == hh_name and c.cycle_type == "WHEEL"}

        for ticker in sorted(tickers):
            # Household-aggregated (legacy path)
            hh_snap = _load_premium_ledger_snapshot(hh_name, ticker, account_id=None)
            results.append({
                "scope": "household_aggregated",
                "household": hh_name,
                "ticker": ticker,
                "account_id": None,
                "snapshot": hh_snap,
            })

            # Per-account (ADR-006 path)
            for acct_id in acct_list:
                key = (acct_id, ticker)
                if key in seen:
                    continue
                seen.add(key)

                acct_snap = _load_premium_ledger_snapshot(hh_name, ticker, account_id=acct_id)
                results.append({
                    "scope": "per_account",
                    "household": hh_name,
                    "ticker": ticker,
                    "account_id": acct_id,
                    "snapshot": acct_snap,
                })

    return results


def _reconcile(walker_cycles: list[dict], ledger_snaps: list[dict]) -> list[dict]:
    """Compare walker per-account data vs ledger snapshots.

    Returns a list of disagreements.
    """
    disagreements = []

    # Index ledger snapshots by (household, ticker, account_id)
    ledger_idx: dict[tuple, dict] = {}
    for ls in ledger_snaps:
        snap = ls.get("snapshot")
        if not snap:
            continue
        key = (ls["household"], ls["ticker"], ls["account_id"])
        ledger_idx[key] = snap

    for cycle in walker_cycles:
        hh = cycle["household_id"]
        ticker = cycle["ticker"]

        # Compare household-aggregated
        hh_ledger = ledger_idx.get((hh, ticker, None))
        if hh_ledger:
            walker_adj = cycle.get("adjusted_basis_household")
            ledger_adj = hh_ledger.get("adjusted_basis")
            if walker_adj is not None and ledger_adj is not None:
                if abs(walker_adj - ledger_adj) > 0.02:
                    disagreements.append({
                        "type": "household_basis_mismatch",
                        "household": hh,
                        "ticker": ticker,
                        "walker_adjusted_basis": walker_adj,
                        "ledger_adjusted_basis": ledger_adj,
                        "delta": round(walker_adj - ledger_adj, 4),
                    })

        # Compare per-account
        for acct_id, acct_data in cycle.get("per_account", {}).items():
            acct_ledger = ledger_idx.get((hh, ticker, acct_id))
            if not acct_ledger:
                disagreements.append({
                    "type": "per_account_missing_in_ledger",
                    "household": hh,
                    "ticker": ticker,
                    "account_id": acct_id,
                    "walker_paper_basis": acct_data.get("paper_basis"),
                    "walker_shares": acct_data.get("shares"),
                })
                continue

            # Paper basis comparison
            w_paper = acct_data.get("paper_basis")
            l_paper = acct_ledger.get("initial_basis")
            if w_paper is not None and l_paper is not None and abs(w_paper - l_paper) > 0.02:
                disagreements.append({
                    "type": "per_account_paper_basis_mismatch",
                    "household": hh,
                    "ticker": ticker,
                    "account_id": acct_id,
                    "walker_paper_basis": w_paper,
                    "ledger_initial_basis": l_paper,
                    "delta": round(w_paper - l_paper, 4),
                })

            # Adjusted basis comparison
            w_adj = acct_data.get("adjusted_basis")
            l_adj = acct_ledger.get("adjusted_basis")
            if w_adj is not None and l_adj is not None and abs(w_adj - l_adj) > 0.02:
                disagreements.append({
                    "type": "per_account_adjusted_basis_mismatch",
                    "household": hh,
                    "ticker": ticker,
                    "account_id": acct_id,
                    "walker_adjusted_basis": w_adj,
                    "ledger_adjusted_basis": l_adj,
                    "delta": round(w_adj - l_adj, 4),
                })

        # Check: does household-aggregated basis equal weighted average of per-account?
        per_acct = cycle.get("per_account", {})
        if per_acct and cycle.get("paper_basis_household") is not None:
            total_shares = sum(a.get("shares", 0) for a in per_acct.values())
            if total_shares > 0:
                weighted_basis = sum(
                    (a.get("paper_basis", 0) or 0) * a.get("shares", 0)
                    for a in per_acct.values()
                ) / total_shares
                hh_basis = cycle["paper_basis_household"]
                if abs(weighted_basis - hh_basis) > 0.02:
                    disagreements.append({
                        "type": "household_vs_weighted_avg_basis",
                        "household": hh,
                        "ticker": ticker,
                        "household_basis": hh_basis,
                        "weighted_avg_per_account": round(weighted_basis, 4),
                        "delta": round(hh_basis - weighted_basis, 4),
                        "note": "This is expected — confirms E1 (WHEEL-5) is real",
                    })

    return disagreements


def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = PROJECT_ROOT / "reports" / f"reconciliation_snapshot_{ts}.json"
    out_path.parent.mkdir(exist_ok=True)

    print(f"[reconciliation] Capturing walker cycles...")
    walker_cycles = _capture_walker_cycles()
    print(f"  → {len(walker_cycles)} active WHEEL cycles")

    print(f"[reconciliation] Capturing ledger snapshots...")
    ledger_snaps = _capture_ledger_snapshots()
    print(f"  → {len(ledger_snaps)} ledger snapshots")

    print(f"[reconciliation] Running reconciliation...")
    disagreements = _reconcile(walker_cycles, ledger_snaps)
    print(f"  → {len(disagreements)} disagreements found")

    output = {
        "captured_at": ts,
        "walker_cycles": walker_cycles,
        "ledger_snapshots": ledger_snaps,
        "disagreements": disagreements,
        "summary": {
            "total_cycles": len(walker_cycles),
            "total_ledger_snaps": len(ledger_snaps),
            "total_disagreements": len(disagreements),
            "disagreement_types": {},
        },
    }

    for d in disagreements:
        dtype = d["type"]
        output["summary"]["disagreement_types"][dtype] = (
            output["summary"]["disagreement_types"].get(dtype, 0) + 1
        )

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n[reconciliation] Snapshot saved to: {out_path}")
    print(f"[reconciliation] Summary:")
    for dtype, count in output["summary"]["disagreement_types"].items():
        print(f"  {dtype}: {count}")

    if not disagreements:
        print("  (no disagreements — walker and ledger agree on all per-account data)")

    return 0 if not disagreements else 1


if __name__ == "__main__":
    sys.exit(main())
