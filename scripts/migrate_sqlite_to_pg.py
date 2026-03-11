#!/usr/bin/env python3
"""
One-time SQLite -> PostgreSQL migration script.

Reads all data from existing SQLite .db files under data/ and inserts
into the single PostgreSQL database defined by DATABASE_URL.

Usage:
    python scripts/migrate_sqlite_to_pg.py [--dry-run]

Prerequisites:
    - PostgreSQL container running (docker compose up traitor-db)
    - DATABASE_URL set (or config/.env loaded)
    - pip install psycopg2-binary
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(os.getcwd(), "data")

# Tables to migrate and their column lists (order matters for INSERT)
TABLES = [
    "trades",
    "portfolio_snapshots",
    "daily_summaries",
    "agent_reasoning",
    "scheduled_reports",
    "strategic_context",
    "simulated_trades",
    "scan_results",
    "pair_follow",
    "prediction_snapshots",
]


def _get_dsn() -> str:
    """Resolve DATABASE_URL from env or config/.env."""
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    env_path = os.path.join("config", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    # Also try config/root.env
    env_path = os.path.join("config", "root.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL not found in environment or config/.env")


def _discover_dbs() -> list[tuple[str, str]]:
    """Find SQLite DB files and return [(path, exchange_label), ...]."""
    results = []
    if not os.path.isdir(DATA_DIR):
        return results
    # Top-level: data/stats.db, data/stats_coinbase.db, etc.
    for f in sorted(glob.glob(os.path.join(DATA_DIR, "stats*.db"))):
        basename = os.path.basename(f)
        if basename == "stats.db":
            exchange = "default"
        else:
            exchange = basename.replace("stats_", "").replace(".db", "")
        results.append((f, exchange))
    # Sub-dirs: data/coinbase/stats.db, data/ibkr/stats.db, etc.
    for sub in sorted(os.listdir(DATA_DIR)):
        sub_path = os.path.join(DATA_DIR, sub)
        if os.path.isdir(sub_path):
            db_file = os.path.join(sub_path, "stats.db")
            if os.path.exists(db_file):
                # Avoid duplicates if already found above
                if not any(p == db_file for p, _ in results):
                    results.append((db_file, sub))
    return results


def _get_sqlite_columns(sqlite_conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for a table in the SQLite DB."""
    cur = sqlite_conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _get_pg_columns(pg_conn, table: str) -> list[str]:
    """Return column names for a table in the PostgreSQL DB."""
    cur = pg_conn.cursor()
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = %s ORDER BY ordinal_position""",
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def _migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    exchange: str,
    dry_run: bool = False,
) -> int:
    """Migrate a single table from SQLite to PostgreSQL. Returns row count."""
    # Check table exists in SQLite
    exists = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not exists:
        return 0

    sqlite_cols = _get_sqlite_columns(sqlite_conn, table)
    pg_cols = _get_pg_columns(pg_conn, table)

    if not pg_cols:
        print(f"  WARNING: Table '{table}' does not exist in PostgreSQL, skipping")
        return 0

    # Find common columns (excluding 'id' which is auto-generated in PG)
    common = [c for c in sqlite_cols if c in pg_cols and c != "id"]

    # If PG has an 'exchange' column and it's not in the SQLite source, add it
    has_exchange_col = "exchange" in pg_cols
    inject_exchange = has_exchange_col and "exchange" not in sqlite_cols

    # Read all rows from SQLite
    select_cols = ", ".join(common)
    rows = sqlite_conn.execute(f"SELECT {select_cols} FROM {table}").fetchall()

    if not rows:
        return 0

    if dry_run:
        print(f"  [DRY-RUN] Would migrate {len(rows)} rows from '{table}'")
        return len(rows)

    # Build INSERT for PG
    insert_cols = common[:]
    if inject_exchange:
        insert_cols.append("exchange")

    placeholders = ", ".join(["%s"] * len(insert_cols))
    col_list = ", ".join(insert_cols)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    cur = pg_conn.cursor()
    batch = []
    for row in rows:
        values = list(row)
        if inject_exchange:
            values.append(exchange)
        batch.append(tuple(values))

    psycopg2.extras.execute_batch(cur, insert_sql, batch, page_size=500)
    pg_conn.commit()

    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite stats DBs to PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    args = parser.parse_args()

    dsn = _get_dsn()
    print(f"Target PostgreSQL: {dsn.split('@')[1] if '@' in dsn else dsn}")

    dbs = _discover_dbs()
    if not dbs:
        print(f"No SQLite databases found in {DATA_DIR}")
        sys.exit(0)

    print(f"Found {len(dbs)} SQLite database(s):")
    for path, exchange in dbs:
        print(f"  {path} -> exchange={exchange}")

    pg_conn = psycopg2.connect(dsn)
    total_rows = 0

    try:
        for db_path, exchange in dbs:
            print(f"\n--- Migrating {db_path} (exchange={exchange}) ---")
            sqlite_conn = sqlite3.connect(db_path)
            sqlite_conn.row_factory = sqlite3.Row

            for table in TABLES:
                try:
                    count = _migrate_table(sqlite_conn, pg_conn, table, exchange, dry_run=args.dry_run)
                    if count > 0:
                        print(f"  {table}: {count} rows")
                        total_rows += count
                except Exception as e:
                    print(f"  ERROR migrating {table}: {e}")
                    pg_conn.rollback()

            sqlite_conn.close()
    finally:
        pg_conn.close()

    action = "Would migrate" if args.dry_run else "Migrated"
    print(f"\n{action} {total_rows} total rows from {len(dbs)} database(s).")


if __name__ == "__main__":
    main()
