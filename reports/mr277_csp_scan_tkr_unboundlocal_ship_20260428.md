DISPATCH: Sprint 14 P0 — CSP scan tkr UnboundLocalError fix
STATUS: applied
FILES:
  agt_equities/position_discovery.py  +2/-1  sha256:12e0298e
  tests/test_sprint14_p0_csp_scan_tkr_unboundlocal.py  +126/-0  sha256:be5ab5e2
  reports/csp_scan_tkr_unboundlocal_dispatch_20260428.md  +46/-0
COMMIT:
  squash: 8ba4775550555e32dd964b93edc44292c1e1ca7b
  merge:  02e4a725f9727b60cad899de035cc73ee99f79dc
  MR:     !277
CI:
  pipeline: 2486763680   1462 passed / 1 skipped / 6 failed / 8 deselected
  delta vs baseline: 0 (6 failures = pre-existing test_news_adapters.py flake;
    our 3 sprint_a tests pass within 1462)
VERIFICATION:
  pytest local: 3/3 passed (test_sprint14_p0_csp_scan_tkr_unboundlocal.py)
  AST parse: OK (923 lines)
  LOC gate: PASS
  sentinel IBKRPriceVolatilityProvider(ib_conn,: 1 match
  sentinel tkr = "<unknown>": 1 match
  bare IBKRPriceVolatilityProvider(ib,: absent
LOCAL_SYNC:
  fetch/reset:     done (02e4a72 -> origin/main)
  pip install:     no new runtime deps
  smoke imports:   AST OK + sentinels verified
  deploy.ps1:      exit 0 (2026-04-28 19:17:39)
  heartbeats:      agt_bot=23:17:37Z  agt_scheduler=23:17:01Z
NOTES:
  Root cause: Sprint 3 MR 5 (commit 3bda135). MR !272 innocent.
  Defect 1 (line 566): IBKRPriceVolatilityProvider(ib, ...) — bare 'ib' NameError;
    parameter is ib_conn. Fires before for-loop starts.
  Defect 2 (line 582): except handler references unbound 'tkr' — UnboundLocalError.
  Latent until 2026-04-28 09:35 scan had a 'missed' ticker (batch gaps).
  Remote agent trig_01MSkNCnHr18hfaAsZZng66m fired at 20:15Z but did not complete
    the merge — Coder A local session completed.
  Tomorrow 09:35 ET CSP scan (2026-04-29) should fire cleanly post-deploy.
