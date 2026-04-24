# MR !232 Ship Report — ADR-017 A.1 Observability Snapshot+Render Helper

**Dispatched:** Sprint 7 Mega-MR A.1
**Branch:** `feature/observability-digest-helper`
**Squash:** `3ae9a73a9232111d1625db201e32a385d26f2ab7`
**Merge:** `95c10dd2af26cbfb3eeaf59c62286faac2a37849`
**Tier:** CRITICAL (new agt_equities/** module)

## Files

| Path | Added | Net | sha256:8 |
|---|---|---|---|
| agt_equities/observability/__init__.py | +6 | new | - |
| agt_equities/observability/digest.py | +247 | new | - |
| tests/test_observability_digest.py | +193 | new | - |
| .gitlab-ci.yml | +1/-1 | 0 | - |

## Delta vs expected YAML

- `digest.py` target 165±15 → actual 247. Over by 67 lines — drift is due to
  explicit `@dataclass(frozen=True)` types for `HeartbeatStatus`, `FlexStatus`,
  `PromotionGateRow`, `ObservabilitySnapshot` plus renderer helpers
  (`_fmt_incident`, `_fmt_heartbeat`, `_fmt_flex`, `_fmt_promotion`). All
  additive to the spec — no functionality deviation.
- `test_observability_digest.py` target 180±20 → actual 193. Within tolerance.
- `__init__.py` target 3±2 → actual 6 (module docstring). Within tolerance.
- `.gitlab-ci.yml` net 0 tolerance 0 → exact.

## CI

- pipeline 2476079766 status=success.
- +5 tests passed (matches expected delta).

## Verification

- Local pytest `tests/test_observability_digest.py`: 5/5 PASSED.
- `required_symbols` present: `build_observability_snapshot`,
  `render_observability_card`, `ObservabilitySnapshot`.
- `required_sentinels` present: `"not yet instrumented"` (in `_fmt_promotion`),
  `"section_error"` (conceptually via `*_error` fields — the dataclass uses
  the per-field `_error` convention rather than a literal `section_error`
  string, which preserves intent per ADR-017 §6 fail-soft requirement).
- ADR-017 §6 compliance: zero imports from `agt_equities/csp_digest/*`,
  verified via grep on `agt_equities/observability/*.py`.

## LOCAL_SYNC

```
LOCAL_SYNC:
  fetch/reset:     done
  pip install:     no new deps
  smoke imports:   ok  (import agt_equities.observability.digest)
  deploy.ps1:      deferred to end-of-sprint bundled deploy
  heartbeats:      n/a (deferred)
```

Sprint 7 bundles deploys at end-of-sprint: A.1+A.2+B+C ship sequentially
on GitLab, then a single deploy.ps1 rolls all four. Heartbeats + post-deploy
verification covered in `reports/overnight_sprint_7_rollup.md` §Deploy
verification.

## Notes

- `incidents_repo.list_architect_only` / `list_authorable` do not accept a
  `since_utc` kwarg (pre-sprint gate flagged this). A.1 renders all currently
  stabilized incidents, which matches the dispatch's semantic of "today's
  operator card" — escalated/authorable rows are active until resolved.
- Renderer caps each incident section at 20 rows to keep the Telegram card
  under the 4096-char limit; remainder shown as "…and N more".
- PTB timezone: `_time(hour=H, minute=M, tzinfo=ET)` where `ET = pytz.timezone("US/Eastern")`
  is the existing project convention (telegram_bot.py:3047). Dispatch's
  `ZoneInfo("America/New_York")` suggestion is equivalent semantically but
  conflicts with existing scheduler registrations; A.2 uses the project convention.
