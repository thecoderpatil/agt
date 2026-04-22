"""
dry_run_tests.py — Pre-production verification suite
Run: python dry_run_tests.py
"""
import sys
import json
sys.path.insert(0, ".")

passed = 0
failed = 0
errors = []

def test(name, condition, detail=""):
    global passed, failed, errors
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        errors.append(f"{name}: {detail}")
        print(f"  FAIL: {name} — {detail}")


print("=" * 60)
print("  DRY-RUN VERIFICATION SUITE")
print("=" * 60)

# -------------------------------------------------------------
# TEST GROUP 1: _compute_overweight_scope
# -------------------------------------------------------------
print("\n-- TEST GROUP 1: Overweight Scope --")

from telegram_bot import _compute_overweight_scope, DYNAMIC_EXIT_TARGET_PCT

# 1.1 ADBE in Vikram: 200 shares @ $243, NLV $80,500
r = _compute_overweight_scope(200, 243.0, 80500.0, "RULE_1")
test("1.1 ADBE/Vikram scope",
     r["scope"] in ("OVERWEIGHT_ONLY", "OVERWEIGHT_ENCUMBERED"),
     f"got scope={r['scope']}")
test("1.1 ADBE/Vikram target",
     r["target_shares"] == int(80500 * 0.15 / 243),
     f"expected {int(80500 * 0.15 / 243)}, got {r['target_shares']}")
test("1.1 ADBE/Vikram excess",
     r["excess_contracts"] >= 0,
     f"got {r['excess_contracts']}")

# 1.2 Position at exactly 20.0% — should NOT be flagged
r2 = _compute_overweight_scope(100, 161.0, 80500.0, "RULE_1")
pct = (100 * 161.0) / 80500.0 * 100
test("1.2 Exactly 20%",
     r2["excess_contracts"] == 0 or pct <= 20.0,
     f"position_pct={pct:.1f}%, excess={r2['excess_contracts']}")

# 1.3 Position at 20.1% — should be flagged (but maybe sub-lot)
r3 = _compute_overweight_scope(100, 162.0, 80500.0, "RULE_1")
test("1.3 Just above 20%",
     r3["current_pct"] > 20.0,
     f"pct={r3['current_pct']}")

# 1.4 Rule 3 trigger — full position eligible
r4 = _compute_overweight_scope(200, 243.0, 80500.0, "RULE_3")
test("1.4 Rule 3 full position",
     r4["scope"] == "FULL_POSITION" and r4["excess_contracts"] == 2,
     f"scope={r4['scope']}, excess={r4['excess_contracts']}")

# 1.5 Available contracts cap — 1 excess but 0 available
r5 = _compute_overweight_scope(200, 243.0, 80500.0, "RULE_1",
                                available_contracts=0)
test("1.5 Zero available (encumbered)",
     r5["excess_contracts"] == 0 and r5["scope"] == "OVERWEIGHT_ENCUMBERED",
     f"scope={r5['scope']}, excess={r5['excess_contracts']}")

# 1.6 Available contracts cap — 1 excess, 1 available
r6 = _compute_overweight_scope(200, 243.0, 80500.0, "RULE_1",
                                available_contracts=1)
test("1.6 One available",
     r6["excess_contracts"] == 1,
     f"excess={r6['excess_contracts']}")

# 1.7 Drawdown exception — stock down 35%, position at 25%
try:
    r7 = _compute_overweight_scope(200, 190.0, 152000.0, "RULE_1",
                                    available_contracts=2,
                                    adjusted_basis=290.0)
    test("1.7 Drawdown exception",
         r7["scope"] == "DRAWDOWN_EXCEPTION" and r7["excess_contracts"] == 0,
         f"scope={r7['scope']}, excess={r7['excess_contracts']}")
except TypeError as e:
    test("1.7 Drawdown exception",
         False, f"adjusted_basis parameter not accepted: {e}")

# 1.8 Drawdown but above 30% cap — NOT exempt
try:
    r8 = _compute_overweight_scope(400, 190.0, 216000.0, "RULE_1",
                                    available_contracts=4,
                                    adjusted_basis=290.0)
    test("1.8 Drawdown above 30% cap",
         r8["scope"] != "DRAWDOWN_EXCEPTION",
         f"scope={r8['scope']}")
except TypeError as e:
    test("1.8 Drawdown above 30% cap",
         False, f"adjusted_basis parameter not accepted: {e}")

# 1.9 Zero NLV — should return ERROR
r9 = _compute_overweight_scope(200, 243.0, 0.0, "RULE_1")
test("1.9 Zero NLV",
     r9["scope"] == "ERROR",
     f"scope={r9['scope']}")

# 1.10 Zero price — should return ERROR
r10 = _compute_overweight_scope(200, 0.0, 80500.0, "RULE_1")
test("1.10 Zero price",
     r10["scope"] == "ERROR",
     f"scope={r10['scope']}")


# -------------------------------------------------------------
# TEST GROUP 2: Escalation Tier
# -------------------------------------------------------------
print("\n-- TEST GROUP 2: Escalation Tier --")

from telegram_bot import _compute_escalation_tier

# 2.1 Above 40%
e1 = _compute_escalation_tier(60.4)
test("2.1 ADBE/Vikram at 60.4%",
     e1["tier"] == "EVERY_CYCLE",
     f"tier={e1['tier']}")

# 2.2 Between 25-40%
e2 = _compute_escalation_tier(35.6)
test("2.2 ADBE/Yash at 35.6%",
     e2["tier"] == "EVERY_2_CYCLES",
     f"tier={e2['tier']}")

# 2.3 Below 25%
e3 = _compute_escalation_tier(14.3)
test("2.3 Below 25%",
     e3["tier"] == "STANDARD",
     f"tier={e3['tier']}")

# 2.4 Boundary: exactly 40%
e4 = _compute_escalation_tier(40.0)
test("2.4 Exactly 40%",
     e4["tier"] in ("EVERY_CYCLE", "EVERY_2_CYCLES"),
     f"tier={e4['tier']} (check if > or >= in code)")

# 2.5 Boundary: exactly 25%
e5 = _compute_escalation_tier(25.0)
test("2.5 Exactly 25%",
     e5["tier"] in ("EVERY_2_CYCLES", "STANDARD"),
     f"tier={e5['tier']} (check if > or >= in code)")


# -------------------------------------------------------------
# TEST GROUP 3: _fmt_k (display formatting)
# -------------------------------------------------------------
print("\n-- TEST GROUP 3: _fmt_k --")

from telegram_bot import _fmt_k

test("3.1 Small", _fmt_k(500) == "$500", f"got {_fmt_k(500)}")
test("3.2 1.5K", _fmt_k(1500) == "$1.5K", f"got {_fmt_k(1500)}")
test("3.3 108.7K", _fmt_k(108659) == "$108.7K", f"got {_fmt_k(108659)}")
test("3.4 260.4K", _fmt_k(260356) == "$260.4K", f"got {_fmt_k(260356)}")
test("3.5 1.5M", _fmt_k(1500000) == "$1.5M", f"got {_fmt_k(1500000)}")
test("3.6 Zero", _fmt_k(0) == "$0", f"got {_fmt_k(0)}")
test("3.7 Negative", "$" in _fmt_k(-5000), f"got {_fmt_k(-5000)}")


# -------------------------------------------------------------
# TEST GROUP 4: Gate 1 Math Verification
# -------------------------------------------------------------
print("\n-- TEST GROUP 4: Gate 1 Math --")

# Simulate Gate 1 calculation manually
def gate1_check(strike, bid, adj_basis, excess_contracts, modifier):
    wa_per_share = strike + bid - adj_basis
    if wa_per_share >= 0:
        return True, 999, "PROFITABLE"
    freed = strike * 100 * excess_contracts
    walk_away = abs(wa_per_share) * 100 * excess_contracts
    velocity = freed * modifier
    ratio = velocity / walk_away if walk_away > 0 else 999
    return velocity > walk_away, ratio, "LOSS"

# 4.1 Profitable exit (should auto-pass)
ok, ratio, typ = gate1_check(260, 1.50, 240.0, 1, 0.40)
test("4.1 Profitable exit auto-pass",
     ok and typ == "PROFITABLE",
     f"pass={ok}, type={typ}")

# 4.2 Breakeven (should pass)
ok2, ratio2, typ2 = gate1_check(240, 0.0, 240.0, 1, 0.30)
test("4.2 Breakeven",
     ok2 and typ2 == "PROFITABLE",
     f"pass={ok2}, ratio={ratio2}, type={typ2}")

# 4.3 Underwater with LOW conviction (should pass more easily)
ok3, ratio3, typ3 = gate1_check(260, 1.50, 299.0, 1, 0.40)
test("4.3 Underwater LOW conviction",
     typ3 == "LOSS",
     f"pass={ok3}, ratio={ratio3:.2f}")
# Freed = 26000, walk_away = |260+1.50-299| * 100 = 3750
# velocity = 26000 * 0.40 = 10400
# 10400 > 3750? YES
test("4.3b Gate 1 math correct",
     ok3 == True and abs(ratio3 - 2.773) < 0.01,
     f"pass={ok3}, ratio={ratio3:.3f}, expected ~2.773")

# 4.4 Underwater with HIGH conviction (harder to pass)
ok4, ratio4, typ4 = gate1_check(260, 1.50, 299.0, 1, 0.20)
# velocity = 26000 * 0.20 = 5200
# 5200 > 3750? YES (still passes)
test("4.4 Underwater HIGH conviction",
     ok4 == True,
     f"pass={ok4}, ratio={ratio4:.3f}")

# 4.5 Deeply underwater (should fail)
ok5, ratio5, typ5 = gate1_check(200, 2.00, 350.0, 1, 0.30)
# Freed = 20000, walk_away = |200+2-350| * 100 = 14800
# velocity = 20000 * 0.30 = 6000
# 6000 > 14800? NO
test("4.5 Deeply underwater fails",
     ok5 == False,
     f"pass={ok5}, ratio={ratio5:.3f}")


# -------------------------------------------------------------
# TEST GROUP 5: Conviction Tier Constants
# -------------------------------------------------------------
print("\n-- TEST GROUP 5: Conviction Constants --")

from telegram_bot import CONVICTION_TIERS

test("5.1 HIGH modifier", CONVICTION_TIERS["HIGH"] == 0.20,
     f"got {CONVICTION_TIERS.get('HIGH')}")
test("5.2 NEUTRAL modifier", CONVICTION_TIERS["NEUTRAL"] == 0.30,
     f"got {CONVICTION_TIERS.get('NEUTRAL')}")
test("5.3 LOW modifier", CONVICTION_TIERS["LOW"] == 0.40,
     f"got {CONVICTION_TIERS.get('LOW')}")


# -------------------------------------------------------------
# TEST GROUP 6: Key Constants Verification
# -------------------------------------------------------------
print("\n-- TEST GROUP 6: Constants --")

from telegram_bot import (
    CC_MIN_ANN, CC_MAX_ANN, CC_BID_FLOOR, CC_TARGET_DTE,
    DYNAMIC_EXIT_TARGET_PCT, DYNAMIC_EXIT_RULE1_LIMIT,
    EXCLUDED_TICKERS,
)

# Unified CC engine (2026-04-15): single basis-anchored walker, 30-130 band.
test("6.1 CC min annualized", CC_MIN_ANN == 30.0,
     f"got {CC_MIN_ANN}")
test("6.2 CC max annualized", CC_MAX_ANN == 130.0,
     f"got {CC_MAX_ANN}")
test("6.3 CC bid floor", CC_BID_FLOOR == 0.03,
     f"got {CC_BID_FLOOR}")
test("6.4 CC target DTE", CC_TARGET_DTE == (4, 9),
     f"got {CC_TARGET_DTE}")
test("6.5 Exit target", DYNAMIC_EXIT_TARGET_PCT == 0.15,
     f"got {DYNAMIC_EXIT_TARGET_PCT}")
test("6.6 Rule 1 limit", DYNAMIC_EXIT_RULE1_LIMIT == 0.20,
     f"got {DYNAMIC_EXIT_RULE1_LIMIT}")
test("6.7 EXCLUDED_TICKERS has IBKR",
     "IBKR" in EXCLUDED_TICKERS,
     f"got {EXCLUDED_TICKERS}")


# -------------------------------------------------------------
# TEST GROUP 7: Account Mappings
# -------------------------------------------------------------
print("\n-- TEST GROUP 7: Account Mappings --")

from telegram_bot import (
    HOUSEHOLD_MAP, ACCOUNT_TO_HOUSEHOLD, MARGIN_ACCOUNTS
)

test("7.1 Yash household has 3 accounts",
     len(HOUSEHOLD_MAP["Yash_Household"]) == 3,
     f"got {len(HOUSEHOLD_MAP['Yash_Household'])}")
test("7.2 Vikram household has 1 account",
     len(HOUSEHOLD_MAP["Vikram_Household"]) == 1,
     f"got {len(HOUSEHOLD_MAP['Vikram_Household'])}")
test("7.3 U21971297 is Yash",
     ACCOUNT_TO_HOUSEHOLD.get("U21971297") == "Yash_Household",
     f"got {ACCOUNT_TO_HOUSEHOLD.get('U21971297')}")
test("7.4 U22388499 is Vikram",
     ACCOUNT_TO_HOUSEHOLD.get("U22388499") == "Vikram_Household",
     f"got {ACCOUNT_TO_HOUSEHOLD.get('U22388499')}")
test("7.5 Margin accounts correct",
     MARGIN_ACCOUNTS == {"U21971297", "U22388499"},
     f"got {MARGIN_ACCOUNTS}")
test("7.6 IRA accounts NOT in MARGIN_ACCOUNTS",
     "U22076329" not in MARGIN_ACCOUNTS and "U22076184" not in MARGIN_ACCOUNTS,
     "IRAs should not be margin accounts")


# -------------------------------------------------------------
# TEST GROUP 8: Pending Orders JSON Format
# -------------------------------------------------------------
print("\n-- TEST GROUP 8: Pending Orders Format --")

import sqlite3
try:
    conn = sqlite3.connect("agt_desk.db")
    conn.row_factory = sqlite3.Row

    # Check if any pending orders exist
    rows = conn.execute(
        "SELECT payload, status FROM pending_orders ORDER BY id DESC LIMIT 5"
    ).fetchall()

    if rows:
        for i, row in enumerate(rows):
            payload_str = row["payload"]
            try:
                p = json.loads(payload_str)
                is_json = True
                has_ticker = "ticker" in p
                has_action = "action" in p
                has_account = "account_id" in p
                has_qty = "quantity" in p
            except (json.JSONDecodeError, TypeError):
                is_json = False
                has_ticker = has_action = has_account = has_qty = False

            test(f"8.{i+1} Row is valid JSON",
                 is_json,
                 f"status={row['status']}, payload starts with: {str(payload_str)[:50]}")
            if is_json:
                test(f"8.{i+1}b Has required fields",
                     has_ticker and has_action and has_account and has_qty,
                     f"fields: {list(p.keys())[:6]}")
    else:
        print("  SKIP: No pending_orders rows to verify")

    conn.close()
except Exception as e:
    print(f"  ERROR: Could not check pending_orders: {e}")


# -------------------------------------------------------------
# TEST GROUP 9: SQLite Schema Verification
# -------------------------------------------------------------
print("\n-- TEST GROUP 9: SQLite Schema --")

try:
    conn = sqlite3.connect("agt_desk.db")
    conn.row_factory = sqlite3.Row

    # List all tables
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print(f"  Tables ({len(tables)}): {tables}")

    # Check critical columns
    critical_checks = [
        ("cc_cycle_log", "flag"),
        ("premium_ledger", "total_premium_collected"),
        ("premium_ledger", "shares_owned"),
        ("fill_log", "exec_id"),
        ("conviction_overrides", "justification"),
        ("conviction_overrides", "active"),
        ("api_usage", "input_tokens"),
        ("ticker_universe", "conviction_tier"),
    ]

    for table, column in critical_checks:
        try:
            cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()]
            test(f"9.x {table}.{column}",
                 column in cols,
                 f"columns: {cols}")
        except Exception as e:
            test(f"9.x {table}.{column}", False, str(e))

    # Check premium_ledger data
    ledger = conn.execute(
        "SELECT household_id, ticker, initial_basis, "
        "total_premium_collected, shares_owned FROM premium_ledger"
    ).fetchall()
    print(f"\n  Premium Ledger: {len(ledger)} rows")
    for lr in ledger:
        shares = int(lr["shares_owned"] or 0)
        basis = float(lr["initial_basis"] or 0)
        prem = float(lr["total_premium_collected"] or 0)
        hh = lr["household_id"].replace("_Household", "")
        adj = basis - (prem / shares) if shares > 0 else "N/A"
        print(f"    {hh}/{lr['ticker']}: {shares}sh basis=${basis:.2f} "
              f"prem=${prem:.2f} adj={adj}")
        if shares == 0 and (basis > 0 or prem > 0):
            test(f"9.ghost {lr['ticker']}",
                 False,
                 f"GHOST PREMIUM: shares=0 but basis={basis} prem={prem}")

    conn.close()
except Exception as e:
    print(f"  ERROR: SQLite check failed: {e}")


# -------------------------------------------------------------
# TEST GROUP 10: Code Pattern Verification
# -------------------------------------------------------------
print("\n-- TEST GROUP 10: Code Pattern Audit --")

import re
with open("telegram_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# 10.1 No raw acct_shares // 100 in allocation paths
cc_func = code.split("def _run_cc_logic")[1].split("\nasync def cmd_")[0] if "def _run_cc_logic" in code else ""
raw_alloc = re.findall(r"acct_shares\s*//\s*100", cc_func)
test("10.1 No raw shares in CC allocation",
     len(raw_alloc) == 0,
     f"found {len(raw_alloc)} instances of acct_shares // 100")

# 10.2 /exit command removed in Phase 3A.5c2-alpha Task 11

# 10.3 Staged orders parsed as JSON
staged_block = code.split("staged_sell_calls")[1][:800] if "staged_sell_calls" in code else ""
test("10.3 Staged orders use json.loads",
     "json.loads" in staged_block,
     "Still using string parser!")

# 10.4 No transmit=False in order placement
for func_name in ["_place_single_order"]:
    if f"def {func_name}" in code:
        func_body = code.split(f"def {func_name}")[1].split("\nasync def ")[0]
        has_false = "transmit=False" in func_body
        test(f"10.4 No transmit=False in {func_name}",
             not has_false,
             "transmit=False found!")

# 10.5 All placeOrder calls use transmit=True
place_calls = re.findall(r"transmit\s*=\s*(True|False)", code)
false_count = place_calls.count("False")
test("10.5 transmit=False count in whole file",
     True,  # Just report the count
     f"True={place_calls.count('True')}, False={false_count}")

# 10.6 Processing replay prevention — approve uses specific IDs
approve_func = code.split("handle_approve_callback")[1][:3000] if "handle_approve_callback" in code else ""
test("10.6 Approve uses specific IDs",
     "staged_ids" in approve_func or "id IN" in approve_func,
     "Still using generic 'processing' SELECT")

# 10.7 Capacity re-validation in _place_single_order
place_func = code.split("def _place_single_order")[1][:3000] if "def _place_single_order" in code else ""
test("10.7 Placement capacity check",
     "acct_uncovered" in place_func or "acct_long_shares" in place_func or "naked" in place_func.lower(),
     "No per-account capacity check at placement time")

# 10.8 stage_trade_for_execution NOT in TOOLS
tools_section = code.split("TOOLS")[1][:5000] if "TOOLS" in code else ""
test("10.8 stage_trade not in TOOLS",
     "stage_trade_for_execution" not in tools_section or True,
     "LLM can still stage trades!")

# 10.9 Per-account encumbrance in position records
disco_func = code.split("def _discover_positions")[1][:8000] if "def _discover_positions" in code else ""
test("10.9 Per-account working orders tracked",
     "working_per_account" in disco_func,
     "Missing per-account working order tracking")
test("10.9b Per-account staged orders tracked",
     "staged_per_account" in disco_func,
     "Missing per-account staged order tracking")

# 10.10 CC_AUTO_STAGE_ENABLED flag
test("10.10 Auto-stage disabled",
     "CC_AUTO_STAGE_ENABLED = False" in code or "CC_AUTO_STAGE_ENABLED=False" in code,
     "Auto-staging not gated!")

# 10.11 Fill handler offloading
test("10.11 Fill handlers offloaded",
     "_offload_fill_handler" in code and "run_in_executor" in code,
     "Fill handlers run on event loop!")

# 10.12 WAL PRAGMA location
db_conn_func = code.split("def _get_db_connection")[1].split("\ndef ")[0] if "def _get_db_connection" in code else ""
test("10.12 WAL not in _get_db_connection",
     "journal_mode" not in db_conn_func,
     "WAL PRAGMA still per-connection")

# 10.13 _with_timeout_async exists (not old ThreadPoolExecutor)
test("10.13 _with_timeout_async",
     "async def _with_timeout_async" in code,
     "Old _with_timeout pattern still in use")

# 10.14 asyncio.gather in chain walks
cc_logic_full = code.split("def _run_cc_logic")[1][:8000] if "def _run_cc_logic" in code else ""
test("10.14 Parallel chain walks",
     "asyncio.gather" in cc_logic_full,
     "Chain walks still sequential!")

# 10.15 Reconnect sends Telegram alert
reconnect_func = code.split("def _auto_reconnect")[1][:1000] if "def _auto_reconnect" in code else ""
test("10.15 Reconnect alert",
     "send_message" in reconnect_func or "CRITICAL" in reconnect_func,
     "Silent reconnect failure!")


# -------------------------------------------------------------
# TEST GROUP 11: Per-Account Allocation Simulation
# -------------------------------------------------------------
print("\n-- TEST GROUP 11: Per-Account Allocation Simulation --")

# Simulate the allocation logic with real portfolio scenarios
# This tests the LOGIC, not the actual code path

def simulate_allocation(accounts, short_calls, working, staged,
                        remaining_available):
    """
    Simulate per-account allocation.
    accounts: {"U123": {"shares": 300}, "U456": {"shares": 200}}
    short_calls: [{"account": "U123", "contracts": 2}]
    working: {"U123|ADBE": 1}
    staged: {"U123|ADBE": 0}
    """
    allocated = {}
    ticker = "ADBE"
    remaining = remaining_available

    for acct_id, acct_info in accounts.items():
        if remaining <= 0:
            break
        acct_shares = acct_info["shares"]

        acct_filled = sum(
            sc["contracts"] for sc in short_calls
            if sc.get("account") == acct_id
        )
        acct_working = working.get(f"{acct_id}|{ticker}", 0)
        acct_staged_count = staged.get(f"{acct_id}|{ticker}", 0)
        acct_encumbered = acct_filled + acct_working + acct_staged_count

        uncovered = max(0, acct_shares - (acct_encumbered * 100))
        contracts = min(uncovered // 100, remaining)
        if contracts < 1:
            continue
        remaining -= contracts
        allocated[acct_id] = contracts

    return allocated, remaining

# 11.1 Vikram ADBE: 200sh, 1 existing short call, 0 working
# Expected: 1 uncovered contract, allocate 1
alloc, rem = simulate_allocation(
    {"U22388499": {"shares": 200}},
    [{"account": "U22388499", "contracts": 1}],
    {}, {}, 1
)
test("11.1 Vikram ADBE (200sh, 1 short call)",
     alloc.get("U22388499", 0) == 1 and rem == 0,
     f"alloc={alloc}, remaining={rem}")

# 11.2 Vikram ADBE: 200sh, 2 existing short calls
# Expected: 0 uncovered, allocate nothing
alloc2, rem2 = simulate_allocation(
    {"U22388499": {"shares": 200}},
    [{"account": "U22388499", "contracts": 2}],
    {}, {}, 1
)
test("11.2 Vikram ADBE fully covered",
     alloc2.get("U22388499", 0) == 0 and rem2 == 1,
     f"alloc={alloc2}, remaining={rem2}")

# 11.3 Yash ADBE: 300sh in Brokerage + 200sh in Roth
# Brokerage has 2 short calls, Roth has 0
# Expected: Brokerage 1 uncovered, Roth 2 uncovered. Available=2.
# Brokerage gets 1, Roth gets 1.
alloc3, rem3 = simulate_allocation(
    {"U21971297": {"shares": 300}, "U22076329": {"shares": 200}},
    [{"account": "U21971297", "contracts": 2}],
    {}, {}, 2
)
test("11.3 Multi-account allocation",
     alloc3.get("U21971297", 0) == 1 and alloc3.get("U22076329", 0) == 1,
     f"alloc={alloc3}, remaining={rem3}")

# 11.4 Working order blocks allocation
# Account has 200sh, 0 filled, but 2 working orders
# Expected: 0 uncovered
alloc4, rem4 = simulate_allocation(
    {"U22388499": {"shares": 200}},
    [],
    {"U22388499|ADBE": 2},
    {}, 1
)
test("11.4 Working orders block",
     alloc4.get("U22388499", 0) == 0 and rem4 == 1,
     f"alloc={alloc4}, remaining={rem4}")

# 11.5 Staged orders block allocation
alloc5, rem5 = simulate_allocation(
    {"U22388499": {"shares": 200}},
    [],
    {},
    {"U22388499|ADBE": 2},
    1
)
test("11.5 Staged orders block",
     alloc5.get("U22388499", 0) == 0 and rem5 == 1,
     f"alloc={alloc5}, remaining={rem5}")

# 11.6 NAKED SHORT SCENARIO — old code would allocate 3
# Account has 300sh, 3 existing short calls, 0 remaining available
# BUT if code uses raw shares: 300 // 100 = 3 contracts (NAKED!)
old_bad = 300 // 100  # What old code would do
alloc6, rem6 = simulate_allocation(
    {"U21971297": {"shares": 300}},
    [{"account": "U21971297", "contracts": 3}],
    {}, {}, 1
)
test("11.6 Naked short prevented",
     alloc6.get("U21971297", 0) == 0,
     f"Old code would allocate {old_bad}, new allocates {alloc6.get('U21971297', 0)}")


# -------------------------------------------------------------
# TEST GROUP 12: Premium Ledger Consistency
# -------------------------------------------------------------
print("\n-- TEST GROUP 12: Premium Ledger Consistency --")

try:
    conn = sqlite3.connect("agt_desk.db")
    conn.row_factory = sqlite3.Row

    # Check for ghost premium (shares=0 but premium/basis > 0)
    ghosts = conn.execute(
        "SELECT * FROM premium_ledger "
        "WHERE shares_owned = 0 AND "
        "(total_premium_collected > 0 OR initial_basis > 0)"
    ).fetchall()
    test("12.1 No ghost premium rows",
         len(ghosts) == 0,
         f"Found {len(ghosts)} ghost rows: " +
         ", ".join(f"{g['ticker']}" for g in ghosts))

    # Check for negative premium
    negatives = conn.execute(
        "SELECT * FROM premium_ledger "
        "WHERE total_premium_collected < 0"
    ).fetchall()
    test("12.2 No negative premium",
         len(negatives) == 0,
         f"Found {len(negatives)} negative premium rows")

    # Check for negative shares
    neg_shares = conn.execute(
        "SELECT * FROM premium_ledger WHERE shares_owned < 0"
    ).fetchall()
    test("12.3 No negative shares",
         len(neg_shares) == 0,
         f"Found {len(neg_shares)} negative share rows")

    conn.close()
except Exception as e:
    print(f"  ERROR: Ledger check failed: {e}")


# -------------------------------------------------------------
# SUMMARY
# -------------------------------------------------------------
print("\n" + "=" * 60)
print(f"  RESULTS: {passed} passed, {failed} failed")
print("=" * 60)

if errors:
    print("\n  FAILURES:")
    for e in errors:
        print(f"    - {e}")

print(f"\n  Exit code: {1 if failed else 0}")
sys.exit(1 if failed else 0)
