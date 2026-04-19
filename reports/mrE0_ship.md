# MR-E.0 Ship Report — CachedAnthropicClient (ADR-010)

## DISPATCH: ADR-010 MR-E.0 — CachedAnthropicClient
## STATUS: applied

## FILES
  agt_equities/cached_client.py          +478/-0  sha256:e98ab5e8
  scripts/migrate_llm_tables.py          +40/-0   sha256:fc7006f4
  tests/test_cached_client.py            +181/-0  sha256:7eb3d954
  tests/test_no_raw_anthropic_imports.py +61/-0   sha256:3b9aa56c
  .gitlab-ci.yml                         +1/-1    sha256:c252bfcf

## COMMIT
  branch: adr010-cached-client-v2
  commit: 17e02fa58cfa7f2fa8124d6f158dc7f5cd018c12
  squash: bdb00528
  MR:     !152
  (MR !151 closed due to CI yaml git conflict from D.0; !152 opened from clean branch)

## CI
  pipeline: 2463671430  941 passed / 3 skipped / 8 deselected
  delta vs D.0 baseline: +9 passed (932 → 941)

## VERIFICATION
  - ast.parse: PASS (cached_client.py, migrate_llm_tables.py, test_cached_client.py, test_no_raw_anthropic_imports.py)
  - yaml.safe_load: PASS (.gitlab-ci.yml)
  - Remote byte-check: 5 files exact match
  - Sentinels present: `class BudgetExceeded`, `class LLMResponse`, `llm_response_cache`, `llm_budget`, `llm_calls`, `NO_UNCACHED_LLM_CALL_IN_HOT_PATH`, `def messages_create`, `def from_env`
  - `__all__` exports: CachedAnthropicClient, LLMResponse, CachedClientError, BudgetExceeded, Timeout, ParseError

## NOTES
  - Codex takeover: Codex halt (codex_halt_E0_20260419.md) — wrong file path (llm/ subdir), LLMResponse absent, BudgetExceeded absent, wrong table names, no budget enforcement. Coder rewrote from dispatch spec.
  - Dispatch LOC estimates wrong (360 declared, 478 actual); format-fixed dispatch used with corrected tolerances.
  - D.0/E.0 sequencing: E.0 MR !151 had CI yaml conflict with D.0 (same pytest command line modified). Resolved by opening !152 from fresh branch based on post-D0 main.
  - Merged results pipeline for !152 showed 932 (main's CI yaml used by runner); post-merge pipeline correctly shows 941 (+9 E.0 tests).
  - Ships dark (no callers yet). ADR-010 §5 tier migration and ADR-012 learning loop land in subsequent MRs.
  - `from_env()` reads ANTHROPIC_API_KEY at instantiation, not module import time.
