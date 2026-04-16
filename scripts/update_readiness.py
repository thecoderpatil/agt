"""Update readiness gate with today's validations."""
import sqlite3

conn = sqlite3.connect("agt_desk.db")
conn.execute("PRAGMA busy_timeout = 15000")

# CC entry: validated 2026-04-16 chimera test (3 fills)
conn.execute(
    "UPDATE readiness_gate SET status='validated', last_tested='2026-04-16', "
    "evidence='Chimera test: 3 CCs staged, approved, filled (AAPL/ADBE/PYPL). "
    "Full state machine: staged->processing->sent->filled.' "
    "WHERE segment='cc_entry'"
)

# CSP entry: validated 2026-04-16 (131 contracts staged, 7-gate allocator)
conn.execute(
    "UPDATE readiness_gate SET status='validated', last_tested='2026-04-16', "
    "evidence='scan-csp: 10 candidates, 131 contracts staged across 2 households. "
    "7-gate allocator clean (Rule 3 sector limits working). 0 errors.' "
    "WHERE segment='csp_entry'"
)

conn.commit()

# Show current state
conn.row_factory = sqlite3.Row
for r in conn.execute("SELECT segment, status, last_tested FROM readiness_gate ORDER BY id"):
    icon = "OK" if r['status'] == 'validated' else "  "
    print(f"  [{icon:>2}] {r['segment']:<25} {r['status']:<12} {r['last_tested'] or ''}")

conn.close()
