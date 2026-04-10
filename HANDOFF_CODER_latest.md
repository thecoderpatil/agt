# HANDOFF_CODER_latest

**Last updated:** 2026-04-10
**Branch:** `main`
**Status:** V2 5-State router is live in `telegram_bot.py`; Apex Survival alerting is now debounced and margin-account-gated; State 1/2 routing math was corrected; autonomous State 0 unwind execution remains TODO by design.

---

## What Shipped

### V2 5-State router in `telegram_bot.py`
- `_scan_and_stage_defensive_rolls` is the active V2 master router for open short calls.
- **State 1 / ASSIGN (Act 60 Velocity):**
  - If `spot >= initial_basis` and `delta >= 0.85`, the router stands down and emits:
  - `[ASSIGN] {ticker} Delta > 0.85. Letting shares get called away.`
- **State 1 / ASSIGN (Microstructure Trap):**
  - Corrected on 2026-04-10 to require the short call to be **ITM** before parity logic can fire.
  - Current logic:
  - `intrinsic_value > 0 and (extrinsic_value <= spread or extrinsic_value <= 0.05)`
  - This fixes the false positives on cheap far-OTM names like `PYPL` / `UBER`.
- **State 2 / HARVEST:**
  - Corrected on 2026-04-10 to compute `pnl_pct` from `initial_credit`.
  - The router now explicitly stages BTC tickets for `pnl_pct >= 0.85` even if the live ask is only `$0.01`.
  - Trigger condition remains:
  - `pnl_pct >= 0.85 or RAY < 0.10`
  - Alert:
  - `[HARVEST] {ticker} Capital dead. Staging BTC.`
- **State 3 / DEFEND:**
  - If `spot < adjusted_basis` and `delta >= 0.40`, the router searches future chains with `DTE < 90`.
  - It stages a BAG debit roll only when:
  - `debit_paid > 0` and `((intrinsic_gained - debit_paid) / debit_paid) >= 2.0`
  - Alert:
  - `[DEFEND] {ticker} EV-Accretive Roll staged.`

### State 0 / Apex Survival watchdog
- `_el_snapshot_writer_job` computes `el_pct = ExcessLiquidity / NetLiquidation`.
- If a qualifying margin account falls to `<= 8%`, the bot sends:
  - `[🚨 APEX SURVIVAL: Excess Liquidity < 8%. Executing Tied-Unwinds!]`
- The actual tied-unwind execution path is still **not** implemented.
- The in-place TODO block was preserved.

### Apex watchdog hardening landed on 2026-04-10
- New global tracker:
  - `_apex_last_alert: dict[str, float]`
- `nlv <= 0` guard prevents reset-window junk values and divide-by-zero cases from firing the watchdog.
- Alerting is now debounced per account for **15 minutes** (`900s`).
- Recovery instantly clears the lock so a fresh drop can alert immediately.
- Apex Survival now evaluates **only** `MARGIN_ACCOUNTS`.
- Cash accounts still write 30-second `el_snapshots` rows to SQLite, but they never trigger State 0 survival alerts.

### State 4 bifurcated yield paths
- `_walk_harvest_chain` anchors to **Assigned Basis** (`initial_basis`), not ACB.
- Harvest band enforced at:
  - `HARVEST_MIN_ANNUALIZED_PCT = 30.0`
  - `HARVEST_MAX_ANNUALIZED_PCT = 130.0`
- Harvest selects the **highest** strike still inside the band.

- `_walk_mode1_chain` anchors to **Adjusted Cost Basis** (`adjusted_basis`).
- Mode 1 minimum annualized yield is:
  - `MODE1_MIN_ANNUALIZED_PCT = 10.0`
- Mode 1 walks **down** from `ACB + 10% of spot` and never stages below ACB.

### Manual command wiring
- `/cc` calls `await _run_cc_logic(None)` and sends the returned `main_text`.
- `/rollcheck` calls `await _scan_and_stage_defensive_rolls(ib_conn)` and sends the V2 router alerts.
- Both handlers remain registered in `main()`.

### Approval / execution plumbing
- `_place_single_order` supports:
  - single-leg `BUY` option tickets for BTC
  - signed BAG prices for debit and credit rolls
- `_build_adaptive_option_order` is the generalized single-leg option order builder.
- BAG confirmation text now distinguishes debit vs credit correctly.

---

## Files Changed In This Checkpoint

- `telegram_bot.py`
- `tests/test_el_snapshots.py`
- `tests/test_v2_state_router.py`
- `HANDOFF_CODER_latest.md`

---

## Verification Run

Passed locally:

```powershell
python -m py_compile telegram_bot.py
python -m pytest tests/test_el_snapshots.py tests/test_v2_state_router.py tests/test_command_prune.py -q
python -m pytest tests/test_halt.py -q
python -m pytest tests/test_el_snapshots.py -q
python -m pytest tests/test_v2_state_router.py -q
```

Results:
- `15 passed` on the focused V2 + command registration slice
- `6 passed` on the halt / EL writer guardrail slice
- `9 passed` on the latest EL watchdog regression slice
- `6 passed` on the latest V2 router regression slice

---

## Intentional TODO / Known Gap

### State 0 unwind execution
- The scheduler now detects the `<= 8%` EL condition, but only for `MARGIN_ACCOUNTS`.
- It debounces alerts for 15 minutes and clears the lock immediately on recovery.
- The autonomous tied-unwind execution itself is still a TODO block.
- That is intentional and matches the requested scope so far.

---

## Working Tree Notes

The repo is still dirty outside this task. These files were already modified or untracked and were **not** part of this checkpoint:

- `agt_deck/templates/cure_lifecycle.html`
- `agt_deck/templates/cure_smart_friction.html`
- `tests/test_command_prune.py`
- numerous reports, screenshots, bundles, and local artifacts

Do not assume a clean tree after this checkpoint.

---

## Recommended Next Step

If the desk wants State 0 truly autonomous, implement the tied-unwind execution block inside `_el_snapshot_writer_job` using the same account-aware staging and approval-bypass semantics already described in the TODO comments.
