"""Quick inspection of stats_ibkr.db to investigate crypto data contamination."""
import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), "..", "data", "stats_ibkr.db")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== SNAPSHOTS (last 20) ===")
rows = db.execute("SELECT id, ts, exchange, portfolio_value FROM portfolio_snapshots ORDER BY id DESC LIMIT 20").fetchall()
for r in rows:
    print(f"  id={r['id']} ts={r['ts']} exchange={r['exchange']} pv={r['portfolio_value']}")

print("\n=== SNAPSHOTS exchange=ibkr (last 5) ===")
rows = db.execute("SELECT id, ts, exchange, portfolio_value FROM portfolio_snapshots WHERE exchange='ibkr' ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  id={r['id']} ts={r['ts']} pv={r['portfolio_value']}")

print("\n=== SNAPSHOTS exchange=coinbase (first+last 5) ===")
rows = db.execute("SELECT id, ts, exchange, portfolio_value FROM portfolio_snapshots WHERE exchange='coinbase' ORDER BY id ASC LIMIT 5").fetchall()
print("  First:")
for r in rows:
    print(f"    id={r['id']} ts={r['ts']} pv={r['portfolio_value']}")
rows = db.execute("SELECT id, ts, exchange, portfolio_value FROM portfolio_snapshots WHERE exchange='coinbase' ORDER BY id DESC LIMIT 5").fetchall()
print("  Last:")
for r in rows:
    print(f"    id={r['id']} ts={r['ts']} pv={r['portfolio_value']}")

print("\n=== REASONING exchange=coinbase (last 10) ===")
rows = db.execute("SELECT id, ts, exchange, pair, agent_name FROM agent_reasoning WHERE exchange='coinbase' ORDER BY id DESC LIMIT 10").fetchall()
for r in rows:
    print(f"  id={r['id']} ts={r['ts']} pair={r['pair']} agent={r['agent_name']}")

print("\n=== REASONING exchange=ibkr (last 5) ===")
rows = db.execute("SELECT id, ts, exchange, pair, agent_name FROM agent_reasoning WHERE exchange='ibkr' ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  id={r['id']} ts={r['ts']} pair={r['pair']} agent={r['agent_name']}")

print("\n=== REASONING crypto pairs timestamp range ===")
rows = db.execute("""
    SELECT MIN(ts) as first_ts, MAX(ts) as last_ts, COUNT(*) as cnt 
    FROM agent_reasoning 
    WHERE pair LIKE '%-EUR' AND pair NOT LIKE '%USD%'
    AND pair NOT IN ('AAPL-EUR','MSFT-EUR','GOOGL-EUR','AMZN-EUR','NVDA-EUR')
""").fetchall()
for r in rows:
    print(f"  first={r['first_ts']} last={r['last_ts']} count={r['cnt']}")

print("\n=== TABLE COUNTS ===")
for tbl in ['trades', 'portfolio_snapshots', 'agent_reasoning', 'simulated_trades', 'events', 'scan_results']:
    try:
        cnt = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl}: {cnt}")
    except:
        print(f"  {tbl}: N/A")

db.close()
