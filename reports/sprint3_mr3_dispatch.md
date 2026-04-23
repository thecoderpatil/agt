# Sprint 3 MR 3 — DEFERRED→IMMEDIATE sweep (E-M-5)

Per `reports/overnight_sprint_3_dispatch_20260424.md` MR 3 section.
Source finding: `reports/opus_bug_hunt_overnight.md` E-M-5.

## Scope

Replace `with conn:` (DEFERRED tx) → `with tx_immediate(conn):` across the remaining
three files cited by E-M-5:
- `agt_equities/incidents_repo.py` — 4 write sites (596, 669, 813, 929) + import
- `agt_equities/remediation.py` — 6 write sites (236, 261, 285, 337, 361, 383) + import
- `agt_equities/author_critic.py` — 2 write sites (649, 671) + import

Line-for-line replacement; no logic change. Matches Sprint 2's `!207`/`!208` pattern.

## Expected delta

```yaml expected_delta
files:
  agt_equities/incidents_repo.py:
    added: 5
    removed: 5
    net: 0
    tolerance: 3
    required_sentinels:
      - "tx_immediate"
  agt_equities/remediation.py:
    added: 7
    removed: 7
    net: 0
    tolerance: 3
    required_sentinels:
      - "tx_immediate"
  agt_equities/author_critic.py:
    added: 3
    removed: 3
    net: 0
    tolerance: 3
    required_sentinels:
      - "tx_immediate"
```

## Tests

Existing test suites exercise the write paths end-to-end. All 75 tests pass post-swap:
- tests/test_incidents_repo.py: 34 pass
- tests/test_remediation.py: 11 pass
- tests/test_author_critic.py: 30 pass

`tx_immediate` is semantically stricter (acquires writer lock at BEGIN instead of first
write), so existing tests are sufficient to validate correctness — they cover both
multi-step transactions and error paths.

## Reasoning latitude

- `author_critic.py` pattern confirmed present (2 sites). Dispatch marked the presence
  as "ambiguous" — it's real.
- No site requires DEFERRED semantics. All are mixed read/write blocks where IMMEDIATE
  is safer under WAL contention.
- No test changes needed; augmentation would be duplicative.
