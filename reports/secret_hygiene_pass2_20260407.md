# SECRETS-2: Repo Audit Pass 2

Generated: 2026-04-07

## Findings

### CRITICAL: Archive contains hardcoded API keys

| File | Secret | Status |
|------|--------|--------|
| `Archive/Trader/Anthropic key sk-ant-api03-RXJpHyFm.md` | Full Anthropic API key (old) | ON DISK, gitignored |
| `Archive/Trader/config.py:44` | Same Anthropic API key hardcoded | ON DISK, gitignored |
| `Archive/Trader/config.py:48` | Old Telegram bot token `8787013182:AAFG...` | ON DISK, gitignored |

**Action required:** Yash to:
1. Confirm these are OLD keys (not the current production keys in `.env`)
2. If old: delete the Archive files or rotate the keys if still active
3. If current: ROTATE IMMEDIATELY — keys are in plaintext on disk

### SAFE: Production code uses env vars

| File | Pattern | Status |
|------|---------|--------|
| telegram_bot.py:70 | `finnhub.Client(api_key=FINNHUB_API_KEY)` | Safe — reads from env |
| telegram_bot.py:840 | `anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)` | Safe — reads from env |
| vrp_veto.py:183,373 | `finnhub.Client(api_key=FINNHUB_API_KEY)` | Safe — reads from env |

### FALSE POSITIVES

| Pattern | File | Reason |
|---------|------|--------|
| `api_key=config.ANTHROPIC_API_KEY` | Archive/Trader/morning_screener.py | Old code, gitignored |
| `TELEGRAM_BOT_TOKEN = "test-token"` | Archive/test_execution_pipeline.py | Test fixture |

### NO HITS (clean)

- No AWS keys (AKIA)
- No Google keys (AIza)
- No Slack tokens (xoxb-)
- No GitHub tokens (ghp_)
- No private keys (BEGIN PRIVATE KEY)
- No password assignments in production code

### .gitignore verification

All required patterns present:
- `*.env` ✓
- `Archive/` ✓
- `*flex*.xml` ✓
- `*.backup` ✓

### Git history check

No git repo initialized (not a git repository). No committed history to audit. When git is initialized, the `.gitignore` will prevent sensitive files from being committed.

## Recommendations

1. **Delete `Archive/Trader/Anthropic key sk-ant-api03-RXJpHyFm.md`** — API key in a markdown filename is maximally exposed
2. **Delete or redact `Archive/Trader/config.py`** — contains both Anthropic and Telegram keys
3. **Confirm old Telegram token (`8787013182:AAFG...`) is decommissioned** — if still active, revoke via BotFather
4. **Consider deleting entire Archive/ folder** if contents are no longer needed
