# Command Deck v1 — Delivery Report

Generated: 2026-04-07
Status: **MVP SHIPPED — ready for live test**

## What renders

The deck produces a single-page dark-mode dashboard at `http://127.0.0.1:8787/?t=<token>` with:

1. **Top strip** (sticky): AGT brand, VIX badge with EL retain band, Portfolio NAV, Period P&L ($ and %), EL gauge (placeholder), concentration check (Rule 1), sector violation count (Rule 3), last sync timestamp

2. **Active Cycles table** (8-col grid): 14 active wheel positions with Ticker, Household, Qty, Paper Basis (IRS-adjusted), Spot, Unrealized $/%, Nearest DTE, Leg description, Premium collected. Sorted by nearest DTE then unrealized % (worst first). Row coloring: red for >20% loss, amber for DTE ≤ 3.

3. **Attention panel** (4-col): Cycles with DTE ≤ 5 or unrealized < -15%

4. **Fills panel** (4-col): Last 10 trades from master_log_trades with time, symbol, side, qty, net cash

5. **Reconciliation panel** (4-col): Cross-check A/B/C status dots, sync ID and timestamp, trade count

6. **Account pills** (sticky bottom): 4 pills with NAV per account

## Auth

Token-based: `AGT_DECK_TOKEN` env var (32-char urlsafe). If not set, auto-generates on startup and prints to console.

```bash
set AGT_DECK_TOKEN=your_secret_here
python -m agt_deck.main
```

Or just run `boot_deck.bat` — it auto-generates a token.

## Files created

```
agt_deck/
  __init__.py
  main.py          # FastAPI app, routes, SSE, VIX/spot caching
  db.py            # Read-only SQLite (mode=ro, query_only=ON)
  queries.py       # All SQL queries, parameterized
  risk.py          # Rule 2/1/3 gate calculations
  formatters.py    # money/pct/color display helpers
  templates/
    base.html      # Tailwind CDN, Inter + JetBrains Mono fonts
    command_deck.html  # Full dashboard layout
  static/
    app.css        # Minimal overrides
    app.js         # SSE connection + auto-reconnect
boot_deck.bat      # Windows launcher
```

## Tests: 13 passing

- `test_deck_queries.py`: 4 tests (NAV, fills, sync, recon shapes)
- `test_deck_risk.py`: 5 tests (VIX→EL table edge cases)
- `test_deck_auth.py`: 4 tests (401 reject, 200 accept, static bypass)

## Bot edits: ZERO

No modifications to telegram_bot.py for the deck. The deck reads from agt_desk.db in read-only mode. EL values show "—" (placeholder) — wiring deferred to tonight.

## Known limitations / deferred to tonight

- **EL gauge**: Shows "—" for EL current/required. telegram_bot.py doesn't persist EL to DB. Tonight: add 2-line risk_state writer to bot's periodic task.
- **Vikram EL**: Same — needs risk_state table.
- **Spot prices**: yfinance batch fetch on page load, cached 60s. May be slow on first load (5-10s).
- **Cycle inspector**: Row click → expand not implemented. Button stubs present.
- **Account detail modal**: Pills are static — click handler deferred.
- **SSE live updates**: Top strip values pushed every 30s. Cycle table rows not yet pushed (tonight).
- **Concentration check**: Uses `shares * paper_basis` as position value estimate. Should use mark price for accuracy (tonight).

## Stack

FastAPI 0.135 + Jinja2 3.1.6 + Tailwind CDN + HTMX 1.9 CDN. No build step. Single process. ~350 lines of Python, ~250 lines of HTML.

## Production DB: READ-ONLY (mode=ro, PRAGMA query_only=ON)
