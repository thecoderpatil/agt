# SAFETY RAILS — Hard Limits for Autonomous Loop

**Authority:** These rails are set by Yash Patil (Managing Partner, CCO).
No scheduled task, directive, or Opus review can override them.
Only Yash can modify this file, in an interactive session.

**Enforcement:** Every Sonnet/Haiku task MUST read this file at the start
of every run. If any rail would be violated by a proposed action, the
action is BLOCKED and an escalation email is sent to yashpatil@gmail.com.

---

## Position Limits

- MAX_SINGLE_NAME_PCT: 15% of household NLV per ticker (CSP notional)
- MAX_SECTOR_NAMES: 2 names per GICS sector per household (Rule 3)
- MAX_CONCURRENT_CSPS: 20 open short puts per household
- MAX_CONCURRENT_CCS: No limit (covered by shares held)
- MIN_OTM_PCT: 5% out-of-the-money for new CSP entries
- CC_ABOVE_BASIS_ONLY: true (never write CCs below paper_basis)

## Market Condition Gates

- VIX_HALT_THRESHOLD: 35 (if VIX >= 35, stage ZERO new CSPs, harvest only)
- RTH_ONLY: true (no scans or orders outside 9:30-16:00 ET)
- GATEWAY_REQUIRED: true (if IB Gateway down, skip all IB operations)

## Execution Limits

- MAX_DAILY_ORDERS: 30 (across all households, all types)
- MAX_DAILY_NOTIONAL: 3000000 (total notional staged per day)
- APPROVAL_MODE: auto (for paper; would be "manual" for live)
- MAX_ORDER_RETRY: 2 (don't retry a rejected order more than twice)

## Directive Constraints

- DIRECTIVE_MAX_AGE_DAYS: 5 (ignore directives older than 5 trading days)
- DIRECTIVE_ABORT_ON_VIX_SPIKE: true (if VIX moves +30% from directive day, abort)
- DIRECTIVE_ABORT_ON_DRAWDOWN: true (if any account NLV drops >5% from directive day, abort)

## Circuit Breaker Triggers (auto-halt + escalate)

- RECONCILIATION_DRIFT: >10% shares mismatch between IB and DB
- FILL_PRICE_DEVIATION: >20% from limit price (bad fill)
- CONSECUTIVE_ERRORS: 3 consecutive task runs with errors
- GATEWAY_DOWN_MINUTES: >60 (Gateway unreachable for over an hour during RTH)
- ACCOUNT_NLV_DROP: >8% single-day drop on any account

## Escalation

- EMAIL: yashpatil@gmail.com
- METHOD: Gmail MCP (mcp__gmail__create_draft or send)
- SUBJECT_PREFIX: "[AGT PAPER ALERT]"
- ON_CIRCUIT_BREAK: Send email immediately, halt all order activity until next Yash session
- ON_RAIL_VIOLATION: Send email, skip the violating action, continue other tasks

## Never Do (absolute, no exceptions)

- Never stage orders on live accounts (U21971297, U22076329, U22388499)
- Never connect to port 4001 (live Gateway)
- Never modify walker.py or flex_sync.py
- Never delete data from master_log_* tables
- Never override this file from a scheduled task
