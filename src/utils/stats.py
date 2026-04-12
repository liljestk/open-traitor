"""
Persistent statistics database for OpenTraitor.

PostgreSQL-backed store for all trading statistics, portfolio snapshots,
events, and scheduled reports. Designed for fast reads and time-series
queries so the LLM and proactive engine can reference historical data.

The concrete methods live in mixin modules for maintainability:
  - stats_portfolio.py  → PortfolioMixin  (snapshots, daily summaries, analytics)
  - stats_trades.py     → TradesMixin     (trades, events, scheduled reports)
  - stats_reasoning.py  → ReasoningMixin  (agent reasoning, strategic context)
  - stats_predictions.py→ PredictionsMixin(prediction accuracy, tracked pairs)
  - stats_simulated.py  → SimulatedMixin  (simulated trades, scans, pair follows)

This file contains only: schema init, connection management, and the composed class.
"""

from __future__ import annotations

import atexit
import os
from contextlib import contextmanager
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from src.utils.logger import get_logger
from src.utils.stats_portfolio import PortfolioMixin
from src.utils.stats_trades import TradesMixin
from src.utils.stats_reasoning import ReasoningMixin
from src.utils.stats_predictions import PredictionsMixin
from src.utils.stats_simulated import SimulatedMixin

logger = get_logger("stats")


def get_dsn() -> str:
    """Return the PostgreSQL DSN from environment."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://traitor:traitor@localhost:5432/autotraitor",
    )


class _ConnProxy:
    """Thin wrapper around a psycopg2 connection giving an sqlite3-like API.

    Mixin methods call ``conn.execute(sql, params).fetchall()`` — this proxy
    transparently creates a RealDictCursor on every ``execute()`` call so
    rows behave like dicts (same as sqlite3.Row).
    """

    __slots__ = ("_conn", "_last_cur")

    def __init__(self, conn):
        self._conn = conn
        self._last_cur = None

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        self._last_cur = cur
        return cur  # cursor has .fetchall(), .fetchone(), .rowcount

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def cursor(self, **kw):
        return self._conn.cursor(**kw)


class StatsDB(
    PortfolioMixin,
    TradesMixin,
    ReasoningMixin,
    PredictionsMixin,
    SimulatedMixin,
):
    """Thread-safe PostgreSQL statistics database.

    All domain-specific methods are inherited from mixin classes.
    This class owns schema initialisation and connection management.
    """

    def __init__(self, dsn: str = None):
        self._dsn = dsn or get_dsn()
        self._pool = psycopg2.pool.ThreadedConnectionPool(2, 10, self._dsn)
        self._init_db()
        atexit.register(self.close)
        logger.info("📊 Stats DB initialized: PostgreSQL")

    @contextmanager
    def _get_conn(self):
        """Borrow a connection from the pool (context manager).

        Yields a ``_ConnProxy`` that provides an sqlite3-like ``.execute()``
        API backed by ``RealDictCursor``.
        """
        raw = self._pool.getconn()
        try:
            yield _ConnProxy(raw)
        except Exception:
            raw.rollback()
            raise
        finally:
            self._pool.putconn(raw)

    def close(self) -> None:
        """Close all connections in the pool."""
        try:
            self._pool.closeall()
        except Exception:
            pass

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        portfolio_value REAL NOT NULL,
                        cash_balance REAL NOT NULL DEFAULT 0,
                        return_pct REAL NOT NULL DEFAULT 0,
                        total_pnl REAL NOT NULL DEFAULT 0,
                        max_drawdown REAL NOT NULL DEFAULT 0,
                        open_positions TEXT DEFAULT '{}',
                        current_prices TEXT DEFAULT '{}',
                        fear_greed_value REAL DEFAULT NULL,
                        high_stakes_active INTEGER DEFAULT 0
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        pair TEXT NOT NULL,
                        action TEXT NOT NULL,
                        quantity REAL NOT NULL DEFAULT 0,
                        price REAL NOT NULL,
                        quote_amount REAL NOT NULL DEFAULT 0,
                        confidence REAL DEFAULT 0,
                        signal_type TEXT DEFAULT '',
                        stop_loss REAL DEFAULT 0,
                        take_profit REAL DEFAULT 0,
                        reasoning TEXT DEFAULT '',
                        pnl REAL DEFAULT NULL,
                        fee_quote REAL DEFAULT 0,
                        is_rotation INTEGER DEFAULT 0,
                        approved_by TEXT DEFAULT 'auto',
                        entry_score REAL DEFAULT NULL
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        event_type TEXT NOT NULL,
                        severity TEXT NOT NULL DEFAULT 'info',
                        pair TEXT DEFAULT NULL,
                        message TEXT NOT NULL,
                        data TEXT DEFAULT '{}'
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scheduled_reports (
                        id SERIAL PRIMARY KEY,
                        created_ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        cron_expression TEXT NOT NULL,
                        query_type TEXT NOT NULL,
                        query_params TEXT DEFAULT '{}',
                        is_active INTEGER DEFAULT 1,
                        last_run_ts TEXT DEFAULT NULL,
                        next_run_ts TEXT DEFAULT NULL,
                        user_id TEXT DEFAULT 'owner'
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS daily_summaries (
                        id SERIAL PRIMARY KEY,
                        date TEXT NOT NULL UNIQUE,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        opening_value REAL DEFAULT 0,
                        closing_value REAL DEFAULT 0,
                        high_value REAL DEFAULT 0,
                        low_value REAL DEFAULT 0,
                        total_trades INTEGER DEFAULT 0,
                        winning_trades INTEGER DEFAULT 0,
                        losing_trades INTEGER DEFAULT 0,
                        total_pnl REAL DEFAULT 0,
                        best_trade TEXT DEFAULT NULL,
                        worst_trade TEXT DEFAULT NULL,
                        events_count INTEGER DEFAULT 0,
                        summary_text TEXT DEFAULT '',
                        plan_text TEXT DEFAULT '',
                        exchange TEXT NOT NULL DEFAULT 'coinbase'
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_reasoning (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        cycle_id TEXT NOT NULL,
                        pair TEXT NOT NULL,
                        agent_name TEXT NOT NULL,
                        reasoning_json TEXT NOT NULL DEFAULT '{}',
                        signal_type TEXT DEFAULT '',
                        confidence REAL DEFAULT 0,
                        trade_id INTEGER DEFAULT NULL REFERENCES trades(id),
                        langfuse_trace_id TEXT DEFAULT NULL,
                        langfuse_span_id TEXT DEFAULT NULL,
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        latency_ms REAL DEFAULT 0,
                        raw_prompt TEXT DEFAULT ''
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS strategic_context (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        horizon TEXT NOT NULL,
                        plan_json TEXT NOT NULL DEFAULT '{}',
                        summary_text TEXT DEFAULT '',
                        langfuse_trace_id TEXT DEFAULT NULL,
                        temporal_workflow_id TEXT DEFAULT NULL,
                        temporal_run_id TEXT DEFAULT NULL
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS simulated_trades (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        pair TEXT NOT NULL,
                        from_currency TEXT NOT NULL,
                        from_amount REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        quantity REAL NOT NULL,
                        to_currency TEXT NOT NULL,
                        notes TEXT DEFAULT '',
                        status TEXT DEFAULT 'open',
                        closed_at TEXT DEFAULT NULL,
                        close_price REAL DEFAULT NULL,
                        close_pnl_abs REAL DEFAULT NULL,
                        close_pnl_pct REAL DEFAULT NULL
                    )
                """)

                # Indexes
                cur.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON portfolio_snapshots(ts)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summaries(date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reasoning_cycle ON agent_reasoning(cycle_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reasoning_pair ON agent_reasoning(pair)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_reasoning_ts ON agent_reasoning(ts)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_strategic_horizon ON strategic_context(horizon)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scan_results (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        universe_size INTEGER NOT NULL DEFAULT 0,
                        scanned_pairs INTEGER NOT NULL DEFAULT 0,
                        results_json TEXT NOT NULL DEFAULT '{}',
                        top_movers TEXT DEFAULT '[]',
                        summary_text TEXT DEFAULT ''
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_results(ts)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pair_follows (
                        id SERIAL PRIMARY KEY,
                        pair TEXT NOT NULL,
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        followed_by TEXT NOT NULL DEFAULT 'human',
                        ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
                        UNIQUE(pair, followed_by)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_pair_follows_pair ON pair_follows(pair)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_pair_follows_exchange ON pair_follows(exchange)")

            conn.commit()

        # Run migrations on a fresh connection so any failure is fully
        # independent of the schema-creation transaction above (MED-10).
        self._migrate_db()

    # Allowlist for DDL migrations — prevents any interpolation of
    # unexpected identifiers into ALTER TABLE statements (CRIT-2).
    _MIGRATION_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
        ("agent_reasoning",      "langfuse_trace_id"),
        ("agent_reasoning",      "langfuse_span_id"),
        ("agent_reasoning",      "prompt_tokens"),
        ("agent_reasoning",      "completion_tokens"),
        ("agent_reasoning",      "latency_ms"),
        ("agent_reasoning",      "raw_prompt"),
        ("agent_reasoning",      "exchange"),
        ("strategic_context",    "langfuse_trace_id"),
        ("strategic_context",    "temporal_workflow_id"),
        ("strategic_context",    "temporal_run_id"),
        ("portfolio_snapshots",  "exchange"),
        ("trades",               "exchange"),
        ("trades",               "external_id"),
        ("trades",               "entry_score"),
        ("simulated_trades",     "exchange"),
        ("scan_results",         "exchange"),
        ("events",               "exchange"),
        ("daily_summaries",      "exchange"),
    })

    def _migrate_db(self) -> None:
        """Add new columns to existing tables without breaking old installs."""
        migrations = [
            ("agent_reasoning",     "langfuse_trace_id",    "TEXT DEFAULT NULL"),
            ("agent_reasoning",     "langfuse_span_id",     "TEXT DEFAULT NULL"),
            ("agent_reasoning",     "prompt_tokens",        "INTEGER DEFAULT 0"),
            ("agent_reasoning",     "completion_tokens",    "INTEGER DEFAULT 0"),
            ("agent_reasoning",     "latency_ms",           "REAL DEFAULT 0"),
            ("agent_reasoning",     "raw_prompt",           "TEXT DEFAULT ''"),
            ("strategic_context",   "langfuse_trace_id",    "TEXT DEFAULT NULL"),
            ("strategic_context",   "temporal_workflow_id", "TEXT DEFAULT NULL"),
            ("strategic_context",   "temporal_run_id",      "TEXT DEFAULT NULL"),
            ("portfolio_snapshots", "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("trades",              "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("agent_reasoning",     "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("simulated_trades",    "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("scan_results",        "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("events",              "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("daily_summaries",     "exchange",             "TEXT NOT NULL DEFAULT 'coinbase'"),
            ("trades",              "external_id",          "TEXT DEFAULT NULL"),
            ("trades",              "entry_score",           "REAL DEFAULT NULL"),
        ]
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for table, column, col_type in migrations:
                    # CRIT-2: assert identifiers are on the allowlist before
                    # interpolating them into the DDL statement.
                    if (table, column) not in self._MIGRATION_ALLOWLIST:
                        logger.error(
                            f"Migration skipped — ({table!r}, {column!r}) not in allowlist"
                        )
                        continue
                    savepoint = f"sp_{table}_{column}"
                    try:
                        cur.execute(f"SAVEPOINT {savepoint}")
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                        cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    except psycopg2.errors.DuplicateColumn:
                        # Column already exists — expected on every restart after first run.
                        cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    except Exception as e:
                        # CRIT-4: log unexpected migration failures instead of silently swallowing.
                        cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        logger.warning(
                            f"Migration {table}.{column} failed unexpectedly: {e}"
                        )
                # Create indexes that depend on migrated columns (must run after
                # ALTER TABLE ADD COLUMN, not during _init_db which runs first).
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_exchange ON events(exchange)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_daily_summaries_exchange ON daily_summaries(exchange)"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_external_id ON trades(external_id) WHERE external_id IS NOT NULL"
                )
            conn.commit()

