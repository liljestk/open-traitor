"""Quick script to inspect DB contents."""
import sqlite3
import os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for db_name in ["data/stats.db", "data/stats_coinbase.db", "data/stats_ibkr.db"]:
    print(f"\n{'='*60}")
    print(f"  {db_name} ({os.path.getsize(db_name) if os.path.exists(db_name) else 'MISSING'} bytes)")
    print(f"{'='*60}")
    if not os.path.exists(db_name):
        continue
    try:
        conn = sqlite3.connect(db_name)
        conn.row_factory = sqlite3.Row
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for t in tables:
            name = t["name"]
            count = conn.execute(f"SELECT COUNT(*) as c FROM [{name}]").fetchone()["c"]
            if count > 0:
                print(f"  {name}: {count} rows")
                # Show a sample row
                sample = conn.execute(f"SELECT * FROM [{name}] ORDER BY rowid DESC LIMIT 1").fetchone()
                if sample:
                    cols = sample.keys()
                    print(f"    Latest: {dict(zip(cols[:5], [sample[c] for c in cols[:5]]))}")
        conn.close()
    except Exception as e:
        print(f"  ERROR: {e}")
