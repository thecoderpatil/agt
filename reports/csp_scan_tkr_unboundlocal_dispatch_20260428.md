# Sprint 14 P0 — CSP scan UnboundLocalError 'tkr'
**Date:** 2026-04-28
**Branch:** sprint-14-p0-csp-scan-tkr-unboundlocal
**MR:** !276 (TBD)
**Tier:** CRITICAL

## Root Cause

Two latent defects in `agt_equities/position_discovery.py` (introduced Sprint 3 MR 5,
commit `3bda135`). MR !272 is innocent — it did not touch `position_discovery.py`.

**Defect 1 — line 566:** `IBKRPriceVolatilityProvider(ib, ...)` references bare `ib`
which is not defined in scope. The function parameter is `ib_conn` (line 74).
This raises `NameError: name 'ib' is not defined` before the `for tkr in missed:` loop
at line 568 starts.

**Defect 2 — line 582:** The except handler logs `tkr` in the format string. Because
defect 1 fires before the loop body ever executes, `tkr` is unbound at the except site →
`UnboundLocalError: cannot access local variable 'tkr' where it is not associated
with a value`.

Latency explanation: the `if missed:` guard at line 560 skips the entire block when all
spot prices resolve via `get_spots_batch`. Today's scan had at least one ticker missing
from the batch response, exposing the path for the first time.

## Crash sequence (from telegram_ui.log)

```
position_discovery.py:566  NameError: name 'ib' is not defined
  (during handling of above exception)
position_discovery.py:582  UnboundLocalError: cannot access local variable 'tkr'
  where it is not associated with a value
```

## Fix

**File:** `agt_equities/position_discovery.py`
**Net change:** +2/-1 (ib→ib_conn on line 566; tkr sentinel added before line 568)

```python
# Before (lines 566-568):
                _prov = IBKRPriceVolatilityProvider(ib, market_data_mode="delayed")

                for tkr in missed:

# After:
                _prov = IBKRPriceVolatilityProvider(ib_conn, market_data_mode="delayed")

                tkr = "<unknown>"

                for tkr in missed:
```

## Tests

New file: `tests/test_sprint14_p0_csp_scan_tkr_unboundlocal.py`
- 3 tests, ~60 lines
- Markers: `pytest.mark.sprint_a`
- Required sentinels: `test_provider_init_error_no_unboundlocal`,
  `test_ib_conn_passed_not_bare_ib`, `test_provider_loop_error_logs_ticker`

## Verification sentinels

- `IBKRPriceVolatilityProvider(ib_conn,` in position_discovery.py
- `tkr = "<unknown>"` in position_discovery.py
- `IBKRPriceVolatilityProvider(ib,` NOT present in position_discovery.py

```yaml expected_delta
files:
  agt_equities/position_discovery.py:
    added: 2
    removed: 1
    net: 1
    tolerance: 3
    required_sentinels:
      - "IBKRPriceVolatilityProvider(ib_conn,"
      - "tkr = \"<unknown>\""
  tests/test_sprint14_p0_csp_scan_tkr_unboundlocal.py:
    added: 126
    removed: 0
    net: 126
    tolerance: 10
    required_sentinels:
      - "test_provider_init_error_no_unboundlocal"
      - "test_ib_conn_passed_not_bare_ib"
      - "test_provider_loop_error_logs_ticker"
```
