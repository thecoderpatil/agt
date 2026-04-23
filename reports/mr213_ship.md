# MR !213 ship report — LOW-severity bundle (Sprint 3 MR 8, E-L-1..E-L-4 minus a5e)

## Status
MERGED. Squash `6a395167`, merge `e71b18a0`.

## Scope (4 of 5 items)

1. **E-L-1 + E-L-2** — `VIKRAM_HOUSEHOLD` + `YASH_HOUSEHOLD` constants exported from `agt_equities/config.py`. Updated 3 call sites:
   - `rule_engine.py:759, 1458`
   - `csp_allocator.py:821`
2. **E-L-3** — `tests/test_acb_per_account.py`: 5 skip markers converted to `@pytest.mark.xfail(strict=True, ...)` with bodies changed from bare `pass` to `raise NotImplementedError(reason)`. Without the raise, strict=True xfail would report xpass-fail on every run; the raise makes the xfail real.
3. **E-L-4** — `agt_equities/trade_repo.py`: `_CYCLE_CACHE_LOCK = threading.Lock()` guards `_cycle_cache`. Grep confirmed no production writer exists today (cache declared but unused by `_run_walker_for_all`). Lock is a tripwire for future writers.

## Punted

- **a5e atomic_cutover tripwire** — `reports/mr201_ship.md` not on disk; tripwire spec unreconstructable in this sprint. Needs MR !201 commit diff to reconstruct. Follow-on MR.

## Delta
+36 / -18 net +18 across 5 files. Per-MR dispatch fence: `reports/sprint3_mr8_dispatch.md` — GATE PASS.

## Verification
- `tests/test_acb_per_account.py`: 5 xfailed as expected, 3 pre-existing unrelated failures (DB path infra — not caused by this MR)
- `ast.parse` clean on all 5 files
- Sentinel: `VIKRAM_HOUSEHOLD` constant + import appears in rule_engine.py and csp_allocator.py; `_CYCLE_CACHE_LOCK` in trade_repo.py

## CI
Pipeline 2474433374: compliance + sprint_a_unit_tests both green.

## LOCAL_SYNC
```
LOCAL_SYNC:
  fetch/reset:     done (Coder worktree)
  pip install:     no new deps
  smoke imports:   deferred — STANDARD tier
  deploy.ps1:      deferred — agt_equities/** touches (CRITICAL tier for config+rule_engine+csp_allocator+trade_repo); batched redeploy at sprint close
  heartbeats:      n/a
```
