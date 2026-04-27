# Sprint 14 P1 — CSP ticket enrichment dispatch
**Date:** 2026-04-27  
**Branch:** sprint-14-p1-ticket-enrichment  
**MR:** !272  
**Tier:** CRITICAL  

## Summary

Bundle of two Tier 1 fixes from inception_delta forensic + zero-fields forensic:

1. `_tickets_from_digest` — add `delta`, `inception_delta`, `otm_pct`, `spot` to staged ticket dict
2. `build_digest_payload` — fix `premium_dollars` to fall back to `limit_price * 100`

## Files

- `agt_equities/csp_allocator.py` — 4 lines added to `_tickets_from_digest`
- `csp_digest_runner.py` — 1 line changed in `build_digest_payload`
- `tests/test_sprint14_p1_ticket_enrichment.py` — new, 4 tests

## Commit message

```
Sprint 14 P1: CSP ticket enrichment — inception_delta + digest fields

_tickets_from_digest now includes delta/inception_delta/otm_pct/spot
from ScanCandidate. build_digest_payload falls back to limit_price*100
for premium_dollars. Fixes 40+ INCEPTION_DELTA_MISS alerts per FA-block
trading day and $0.00 rendering in the 09:37 ET digest.
```

## Verification

- Sentinel: `inception_delta` in `agt_equities/csp_allocator.py`
- Sentinel: `limit_price` in `csp_digest_runner.py`
- Walker grep: zero matches for `inception_delta` in `agt_equities/walker.py`
- pytest tests/test_sprint14_p1_ticket_enrichment.py → 4/4

```yaml expected_delta
files:
  agt_equities/csp_allocator.py:
    added: 4
    removed: 0
    net: 4
    tolerance: 2
    required_sentinels:
      - "inception_delta"
      - "delta"
      - "otm_pct"
      - "spot"
  csp_digest_runner.py:
    added: 1
    removed: 1
    net: 0
    tolerance: 2
    required_sentinels:
      - "limit_price"
  tests/test_sprint14_p1_ticket_enrichment.py:
    added: 120
    removed: 0
    net: 120
    tolerance: 10
    required_sentinels:
      - "inception_delta"
      - "premium_dollars"
      - "sprint_a"
```
