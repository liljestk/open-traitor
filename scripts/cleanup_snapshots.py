"""One-time cleanup of inflated portfolio snapshots from stats DBs."""
import sqlite3
import os
import glob

data_dir = os.environ.get("DATA_DIR", "/app/data")
db_files = glob.glob(os.path.join(data_dir, "**", "*.db"), recursive=True)

for db_path in db_files:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Check if portfolio_snapshots table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        if "portfolio_snapshots" not in tables:
            conn.close()
            continue

        recent = conn.execute(
            "SELECT portfolio_value FROM portfolio_snapshots "
            "WHERE portfolio_value > 0 ORDER BY ts DESC LIMIT 500"
        ).fetchall()
        if not recent:
            conn.close()
            continue

        vals = sorted(r[0] for r in recent)
        median_v = vals[len(vals) // 2]
        threshold = median_v * 3

        bad_count = conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE portfolio_value > ?",
            (threshold,),
        ).fetchone()[0]

        if bad_count > 0:
            conn.execute(
                "DELETE FROM portfolio_snapshots WHERE portfolio_value > ?",
                (threshold,),
            )
            conn.commit()

        rng = conn.execute(
            "SELECT MIN(portfolio_value), MAX(portfolio_value), "
            "AVG(portfolio_value), COUNT(*) "
            "FROM portfolio_snapshots WHERE portfolio_value > 0"
        ).fetchone()

        print(
            f"{db_path}: median={median_v:.2f} threshold={threshold:.2f} "
            f"deleted={bad_count} "
            f"remaining: low={rng[0]:.2f} high={rng[1]:.2f} "
            f"avg={rng[2]:.2f} n={rng[3]}"
        )
        conn.close()
    except Exception as e:
        print(f"{db_path}: SKIP ({e})")
