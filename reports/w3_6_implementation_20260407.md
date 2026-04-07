# W3.6 Implementation Report — Walker Warnings UI Integration

**Date:** 2026-04-07
**Author:** Coder (Claude Code)
**Status:** COMPLETE — awaiting Yash review
**Tests:** 91/91 (77 original + 14 new)

---

## Files Changed

| File | Change |
|------|--------|
| `agt_equities/walker.py` | Added `WalkerWarning` dataclass (frozen). Migrated `_walker_warnings` from `list[str]` to `list[WalkerWarning]`. Updated 3 `.append()` sites + `get_walker_warnings()` return type. Added `EXCLUDED_SKIP` warning at excluded-ticker skip. Updated logger format. |
| `agt_equities/schema.py` | Added `walker_warnings_log` table DDL + `idx_walker_warnings_sync_id` index inside `register_master_log_tables()`. |
| `agt_equities/flex_sync.py` | Added `_persist_walker_warnings()` helper. Called at end of successful `run_sync()` inside try/except (non-fatal). |
| `telegram_bot.py` | Imported `get_walker_warnings`. Fixed stale-warnings accumulation bug in `/reconcile` loop. Appended `WALKER WARNINGS` section to output. |
| `agt_deck/main.py` | Added `walker_warning_count` and `walker_worst_severity` to `build_top_strip()` return dict, reading from `walker_warnings_log`. |
| `agt_deck/templates/command_deck.html` | Added "Warn" badge between Sector and Sync in top strip. |
| `tests/test_walker.py` | Added 14 new tests across 5 test classes. |

---

## WalkerWarning Dataclass

```python
@dataclass(frozen=True)
class WalkerWarning:
    code: str                                     # COUNTER_GUARD, UNKNOWN_ACCT, ORPHAN_TRANSFER, EXCLUDED_SKIP
    severity: Literal["INFO", "WARN", "ERROR"]
    ticker: str | None
    household: str | None
    account: str | None
    message: str
    context: dict = field(default_factory=dict)
```

**Warning codes and severity mapping:**

| Code | Severity | Emit site |
|------|----------|-----------|
| `COUNTER_GUARD` | WARN | `_guard_decrement()` — counter would go negative |
| `UNKNOWN_ACCT` | ERROR | `walk_cycles()` validation — account not in HOUSEHOLD_MAP |
| `ORPHAN_TRANSFER` | WARN | `walk_cycles()` — TRANSFER_IN with no active cycle |
| `EXCLUDED_SKIP` | INFO | `walk_cycles()` — excluded ticker skipped (NEW in W3.6) |

---

## Pre-existing Bug Fix: Stale Warnings Accumulation

**Bug:** `walk_cycles()` clears `_walker_warnings` on each invocation (line 571). The `/reconcile` loop calls `walk_cycles()` per (household, ticker) group. After the loop, `get_walker_warnings()` only returns the LAST group's warnings — all prior groups' warnings are lost.

**Fix:** Accumulate in the caller:
```python
all_walker_warnings = []
for (hh, tk), grp in _gb(combined, key=lambda e: (e.household_id, e.ticker)):
    try:
        all_cycles.extend(_wc(list(grp)))
        all_walker_warnings.extend(_gww())  # capture before next call clears
    except _UE:
        frozen_count += 1
```

**NOT changed:** `walk_cycles()` internals — purity preserved. The function still clears its buffer on entry and returns warnings from its single walk only.

**Regression test:** `TestStaleWarningsAccumulation.test_multi_group_accumulation` — walks two excluded tickers, verifies both warnings are captured in accumulated list, and that `get_walker_warnings()` alone only has the last group's warnings.

---

## walker_warnings_log Table

```sql
CREATE TABLE IF NOT EXISTS walker_warnings_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    code            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    ticker          TEXT,
    household       TEXT,
    account         TEXT,
    message         TEXT NOT NULL,
    context_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_walker_warnings_sync_id ON walker_warnings_log(sync_id);
```

**Write path:** `flex_sync.py:_persist_walker_warnings()` — runs full walker pass after successful sync, writes all warnings keyed by sync_id. Non-fatal: if walker fails, sync result is not affected (error logged to stderr).

**Read path:** `agt_deck/main.py:build_top_strip()` — reads count + worst severity from latest sync_id.

---

## /reconcile Telegram Output — Worked Examples

### Current live state (0 warnings):

```
RECONCILIATION REPORT
Last sync: #42 2026-04-07T18:30:00 (success, 150 rows)
Parity: 438/438
Cycles: 173 (14 active, 171 wheel, 2 satellite)
Frozen: 0

A (realized P&L): 49/49
B (cost basis): 14/14
C (NAV recon): 3/4
  U21971297: $0.15
  U22388499: -$21.53
  U22076329: $0.00
  U22076184: $0.00

ORDER HEALTH: clean

WALKER WARNINGS: 0 ✅
```

### Hypothetical with 3 warnings (1 ERROR, 1 WARN, 1 INFO):

```
WALKER WARNINGS: 3
  🔴 [UNKNOWN_ACCT] Yash_Household/AAPL: Unknown account_id 'U99999999' in event[0] for AAPL (tid=12345)
  🟡 [COUNTER_GUARD] Yash_Household/MSFT: Non-negative guard: shares_held would go to -100 (current=0, delta=100) on MSFT 20260407;160000 tid=67890
  ℹ️ [EXCLUDED_SKIP] Yash_Household/SPX: Excluded ticker SPX skipped (5 events)
```

### Hypothetical with >20 warnings:

```
WALKER WARNINGS: 25
  🟡 [COUNTER_GUARD] ...
  ... (20 shown)
  ... +5 more
```

### Walker failure:

```
WALKER WARNINGS: ⚠️ walker unavailable
```

---

## Command Deck Badge — Worked Examples

### 0 warnings (current):
```
[Sector: OK] [Warn: 0] [Sync: 3h ago]
                  ^-- text-emerald-400
```

### 3 warnings, worst = ERROR:
```
[Sector: OK] [Warn: 3] [Sync: 3h ago]
                  ^-- text-rose-400 font-semibold
```

### 2 warnings, worst = WARN:
```
[Sector: OK] [Warn: 2] [Sync: 3h ago]
                  ^-- text-amber-400
```

### INFO only:
```
[Sector: OK] [Warn: 1] [Sync: 3h ago]
                  ^-- text-slate-300
```

### Table not yet populated (no sync since W3.6 deploy):
```
[Sector: OK] [Warn: —] [Sync: 3h ago]
                  ^-- text-slate-500
```

Badge color logic (matches Jinja2 template):
- `walker_warning_count is None` → slate-500 (dash)
- `walker_worst_severity == 'ERROR'` → rose-400
- `walker_worst_severity == 'WARN'` → amber-400
- `walker_warning_count > 0` (INFO only) → slate-300
- `walker_warning_count == 0` → emerald-400

---

## New Tests (14)

| # | Class | Test | Purpose |
|---|-------|------|---------|
| 1 | TestWalkerWarningDataclass | test_dataclass_fields | All 7 fields accessible |
| 2 | TestWalkerWarningDataclass | test_dataclass_frozen | Immutability enforced |
| 3 | TestWalkerWarningDataclass | test_default_context | Default empty dict |
| 4 | TestStaleWarningsAccumulation | test_multi_group_accumulation | Multi-group accumulation + stale regression |
| 5 | TestExcludedSkipWarning | test_excluded_ticker_emits_warning | EXCLUDED_SKIP emission |
| 6 | TestExcludedSkipWarning | test_non_excluded_ticker_no_skip_warning | No false positive |
| 7 | TestWalkerWarningsLogRoundtrip | test_write_and_read | DB write/read with all fields |
| 8 | TestWalkerWarningsLogRoundtrip | test_severity_aggregation_query | Exact build_top_strip query |
| 9 | TestWalkerWarningsLogRoundtrip | test_empty_table_returns_zero | Empty table edge case |
| 10 | TestDeckBadgeSeverityColor | test_zero_warnings | Emerald |
| 11 | TestDeckBadgeSeverityColor | test_info_only | Slate-300 |
| 12 | TestDeckBadgeSeverityColor | test_warn_severity | Amber |
| 13 | TestDeckBadgeSeverityColor | test_error_severity | Rose |
| 14 | TestDeckBadgeSeverityColor | test_none_count | Slate-500 dash |

---

## Constraints Verified

- [x] No writes to `master_log_*` (Bucket 2 pristine) — `walker_warnings_log` is Bucket 3
- [x] No new dependencies
- [x] Try/except on both surfaces — Telegram shows "⚠️ walker unavailable", Deck shows "—"
- [x] Deck badge follows existing top-strip pattern exactly
- [x] Telegram output appended to existing `/reconcile`, not a new command
- [x] Walker purity preserved — `walk_cycles()` internals unchanged
- [x] All 6 `UnknownEventError` raise sites remain hard raises
- [x] Full codebase grep confirmed no other `get_walker_warnings()` callers

---

## Followups (NOT in scope)

- **UnknownEventError demotion audit** — 6 raise sites reviewed, all kept as hard raises per decision. Separate audit if needed.
- **Click badge → modal** — spec item (c). Requires HTMX endpoint + modal template. Deferred to implementation phase 2.
- **`/reconcile` also persists to `walker_warnings_log`** — Currently only `flex_sync.py` writes. `/reconcile` shows live warnings but does not persist. Could add if needed for audit trail of manual reconcile runs.

---

## Reconciliation Gate Check

```
Tests: 91/91 (77 original + 14 new)
Live DB warnings: 0
Live DB cycles: 173
A (realized P&L): unchanged
B (cost basis): unchanged
C (NAV recon): unchanged
```

**STOP. Awaiting Yash review.**
