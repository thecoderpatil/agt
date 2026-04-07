# R3: Corp Action Handler Merge

Generated: 2026-04-07

## Merged to production path

- `corp_action_quarantine` table: created in live agt_desk.db
- `market_data_log` table: created in live agt_desk.db
- Walker CORP_ACTION handler: active (dispatches on type: FS, RS, TC, IC, SD, SO, CM, TM)
- `/clear_quarantine TICKER` command: registered in telegram_bot.py
- Reconciliation: A 49/49, B 14/14, C 3/4, 0 frozen — no change (0 corp actions in data)
- Tests: 63/63 passing (includes 3 synthetic corp action tests)

## Validation caveat

Handler logic validated against **synthetic fixtures only**. IBKR Flex CorporateAction row shapes have NOT been verified against real IBKR output because zero corp actions exist in the current 365-day window.

**First real corp action will require:**
1. Compare actual IBKR XML attributes against handler assumptions
2. Verify `type` field values match our dispatch codes (FS, RS, TC, etc.)
3. Confirm `quantity`, `amount`, `proceeds` fields carry expected values
4. Run Walker on the real event and verify cycle state changes
