# Trading-Logic Hardening: Code-vs-Canonical Mismatch Spec

Sprint: Trading-Logic Hardening (2026-04-15)
Scope: CC writes (per-account), CC harvest, CSP harvest
Canonical source: Yash verbatim 2026-04-15

---

## Canonical Rules

### CC Strike Picker
> "Start at paper basis, round up. If that strike provides between 30 and 130%
> done, if above 130 move up in strike till below 130 ROI annualized. If below
> 30%, move on."

Per-account, not household-aggregated. UBER Roth $73 basis and Individual $86
basis are two separate decisions.

### Harvest (CC and CSP identical)
> "harvest if 80% gains in 1 trading day, and 90% gains up till the day before
> last trading day."

Interpretation:
- Day 1 (opened today, held 1 trading day): ≥80% profit → harvest
- Day 2+ through day before expiry: ≥90% profit → harvest
- Expiry day (DTE=0): let it ride to expiration

---

## Mismatches

### E1 — WHEEL-5: Household-aggregated basis feeds per-account chain walk
- **File:** telegram_bot.py L8094-8107, L8629
- **Bug:** `_discover_positions` batch-fetches walker cycles keyed by
  (household, ticker). The `initial_basis` stored is household-aggregated
  (weighted-average). `_walk_target` uses this blended basis for the chain
  walk. The per-account distribution loop at L8695 stamps the SAME chain
  result onto every account.
- **Impact:** UBER Roth@$73 and Individual@$86 both get blended $82 walk →
  STAND DOWN at $90C/9.3% ann. Per-account: Roth would WRITE at ~$75-80C,
  Individual would STAND DOWN correctly.
- **Severity:** CRITICAL — active material misallocation.
- **Fix sprint phase:** Phase 3

### E2 — No day-1 80% CC harvest
- **File:** agt_equities/roll_engine.py L360-384 (defense), L526-534 (offense)
- **Bug:** Neither regime checks "days held = 1 AND profit ≥ 80%". Defense
  uses velocity-ratio (V_r ≥ 1.5 AND P_pct ≥ 0.50). Offense uses flat 90%.
- **Impact:** Rapid day-1 gains that hit 80-89% are missed in offense regime.
  In defense, V_r=∞ on day-0 catches some but via wrong logic.
- **Severity:** MEDIUM

### E3 — CC 90% harvest fires at any DTE
- **File:** agt_equities/roll_engine.py L528
- **Bug:** `if p_pct >= 0.90: return HarvestResult(...)` — no DTE guard.
  Fires on expiry day when canonical says let it ride.
- **Impact:** Harvests 0DTE positions at 90% instead of letting them expire.
- **Severity:** LOW (small dollar impact, position expires same day)

### E4 — No "let it ride on expiry" safeguard in CC
- **File:** agt_equities/roll_engine.py (both regimes)
- **Bug:** No `dte == 0 → HOLD` gate before harvest checks.
- **Impact:** Same as E3 — harvests when should hold.
- **Severity:** LOW

### E5 — Defense velocity-ratio ≠ canonical 80/90
- **File:** agt_equities/roll_engine.py L373-384
- **Bug:** V_r ≥ 1.5 AND P_pct ≥ 0.50 is a rate-of-decay signal, not a
  simple profit threshold. A slow 85% gain over 20 days (V_r ≈ 1.27 < 1.5)
  would NOT fire — but should at 90%.
- **Impact:** Under-harvests slow-grind winners in defense.
- **Severity:** MEDIUM
- **Note:** Requires architectural decision: retrofit 80/90 into evaluator,
  or add separate CC harvest pass pre-evaluator.

### E6 — CSP 80% triggers on DTE, not days-held
- **File:** agt_equities/csp_harvest.py L112
- **Bug:** `dte >= 1 and profit_pct >= 0.80` fires for ANY position with 1+
  DTE at 80% profit. A 30-day-old position at 80% incorrectly harvests.
  Should only fire when position opened today (1 trading day held).
- **Impact:** Over-harvests — closes positions too early.
- **Severity:** HIGH

### E7 — CSP harvests on expiry day (0DTE)
- **File:** agt_equities/csp_harvest.py L116
- **Bug:** `dte <= 1 and profit_pct >= 0.90` fires when dte=0. Canonical:
  let it ride on expiry day.
- **Impact:** Pays ask spread to close a position expiring worthless.
- **Severity:** LOW

### E8 — CSP harvest has no days-held tracking
- **File:** agt_equities/csp_harvest.py L73-77
- **Bug:** `_should_harvest_csp(initial_credit, current_ask, dte)` has no
  `opened_date` parameter. Can't implement canonical day-1 vs day-2+ gate.
  Need to join against master_log_trades or pending_orders for entry date.
- **Impact:** Blocks correct implementation of E6 fix.
- **Severity:** HIGH (infrastructure gap)

---

## Severity Ranking

1. E1 (CRITICAL) — per-account basis. Active misallocation.
2. E6 (HIGH) — CSP 80% fires on wrong condition.
3. E8 (HIGH) — infrastructure gap blocking E6 fix.
4. E2 (MEDIUM) — missing day-1 80% CC harvest.
5. E5 (MEDIUM) — defense harvest algorithm mismatch.
6. E3 (LOW) — CC 0DTE harvest.
7. E4 (LOW) — no expiry-day hold.
8. E7 (LOW) — CSP 0DTE harvest.
