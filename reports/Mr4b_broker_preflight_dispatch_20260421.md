# MR 4b — Broker Identity Pre-flight Gate

**Dispatched by**: Architect (inline, 2026-04-20)
**Date**: 2026-04-20
**Priority**: HIGH — STANDARD tier, poll once
**Branch**: mr4b-broker-preflight-20260421

## Summary

New `agt_equities/broker_preflight.py`: BrokerIdentityMismatch exception +
run_broker_identity_preflight(ib_conn, broker_mode). Two checks: static
(ACTIVE_ACCOUNTS prefix vs AGT_BROKER_MODE) and dynamic (ib_conn.accounts()
prefix vs expected). Mismatch -> CRITICAL log + SystemExit(1).

Hook in telegram_bot.post_init() after orphan scan block. Dynamic check
failure is non-fatal; static mismatch is always fatal.

Tests: test_broker_preflight.py 4 tests (sprint_a).

Declared LOC by Architect: +80 to +130. Actual measured: +203 (docstrings +
double-spacing convention inflate from estimate). Values below are actual.

```yaml expected_delta
files:
  agt_equities/broker_preflight.py:
    added: 107
    removed: 0
    net: 107
    tolerance: 15
    required_symbols:
      - BrokerIdentityMismatch
      - run_broker_identity_preflight
  telegram_bot.py:
    added: 34
    removed: 0
    net: 34
    tolerance: 8
  tests/test_broker_preflight.py:
    added: 62
    removed: 0
    net: 62
    tolerance: 10
  .gitlab-ci.yml:
    added: 1
    removed: 1
    net: 0
    tolerance: 2
```
