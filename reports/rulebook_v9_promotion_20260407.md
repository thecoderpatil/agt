# Rulebook v9 Promotion

Promoted: 2026-04-07
Previous: v8
Trigger: Independent audit recommendation (survival bunker)

## Change

Rule 2 VIX→EL deployment table capped at 60% max deployment.

| VIX | v8 Retain | v8 Deploy | v9 Retain | v9 Deploy |
|-----|-----------|-----------|-----------|-----------|
| <20 | 80% | 20% | 80% | 20% |
| 20-25 | 85% | 15% | 70% | 30% |
| 25-30 | 90% | 10% | 60% | 40% |
| 30-35 | 95% | 5% | 50% | 50% |
| 35-40 | 100% | 0% | 50% | 50% |
| 40+ | 100% | 0% | 40% | 60% |

## Reasoning

The last 40% of EL is reserved as a survival bunker against IBKR maintenance margin expansion during tail events. No VIX level unlocks the bunker. v8 allowed 100% retain at VIX 35+ (0% deployment), which was conservative but didn't explicitly define a maximum deployment cap. v9 explicitly caps deployment at 60% even at VIX 40+, which paradoxically allows MORE deployment during moderate stress (30% at VIX 20-25 vs 15% under v8) while capping the maximum.

## Implementation

- `agt_deck/risk.py`: `RULEBOOK_VERSION = 'v9'`, `_VIX_EL_TABLE = _VIX_EL_TABLE_V9`
- Tests: 12/12 pass (5 v8 legacy + 7 v9 including the 60% cap invariant)
- Command Deck top strip reflects new VIX/EL bands immediately on next page load
