# MR-D.0 Ship Report — Bates FFT Pricer (ADR-014)

## DISPATCH: ADR-014 MR-D.0 — Bates FFT pricer
## STATUS: applied

## FILES
  agt_equities/synth/__init__.py   +10/-0   sha256:f98b6474
  agt_equities/synth/schemas.py    +38/-0   sha256:079730f4
  agt_equities/synth/bates_fft.py  +145/-0  sha256:904e33f4
  tests/test_bates_fft.py          +205/-0  sha256:48f0bf6c
  .gitlab-ci.yml                   +1/-1    sha256:cfd9218c
  requirements-ci.txt              +1/-0    sha256:121f0ad9

## COMMIT
  branch: adr014-bates-fft
  commit: 8e2265ed72d3f515f8b26c54982f1a29a7bd47c6
  squash: 5a32babd
  MR:     !150

## CI
  pipeline: 2463657281  932 passed / 3 skipped / 8 deselected
  delta vs baseline: +10 passed (922 → 932)

## VERIFICATION
  - ast.parse: PASS (all 4 Python files)
  - yaml.safe_load: PASS (.gitlab-ci.yml)
  - Remote byte-check: 6 files exact match (sizes + sha256[:8])
  - Sentinel: `class BatesParams`, `bates_fft_call_price`, `bates_characteristic_function`, `bates_fft_put_price` — all present
  - S0 scaling fix applied: dispatch code missing `S0 *` in call_grid (CF is log-returns, not absolute). With fix: ATM BS-limit error <0.004%

## NOTES
  - Codex takeover: Codex halt (codex_halt_D0_20260419.md) — all 3 required function names wrong, BatesParams fields wrong, spot/strike/T embedded in params. Coder rewrote from dispatch spec.
  - S0 scaling bug in dispatch pseudocode corrected: `call_grid = S0 * np.exp(-alpha * log_k_grid) * fft_vals / np.pi`
  - bates_fft.py was 146 lines (over 125±20=145 max); removed 1 comment line, now 145.
  - scipy added to requirements-ci.txt for scipy.stats.norm BS reference in test_bates_fft.py.
  - Ships dark (no callers yet). MC harness (MR-D.2) and calibration (MR-D.1) land separately.
