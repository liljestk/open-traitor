"""
Persistent statistics database for Auto-Traitor.

SQLite-backed store for all trading statistics, portfolio snapshots,
events, and scheduled reports. Designed for fast reads and time-series
queries so the LLM and proactive engine can reference historical data.

The concrete methods live in mixin modules for maintainability:
  - stats_portfolio.py  â†’ PortfolioMixin  (snapshots, daily summaries, analytics)
  - stats_trades.py     â†’ TradesMixin     (trades, events, scheduled reports)
  - stats_reasoning.py  â†’ ReasoningMixin  (agent reasoning, strategic context)
  - stats_predictions.pyâ†’ PredictionsMixin(prediction accuracy, tracked pairs)
  - stats_simulated.py  â†’ SimulatedMixin  (simulated trades, scans, pair follows)

This file contains only: schema init, connection management, and the composed class.
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import threading
from typing import Any, Optional

from src.utils.logger import get_logger
from src.utils.stats_portfolio import PortfolioMixin
from src.utils.stats_trades import TradesMixin
from src.utils.stats_reasoning import ReasoningMixin
from src.utils.stats_predictions import PredictionsMixin
from src.utils.stats_simulated import SimulatedMixin

logger = get_logger("stats")


def get_db_path() -> str:
    """Return the active SQLite DB path, based on profile."""
    profile = os.environ.get("AUTO_TRAITOR_PROFILE", "")
    filename = f"stats_{profile}.db" if profile else "stats.db"
    return os.environ.get("AUTO_TRAITOR_STATS_DB", os.path.join("data", filename))


class StatsDB(
    PortfolioMixin,
    TradesMixin,
    ReasoningMixin,
    PredictionsMixin,
    SimulatedMixin,
):
    """Thread-safe SQLite statistics database.

    All domain-specific methods are inherited from mixin classes.
    This class owns schema initialisation and connection management.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self.db_path: str = db_path  # Public accessor for external consumers
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []  # Track all thread-local connections
        self._conn_lock = threading.Lock()
        self._init_db()
        # L18 fix: ensure connections are closed on process exit
        atexit.register(self.close)
        logger.info(f"ðŸ“Š Stats DB initialized: {db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._conn_lock:
                self._connections.append(conn)
        return self._local.conn

    def close(self) -> None:
        """Close all thread-local connections."""
        with self._conn_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        # Null the current thread's connection so _get_conn() re-opens if needed
        self._local.conn = None

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
                approved_by TEXT DEFAULT 'auto'
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                pair TEXT DEFAULT NULL,
                message TEXT NOT NULL,
                data TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS scheduled_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                cron_expression TEXT NOT NULL,
                query_type TEXT NOT NULL,
                query_params TEXT DEFAULT '{}',
                is_active INTEGER DEFAULT 1,
                last_run_ts TEXT DEFAULT NULL,
                next_run_ts TEXT DEFAULT NULL,
                user_id TEXT DEFAULT 'owner'
            );

            CREATE TABLE IF NOT EXISTS daily_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
                plan_text TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS agent_reasoning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
            );

            CREATE TABLE IF NOT EXISTS strategic_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                horizon TEXT NOT NULL,
                plan_json TEXT NOT NULL DEFAULT '{}',
                summary_text TEXT DEFAULT '',
                langfuse_trace_id TEXT DEFAULT NULL,
                temporal_workflow_id TEXT DEFAULT NULL,
                temporal_run_id TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS simulated_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
            );

            -- Indexes for fast time-range queries
            CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON portfolio_snapshots(ts);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
            CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summaries(date);
            CREATE INDEX IF NOT EXISTS idx_reasoning_cycle ON agent_reasoning(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_reasoning_pair ON agent_reasoning(pair);
            CREATE INDEX IF NOT EXISTS idx_strategic_horizon ON strategic_context(horizon);

            -- Universe scan results (persisted for planning + dashboard)
            CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                exchange TEXT NOT NULL DEFAULT 'coinbase',
                universe_size INTEGER NOT NULL DEFAULT 0,
                scanned_pairs INTEGER NOT NULL DEFAULT 0,
                results_json TEXT NOT NULL DEFAULT '{}',
                top_movers TEXT DEFAULT '[]',
                summary_text TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_results(ts);

            -- Pair follows: tracks which pairs are followed by LLM and/or human
            CREATE TABLE IF NOT EXISTS pair_follows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT 'coinbase',
                followed_by TEXT NOT NULL DEFAULT 'human',  -- 'llm' or 'human'
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                UNIQUE(pair, followed_by)
            );
            CREATE INDEX IF NOT EXISTS idx_pair_follows_pair ON pair_follows(pair);
            CREATE INDEX IF NOT EXISTS idx_pair_follows_exchange ON pair_follows(exchange);
        """)
        conn.commit()
        self._migrate_db(conn)

    def _migrate_db(self, conn: sqlite3.Connection) -> None:
        """Add new columns to existing tables without breaking old installs."""
        migrations = [
            "ALTER TABLE agent_reasoning ADD COLUMN langfuse_trace_id TEXT DEFAULT NULL",
            "ALTER TABLE agent_reasoning ADD COLUMN langfuse_span_id TEXT DEFAULT NULL",
            "ALTER TABLE agent_reasoning ADD COLUMN prompt_tokens INTEGER DEFAULT 0",
            "ALTER TABLE agent_reasoning ADD COLUMN completion_tokens INTEGER DEFAULT 0",
            "ALTER TABLE agent_reasoning ADD COLUMN latency_ms REAL DEFAULT 0",
            "ALTER TABLE agent_reasoning ADD COLUMN raw_prompt TEXT DEFAULT ''",
            "ALTER TABLE strategic_context ADD COLUMN langfuse_trace_id TEXT DEFAULT NULL",
            "ALTER TABLE strategic_context ADD COLUMN temporal_workflow_id TEXT DEFAULT NULL",
            "ALTER TABLE strategic_context ADD COLUMN temporal_run_id TEXT DEFAULT NULL",
            # simulated_trades table itself is created in _init_db; these handle old DBs that
            # were created before the simulated_trades table was added.
            """CREATE TABLE IF NOT EXISTS simulated_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
            )""",
            # Currency-agnostic rename: usd_amount â†’ quote_amount, fee_usd â†’ fee_quote
            "ALTER TABLE trades RENAME COLUMN usd_amount TO quote_amount",
            "ALTER TABLE trades RENAME COLUMN fee_usd TO fee_quote",
            # Multi-exchange: add exchange column to existing tables
            "ALTER TABLE portfolio_snapshots ADD COLUMN exchange TEXT NOT NULL DEFAULT 'coinbase'",
            "ALTER TABLE trades ADD COLUMN exchange TEXT NOT NULL DEFAULT 'coinbase'",
            "ALTER TABLE agent_reasoning ADD COLUMN exchange TEXT NOT NULL DEFAULT 'coinbase'",
            "ALTER TABLE simulated_trades ADD COLUMN exchange TEXT NOT NULL DEFAULT 'coinbase'",
            "ALTER TABLE scan_results ADD COLUMN exchange TEXT NOT NULL DEFAULT 'coinbase'",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists or already renamed
        conn.commit()

