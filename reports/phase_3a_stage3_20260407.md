# Phase 3A Stage 3 Implementation Report — Telegram Integration

**Date:** 2026-04-07
**Author:** Coder (Claude Code)
**Status:** COMPLETE — awaiting Yash review before Stage 4
**Tests:** 170/170 (156 existing + 14 new mode gate/transition/message tests)
**Runtime:** 14.29s

---

## Files Changed

| File | Change |
|------|--------|
| `telegram_bot.py` | Added: `_get_current_desk_mode()`, `_check_mode_gate()`, `_push_mode_transition()` helpers. Added 4 commands: `cmd_declare_wartime`, `cmd_declare_peacetime`, `cmd_mode`, `cmd_cure`. Updated `/scan` with PEACETIME-only mode gate. Updated `/cc` with AMBER-or-below mode gate. Registered all 4 new handlers. |
| `tests/test_phase3a.py` | Added `TestModeGateLogic` (6 tests) + `TestModeTransitionFlow` (5 tests) |

---

## New Commands

### `/declare_wartime [reason]`

Manually escalates to WARTIME mode. Logs transition to `mode_history` with audit trail. Pushes alert to Telegram.

```
/declare_wartime margin call imminent
→ 🚨 WARTIME declared.
  Previous mode: PEACETIME
  Reason: margin call imminent
  All commands blocked except Cure Console actions.
  Use /declare_peacetime to revert (requires audit memo).
```

### `/declare_peacetime <audit_memo>`

Reverts from WARTIME/AMBER to PEACETIME. **Requires audit memo when reverting from WARTIME** (enforced at command level — empty memo is rejected).

```
/declare_peacetime leverage cured to 1.45x across both households
→ ✅ PEACETIME restored.
  Previous mode: WARTIME
  Audit memo: leverage cured to 1.45x across both households
```

### `/mode`

Shows current desk mode and last 3 transitions.

```
/mode
→ ✅ Current mode: PEACETIME

  Recent transitions:
    2026-04-07T17:00: PEACETIME → PEACETIME (manual)
      Phase 3A Day 1 initialization
```

### `/cure`

Returns link to the Deck Cure Console. Includes Tailscale note.

```
/cure
→ ✅ Mode: PEACETIME

  Cure Console: http://127.0.0.1:8787/cure?t=<token>

  (Tailscale: replace 127.0.0.1 with your Tailscale IP)
```

---

## Mode Gates

### `/scan` — blocked in AMBER and WARTIME

```python
# Phase 3A: Mode gate — /scan blocked in AMBER and WARTIME (CSP entry)
mode_ok, mode_msg = _check_mode_gate("PEACETIME")
if not mode_ok:
    await update.message.reply_text(mode_msg)
    return
```

**Existing Rule 11 leverage check preserved** — runs after the mode gate. Both checks must pass.

### `/cc` — blocked in WARTIME only

```python
# Phase 3A: Mode gate — /cc blocked in WARTIME only (exits/rolls allowed in AMBER)
mode_ok, mode_msg = _check_mode_gate("AMBER")
if not mode_ok:
    await update.message.reply_text(mode_msg)
    return
```

### Gate Logic

```python
def _check_mode_gate(required_mode_max: str) -> tuple[bool, str]:
    mode_rank = {"PEACETIME": 0, "AMBER": 1, "WARTIME": 2}
    # If current_rank > allowed_rank → blocked
```

| Command | `required_mode_max` | Blocked in |
|---------|---------------------|------------|
| `/scan` | `PEACETIME` | AMBER, WARTIME |
| `/cc` | `AMBER` | WARTIME |

---

## Push Alerts

Mode transitions fire `_push_mode_transition()` which sends a Telegram message to `AUTHORIZED_USER_ID`:

```
🚨 DESK MODE: PEACETIME → WARTIME
Manual: margin call imminent
```

Emoji mapping:
- PEACETIME: ✅
- AMBER: ⚠️
- WARTIME: 🚨

---

## Mode Gate Test Matrix (verified)

| Current Mode | `/scan` (PEACETIME gate) | `/cc` (AMBER gate) |
|-------------|--------------------------|---------------------|
| PEACETIME | ✅ allowed | ✅ allowed |
| AMBER | ⛔ blocked | ✅ allowed |
| WARTIME | ⛔ blocked | ⛔ blocked |

---

## New Tests (11)

### TestModeGateLogic (6 tests)

| Test | Assertion |
|------|-----------|
| `test_peacetime_allows_scan` | PEACETIME rank ≤ PEACETIME gate |
| `test_amber_blocks_scan` | AMBER rank > PEACETIME gate |
| `test_amber_allows_cc` | AMBER rank ≤ AMBER gate |
| `test_wartime_blocks_cc` | WARTIME rank > AMBER gate |
| `test_wartime_blocks_scan` | WARTIME rank > PEACETIME gate |
| `test_peacetime_allows_cc` | PEACETIME rank ≤ AMBER gate |

### TestModeTransitionFlow (5 tests)

| Test | Flow |
|------|------|
| `test_peacetime_to_wartime` | P→W logged + readable |
| `test_wartime_to_peacetime` | W→P with audit memo |
| `test_peacetime_to_amber_to_wartime` | P→A→W escalation |
| `test_wartime_requires_audit_memo_concept` | Notes field populated |
| `test_transition_history_limit` | 10 transitions, limit=3 returns 3 |

---

## Backward Compatibility

| Command | Before | After |
|---------|--------|-------|
| `/scan` | Rule 11 check only | Mode gate (PEACETIME) + Rule 11 check |
| `/cc` | No compliance gate | Mode gate (AMBER) — new, non-breaking |
| `/reconcile` | Unchanged | Unchanged |
| `/orders` | Unchanged | Unchanged |
| All other commands | Unchanged | Unchanged |

**Current mode is PEACETIME** → no commands are blocked today. The gates are additive — they add a check before existing logic, never remove existing checks.

---

## Verified on Live

```
$ python -c "from telegram_bot import _check_mode_gate, _get_current_desk_mode; ..."
Current desk mode: PEACETIME
scan gate (PEACETIME): allowed=True
cc gate (AMBER): allowed=True
```

---

## Post-Review Fixes (Q1-Q5 from Chat audit)

### Q1: Audit memo persistence
**Already correct.** Both `/declare_wartime` and `/declare_peacetime` write to `mode_history.notes` via `log_mode_transition(conn, ..., notes=...)`. Queryable: `SELECT notes FROM mode_history WHERE new_mode='PEACETIME'`.

### Q2: Mode source
**Reads from `mode_history` TABLE, not `desk_state.md`.** `_get_current_desk_mode()` → `get_current_mode(conn)` → `SELECT new_mode FROM mode_history ORDER BY id DESC LIMIT 1`. Zero file staleness — direct SQLite read. Mode transitions are visible within the same WAL transaction boundary.

### Q3: `/declare_wartime` reason — FIXED
**Was optional** (default "manual escalation"). Now **required** — empty args returns:
```
⛔ Reason required for audit trail.
Usage: /declare_wartime <reason for escalation>
```
Symmetric with `/declare_peacetime` memo enforcement. Both directions now have mandatory forensic context.

### Q4: Race condition (TOCTOU)
**Gate-then-execute.** The mode gate reads at `/scan` entry. If mode flips PEACETIME→AMBER between gate check and scan execution, the scan proceeds through the TOCTOU window. This is inherent to any check-then-act pattern. Mitigation: mode transitions are infrequent (manual or 5-min cron), scan takes 5-10s, single-operator desk. The risk is negligible. A stricter solution (hold a DB lock during scan) would add complexity disproportionate to the risk.

### Q5: Test coverage on blocked message — FIXED
**Added 3 tests** in `TestModeGateMessage`:
- `test_blocked_message_contains_mode_name`: message includes "AMBER" and "/cure"
- `test_wartime_blocked_message`: message includes "WARTIME" and "/cure"
- `test_blocked_message_not_silent`: blocked path always returns 20+ char message, never empty

---

## Followups (Stage 4)

- Full validation pass against prod DB in read-only mode
- Generate sample `desk_state.md`
- Render Cure Console screenshots
- Confirm mode engine computes PEACETIME on Day 1
- Final implementation report

---

**STOP. Awaiting Yash review before Stage 4.**
