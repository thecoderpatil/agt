DISPATCH: Sprint 14 P1.1 — STC liquidate KeyError + ADR-020 enrichment
STATUS: applied
FILES:
  telegram_bot.py  +15/-1  sha256:0348dd4b
  tests/test_sprint14_p1_1_liquidate_enrichment.py  +61/-0  sha256:d534cf20
  reports/sprint_14_p1_1_liquidate_keyerror_dispatch_20260427.md  +69/-0  sha256:5ef07499
COMMIT:
  squash: 4f85748be3318b3dbcaca7100873046dd38e0644
  merge:  04a51344baf1130e61e8cf70b360e3f28f202121
  MR:     !275
CI:
  pipeline: 2483821981   1462 passed / 1 skipped / 6 failed / 8 deselected
  delta vs baseline: 0 (6 failures = pre-existing test_news_adapters.py live-API flake;
    identical across 2 pipeline runs; unrelated to our telegram_bot.py/liquidate changes;
    confirmed: our 3 new sprint_a tests pass within the 1462 count)
VERIFICATION:
  pytest local: 3/3 passed (test_sprint14_p1_1_liquidate_enrichment.py)
  AST parse: OK (23223 lines)
  LOC gate: PASS
  walker grep: 0 matches (walker.py untouched)
  sentinel if sec_type == "OPT":: 6 matches in telegram_bot.py (was 5 pre-fix)
  sentinel payload.get("limit_price": 2 matches in telegram_bot.py
  sentinel v2_router_liquidate: 2 matches in telegram_bot.py
LOCAL_SYNC:
  fetch/reset:     done (04a5134 → origin/main)
  pip install:     done (no new runtime deps)
  smoke imports:   ok (AST parse + sentinel grep; env-dependent import skipped, AST clean)
  deploy.ps1:      exit 0  (C:\AGT_Telegram_Bridge\.worktrees\coder\scripts\deploy\deploy.ps1)
  heartbeats:      agt_bot=2026-04-28T01:37:55Z  agt_scheduler=2026-04-28T01:38:19Z
NOTES:
  Fix 1: ADR-020 quote-freshness block in _place_single_order wrapped in
    "if sec_type == 'OPT':". STK/MKT LIQUIDATE tickets now bypass _ibkr_get_option_bid
    and bare payload["limit_price"] access. Defense-in-depth: payload.get("limit_price", bid).
  Fix 2: _build_liquidate_tickets now injects engine="v2_router_liquidate",
    run_id=uuid.uuid4().hex, gate_verdicts={"v2_router":True,"liquidate":True} into
    both BTC and STC ticket dicts before append_pending_tickets.
  Rows 438+439 stay NULL — pre-fix baseline, documented in forensic report.
  Future liquidates will have ADR-020 columns populated.
  CI flake: test_news_adapters.py (6 tests) fail on live yfinance/finnhub API returning
    empty results. Pre-existing, confirmed identical on 2 independent pipeline runs.
    Not introduced by this MR.
