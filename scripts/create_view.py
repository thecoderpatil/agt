"""Create v_available_nlv view in agt_desk.db."""
import sqlite3

conn = sqlite3.connect("agt_desk.db")
conn.execute("PRAGMA busy_timeout = 15000")

conn.execute("""
    CREATE VIEW IF NOT EXISTS v_available_nlv AS
    SELECT
        account_id,
        household,
        nlv,
        excess_liquidity,
        (nlv - excess_liquidity) AS encumbered_capital,
        excess_liquidity          AS available_nlv,
        timestamp                 AS nlv_timestamp
    FROM (
        SELECT
            account_id, household, nlv, excess_liquidity, timestamp,
            ROW_NUMBER() OVER (
                PARTITION BY account_id
                ORDER BY timestamp DESC
            ) AS rn
        FROM el_snapshots
        WHERE account_id      IS NOT NULL
          AND nlv              IS NOT NULL
          AND excess_liquidity IS NOT NULL
    )
    WHERE rn = 1
""")
conn.commit()

# Verify
rows = conn.execute("SELECT * FROM v_available_nlv").fetchall()
cols = [d[0] for d in conn.execute("SELECT * FROM v_available_nlv LIMIT 0").description]
for r in rows:
    print(dict(zip(cols, r)))

print(f"\nv_available_nlv created, {len(rows)} rows")
conn.close()
