#!/usr/bin/env python3
"""
dev_cli.py — Headless CLI bypass for autonomous AGT paper testing.

Imports and executes the EXACT production functions from telegram_bot.py.
No shadow pipelines, no duplicate IB clients. Same code path as Telegram.

Usage:
    python dev_cli.py scan-daily
    python dev_cli.py stage-cc [--household HOUSEHOLD]
    python dev_cli.py list-staged
    python dev_cli.py approve <order_id>
    python dev_cli.py approve-all
    python dev_cli.py positions
    python dev_cli.py health
"""
import argparse
import asyncio
import io
import json
import os
import sys
import traceback
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# ── Force UTF-8 stdout so Windows CMD doesn't choke on Unicode ──
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Ensure we're running from project root ──
os.chdir(Path(__file__).resolve().parent)


# ── Import the production module ──
# telegram_bot.py no longer has import-time side effects (init_db moved to
# main() in Decoupling Sprint A).  We import it as a library module and call
# init_db() ourselves before touching the DB.
import telegram_bot as bot
from agt_equities import roll_scanner

# Shared DB helpers (same ones telegram_bot uses internally)
from agt_equities.db import get_db_connection, get_ro_connection, tx_immediate

# CLI clientId — distinct from bot (1) and scheduler (2) so we can coexist.
# We patch the module-level constant BEFORE any ensure_ib_connected() call.
CLI_CLIENT_ID = 12


def _init():
    """One-time setup: clientId override. Skip init_db() — the running bot
    already initialized the schema, and init_db's _cleanup_test_orders()
    would supersede any staged orders we just created."""
    # Override the production clientId so the CLI can connect while the bot runs.
    # All production code paths (event handlers, state machine, gates) are
    # unchanged — only the socket identity differs.
    bot.IB_CLIENT_ID = CLI_CLIENT_ID




# ════════════════════════════════════════════════════════
# MR #2: autonomous_session_log writer (paper-mode observability)
# ════════════════════════════════════════════════════════

def _log_session(
    task_name: str,
    summary: str,
    *,
    actions: dict | list | None = None,
    errors: dict | list | None = None,
    metrics: dict | None = None,
    notes: str | None = None,
) -> int | None:
    """Append a row to autonomous_session_log. Best-effort — failure never
    prevents the CLI command from returning. Returns the inserted rowid or
    None on failure. Keeps payload columns as JSON strings per schema.py.
    """
    try:
        payload = (
            task_name,
            datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            summary,
            None,                                                # positions_snapshot
            None,                                                # orders_snapshot
            json.dumps(actions, default=str) if actions is not None else None,
            json.dumps(errors, default=str) if errors is not None else None,
            json.dumps(metrics, default=str) if metrics is not None else None,
            notes,
        )
        with closing(get_db_connection()) as conn:
            with tx_immediate(conn):
                cur = conn.execute(
                    "INSERT INTO autonomous_session_log "
                    "(task_name, run_at, summary, positions_snapshot, "
                    " orders_snapshot, actions_taken, errors, metrics, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    payload,
                )
                return cur.lastrowid
    except Exception as exc:
        # dev_cli must never die on session-log failure — paper ops continue.
        print(f"[warn] _log_session({task_name!r}) failed: {exc}", file=sys.stderr)
        return None


# ════════════════════════════════════════════════════════
# COMMAND: positions
# ════════════════════════════════════════════════════════

async def cmd_positions(args):
    """Show current IB positions + NLV per account (via production IB singleton)."""
    ib_conn = await bot.ensure_ib_connected()
    positions = ib_conn.positions()

    print(f"\n{'='*70}")
    print(f"POSITIONS ({len(positions)} total)  —  via production IB singleton (clientId={bot.IB_CLIENT_ID})")
    print(f"{'='*70}")
    print(f"  {'Account':<12} {'Symbol':<8} {'Shares':>8} {'AvgCost':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*10}")
    for p in sorted(positions, key=lambda x: (x.account, x.contract.symbol)):
        if p.contract.secType == "STK":
            print(f"  {p.account:<12} {p.contract.symbol:<8} "
                  f"{p.position:>8.0f} ${p.avgCost:>9.2f}")

    # NLV
    print(f"\n  {'Account':<12} {'NLV':>14}")
    print(f"  {'-'*12} {'-'*14}")
    seen = set()
    for p in positions:
        if p.account in seen:
            continue
        seen.add(p.account)
        tags = ib_conn.accountValues(p.account)
        nlv = [t for t in tags if t.tag == "NetLiquidation" and t.currency == "USD"]
        if nlv:
            print(f"  {p.account:<12} ${float(nlv[0].value):>13,.2f}")


# ════════════════════════════════════════════════════════
# COMMAND: scan-daily
# ════════════════════════════════════════════════════════

async def cmd_scan_daily(args):
    """Execute the EXACT production /daily pipeline and print results."""
    print(f"\n{'='*70}")
    print(f"DAILY SCAN  —  {datetime.now().strftime('%H:%M:%S ET')}")
    print(f"{'='*70}")

    _log_session("dev_cli.scan_daily.begin", "dev_cli scan-daily started")

    # This is the exact function cmd_daily() calls.
    # ensure_ib_connected() is called inside _run_cc_logic, etc.
    sections = []

    # ── 1. CC staging (same call as cmd_daily line 7074) ──
    try:
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
        import uuid as _uuid_cc1
        _cc_ctx_daily = RunContext(
            mode=RunMode.LIVE,
            run_id=_uuid_cc1.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=bot.append_pending_tickets),
            decision_sink=NullDecisionSink(),
            broker_mode="paper" if os.environ.get("AGT_BROKER_MODE", "paper").strip().lower() != "live" else "live",
            engine="cc",
        )
        cc_result = await bot._run_cc_logic(None, ctx=_cc_ctx_daily)
        cc_text = cc_result.get("main_text", "No CC output.")
        sections.append(("COVERED CALLS", cc_text))
    except Exception as exc:
        sections.append(("COVERED CALLS", f"ERROR: {exc}"))
        traceback.print_exc()

    # ── 2. Roll check (same call as cmd_daily line 7092) ──
    try:
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
        import uuid as _uuid_mr4
        ib_conn = await bot.ensure_ib_connected()
        _cli_roll_ctx = RunContext(
            mode=RunMode.LIVE,
            run_id=_uuid_mr4.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=bot.append_pending_tickets),
            decision_sink=NullDecisionSink(),
            broker_mode="paper" if os.environ.get("AGT_BROKER_MODE", "paper").strip().lower() != "live" else "live",
            engine="roll",
        )
        roll_alerts = await roll_scanner.scan_and_stage_defensive_rolls(
            ib_conn,
            ctx=_cli_roll_ctx,
            ibkr_get_spot=bot._ibkr_get_spot,
            load_premium_ledger=bot._load_premium_ledger_snapshot,
            get_desk_mode=bot._get_current_desk_mode,
            ibkr_get_expirations=bot._ibkr_get_expirations,
            ibkr_get_chain=bot._ibkr_get_chain,
            account_labels=bot.ACCOUNT_LABELS,
            is_halted=bot._HALTED,
        )
        roll_text = "\n".join(roll_alerts) if roll_alerts else "No roll actions triggered."
        sections.append(("ROLL CHECK", roll_text))
    except Exception as exc:
        sections.append(("ROLL CHECK", f"ERROR: {exc}"))
        traceback.print_exc()

    # ── 3. CSP harvest (same call as cmd_daily line 7111) ──
    try:
        from agt_equities.csp_harvest import scan_csp_harvest_candidates
        from agt_equities.runtime import RunContext, RunMode
        from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
        import uuid
        ib_conn = await bot.ensure_ib_connected()
        ctx = RunContext(
            mode=RunMode.LIVE,
            run_id=uuid.uuid4().hex,
            order_sink=SQLiteOrderSink(staging_fn=bot.append_pending_tickets),
            decision_sink=NullDecisionSink(),
            broker_mode="paper" if os.environ.get("AGT_BROKER_MODE", "paper").strip().lower() != "live" else "live",
            engine="harvest",
        )
        harvest_result = await scan_csp_harvest_candidates(ib_conn, ctx=ctx)
        staged = harvest_result.get("staged", [])
        alerts = harvest_result.get("alerts", [])
        harvest_text = f"Staged: {len(staged)}"
        if alerts:
            harvest_text += "\n" + "\n".join(alerts)
        sections.append(("CSP HARVEST", harvest_text))
    except Exception as exc:
        sections.append(("CSP HARVEST", f"ERROR: {exc}"))
        traceback.print_exc()

    # ── 4. CSP entry scan (same pipeline as /scan in telegram_bot.py) ──
    try:
        csp_text = await _run_csp_scan_pipeline()
        sections.append(("CSP ENTRY SCAN", csp_text))
    except Exception as exc:
        sections.append(("CSP ENTRY SCAN", f"ERROR: {exc}"))
        traceback.print_exc()

    # ── Print digest ──
    for title, body in sections:
        print(f"\n>> {title}")
        for line in body.strip().splitlines():
            print(f"   {line}")

    print(f"\n{'='*70}")
    print("SCAN COMPLETE")
    _log_session(
        "dev_cli.scan_daily.end",
        f"sections={len(sections)}",
        metrics={"section_titles": [t for t, _ in sections]},
        errors=[
            {"section": t, "body": b}
            for t, b in sections
            if b.startswith("ERROR")
        ] or None,
    )


# ════════════════════════════════════════════════════════
# COMMAND: stage-cc
# ════════════════════════════════════════════════════════

async def cmd_stage_cc(args):
    """Execute the EXACT production /cc pipeline (writes to pending_orders)."""
    hh_filter = getattr(args, 'household', None)
    print(f"\n{'='*70}")
    print(f"CC STAGING  —  {datetime.now().strftime('%H:%M:%S ET')}")
    if hh_filter:
        print(f"Household filter: {hh_filter}")
    print(f"{'='*70}")

    _log_session(
        "dev_cli.stage_cc.begin",
        f"household={hh_filter or 'ALL'}",
        metrics={"household": hh_filter},
    )

    # This is the EXACT function that /cc invokes.
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
    import uuid as _uuid_cc2
    _cc_ctx_stage = RunContext(
        mode=RunMode.LIVE,
        run_id=_uuid_cc2.uuid4().hex,
        order_sink=SQLiteOrderSink(staging_fn=bot.append_pending_tickets),
        decision_sink=NullDecisionSink(),
        broker_mode="paper" if os.environ.get("AGT_BROKER_MODE", "paper").strip().lower() != "live" else "live",
        engine="cc",
    )
    result = await bot._run_cc_logic(hh_filter, ctx=_cc_ctx_stage)

    main_text = result.get("main_text", "")
    print(f"\n{main_text}")

    # Show staged/skipped summary if available
    staged = result.get("staged", [])
    skipped = result.get("skipped", [])
    if staged:
        print(f"\n  STAGED ({len(staged)}):")
        for s in staged:
            print(f"    {s}")
    if skipped:
        print(f"\n  SKIPPED ({len(skipped)}):")
        for s in skipped:
            print(f"    {s}")

    _log_session(
        "dev_cli.stage_cc.end",
        f"staged={len(staged)} skipped={len(skipped)}",
        metrics={
            "household": hh_filter,
            "staged_count": len(staged),
            "skipped_count": len(skipped),
        },
    )


# ════════════════════════════════════════════════════════
# CSP ENTRY SCAN — shared pipeline (used by scan-daily and scan-csp)
# ════════════════════════════════════════════════════════

async def _run_csp_scan_pipeline() -> str:
    """Run the EXACT production /scan pipeline for CSP entries.

    Replicates cmd_scan from telegram_bot.py:
    1. Load watchlist + run pxo_scanner (6-phase screener)
    2. Adapt to ScanCandidate objects
    3. Fetch VIX
    4. Build household buying-power snapshots
    5. Fetch earnings + correlations (bridge-2 extras)
    6. Run allocator with all 7 gates
    7. Stage tickets
    """
    import yfinance as yf
    from pxo_scanner import _load_scan_universe, scan_csp_candidates
    from agt_equities.scan_bridge import (
        adapt_scanner_candidates,
        build_watchlist_sector_map,
        make_bridge2_extras_provider,
    )
    from agt_equities.csp_allocator import (
        _fetch_household_buying_power_snapshot,
        run_csp_allocator,
    )
    from agt_equities.runtime import RunContext, RunMode
    from agt_equities.sinks import NullDecisionSink, SQLiteOrderSink
    import uuid as _uuid
    from agt_equities.scan_extras import (
        fetch_earnings_map,
        build_correlation_pairs,
    )

    lines = []

    # ── 1. Screener ──
    lines.append("Phase 1: Loading scan universe...")
    watchlist = await asyncio.to_thread(_load_scan_universe)
    lines.append(f"  Universe: {len(watchlist)} tickers")

    lines.append("Phase 2-5: Running 6-phase screener (yfinance)...")
    rows = await asyncio.to_thread(scan_csp_candidates, watchlist, 10, 50)

    if not rows:
        lines.append("  No CSP candidates meet Heitkoetter criteria.")
        return "\n".join(lines)

    lines.append(f"  Screener output: {len(rows)} candidates")
    for r in rows:
        lines.append(f"    {r.get('ticker','?')} ${r.get('strike',0):.0f}P "
                      f"exp={r.get('expiry','?')} ann_roi={r.get('ann_roi',0):.1f}%")

    # ── 2. Adapt ──
    candidates = adapt_scanner_candidates(rows)
    if not candidates:
        lines.append("  No candidates survived adapter validation.")
        return "\n".join(lines)

    # ── 3. VIX ──
    def _fetch_vix() -> float:
        try:
            hist = yf.Ticker("^VIX").history(period="1d")
            if len(hist) and "Close" in hist.columns:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return 20.0
    vix = await asyncio.to_thread(_fetch_vix)
    lines.append(f"  VIX: {vix:.1f}")

    # ── 4. Household snapshots ──
    lines.append("Phase 6: Building household buying-power snapshots...")
    disco = await bot._discover_positions(None)
    if disco.get("error"):
        lines.append(f"  WARNING: _discover_positions: {disco['error']}")

    ib_conn = await bot.ensure_ib_connected()
    snapshots = await _fetch_household_buying_power_snapshot(ib_conn, disco)
    if not snapshots:
        lines.append("  FAILED: Could not build household snapshots.")
        return "\n".join(lines)

    for hh_name, hh in snapshots.items():
        nlv = sum(a.get("nlv", 0) for a in hh.get("accounts", {}).values())
        lines.append(f"  {hh_name}: NLV=${nlv:,.0f}")

    # ── 5. Extras (earnings, correlations) ──
    lines.append("Fetching earnings + correlations...")
    candidate_tickers = [c.ticker for c in candidates]
    all_holding_tickers: set[str] = set()
    for _hh_snap in snapshots.values():
        all_holding_tickers.update(_hh_snap.get("existing_positions", {}).keys())
        all_holding_tickers.update(_hh_snap.get("existing_csps", {}).keys())

    earnings_map = await asyncio.to_thread(fetch_earnings_map, candidate_tickers)
    correlation_pairs = await asyncio.to_thread(
        build_correlation_pairs, candidate_tickers, sorted(all_holding_tickers),
    )
    lines.append(f"  Earnings: {sum(1 for v in earnings_map.values() if v)} with data")
    lines.append(f"  Correlations: {len(correlation_pairs)} pairs")

    # ── 6. Allocator ──
    lines.append("Running CSP allocator (7 gates)...")
    sector_map = build_watchlist_sector_map(watchlist)
    extras_provider = make_bridge2_extras_provider(
        sector_map, earnings_map, correlation_pairs,
    )

    # ADR-008 MR 2: live ctx -> SQLiteOrderSink forwards tickets to
    # bot.append_pending_tickets positionally. Byte-identical to the
    # prior _cli_csp_staging_cb closure.
    ctx = RunContext(
        mode=RunMode.LIVE,
        run_id=_uuid.uuid4().hex,
        order_sink=SQLiteOrderSink(staging_fn=bot.append_pending_tickets),
        decision_sink=NullDecisionSink(),
        broker_mode="paper" if os.environ.get("AGT_BROKER_MODE", "paper").strip().lower() != "live" else "live",
        engine="csp",
    )

    result = run_csp_allocator(
        ray_candidates=candidates,
        snapshots=snapshots,
        vix=vix,
        extras_provider=extras_provider,
        ctx=ctx,
    )

    # ── 7. Report ──
    if result.digest_lines:
        for dl in result.digest_lines:
            lines.append(f"  {dl}")

    staged_n = result.total_staged_contracts
    lines.append(f"\n  RESULT: {staged_n} contracts staged, "
                  f"{len(result.skipped)} skipped, "
                  f"{len(result.errors)} errors")

    if result.errors:
        for err in result.errors[:5]:
            lines.append(f"  ERROR: {err}")

    return "\n".join(lines)


async def cmd_scan_csp(args):
    """Standalone CSP entry scan — same pipeline as /scan in Telegram."""
    print(f"\n{'='*70}")
    print(f"CSP ENTRY SCAN  —  {datetime.now().strftime('%H:%M:%S ET')}")
    print(f"{'='*70}")

    result_text = await _run_csp_scan_pipeline()
    print(f"\n{result_text}")


# ════════════════════════════════════════════════════════
# COMMAND: list-staged
# ════════════════════════════════════════════════════════

async def cmd_list_staged(args):
    """List all staged/active orders from pending_orders."""
    with closing(get_ro_connection()) as db:
        rows = db.execute("""
            SELECT id, payload, status, created_at,
                   ib_order_id, ib_perm_id, fill_price, fill_qty, last_ib_status
            FROM pending_orders
            WHERE status IN ('staged', 'processing', 'sent', 'working', 'pending')
            ORDER BY id DESC
            LIMIT 50
        """).fetchall()

    print(f"\n{'='*70}")
    print(f"STAGED/ACTIVE ORDERS ({len(rows)} rows)")
    print(f"{'='*70}")
    print(f"  {'ID':>5} {'Status':<12} {'Ticker':<6} {'Strike':>8} {'Exp':<12} "
          f"{'Qty':>4} {'Account':<12} {'IB_Status':<10}")
    print(f"  {'-'*5} {'-'*12} {'-'*6} {'-'*8} {'-'*12} {'-'*4} {'-'*12} {'-'*10}")

    for row in rows:
        try:
            p = json.loads(row[1])
        except Exception:
            p = {}
        ib_status = row[8] or ""
        print(f"  {row[0]:>5} {row[2]:<12} {p.get('ticker','?'):<6} "
              f"${p.get('strike',0):>7.0f} {p.get('expiry','?'):<12} "
              f"{p.get('quantity',0):>4} {p.get('account_id','?'):<12} {ib_status:<10}")


# ════════════════════════════════════════════════════════
# COMMAND: approve <order_id>
# ════════════════════════════════════════════════════════

async def cmd_approve(args):
    """Approve a specific staged order — calls the EXACT production _place_single_order."""
    order_id = args.order_id
    await _approve_single(order_id)


async def cmd_approve_all(args):
    """Approve ALL staged orders."""
    with closing(get_ro_connection()) as db:
        rows = db.execute(
            "SELECT id FROM pending_orders WHERE status = 'staged' ORDER BY id"
        ).fetchall()

    if not rows:
        print("No staged orders to approve.")
        return

    ids = [r[0] for r in rows]
    print(f"Approving {len(ids)} staged orders: {ids}")

    for oid in ids:
        await _approve_single(oid)
        await asyncio.sleep(0.5)


async def _approve_single(order_id: int):
    """Transition staged -> sent via the production execution bridge."""
    print(f"\n  --- Approving order #{order_id} ---")

    _log_session(
        "dev_cli.approve.begin",
        f"order_id={order_id}",
        metrics={"order_id": order_id},
    )

    # CAS: staged -> processing (same guard as handle_approve_callback)
    with closing(get_db_connection()) as conn:
        with tx_immediate(conn):
            cur = conn.execute(
                "UPDATE pending_orders SET status = 'processing' "
                "WHERE id = ? AND status = 'staged'",
                (order_id,),
            )
            if cur.rowcount != 1:
                # Check actual status
                row = conn.execute(
                    "SELECT status FROM pending_orders WHERE id = ?", (order_id,)
                ).fetchone()
                actual = row[0] if row else "NOT_FOUND"
                print(f"  [FAIL] CAS failed: order #{order_id} is '{actual}', not 'staged'")
                return

            row = conn.execute(
                "SELECT payload FROM pending_orders WHERE id = ?", (order_id,)
            ).fetchone()

    payload = json.loads(row[0])
    ticker = payload.get("ticker", "?")
    strike = payload.get("strike", 0)
    qty = payload.get("quantity", 0)
    acct = payload.get("account_id", "?")
    print(f"  Payload: {payload.get('action','SELL')} {qty}x {ticker} "
          f"${strike:.0f}{payload.get('right','C')} {payload.get('expiry','?')} "
          f"-> account {acct}")

    # Call the PRODUCTION execution bridge
    success, message = await bot._place_single_order(payload, order_id)

    if success:
        print(f"  [OK] {message}")
    else:
        print(f"  [FAIL] {message}")

    # Check final DB state
    with closing(get_ro_connection()) as db:
        final = db.execute(
            "SELECT status, ib_order_id, ib_perm_id, last_ib_status "
            "FROM pending_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    if final:
        print(f"  DB state: status={final[0]} ib_order={final[1]} "
              f"perm={final[2]} ib_status={final[3]}")

    _log_session(
        "dev_cli.approve.end",
        f"order_id={order_id} success={success}",
        metrics={
            "order_id": order_id,
            "success": bool(success),
            "final_status": (final[0] if final else None),
            "ib_order_id": (final[1] if final else None),
            "ib_perm_id": (final[2] if final else None),
        },
        errors=[{"message": message}] if not success else None,
    )


# ════════════════════════════════════════════════════════
# COMMAND: health
# ════════════════════════════════════════════════════════

async def cmd_health(args):
    """Health check: IB connection, DB state, pending_order anomalies."""
    print(f"\n{'='*70}")
    print(f"HEALTH CHECK  —  {datetime.now().strftime('%H:%M:%S ET')}")
    print(f"{'='*70}")

    # IB — via production singleton
    try:
        ib_conn = await bot.ensure_ib_connected()
        positions = ib_conn.positions()
        print(f"  [OK] IB connected (clientId={bot.IB_CLIENT_ID})")
        print(f"  [OK] Positions: {len(positions)}")
        print(f"  [OK] Accounts: {ib_conn.managedAccounts()}")
    except Exception as e:
        print(f"  [FAIL] IB connection: {e}")

    # DB
    with closing(get_ro_connection()) as db:
        stats = db.execute("""
            SELECT status, COUNT(*) FROM pending_orders
            GROUP BY status ORDER BY COUNT(*) DESC
        """).fetchall()
        print(f"\n  pending_orders by status:")
        for s, c in stats:
            print(f"    {s}: {c}")

        # State machine anomalies in pending_order_children
        anomalies = db.execute("""
            SELECT id, status, last_ib_status, fill_price
            FROM pending_order_children
            WHERE (status = 'SENT' AND fill_price IS NOT NULL)
               OR (last_ib_status = 'Filled' AND status != 'FILLED')
               OR (child_ib_perm_id IS NULL AND status NOT IN ('PENDING', 'MARGIN_CHECK'))
            LIMIT 10
        """).fetchall()

        if anomalies:
            print(f"\n  [WARN] {len(anomalies)} state machine anomalies in children:")
            for a in anomalies:
                print(f"    id={a[0]} status={a[1]} ib_status={a[2]} fill=${a[3]}")
        else:
            print(f"  [OK] No anomalies in pending_order_children")


# ════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="AGT dev_cli — headless bypass for production pipeline testing",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("positions",   help="Show IB positions + NLV")
    sub.add_parser("scan-daily",  help="Run production /daily pipeline")

    p_cc = sub.add_parser("stage-cc", help="Run production /cc pipeline")
    p_cc.add_argument("--household", help="Filter to specific household")

    sub.add_parser("scan-csp",    help="Run CSP entry scan (screener + allocator)")
    sub.add_parser("list-staged", help="List staged/active orders from DB")

    p_appr = sub.add_parser("approve", help="Approve a specific staged order")
    p_appr.add_argument("order_id", type=int)

    sub.add_parser("approve-all", help="Approve ALL staged orders")
    sub.add_parser("health",      help="IB + DB health check")

    args = parser.parse_args()

    _init()

    cmd_map = {
        "positions":  cmd_positions,
        "scan-daily": cmd_scan_daily,
        "stage-cc":   cmd_stage_cc,
        "scan-csp":   cmd_scan_csp,
        "list-staged": cmd_list_staged,
        "approve":    cmd_approve,
        "approve-all": cmd_approve_all,
        "health":     cmd_health,
    }

    try:
        asyncio.run(cmd_map[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Clean disconnect to avoid the ib_async __del__ traceback
        if bot.ib is not None and bot.ib.isConnected():
            bot.ib.disconnect()


if __name__ == "__main__":
    from agt_equities.boot import assert_boot_contract
    assert_boot_contract()
    main()
