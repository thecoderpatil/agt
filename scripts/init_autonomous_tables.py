"""Create autonomous session state tables in agt_desk.db."""
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

conn = sqlite3.connect("agt_desk.db")
conn.execute("PRAGMA busy_timeout = 15000")

from agt_equities.schema import _register_autonomous_tables
_register_autonomous_tables(conn)

# Verify
for tbl in ("autonomous_session_log", "readiness_gate"):
    cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"{tbl}: {cnt} rows")

print("\nReadiness gate segments:")
conn.row_factory = sqlite3.Row
for r in conn.execute("SELECT * FROM readiness_gate ORDER BY id"):
    print(f"  [{r['status']:>10}] {r['segment']}: {r['notes']}")

conn.close()
print("\nDone.")
