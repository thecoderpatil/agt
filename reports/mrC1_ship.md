# MR-C.1 Ship Report - paper_baseline.py ADR-011 G2/G5 Gate Adapter

## DISPATCH: C.1 paper_baseline.py ADR-011 gate adapter
## STATUS: applied

## FILES
  agt_equities/paper_baseline.py  +292/-0  sha256:cc723866
  config/promotion_gates.yaml     reverted (see NOTES)
  tests/test_paper_baseline.py    +224/-0  sha256:6ee0993d
  .gitlab-ci.yml                  +1/-1    sha256:8f59271e

## COMMIT
  branch: adr011-paper-baseline
  commit: 0d9dc374 (initial) + 59e3eea4 (config revert)
  squash: 197408f2
  MR:     !153

## CI
  pipeline: 2463825744  953 passed / 3 skipped / 8 deselected
  delta vs baseline (941): +12 passed
  MR pipeline: 941 passed (merged result uses main CI yaml -- expected per E.0 pattern)

## VERIFICATION
  ast.parse: paper_baseline.py OK / test_paper_baseline.py OK
  sentinel grep: evaluate_g5 OK / pytest.mark.sprint_a OK / CI test_paper_baseline OK
  local smoke: 12/12 passed (before commit)

## NOTES
  Schema gap flags (G3/G4): confirmed as expected -- no engine column on pending_orders.
  G1: no shadow_scan bps persistence, confirmed stub correct.

  Dispatch vs reality divergences (both resolved in-session):

  1. test file name collision: dispatch specified CREATE tests/test_promotion_gates.py,
     but that file already exists from MR !149 (tests agt_equities.promotion_gates pure
     evaluator). Tests shipped as tests/test_paper_baseline.py instead. CI yaml updated
     to add test_paper_baseline.py after test_promotion_gates.py (already present).

  2. config/promotion_gates.yaml collision: dispatch specified CREATE config/promotion_gates.yaml
     (new dir), but file already exists from MR !149 with different schema expected by
     promotion_gates.py. Initial commit updated it (causing 12 errors in existing tests).
     Fixup commit 59e3eea4 reverted to original. paper_baseline.py has all thresholds
     hardcoded; it does not read the YAML file.

  3. Zero-variance guard fix in evaluate_g5: dispatch code returned green unconditionally when
     variance==0. Fixed to red when mean_diff > 0 (operator consistently beats engine at
     identical PnL differential = infinite t-stat). Correctness fix; 12/12 local smoke.

  4. LOC: dispatch estimated ~120/35/110; actual 292/56/224. Gate updated with actual counts.
