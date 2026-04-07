# ADBE Per-Account paper_basis Refactor Proposal

Generated: 2026-04-07
Status: PROPOSAL ONLY — awaiting Yash review before any walker.py edit

## Problem Statement

ADBE Yash_Household cross-check B shows a $0.23/share divergence:
- Walker `paper_basis` = $332.5925 (household-wide weighted average)
- IBKR `costBasisPrice` = $332.8212 (per-account weighted averages, then household average)

Root cause: the Walker computes a single running weighted average across all assignments regardless of account, while IBKR maintains separate per-account cost bases. The mathematical difference arises because sequential weighted averaging across interleaved accounts produces a different result than averaging two per-account averages.

## (a) Current paper_basis storage

**walker.py line 121:**
```python
@dataclass
class Cycle:
    ...
    paper_basis:         float | None
```

Single `float | None` value. Updated by `_update_paper_basis()` (line 248) which computes a household-wide running weighted average:

```python
def _update_paper_basis(cycle, delta_shares, price_per_share):
    old_basis = cycle.paper_basis if cycle.paper_basis is not None else 0.0
    new_shares = old_shares + delta_shares
    cycle.paper_basis = ((old_basis * old_shares) + (price_per_share * delta_shares)) / new_shares
```

## (b) Proposed refactor

```python
@dataclass
class Cycle:
    ...
    _paper_basis_by_account: dict[str, tuple[float, float]]  # {account_id: (total_cost, total_shares)}

    @property
    def paper_basis(self) -> float | None:
        """Household-aggregate paper_basis for legacy callers."""
        total_shares = sum(shares for _, shares in self._paper_basis_by_account.values())
        if total_shares <= 0:
            return None
        total_cost = sum(cost for cost, _ in self._paper_basis_by_account.values())
        return total_cost / total_shares

    def paper_basis_for_account(self, account_id: str) -> float | None:
        """Per-account IRS cost basis. Matches IBKR costBasisPrice."""
        entry = self._paper_basis_by_account.get(account_id)
        if entry is None or entry[1] <= 0:
            return None
        return entry[0] / entry[1]
```

Updated `_update_paper_basis`:
```python
def _update_paper_basis(cycle, delta_shares, price_per_share, account_id):
    entry = cycle._paper_basis_by_account.get(account_id, (0.0, 0.0))
    old_cost, old_shares = entry
    new_cost = old_cost + (price_per_share * delta_shares)
    new_shares = old_shares + delta_shares
    cycle._paper_basis_by_account[account_id] = (new_cost, new_shares)
```

For stock sells (called away, direct sells), subtract from the account's lot:
```python
def _reduce_paper_basis(cycle, delta_shares, account_id):
    entry = cycle._paper_basis_by_account.get(account_id, (0.0, 0.0))
    old_cost, old_shares = entry
    if old_shares > 0:
        per_share = old_cost / old_shares
        new_shares = old_shares - delta_shares
        new_cost = per_share * new_shares
        cycle._paper_basis_by_account[account_id] = (new_cost, max(new_shares, 0))
```

## (c) All call sites that read paper_basis

### In walker.py (direct field access):
1. **Line 121**: `paper_basis: float | None` — field declaration → becomes `_paper_basis_by_account: dict`
2. **Line 139**: `if self.shares_held <= 0 or self.paper_basis is None` — in `adjusted_basis` property → uses new `paper_basis` property (no change needed)
3. **Line 141**: `return self.paper_basis - (self.premium_total / self.shares_held)` — same
4. **Line 239**: `paper_basis=None` in `_new_cycle()` → becomes `_paper_basis_by_account={}`
5. **Lines 248-256**: `_update_paper_basis()` — rewritten per proposal
6. **Lines 304-329**: ASSIGN_STK_LEG handler — add `account_id` param to `_update_paper_basis` call
7. **Lines 337, 347**: STK_BUY_DIRECT, EXERCISE_STK_LEG — same
8. **Line 362**: CARRYIN_STK — `cycle.paper_basis = ev.trade_price` → becomes `cycle._paper_basis_by_account[ev.account_id] = (ev.trade_price * ev.quantity, ev.quantity)`

### In test_walker.py:
9. **Line 180**: `self.assertAlmostEqual(c.paper_basis, 74.04, delta=0.10)` — uses property, no change needed
10. **Line 324-327**: `c.paper_basis` and `paper_delta` — uses property, no change needed

### In run_crosschecks.py:
11. **Line 201**: `w_basis = c.paper_basis` — cross-check B. **MUST change** to per-account comparison:
    ```python
    for ibkr_row in ibkr_rows:
        acct = ibkr_row['account_id']
        w_acct_basis = c.paper_basis_for_account(acct)
        ibkr_acct_cbp = float(ibkr_row['cost_basis_price'])
        delta = w_acct_basis - ibkr_acct_cbp  # compare per-account
    ```

### In telegram_bot.py (Phase 2+ migration, not touched now):
12. **Line 2089**: `paper_basis = round(cost_total / long_shares, 2)` — this is the OLD premium_ledger path, not Walker. Will be replaced in Phase 2.
13. **Lines 2100, 2106, 2113, 2127**: Various dashboard/CC logic reading `paper_basis` from the old path. Phase 2 migration.
14. **Lines 2713, 2769, 2779, 2852, 2858-2859, 2883, 2895**: CC ladder and decision logic. Phase 2/3 migration.

## (d) Cross-check B aggregation change

Current:
```python
# Single household-wide comparison
w_basis = c.paper_basis
ibkr_cbp = weighted_avg_across_accounts(ibkr_rows)
delta = w_basis - ibkr_cbp
```

Must change to per-account:
```python
for ibkr_row in ibkr_rows:
    acct = ibkr_row['account_id']
    w_acct = c.paper_basis_for_account(acct)
    i_acct = float(ibkr_row['cost_basis_price'])
    delta = w_acct - i_acct  # per-account, should be < $0.10
```

This eliminates the weighted-average artifact entirely. Each account's paper_basis is compared directly to IBKR's per-account costBasisPrice.

## (e) Test impact

Of the 27 tests:
- **11 walker tests**: 2 directly assert `c.paper_basis` (tests 3 and 8). These use the `paper_basis` property which becomes a household-aggregate accessor — **no change needed** for single-account test data (UBER U22076329 only). The aggregate matches the per-account value when there's only 1 account.
- **10 flex parser tests**: Don't touch paper_basis. **No impact.**
- **6 trade_repo tests**: Don't assert paper_basis values. **No impact.**
- **1 CRM test**: Doesn't check paper_basis. **No impact.**

**Net: 0 test changes required** for the Walker refactor itself. Cross-check B test logic would need updating but that's in `run_crosschecks.py`, not the unit tests.

## (f) Risk assessment: single-account cycles

For single-account cycles (most of Vikram_Household, and some Yash cycles):
- `_paper_basis_by_account` has exactly 1 entry
- `paper_basis` property: `total_cost / total_shares` = same as current scalar
- `paper_basis_for_account(acct)`: same value

**Zero behavioral change for single-account cycles.** The refactor is a strict generalization — it degrades to the current behavior when all events come from one account.

## Summary

| Aspect | Risk | Notes |
|--------|------|-------|
| Walker logic | Low | Mechanical change to per-account tracking |
| Existing tests | None | Property accessor preserves aggregate behavior |
| Cross-check B | Medium | Must update comparison logic |
| telegram_bot.py | None (Phase 2) | Old code paths untouched |
| Single-account cycles | Zero | Mathematical identity |

The $0.23 ADBE divergence would be fully resolved. Per-account basis would match IBKR costBasisPrice to $0.00 for every account (verified in Task 3A: all ADBE assignments have matching CSP_OPENs with correct premiums).
