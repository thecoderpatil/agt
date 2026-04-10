# HANDOFF_CODER_latest

**Last updated:** 2026-04-09
**Branch:** `main`
**Status:** V2 5-State architecture wired into `telegram_bot.py`; focused regression slice green; autonomous State 0 unwind execution still TODO by design.

---

## What Shipped

### V2 5-State upgrade in `telegram_bot.py`
- **State 0 / Apex Survival Watchdog:** `_el_snapshot_writer_job` now computes `el_pct = ExcessLiquidity / NetLiquidation`.
- If `el_pct <= 0.08`, the bot now sends:
  - `[🚨 APEX SURVIVAL: Excess Liquidity < 8%. Executing Tied-Unwinds!]`
- The actual tied-unwind execution path is **not** implemented yet.
- A TODO block was added in-place for the autonomous unwind steps.

### Open short-call master router
- `_scan_and_stage_defensive_rolls` was rewritten to act as the V2 router.
- **State 1 / ASSIGN (Act 60 Velocity):**
  - If `spot >= initial_basis` and `delta >= 0.85`, the router stands down and emits:
  - `[ASSIGN] {ticker} Delta > 0.85. Letting shares get called away.`
- **State 1 / ASSIGN (Microstructure Trap):**
  - If `extrinsic <= spread` or `extrinsic <= 0.05`, the router emits:
  - `[ASSIGN] {ticker} Extrinsic exhausted. Parity breached. Defense standing down.`
- **State 2 / HARVEST:**
  - If `pnl_pct >= 0.85` or `RAY < 0.10`, the router stages a BUY-to-close ticket in `pending_orders`.
  - Alert:
  - `[HARVEST] {ticker} Capital dead. Staging BTC.`
- **State 3 / DEFEND:**
  - If `spot < adjusted_basis` and `delta >= 0.40`, the router searches future chains with `DTE < 90`.
  - It stages a BAG debit roll only when:
  - `debit_paid > 0` and `((intrinsic_gained - debit_paid) / debit_paid) >= 2.0`
  - Alert:
  - `[DEFEND] {ticker} EV-Accretive Roll staged.`

### State 4 bifurcated yield paths
- `_walk_harvest_chain` now anchors to **Assigned Basis** (`initial_basis`), not ACB.
- Harvest band enforced at:
  - `HARVEST_MIN_ANNUALIZED_PCT = 30.0`
  - `HARVEST_MAX_ANNUALIZED_PCT = 130.0`
- Harvest now selects the **highest** strike that still falls inside the band.

- `_walk_mode1_chain` now anchors to **Adjusted Cost Basis** (`adjusted_basis`).
- Mode 1 minimum annualized yield is now:
  - `MODE1_MIN_ANNUALIZED_PCT = 10.0`
- Mode 1 now walks **down** from `ACB + 10% of spot`, and it will never stage below ACB.

### Manual command wiring
- `/cc` calls `await _run_cc_logic(None)` and sends the returned `main_text`.
- `/rollcheck` now calls `await _scan_and_stage_defensive_rolls(ib_conn)` and sends the V2 router alerts.
- Both handlers remain registered in `main()`.

### Approval/execution plumbing
- `_place_single_order` now supports:
  - single-leg `BUY` option tickets (needed for BTC)
  - signed BAG prices for debit and credit rolls
- `_build_adaptive_option_order` was added as the generalized single-leg order builder.
- BAG confirmation text now reflects debit vs credit correctly.

---

## Files Changed For This Work

- `telegram_bot.py`
- `tests/test_el_snapshots.py`
- `tests/test_v2_state_router.py`
- `HANDOFF_CODER_latest.md`

---

## Verification Run

Passed locally:

```powershell
python -m pytest tests/test_el_snapshots.py tests/test_v2_state_router.py tests/test_command_prune.py -q
python -m pytest tests/test_halt.py -q
```

Results:
- `15 passed` on the focused V2 + command registration slice
- `6 passed` on the halt/EL writer guardrail slice

---

## Intentional TODO / Known Gap

### State 0 unwind execution
- The scheduler now detects the `<= 8%` EL condition and alerts immediately.
- The autonomous tied-unwind execution itself is still a TODO block.
- That is intentional and matches the current requested scope.

---

## Working Tree Notes

The repo is still dirty outside this task. These files were already modified or untracked and were **not** part of this V2 commit:

- `agt_deck/templates/cure_lifecycle.html`
- `agt_deck/templates/cure_smart_friction.html`
- `tests/test_command_prune.py`
- numerous reports, audit bundles, screenshots, and local artifacts

Do not assume a clean tree after this checkpoint.

---

## Recommended Next Step

If the desk wants State 0 truly autonomous, implement the tied-unwind execution block inside `_el_snapshot_writer_job` using the same account-aware staging and approval-bypass semantics described in the TODO comments.
