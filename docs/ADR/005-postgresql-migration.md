# ADR-005: Migration from SQLite to PostgreSQL

**Status:** Accepted

## Context

The system originally used per-exchange SQLite files (`data/coinbase/stats.db`, `data/ibkr/stats.db`) for persistence. As the system grew, SQLite limitations became apparent:
- No concurrent write access (WAL mode helps but doesn't eliminate).
- No cross-exchange queries without opening multiple connections.
- No built-in replication or backup tooling.
- Difficult to run alongside a dashboard server that needs read access.

## Decision

Migrate to a single **PostgreSQL** database with an `exchange` column for domain separation, using a one-time migration script.

### Migration Script (`scripts/migrate_sqlite_to_pg.py`)

**Discovery phase:**
1. Scans for SQLite databases:
   - `data/stats.db` → `exchange="default"`
   - `data/coinbase/stats.db` → `exchange="coinbase"`
   - `data/ibkr/stats.db` → `exchange="ibkr"`

**Migration phase per table:**
1. Read column definitions from both SQLite source and PostgreSQL target.
2. If PostgreSQL has an `exchange` column but SQLite doesn't → inject the exchange value during INSERT.
3. Batch inserts using `psycopg2.extras.execute_batch()` with 500-row pages.
4. `ON CONFLICT DO NOTHING` to skip duplicate rows (idempotent re-runs).

### Tables Migrated

`trades`, `portfolio_snapshots`, `daily_summaries`, `agent_reasoning`, `scheduled_reports`, `strategic_context`, `simulated_trades`, `scan_results`, `pair_follow`, `prediction_snapshots`

### Exchange Tagging

```python
for db_path, exchange in _discover_dbs():
    for table in TABLES:
        sqlite_cols = _get_sqlite_columns(sqlite_conn, table)
        pg_cols = _get_pg_columns(pg_conn, table)
        
        inject_exchange = "exchange" in pg_cols and "exchange" not in sqlite_cols
        
        for row in sqlite_rows:
            if inject_exchange:
                row.append(exchange)
            batch.append(row)
        
        execute_batch(cur, insert_sql, batch)
```

### PostgreSQL Schema

The PostgreSQL schema includes `exchange` columns on all profile-scoped tables, with indexes on `(exchange, ts)` for efficient domain-filtered queries. This aligns with the domain separation rules (ADR-003).

## Consequences

**Benefits:**
- Single database for all exchanges; cross-profile analytics possible with explicit queries.
- Concurrent read/write access for trading bot + dashboard + planning workers.
- `pg_dump` and WAL archiving for reliable backups.
- `exchange` column enables clean domain separation at the SQL layer.

**Risks:**
- PostgreSQL becomes an infrastructure dependency (mitigated: runs in Docker via `docker-compose.yml`).
- One-time migration requires downtime (script is idempotent, can be re-run safely).
- SQLite files remain on disk after migration; manual cleanup needed.

**Follow-on:**
- All new SQL queries must include `exchange` filter when profile is active (ADR-003).
- Container setup (`docker-compose.yml`) provisions PostgreSQL with a dedicated `traitor` user (ADR-014).
