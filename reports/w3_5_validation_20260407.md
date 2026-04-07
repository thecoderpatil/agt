# W3.5: Input Validation

Generated: 2026-04-07

## Change

`walk_cycles()` now validates at entry:
1. All events share `household_id` — raises `ValueError` if mixed
2. All events share `ticker` — raises `ValueError` if mixed
3. All `account_id` values are in `HOUSEHOLD_MAP` — warns if unknown
4. All `account_id` values map to the expected household — raises `ValueError` if cross-household

## Tests: 77/77 passing
## Codex invariants addressed: I15, I16
