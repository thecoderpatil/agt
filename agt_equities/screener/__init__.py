"""
agt_equities.screener — Act 60 Fortress CSP Screener (READ-ONLY).

Side project: read-only data aggregation engine for surfacing wheel-eligible
cash-secured put candidates from a curated S&P 500 + NASDAQ 100 universe.

LOAD-BEARING ISOLATION CONTRACT:
This package MUST NOT import from telegram_bot.py execution paths, the V2
router, _pre_trade_gates, placeOrder, execution_gate, _HALTED, or any
operational table writer. The AST guard test at
tests/test_screener_isolation.py enforces this contract on every commit.

Pipeline phases (Tech-First Reorder per Finnhub Free Tier pivot 2026-04-10):
  Phase 1 — Universe exclusions       (Finnhub Free profile2 only — MC, sector, country)
  Phase 2 — Technical pullback        (yfinance BATCH download — SMA200, RSI14, BBands)
  Phase 3 — Fundamental fortress      (yfinance per-ticker — Z, FCF, ND/EBITDA, ROIC, SI)
  Phase 4 — Volatility + event armor  (Finnhub Free dividend2 + yfinance ATM IV
                                       + existing corporate_intel cache for earnings
                                       + IVR bootstrap fail-open <30 days)
  Phase 5 — Options chain iterator    (ib_async — the ONLY IBKR call site)
  Phase 6 — Act 60 yield calculator   (RAY filter, return tuple)

CRITICAL ORDER NOTE: Phase 2 (Technical) runs BEFORE Phase 3 (Fundamental).
This is a deliberate pivot from the original spec ordering. The technical
pullback gate is the most violently narrowing filter in the pipeline
(~480 → ~30), so running it before the expensive yfinance fundamental
pulls means we only fetch balance sheets / cashflow for ~30 tickers
instead of ~480. This completely neutralizes yfinance throttling risk
under the Finnhub Free tier constraint.

Spec-to-runtime mapping for the original spec doc:
  Original spec Phase 2 (Fundamental) → runtime Phase 3
  Original spec Phase 3 (Technical)   → runtime Phase 2
  All other phases unchanged.

This module is intentionally empty at the package level. Each phase lives
in its own file for auditability of the local-first ordering.
"""
