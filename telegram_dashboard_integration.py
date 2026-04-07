"""
AGT Equities — /dashboard Telegram Integration
=================================================
Add this code to telegram_bot.py to enable the /dashboard command.

INSTALLATION:
1. Place backfill_trade_ledger.py and dashboard_renderer.py alongside telegram_bot.py
2. Run backfill once: python backfill_trade_ledger.py IBKRTRADEFILE.csv --db agt_desk.db
3. Add the import and handler below to telegram_bot.py
4. Register the command in the ApplicationBuilder section

WHAT IT DOES:
- /dashboard → sends 2 images: Performance Card + Active Positions Grid
- Pulls live positions from ib_async when IB Gateway is connected
- Falls back to premium_ledger data when offline
"""

# ── Add to imports section of telegram_bot.py ─────────────────────
# from dashboard_renderer import generate_dashboard, render_performance_card, render_positions_grid


# ── Add this function to telegram_bot.py ──────────────────────────

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dashboard — Generate and send performance dashboard images.
    Pulls from trade_ledger (historical) + ib_async (live positions).
    """
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    await update.message.reply_text("⏳ Generating dashboard...")

    try:
        import dashboard_renderer as dr

        output_dir = BASE_DIR / "dashboard_output"
        output_dir.mkdir(exist_ok=True)

        # ── Panel 1: Performance Card (always available) ──
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        perf_path = str(output_dir / "dashboard_performance.png")
        try:
            dr.render_performance_card(conn, perf_path)
        except Exception as e:
            logger.exception("Performance card render failed: %s", e)
            await update.message.reply_text(f"❌ Performance card failed: {e}")
            conn.close()
            return

        # ── Panel 2: Positions Grid (needs live or ledger data) ──
        positions = []
        ib_connected = False

        # Try to get live positions from ib_async
        try:
            ib = _ib_for_account()  # Use existing IB connection helper
            if ib and ib.isConnected():
                ib_connected = True
                portfolio_items = ib.portfolio()

                # Group by underlying for stock positions
                stock_positions = {}
                option_positions = {}

                for item in portfolio_items:
                    contract = item.contract
                    symbol = contract.symbol

                    # Skip SPX box spreads, negligible holdings
                    if symbol in ("SPX", "TRAW", "SLS", "GTLB"):
                        continue

                    if contract.secType == "STK":
                        if item.position != 0:
                            stock_positions[symbol] = {
                                "ticker": symbol,
                                "account_id": item.account if hasattr(item, 'account') else "U21971297",
                                "shares": int(item.position),
                                "cost_basis": round(item.averageCost, 2),
                                "current_price": round(item.marketPrice, 2),
                                "unrealized_pnl": round(item.unrealizedPNL, 2),
                                "total_premium": 0,  # Will fill from ledger
                                "active_option": None,
                            }
                    elif contract.secType == "OPT":
                        if item.position != 0:
                            underlying = contract.symbol
                            strike = contract.strike
                            right = contract.right
                            expiry = contract.lastTradeDateOrContractMonth
                            opt_str = f"{right}{strike} {expiry[4:6]}/{expiry[6:]}"

                            if underlying not in option_positions:
                                option_positions[underlying] = []
                            option_positions[underlying].append(opt_str)

                # Merge options into stock positions
                for sym, opts in option_positions.items():
                    if sym in stock_positions:
                        stock_positions[sym]["active_option"] = ", ".join(opts[:2])

                # Enrich with premium from Walker cycles (or legacy fallback)
                _enriched = False
                if READ_FROM_MASTER_LOG:
                    try:
                        from agt_equities import trade_repo
                        trade_repo.DB_PATH = DB_PATH
                        for c in trade_repo.get_active_cycles():
                            if c.cycle_type == 'WHEEL' and c.ticker in stock_positions:
                                stock_positions[c.ticker]["total_premium"] = c.premium_total
                        _enriched = True
                    except Exception as exc:
                        logger.warning("Walker premium enrichment fallback: %s", exc)
                if not _enriched:
                    for sym, pos in stock_positions.items():
                        try:
                            row = conn.execute("""
                                SELECT total_premium_collected
                                FROM premium_ledger
                                WHERE ticker = ?
                                ORDER BY rowid DESC LIMIT 1
                            """, (sym,)).fetchone()
                            if row:
                                pos["total_premium"] = float(row[0] or 0)
                        except Exception:
                            pass

                positions = list(stock_positions.values())

        except Exception as e:
            logger.warning("IB live positions unavailable: %s", e)

        # Fallback: build from Walker cycles (or legacy premium_ledger)
        if not positions:
            _built_from_walker = False
            if READ_FROM_MASTER_LOG:
                try:
                    from agt_equities import trade_repo
                    trade_repo.DB_PATH = DB_PATH
                    for c in trade_repo.get_active_cycles():
                        if c.cycle_type != 'WHEEL' or c.shares_held <= 0:
                            continue
                        positions.append({
                            "ticker": c.ticker,
                            "shares": int(c.shares_held),
                            "avg_price": round(c.paper_basis, 2) if c.paper_basis else 0,
                            "market_value": 0,
                            "unrealized_pnl": 0,
                            "total_premium": round(c.premium_total, 2),
                            "account": "",
                            "active_option": "",
                        })
                    _built_from_walker = True
                except Exception as exc:
                    logger.warning("Walker fallback positions failed: %s", exc)
            if not _built_from_walker and not positions:
                try:
                    rows = conn.execute("""
                        SELECT household_id, ticker, initial_basis,
                               total_premium_collected, shares_owned
                        FROM premium_ledger
                        WHERE shares_owned > 0
                    """).fetchall()

                    for r in rows:
                        household = r[0]
                        acct_id = "U21971297" if household == "Yash_Household" else "U22388499"
                        shares = int(r[4])
                        basis = float(r[2]) if r[2] else 0
                        premium = float(r[3]) if r[3] else 0

                    positions.append({
                        "ticker": r[1],
                        "account_id": acct_id,
                        "shares": shares,
                        "cost_basis": round(basis / shares, 2) if shares > 0 else 0,
                        "total_premium": premium,
                        "current_price": None,
                        "unrealized_pnl": None,
                        "active_option": None,
                    })
            except Exception as e:
                logger.warning("Premium ledger fallback failed: %s", e)

        pos_path = None
        if positions:
            pos_path = str(output_dir / "dashboard_positions.png")
            try:
                dr.render_positions_grid(conn, positions, pos_path)
            except Exception as e:
                logger.warning("Positions grid render failed: %s", e)
                pos_path = None

        conn.close()

        # ── Send images ──
        source = "🟢 Live" if ib_connected else "📊 Ledger"

        with open(perf_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"📈 Performance Dashboard  ({source})"
            )

        if pos_path:
            with open(pos_path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"📋 Active Positions  ({source})"
                )

    except Exception as e:
        logger.exception("Dashboard generation failed: %s", e)
        await update.message.reply_text(f"❌ Dashboard error: {e}")


# ── Add to the fill handler section to auto-populate trade_ledger ──

def _record_fill_to_trade_ledger(
    ticker: str, action: str, quantity: float, price: float,
    realized_pnl: float, account_id: str, household_id: str,
    asset_category: str, symbol: str, trade_type: str,
    return_category: str
):
    """Write a fill event to trade_ledger for dashboard tracking."""
    try:
        from datetime import datetime
        now = datetime.now()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT OR IGNORE INTO trade_ledger
                (account_id, household_id, trade_date, trade_datetime,
                 symbol, underlying, asset_category, trade_type,
                 quantity, price, proceeds, realized_pnl,
                 return_category, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LIVE')
        """, (
            account_id, household_id, now.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d %H:%M:%S"), symbol, ticker,
            asset_category, trade_type, quantity, price,
            round(price * abs(quantity) * (100 if "Option" in asset_category else 1), 2),
            realized_pnl, return_category,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("trade_ledger insert failed: %s", e)


# ── Register in ApplicationBuilder section ────────────────────────
# app.add_handler(CommandHandler("dashboard", cmd_dashboard))


# ── DEPLOYMENT CHECKLIST ──────────────────────────────────────────
#
# 1. Copy these files to your AGT desk directory:
#    - backfill_trade_ledger.py
#    - dashboard_renderer.py
#
# 2. Run the one-time backfill:
#    python backfill_trade_ledger.py IBKRTRADEFILE.csv --db agt_desk.db
#
# 3. Add to telegram_bot.py:
#    a) Import: from dashboard_renderer import render_performance_card, render_positions_grid
#    b) Paste cmd_dashboard function
#    c) Register: app.add_handler(CommandHandler("dashboard", cmd_dashboard))
#
# 4. Test: send /dashboard in Telegram
#
# 5. Monthly refresh: export new IBKR Activity Statement CSV and re-run backfill
#    (duplicates are skipped via UNIQUE constraint)
