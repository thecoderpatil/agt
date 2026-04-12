# HANDOFF_CODER_latest

**Last updated:** 2026-04-11
**Branch:** `main`
**Head:** `0268d3c` — `M2: csp_harvest module + /csp_harvest command + watchdog hook`
**Status:** CSP allocator subsystem is now complete through M1.4 (sizing + routing + gates + orchestrator). M2 landed the CSP profit-take harvester (`agt_equities/csp_harvest.py`) with `/csp_harvest` manual trigger and a scheduled watchdog sweep. V2 5-State router remains the authority for short calls; `csp_harvest` is the analogous subsystem for short puts. Full suite: **925 passed, 5 skipped**.

---

## What Shipped Since 2026-04-10

### Sprint M1.2 — CSP pure sizing + routing (`e67bebf`)
- `agt_equities/csp_allocator.py` gained:
  - Constants `CSP_TARGET_NLV_PCT=0.10`, `CSP_CEILING_NLV_PCT=0.20`, `MAINTENANCE_MARGIN_HAIRCUT=0.30`
  - `VIX_RETAIN_TABLE` and `_vix_retain_pct(vix)` helper (Rule 2 VIX-scaled deployment governor)
  - `_csp_size_household(hh_snapshot, candidate, vix) -> int` — NLV-proportional sizing with Rule 1 ceiling + Rule 2 feasibility probe, prefers lower on tie
  - `_csp_route_to_accounts(n_contracts, hh_snapshot, candidate) -> list[dict]` — IRA-first / margin-last greedy fill with partial allocation
- +12 tests → suite at 21 CSP allocator tests.
- **Latent known issue:** IRA-only households with `hh_margin_nlv=0` cannot size any contracts because `_csp_size_household`'s Rule 2 feasibility probe computes `new_margin_impact > 0 = headroom` and skips. This contradicts the Rule 2 gate predicate (which short-circuits on `hh_margin_nlv=0`). Documented in M1.4 discovery; not fixed in M2. Will need attention before IRA-only households see live allocation.

### Sprint M1.3 — CSP pure gate predicates + registry (`de45176`)
- Added `CSPGate = Callable[[dict, Any, int, float, dict], tuple[bool, str]]` type alias.
- Six uniform-signature gate predicates landed in `csp_allocator.py`:
  - `_csp_check_rule_1` — 20% NLV concentration ceiling
  - `_csp_check_rule_2` — VIX-scaled deployment governor (short-circuits on IRA-only)
  - `_csp_check_rule_3` — ≤2 names per GICS sector (fail-open on missing sector data — documented, not a bug)
  - `_csp_check_rule_4` — ≤0.6 correlation (fail-open on missing corr data)
  - `_csp_check_rule_6` — Vikram 20% EL floor
  - `_csp_check_rule_7` — delta ≤0.25, 7-day earnings blackout, working-order dedup
- `CSP_GATE_REGISTRY: list[tuple[str, CSPGate]]` enumerates the 6 gates in canonical order for composable iteration.
- +16 tests → suite at 37 CSP allocator tests.

### Sprint M1.4 — CSP orchestrator + AllocatorResult (`cce3541`)
- New `AllocatorResult` dataclass with `staged / skipped / errors / digest_lines` lists and `total_staged_contracts` / `total_staged_notional` properties.
- New `run_csp_allocator(ray_candidates, snapshots, vix, extras_provider, staging_callback=None) -> AllocatorResult` orchestrator:
  - Outer loop over `candidates × households` with try/except per pair
  - 6-step dispatch in `_process_one`: extras → gates (registry iteration) → size → route → stage → mutate
  - In-memory snapshot mutation between candidates prevents double-booking under a single allocator call
- `_format_digest(result)` emits human-readable telegram-style digest lines.
- Injection-based design: `staging_callback` and `extras_provider` are parameters, preserving the one-way dependency rule (no `telegram_bot` import from `csp_allocator.py`).
- +8 orchestrator tests → suite at 45 CSP allocator tests.

### Sprint M2 — CSP profit-take harvester (`0268d3c`) **[THIS CHECKPOINT]**
- New module `agt_equities/csp_harvest.py` (~280 lines):
  - Thresholds: `CSP_HARVEST_THRESHOLD_NEXT_DAY = 0.80` (dte ≥ 1), `CSP_HARVEST_THRESHOLD_LAST_DAY = 0.90` (dte ≤ 1)
  - `_should_harvest_csp(initial_credit, current_ask, dte) -> (bool, str)` — pure predicate. Rejects None/NaN/inf/zero-credit/negative-ask (guards the IBKR OPRA-missing path documented in C6.2). Returns structured reason string.
  - `async scan_csp_harvest_candidates(ib_conn, staging_callback=None) -> dict` — mirrors V2 router STATE_2 HARVEST flow (`telegram_bot.py:8768-8869`) with these adjustments: filters for short **puts** (`right == "P"`), excludes `EXCLUDED_TICKERS`, uses `_should_harvest_csp` thresholds instead of the CC 0.85 rule, tags tickets with `mode="CSP_HARVEST"` / `origin="csp_harvest"`, no spot fetch / no Greeks / no ledger (pure profit-take).
  - Returns `{staged, skipped, errors, alerts}`.
  - `EXCLUDED_TICKERS` is defined **locally** in `csp_harvest.py` as a frozenset mirror of the `telegram_bot.py:1463` set (5 entries). This preserves the one-way dependency rule (`csp_harvest` MUST NOT import `telegram_bot`). Maintenance note: keep in sync manually if the blocklist changes — the constant rarely moves.
- Wired into `telegram_bot.py`:
  - `async def cmd_csp_harvest` handler after `cmd_rollcheck` (~L6714). Imports `scan_csp_harvest_candidates` locally, wraps `append_pending_tickets` as the staging callback, renders a digest via `<pre>`.
  - `CommandHandler("csp_harvest", cmd_csp_harvest)` registered in `main()`.
  - `/csp_harvest` menu line added in the `_send_command_menu` Trade block.
  - `_scheduled_watchdog` now sweeps CSP harvest candidates **before** the cache cleanup block. Uses the same `append_pending_tickets` staging callback and swallows exceptions with a warning log (watchdog-safety contract — a flaky IBKR connection must not bring down the 3:30 PM ET scheduled sweep).
- 11 new tests in `tests/test_csp_harvest.py`:
  - 6 threshold tests (next-day 80% pass, next-day <80% fail, last-day 90% pass, last-day 85% fail, zero-credit reject, NaN/None reject)
  - 1 constants-sanity test (guards against dispatch drift on threshold values)
  - 4 FakeIB scanner tests (passing put stages, below-threshold skips, staging_callback injection, `reqPositionsAsync` failure returns structured error not propagation)
- `tests/test_csp_harvest.py` uses an `autouse` fixture to patch `agt_equities.csp_harvest.asyncio.sleep` to a no-op, eliminating the 2s `reqMktData` settle wait in CI.

---

## V2 5-State Router (unchanged since 2026-04-10)

The V2 router in `_scan_and_stage_defensive_rolls` remains the authority for open short **calls** and is not touched by M2:

- **State 1 / ASSIGN (Act 60 Velocity):** `spot >= initial_basis and delta >= 0.85` → stand down.
- **State 1 / ASSIGN (Microstructure Trap):** `intrinsic_value > 0 and (extrinsic_value <= spread or extrinsic_value <= 0.05)`.
- **State 2 / HARVEST:** `pnl_pct >= 0.85 or RAY < 0.10` → stage BTC at live ask.
- **State 3 / DEFEND:** `spot < adjusted_basis and delta >= 0.40` → BAG debit roll when `((intrinsic_gained - debit_paid) / debit_paid) >= 2.0`.

CSP harvest is the analogous subsystem for short **puts** but with its own module, its own thresholds, and its own command/watchdog wiring.

---

## State 0 / Apex Survival Watchdog (unchanged since 2026-04-10)

- `_el_snapshot_writer_job` computes `el_pct = ExcessLiquidity / NetLiquidation`, debounces per account for 15 minutes (`_apex_last_alert` global), evaluates only `MARGIN_ACCOUNTS`, clears the lock immediately on recovery.
- Alert: `[🚨 APEX SURVIVAL: Excess Liquidity < 8%. Executing Tied-Unwinds!]`
- Autonomous tied-unwind execution is still the intentional TODO block.

---

## Architectural Contracts Preserved

- **One-way dependency rule:** `agt_equities/csp_allocator.py` and `agt_equities/csp_harvest.py` do NOT import `telegram_bot`. Verified: `grep '^(from telegram_bot|import telegram_bot)'` returns 0 matches in both modules. `telegram_bot.py` is the only direction — it imports from `agt_equities.csp_allocator` / `agt_equities.csp_harvest` (the latter via local import inside `cmd_csp_harvest` and `_scheduled_watchdog`).
- **Pure-core + injected callbacks:** Core logic (`_csp_size_household`, `_csp_route_to_accounts`, gate predicates, `run_csp_allocator`, `_should_harvest_csp`) has no IB/DB side effects. Staging goes through injected `staging_callback` parameters so tests can supply list-append sinks.
- **Screener isolation guard:** `tests/test_screener_isolation.py` remains 3/3 green — neither M1.2–M1.4 nor M2 touched the screener package.

---

## Files Changed In This Checkpoint (M2 only)

- `agt_equities/csp_harvest.py` (NEW, 280 lines)
- `tests/test_csp_harvest.py` (NEW, ~280 lines, 11 tests)
- `telegram_bot.py` (+69 lines: menu line, `cmd_csp_harvest` handler, `CommandHandler` registration, watchdog hook)
- `HANDOFF_CODER_latest.md` (this file)

---

## Verification Run (2026-04-11)

```powershell
python -m pytest tests/test_csp_harvest.py -q
python -m pytest tests/test_csp_harvest.py tests/test_csp_allocator.py -q
python -m pytest tests/ -q
python -m pytest tests/test_screener_isolation.py -q
python -c "import telegram_bot"
python -c "from agt_equities.csp_harvest import scan_csp_harvest_candidates, _should_harvest_csp"
```

Results:
- `11 passed` on `tests/test_csp_harvest.py`
- `56 passed` on the combined CSP slice (`csp_harvest` + `csp_allocator`)
- `925 passed, 5 skipped` on the full suite (was `914 / 5` pre-M2; +11 new tests accounted for)
- `3 passed` on screener isolation guard
- Both import smokes clean
- `CSP_GATE_REGISTRY` still = 6 entries (M1.3 untouched)

---

## Intentional TODO / Known Gaps

### State 0 unwind execution
- Unchanged from 2026-04-10 — detection + debounce are live, autonomous tied-unwind execution is still the TODO block.

### CSP allocator — IRA-only household sizing
- `_csp_size_household` cannot size any contracts for a household with `hh_margin_nlv=0` (all-IRA). The Rule 2 feasibility probe inside sizing is stricter than the Rule 2 gate predicate. Fix will land in a future M1.x sprint; M2 does not touch allocator internals.

### CSP harvest — `EXCLUDED_TICKERS` manual sync
- Mirrored locally in `csp_harvest.py` to preserve the no-`telegram_bot`-import rule. If the `telegram_bot.py:1463` blocklist changes, update the local frozenset in `csp_harvest.py` to match. Consider promoting `EXCLUDED_TICKERS` into `agt_equities/config.py` in a future housekeeping pass so both modules can import from a single source.

### M1.5 — allocator wiring into `/cmd_scan`
- `run_csp_allocator` exists but is not yet wired into any manual command or scheduled job. M1.5 will add the integration (pass pre-fetched `_discover_positions` output + VIX + RAY candidates into `run_csp_allocator`, render the digest via `<pre>`).

---

## Working Tree Notes

The repo is still dirty outside this task. These files were already modified or untracked and were **not** part of this checkpoint:

- `agt_deck/templates/cure_lifecycle.html`
- `agt_deck/templates/cure_smart_friction.html`
- `tests/test_command_prune.py`
- `tests/test_rule_9_contradiction.py`
- numerous reports, screenshots, bundles, ADRs, and local artifacts under `reports/`, `audit_packages/`, `architect_kb_day2/`

Do not assume a clean tree after this checkpoint.

---

## Recommended Next Step

**Sprint M1.5** — wire `run_csp_allocator` into a `/cmd_scan` (or equivalent) telegram handler. The orchestrator already takes `staging_callback` as a parameter, so the integration is a thin shim that:
1. Fetches `_discover_positions` output + current VIX + RAY candidates from the screener pipeline
2. Builds an `extras_provider` closure that returns sector/correlation/earnings data per candidate
3. Calls `run_csp_allocator(...)` with `append_pending_tickets` wrapped as the `staging_callback`
4. Renders `result.digest_lines` via `<pre>` to the operator

Alternatively, address the IRA-only sizing gap in `_csp_size_household` first if Vikram_Household or the dormant Trad IRA will need allocation before live go-live.
