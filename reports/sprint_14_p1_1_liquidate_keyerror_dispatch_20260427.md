# Sprint 14 P1.1 — STC liquidate KeyError + ADR-020 enrichment
**Date:** 2026-04-27  
**Branch:** sprint-14-p1-1-stc-liquidate-keyerror  
**MR:** !273  
**Tier:** CRITICAL  

## Summary

Two fixes bundled atomic:

1. `_place_single_order` (telegram_bot.py) — ADR-020 quote-freshness block wrapped in
   `if sec_type == "OPT":`. Stock market-order tickets now bypass the OPT-only gate.
   Bare `payload["limit_price"]` hardened to `payload.get("limit_price", bid)`.

2. `_build_liquidate_tickets` (telegram_bot.py) — injects `engine`, `run_id`,
   `gate_verdicts` into BTC + STC ticket dicts before `append_pending_tickets`.
   Populates the ADR-020 DB columns that rows 438+439 lacked.

## Files

- `telegram_bot.py` — Fix 1: +1/-0 net (guard added); Fix 2: +14/-2 net (run_id + 6 keys per ticket)
- `tests/test_sprint14_p1_1_liquidate_enrichment.py` — new, 3 tests, ~50 lines

## Commit message

```
Sprint 14 P1.1: STC liquidate KeyError + ADR-020 enrichment

_place_single_order ADR-020 quote-freshness block now gated on
sec_type=="OPT". STK/MKT orders skip _ibkr_get_option_bid and the
bare payload["limit_price"] access that produced the KeyError on
every LIQUIDATE since MR !71. Defense-in-depth: payload.get().

_build_liquidate_tickets injects engine/run_id/gate_verdicts into
BTC+STC ticket dicts so append_pending_tickets populates ADR-020
columns. Fixes NULL engine/run_id/gate_verdicts for future liquidates
(rows 438+439 stay NULL — pre-fix baseline, documented).
```

## Verification

- Sentinel: `if sec_type == "OPT":` in telegram_bot.py
- Sentinel: `payload.get("limit_price"` in telegram_bot.py
- Sentinel: `v2_router_liquidate` in telegram_bot.py
- Walker grep: zero matches (no walker.py touch)
- pytest tests/test_sprint14_p1_1_liquidate_enrichment.py → 3/3

```yaml expected_delta
files:
  telegram_bot.py:
    added: 15
    removed: 1
    net: 14
    tolerance: 5
    required_sentinels:
      - "if sec_type == \"OPT\":"
      - "payload.get(\"limit_price\""
      - "v2_router_liquidate"
  tests/test_sprint14_p1_1_liquidate_enrichment.py:
    added: 61
    removed: 0
    net: 61
    tolerance: 5
    required_sentinels:
      - "test_stc_market_order_no_keyerror"
      - "v2_router_liquidate"
      - "sprint_a"
```
