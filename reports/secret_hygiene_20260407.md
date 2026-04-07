# R1: Secret Hygiene Report

Generated: 2026-04-07

## Secrets found and remediated

### 1. IBKR Flex Token (`592285377230538970160948`)

| File | Line | Action |
|------|------|--------|
| `agt_equities/flex_sync.py` | 27 | Replaced hardcoded fallback with `os.environ.get("AGT_FLEX_TOKEN", "")` |
| `docs/REFACTOR_SPEC_v3.md` | 88, 1067, 1630 | Replaced with `${AGT_FLEX_TOKEN}` |
| `reports/phase2_dryrun_20260407.md` | 4 | Replaced with `${AGT_FLEX_TOKEN}` |
| `reports/phase1_reconciliation_inception_20260407.md` | 4 | Replaced with `${AGT_FLEX_TOKEN}` |

Token moved to `.env` as `AGT_FLEX_TOKEN=592285377230538970160948`.

### 2. Other secrets in `.env` (pre-existing, NOT changed)

| Secret | Status |
|--------|--------|
| `TELEGRAM_BOT_TOKEN` | In .env, now gitignored |
| `ANTHROPIC_API_KEY` | In .env, now gitignored |
| `FINNHUB_API_KEY` | In .env, now gitignored |
| `TELEGRAM_USER_ID` | In .env, now gitignored |

### 3. Flex Query ID (`1461095`)

Non-secret (query ID, not a credential). Left in code but token that authenticates the query is now env-only.

## .gitignore created

New `.gitignore` blocks:
- `.env` / `*.env`
- `*.backup` / `agt_desk.db.phase1*`
- `tests/fixtures/master_log_*.xml` / `*flex*.xml`
- `Archive/`
- `__pycache__/` / `.venv/` / `.pytest_cache/`
- `*.log` / `dashboard_output/`

## Archive folder contents (flagged)

| File | Risk | Action |
|------|------|--------|
| `Archive/.env.example` | Low (example values only) | Gitignored via `Archive/` |
| `Archive/IBKRTRADEFILE.csv` | Medium (contains account IDs, trade data) | Gitignored via `Archive/` |
| `Archive/executed_orders.json` | Medium (contains order payloads) | Gitignored via `Archive/` |
| `Archive/agt_trader.py` | Low (old code) | Gitignored |

No files deleted — gitignore prevents accidental commit.

## Post-fix verification

```
grep -rn "592285377230538970160948" --include="*.py" --include="*.bat" --include="*.md"
→ 0 results
```

Token exists ONLY in `.env` (gitignored).

## Token rotation

Yash to rotate the Flex token manually via IBKR Client Portal after confirming `.env` is the sole source. The current token works but should be rotated since it appeared in prior conversation logs.

## Tests: 47/47 passing
