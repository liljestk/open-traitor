"""
Persistent statistics database for Auto-Traitor.

SQLite-backed store for all trading statistics, portfolio snapshots,
events, and scheduled reports. Designed for fast reads and time-series
queries so the LLM and proactive engine can reference historical data.

Tables:
  - portfolio_snapshots: Periodic portfolio value, PnL, drawdown snapshots
  - trades: Every trade executed (buy/sell), with prices, confidence, PnL
  - events: All notable events (signals, stops, circuit breakers, user actions)
  - scheduled_reports: User-configured recurring Telegram reports
  - daily_summaries: End-of-day summaries (auto-generated)
  - agent_reasoning: Full LLM reasoning JSON for every agent call, linked to trade outcomes
  - strategic_context: Output of Temporal planning workflows (daily/weekly/monthly plans)
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("stats")

def get_db_path() -> str:
    """Return the active SQLite DB path, based on profile."""
    profile = os.environ.get("AUTO_TRAITOR_PROFILE", "")
    filename = f"stats_{profile}.db" if profile else "stats.db"
    return os.environ.get("AUTO_TRAITOR_STATS_DB", os.path.join("data", filename))


class StatsDB:
    """Thread-safe SQLite statistics database."""

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
        logger.info(f"📊 Stats DB initialized: {db_path}")

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
            # Currency-agnostic rename: usd_amount → quote_amount, fee_usd → fee_quote
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

    # ─── Portfolio Snapshots ───────────────────────────────────────────────

    def record_snapshot(
        self,
        portfolio_value: float,
        cash_balance: float = 0,
        return_pct: float = 0,
        total_pnl: float = 0,
        max_drawdown: float = 0,
        open_positions: Optional[dict] = None,
        current_prices: Optional[dict] = None,
        fear_greed_value: Optional[float] = None,
        high_stakes_active: bool = False,
        exchange: str = "coinbase",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO portfolio_snapshots
               (exchange, portfolio_value, cash_balance, return_pct, total_pnl, max_drawdown,
                open_positions, current_prices, fear_greed_value, high_stakes_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exchange, portfolio_value, cash_balance, return_pct, total_pnl, max_drawdown,
                json.dumps(open_positions or {}, default=str),
                json.dumps(current_prices or {}, default=str),
                fear_greed_value,
                1 if high_stakes_active else 0,
            ),
        )
        conn.commit()

    def get_snapshots(self, hours: int = 24, limit: int = 100) -> list[dict]:
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_portfolio_range(self, hours: int = 24, quote_currency: str | None = None) -> dict:
        """Get min/max/avg portfolio value over a period.

        Uses the same anomaly-filtering logic as get_portfolio_history to exclude
        paper-mode bleed-through values.
        """
        history = self.get_portfolio_history(hours=hours, quote_currency=quote_currency)
        if not history:
            return {"low": 0, "high": 0, "avg": 0, "samples": 0}
        values = [h["portfolio_value"] for h in history]
        return {
            "low": min(values),
            "high": max(values),
            "avg": sum(values) / len(values),
            "samples": len(values),
        }

    # ─── Trades ────────────────────────────────────────────────────────────

    def record_trade(
        self,
        pair: str,
        action: str,
        price: float,
        quantity: float = 0,
        quote_amount: float = 0,
        confidence: float = 0,
        signal_type: str = "",
        stop_loss: float = 0,
        take_profit: float = 0,
        reasoning: str = "",
        pnl: Optional[float] = None,
        fee_quote: float = 0,
        is_rotation: bool = False,
        approved_by: str = "auto",
        exchange: str = "coinbase",
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO trades
               (exchange, pair, action, quantity, price, quote_amount, confidence,
                signal_type, stop_loss, take_profit, reasoning, pnl,
                fee_quote, is_rotation, approved_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exchange, pair, action, quantity, price, quote_amount, confidence,
                signal_type, stop_loss, take_profit, reasoning, pnl,
                fee_quote, 1 if is_rotation else 0, approved_by,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def get_trades(self, hours: int = 24, pair: Optional[str] = None, limit: int = 50, quote_currency: str | None = None) -> list[dict]:
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if pair:
            rows = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? AND pair = ? ORDER BY ts DESC LIMIT ?",
                (cutoff, pair, limit),
            ).fetchall()
        elif quote_currency:
            suffix = f"%-{quote_currency.upper()}"
            rows = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? AND UPPER(pair) LIKE ? ORDER BY ts DESC LIMIT ?",
                (cutoff, suffix, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_stats(self, hours: int = 24, quote_currency: str | None = None) -> dict:
        """Get aggregate trade statistics."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        base_sql = """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing,
                SUM(CASE WHEN pnl = 0 OR pnl IS NULL THEN 1 ELSE 0 END) as pending,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(MAX(pnl), 0) as best_pnl,
                COALESCE(MIN(pnl), 0) as worst_pnl,
                COALESCE(AVG(pnl), 0) as avg_pnl,
                COALESCE(SUM(quote_amount), 0) as total_volume,
                COALESCE(SUM(fee_quote), 0) as total_fees,
                COALESCE(AVG(confidence), 0) as avg_confidence
               FROM trades WHERE ts >= ?"""
        if quote_currency:
            row = conn.execute(base_sql + " AND UPPER(pair) LIKE ?", (cutoff, f"%-{quote_currency.upper()}")).fetchone()
        else:
            row = conn.execute(base_sql, (cutoff,)).fetchone()
        return dict(row) if row else {}

    def get_pair_stats(self, pair: str, hours: int = 168) -> dict:
        """Get stats for a specific trading pair (default 7 days)."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN action='buy' THEN 1 ELSE 0 END) as buys,
                SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END) as sells,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(AVG(confidence), 0) as avg_confidence,
                COALESCE(SUM(quote_amount), 0) as total_volume
               FROM trades WHERE ts >= ? AND pair = ?""",
            (cutoff, pair),
        ).fetchone()
        return dict(row) if row else {}

    def get_win_loss_stats(self, hours: int = 720, quote_currency: str | None = None) -> dict:
        """
        Get win rate and average win/loss for Kelly Criterion position sizing.
        Default look-back: 30 days (720 hours).

        Returns:
            win_rate: fraction of profitable trades (0-1)
            avg_win: average profit on winning trades (absolute)
            avg_loss: average loss on losing trades (absolute, positive number)
            sample_size: number of closed trades used
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        base_sql = """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
                COALESCE(AVG(CASE WHEN pnl < 0 THEN ABS(pnl) END), 0) as avg_loss
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL AND pnl != 0"""
        if quote_currency:
            row = conn.execute(base_sql + " AND UPPER(pair) LIKE ?", (cutoff, f"%-{quote_currency.upper()}")).fetchone()
        else:
            row = conn.execute(base_sql, (cutoff,)).fetchone()
        if not row or row["total"] == 0:
            return {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0}
        return {
            "win_rate": row["wins"] / row["total"],
            "avg_win": row["avg_win"],
            "avg_loss": row["avg_loss"],
            "sample_size": row["total"],
        }

    # ─── Events ────────────────────────────────────────────────────────────

    def record_event(
        self,
        event_type: str,
        message: str,
        severity: str = "info",
        pair: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO events (event_type, severity, pair, message, data)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, severity, pair, message, json.dumps(data or {}, default=str)),
        )
        conn.commit()

    def get_events(self, hours: int = 24, event_type: Optional[str] = None, limit: int = 50, quote_currency: str | None = None) -> list[dict]:
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # Build optional quote-currency filter on the pair column
        qc_clause = ""
        params_extra: list = []
        if quote_currency:
            qc_clause = " AND UPPER(pair) LIKE ?"
            params_extra = [f"%-{quote_currency.upper()}"]
        if event_type:
            rows = conn.execute(
                "SELECT * FROM events WHERE ts >= ? AND event_type = ?" + qc_clause + " ORDER BY ts DESC LIMIT ?",
                (cutoff, event_type, *params_extra, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE ts >= ?" + qc_clause + " ORDER BY ts DESC LIMIT ?",
                (cutoff, *params_extra, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_event_counts(self, hours: int = 24) -> dict:
        """Get event counts by type."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """SELECT event_type, COUNT(*) as count, severity
               FROM events WHERE ts >= ?
               GROUP BY event_type, severity ORDER BY count DESC""",
            (cutoff,),
        ).fetchall()
        return {f"{r['event_type']}({r['severity']})": r["count"] for r in rows}

    # ─── Scheduled Reports ─────────────────────────────────────────────────

    def add_scheduled_report(
        self,
        name: str,
        description: str,
        cron_expression: str,
        query_type: str,
        query_params: Optional[dict] = None,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO scheduled_reports
               (name, description, cron_expression, query_type, query_params)
               VALUES (?, ?, ?, ?, ?)""",
            (name, description, cron_expression, query_type,
             json.dumps(query_params or {})),
        )
        conn.commit()
        return cursor.lastrowid

    def get_active_schedules(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM scheduled_reports WHERE is_active = 1 ORDER BY id",
        ).fetchall()
        return [dict(r) for r in rows]

    def update_schedule_last_run(self, schedule_id: int) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE scheduled_reports SET last_run_ts = ? WHERE id = ?",
            (now, schedule_id),
        )
        conn.commit()

    def delete_schedule(self, schedule_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM scheduled_reports WHERE id = ?", (schedule_id,),
        )
        conn.commit()
        return cursor.rowcount > 0

    # ─── Daily Summaries ───────────────────────────────────────────────────

    def save_daily_summary(
        self,
        date: str,
        opening_value: float = 0,
        closing_value: float = 0,
        high_value: float = 0,
        low_value: float = 0,
        total_trades: int = 0,
        winning_trades: int = 0,
        losing_trades: int = 0,
        total_pnl: float = 0,
        best_trade: Optional[str] = None,
        worst_trade: Optional[str] = None,
        events_count: int = 0,
        summary_text: str = "",
        plan_text: str = "",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO daily_summaries
               (date, opening_value, closing_value, high_value, low_value,
                total_trades, winning_trades, losing_trades, total_pnl,
                best_trade, worst_trade, events_count, summary_text, plan_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date, opening_value, closing_value, high_value, low_value,
                total_trades, winning_trades, losing_trades, total_pnl,
                best_trade, worst_trade, events_count, summary_text, plan_text,
            ),
        )
        conn.commit()

    def get_daily_summary(self, date: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM daily_summaries WHERE date = ?", (date,),
        ).fetchone()
        return dict(row) if row else None

    def get_daily_summaries(self, days: int = 7, quote_currency: str | None = None) -> list[dict]:
        """Get daily summaries.  When *quote_currency* is provided and the
        table has an ``exchange`` column, filter by exchange.  Falls back
        gracefully when the column doesn't exist (legacy DBs)."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        if quote_currency:
            _qc_exchange_map = {"EUR": "coinbase", "SEK": "nordnet", "USD": "ibkr"}
            exchange = _qc_exchange_map.get(quote_currency.upper())
            if exchange:
                # Try filtering by exchange; fall back if column doesn't exist
                try:
                    rows = conn.execute(
                        "SELECT * FROM daily_summaries WHERE date >= ? AND exchange = ? ORDER BY date DESC",
                        (cutoff, exchange),
                    ).fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    pass  # exchange column doesn't exist in this DB
        rows = conn.execute(
            "SELECT * FROM daily_summaries WHERE date >= ? ORDER BY date DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Analytics Queries ─────────────────────────────────────────────────

    def get_performance_summary(self, hours: int = 24, quote_currency: str | None = None) -> dict:
        """Get a comprehensive performance summary for the LLM."""
        return {
            "trade_stats": self.get_trade_stats(hours, quote_currency=quote_currency),
            "portfolio_range": self.get_portfolio_range(hours, quote_currency=quote_currency),
            "event_counts": self.get_event_counts(hours),
            "recent_trades": self.get_trades(hours, limit=10, quote_currency=quote_currency),
        }

    def get_portfolio_history(self, hours: int = 24, quote_currency: str | None = None) -> list[dict]:
        """Get portfolio value over time (for trend analysis).

        Filters out anomalous snapshots from first-boot:
        - portfolio_value == 0  (before live sync)
        - portfolio_value wildly different from the stable median
          (paper-mode initial_balance bleed-through, e.g. ~914 vs real ~6)

        When *quote_currency* is given, only snapshots whose exchange matches
        the currency's canonical exchange are returned (via the ``exchange``
        column added in the multi-exchange migration).
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        base_sql = """SELECT ts, portfolio_value, return_pct, total_pnl
               FROM portfolio_snapshots WHERE ts >= ? AND portfolio_value > 0"""
        params: list = [cutoff]
        if quote_currency:
            # Map quote currency → exchange name stored in the exchange column
            _qc_exchange_map = {"EUR": "coinbase", "SEK": "nordnet", "USD": "ibkr"}
            exchange = _qc_exchange_map.get(quote_currency.upper())
            if exchange:
                base_sql += " AND exchange = ?"
                params.append(exchange)
        rows = conn.execute(base_sql + " ORDER BY ts", params).fetchall()
        if not rows:
            return []

        # Detect and remove paper-mode bleed-through values.
        # Strategy: find the *median* of the last 20% of values (most recent = most trustworthy),
        # then discard anything more than 10x above that median.
        values = [r["portfolio_value"] for r in rows]
        tail = sorted(values[max(0, len(values) - len(values) // 5):])
        if tail:
            median_val = tail[len(tail) // 2]
            if median_val > 0:
                threshold = max(median_val * 10, 100)  # at least 100 to avoid filtering micro-portfolios
                rows = [r for r in rows if r["portfolio_value"] <= threshold]

        return [dict(r) for r in rows]

    def get_best_worst_trades(self, hours: int = 168, quote_currency: str | None = None) -> dict:
        """Get best and worst trades in a period."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        base_best = """SELECT pair, action, pnl, price, quote_amount, ts
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL"""
        base_worst = base_best  # same WHERE clause
        if quote_currency:
            currency_filter = " AND UPPER(pair) LIKE ?"
            suffix = f"%-{quote_currency.upper()}"
            best = conn.execute(base_best + currency_filter + " ORDER BY pnl DESC LIMIT 3", (cutoff, suffix)).fetchall()
            worst = conn.execute(base_worst + currency_filter + " ORDER BY pnl ASC LIMIT 3", (cutoff, suffix)).fetchall()
        else:
            best = conn.execute(base_best + " ORDER BY pnl DESC LIMIT 3", (cutoff,)).fetchall()
            worst = conn.execute(base_worst + " ORDER BY pnl ASC LIMIT 3", (cutoff,)).fetchall()
        return {
            "best": [dict(r) for r in best],
            "worst": [dict(r) for r in worst],
        }

    # ─── Agent Reasoning ───────────────────────────────────────────────────

    def save_reasoning(
        self,
        cycle_id: str,
        pair: str,
        agent_name: str,
        reasoning_json: dict,
        signal_type: str = "",
        confidence: float = 0.0,
        trade_id: Optional[int] = None,
        langfuse_trace_id: Optional[str] = None,
        langfuse_span_id: Optional[str] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        raw_prompt: str = "",
        exchange: str = "coinbase",
    ) -> int:
        """Persist a full LLM reasoning trace for one agent call."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO agent_reasoning
               (exchange, cycle_id, pair, agent_name, reasoning_json, signal_type, confidence,
                trade_id, langfuse_trace_id, langfuse_span_id,
                prompt_tokens, completion_tokens, latency_ms, raw_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exchange, cycle_id, pair, agent_name,
                json.dumps(reasoning_json, default=str),
                signal_type, confidence, trade_id,
                langfuse_trace_id, langfuse_span_id,
                prompt_tokens, completion_tokens, latency_ms, raw_prompt,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def backfill_reasoning_trade_id(self, cycle_id: str, trade_id: int) -> None:
        """Link all reasoning rows for a cycle to the trade that resulted from it."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE agent_reasoning SET trade_id = ? WHERE cycle_id = ? AND trade_id IS NULL",
            (trade_id, cycle_id),
        )
        conn.commit()

    def get_recent_outcomes(self, pair: str, n: int = 10, currency_symbol: str = "$") -> str:
        """
        Return a human-readable summary of the last N closed trades for a pair,
        with the reasoning that produced them. Used for outcome feedback injection
        into agent prompts.
        """
        sym = currency_symbol
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT
                t.ts, t.action, t.price, t.pnl, t.confidence, t.signal_type,
                ar.reasoning_json, ar.agent_name
               FROM trades t
               LEFT JOIN agent_reasoning ar
                   ON ar.trade_id = t.id AND ar.agent_name = 'market_analyst'
               WHERE t.pair = ? AND t.pnl IS NOT NULL
               ORDER BY t.ts DESC
               LIMIT ?""",
            (pair, n),
        ).fetchall()

        if not rows:
            return "No closed trade history for this pair yet."

        lines = []
        for r in rows:
            pnl_str = f"+{sym}{r['pnl']:.2f}" if r["pnl"] >= 0 else f"-{sym}{abs(r['pnl']):.2f}"
            outcome = "WIN" if r["pnl"] >= 0 else "LOSS"
            key_factors = "N/A"
            if r["reasoning_json"]:
                try:
                    rj = json.loads(r["reasoning_json"])
                    factors = rj.get("key_factors", [])
                    if factors:
                        key_factors = ", ".join(str(f) for f in factors[:3])
                except Exception:
                    pass
            lines.append(
                f"[{r['ts'][:10]}] {outcome} {pnl_str} | {r['action'].upper()} "
                f"@ {sym}{r['price']:,.2f} | signal={r['signal_type']} "
                f"conf={r['confidence']:.0%} | factors: {key_factors}"
            )

        return "\n".join(lines)

    def get_cycles(
        self,
        pair: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        quote_currency: str | None = None,
    ) -> list[dict]:
        """
        Return a paginated list of trading cycles with outcome summary.
        Each row represents one unique cycle_id with its final trade outcome.
        Used by the dashboard Cycle Explorer page.
        """
        conn = self._get_conn()
        if pair:
            rows = conn.execute(
                """SELECT
                    ar.cycle_id,
                    ar.pair,
                    MIN(ar.ts) as started_at,
                    MAX(ar.ts) as finished_at,
                    COUNT(DISTINCT ar.agent_name) as agent_count,
                    MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.signal_type END) as signal_type,
                    MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.confidence END) as confidence,
                    MAX(CASE WHEN ar.agent_name='strategist' THEN
                        json_extract(ar.reasoning_json, '$.action') END) as action,
                    t.id as trade_id,
                    t.pnl,
                    t.quote_amount,
                    t.price,
                    ar.langfuse_trace_id,
                    SUM(ar.prompt_tokens) as total_prompt_tokens,
                    SUM(ar.completion_tokens) as total_completion_tokens,
                    SUM(ar.latency_ms) as total_latency_ms
                   FROM agent_reasoning ar
                   LEFT JOIN trades t ON t.id = ar.trade_id
                   WHERE ar.pair = ?
                   GROUP BY ar.cycle_id
                   ORDER BY started_at DESC
                   LIMIT ? OFFSET ?""",
                (pair, limit, offset),
            ).fetchall()
        elif quote_currency:
            suffix = f"%-{quote_currency.upper()}"
            rows = conn.execute(
                """SELECT
                    ar.cycle_id,
                    ar.pair,
                    MIN(ar.ts) as started_at,
                    MAX(ar.ts) as finished_at,
                    COUNT(DISTINCT ar.agent_name) as agent_count,
                    MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.signal_type END) as signal_type,
                    MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.confidence END) as confidence,
                    MAX(CASE WHEN ar.agent_name='strategist' THEN
                        json_extract(ar.reasoning_json, '$.action') END) as action,
                    t.id as trade_id,
                    t.pnl,
                    t.quote_amount,
                    t.price,
                    ar.langfuse_trace_id,
                    SUM(ar.prompt_tokens) as total_prompt_tokens,
                    SUM(ar.completion_tokens) as total_completion_tokens,
                    SUM(ar.latency_ms) as total_latency_ms
                   FROM agent_reasoning ar
                   LEFT JOIN trades t ON t.id = ar.trade_id
                   WHERE UPPER(ar.pair) LIKE ?
                   GROUP BY ar.cycle_id
                   ORDER BY started_at DESC
                   LIMIT ? OFFSET ?""",
                (suffix, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT
                    ar.cycle_id,
                    ar.pair,
                    MIN(ar.ts) as started_at,
                    MAX(ar.ts) as finished_at,
                    COUNT(DISTINCT ar.agent_name) as agent_count,
                    MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.signal_type END) as signal_type,
                    MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.confidence END) as confidence,
                    MAX(CASE WHEN ar.agent_name='strategist' THEN
                        json_extract(ar.reasoning_json, '$.action') END) as action,
                    t.id as trade_id,
                    t.pnl,
                    t.quote_amount,
                    t.price,
                    ar.langfuse_trace_id,
                    SUM(ar.prompt_tokens) as total_prompt_tokens,
                    SUM(ar.completion_tokens) as total_completion_tokens,
                    SUM(ar.latency_ms) as total_latency_ms
                   FROM agent_reasoning ar
                   LEFT JOIN trades t ON t.id = ar.trade_id
                   GROUP BY ar.cycle_id
                   ORDER BY started_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cycle_full(self, cycle_id: str) -> Optional[dict]:
        """
        Return the complete trace for one cycle: all agent spans + trade outcome.
        Used by the dashboard Cycle Playback page and the REST API.
        """
        conn = self._get_conn()
        spans = conn.execute(
            """SELECT
                ar.id, ar.ts, ar.agent_name, ar.reasoning_json,
                ar.signal_type, ar.confidence, ar.langfuse_trace_id,
                ar.langfuse_span_id, ar.prompt_tokens, ar.completion_tokens,
                ar.latency_ms, ar.raw_prompt, ar.pair
               FROM agent_reasoning ar
               WHERE ar.cycle_id = ?
               ORDER BY ar.ts ASC""",
            (cycle_id,),
        ).fetchall()

        if not spans:
            return None

        # Parse JSON fields
        spans_list = []
        for s in spans:
            row = dict(s)
            try:
                row["reasoning_json"] = json.loads(row["reasoning_json"] or "{}")
            except Exception:
                pass
            spans_list.append(row)

        # Trade outcome (if one resulted from this cycle)
        trade_row = conn.execute(
            """SELECT t.* FROM trades t
               INNER JOIN agent_reasoning ar ON ar.trade_id = t.id
               WHERE ar.cycle_id = ?
               LIMIT 1""",
            (cycle_id,),
        ).fetchone()

        first = spans_list[0]
        last = spans_list[-1]
        total_latency = sum(s["latency_ms"] or 0 for s in spans_list)
        total_tokens = sum((s["prompt_tokens"] or 0) + (s["completion_tokens"] or 0) for s in spans_list)

        # Derive decision outcome + reason from the spans
        decision_outcome = "executed" if trade_row else "hold"
        decision_reason = ""

        # Check agent spans for more specific outcomes
        risk_span = next((s for s in spans_list if s["agent_name"] == "risk_manager"), None)
        strategist_span = next((s for s in spans_list if s["agent_name"] == "strategist"), None)

        if trade_row:
            decision_outcome = "executed"
            decision_reason = "Trade passed all checks and was executed."
        elif risk_span:
            rj = risk_span.get("reasoning_json") or {}
            if not rj.get("approved", True):
                decision_outcome = "rejected"
                decision_reason = rj.get("reason", "Rejected by risk manager.")
            elif rj.get("needs_approval"):
                decision_outcome = "pending_approval"
                decision_reason = "Trade queued for Telegram approval."
            else:
                # Risk approved but no trade recorded — execution may have failed
                decision_outcome = "execution_failed"
                decision_reason = "Risk manager approved but trade was not recorded."
        elif strategist_span:
            rj = strategist_span.get("reasoning_json") or {}
            if rj.get("action") == "hold":
                decision_outcome = "hold"
                decision_reason = rj.get("reasoning") or rj.get("reason") or "Strategist recommended hold."
        else:
            decision_outcome = "hold"
            decision_reason = "No strategy generated."

        return {
            "cycle_id": cycle_id,
            "pair": first["pair"],
            "started_at": first["ts"],
            "finished_at": last["ts"],
            "total_latency_ms": round(total_latency, 1),
            "total_tokens": total_tokens,
            "langfuse_trace_id": first.get("langfuse_trace_id"),
            "spans": spans_list,
            "trade": dict(trade_row) if trade_row else None,
            "decision_outcome": decision_outcome,
            "decision_reason": decision_reason,
        }

    def get_reasoning_for_review(self, days: int = 7, pair: Optional[str] = None) -> list[dict]:
        """Fetch reasoning+outcome rows for use in planning workflow LLM review."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if pair:
            rows = conn.execute(
                """SELECT ar.ts, ar.pair, ar.agent_name, ar.reasoning_json,
                          ar.signal_type, ar.confidence,
                          t.action, t.pnl, t.price
                   FROM agent_reasoning ar
                   LEFT JOIN trades t ON t.id = ar.trade_id
                   WHERE ar.ts >= ? AND ar.pair = ?
                   ORDER BY ar.ts DESC LIMIT 200""",
                (cutoff, pair),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ar.ts, ar.pair, ar.agent_name, ar.reasoning_json,
                          ar.signal_type, ar.confidence,
                          t.action, t.pnl, t.price
                   FROM agent_reasoning ar
                   LEFT JOIN trades t ON t.id = ar.trade_id
                   WHERE ar.ts >= ?
                   ORDER BY ar.ts DESC LIMIT 200""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Strategic Context ─────────────────────────────────────────────────

    def save_strategic_context(
        self,
        horizon: str,
        plan_json: dict,
        summary_text: str = "",
        langfuse_trace_id: Optional[str] = None,
        temporal_workflow_id: Optional[str] = None,
        temporal_run_id: Optional[str] = None,
    ) -> int:
        """Persist a planning workflow output (daily / weekly / monthly)."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO strategic_context
               (horizon, plan_json, summary_text,
                langfuse_trace_id, temporal_workflow_id, temporal_run_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                horizon, json.dumps(plan_json, default=str), summary_text,
                langfuse_trace_id, temporal_workflow_id, temporal_run_id,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def get_latest_strategic_context(self, horizon: Optional[str] = None) -> list[dict]:
        """Get the most recent strategic context, optionally filtered by horizon."""
        conn = self._get_conn()
        if horizon:
            rows = conn.execute(
                """SELECT * FROM strategic_context WHERE horizon = ?
                   ORDER BY ts DESC LIMIT 1""",
                (horizon,),
            ).fetchall()
        else:
            # Latest one per horizon
            rows = conn.execute(
                """SELECT sc.* FROM strategic_context sc
                   INNER JOIN (
                       SELECT horizon, MAX(ts) as max_ts
                       FROM strategic_context GROUP BY horizon
                   ) latest ON sc.horizon = latest.horizon AND sc.ts = latest.max_ts
                   ORDER BY sc.horizon""",
            ).fetchall()
        return [dict(r) for r in rows]

    def write_daily_plan(self, date: str, plan_text: str) -> None:
        """Write the daily plan text into the daily_summaries table."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO daily_summaries (date, plan_text)
               VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET plan_text = excluded.plan_text""",
            (date, plan_text),
        )
        conn.commit()

    # ─── Simulated Trades ──────────────────────────────────────────────────

    def record_simulated_trade(
        self,
        pair: str,
        from_currency: str,
        from_amount: float,
        entry_price: float,
        quantity: float,
        to_currency: str,
        notes: str = "",
        exchange: str = "coinbase",
    ) -> int:
        """Record a new simulated (paper) trade and return its id."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO simulated_trades
               (exchange, pair, from_currency, from_amount, entry_price, quantity, to_currency, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (exchange, pair, from_currency, from_amount, entry_price, quantity, to_currency, notes),
        )
        conn.commit()
        return cursor.lastrowid

    def get_simulated_trades(self, include_closed: bool = False, quote_currency: str | None = None) -> list[dict]:
        """Return all (open, or all including closed) simulated trades."""
        conn = self._get_conn()
        if include_closed:
            if quote_currency:
                rows = conn.execute(
                    "SELECT * FROM simulated_trades WHERE UPPER(pair) LIKE ? ORDER BY ts DESC",
                    (f"%-{quote_currency.upper()}",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM simulated_trades ORDER BY ts DESC"
                ).fetchall()
        else:
            if quote_currency:
                rows = conn.execute(
                    "SELECT * FROM simulated_trades WHERE status = 'open' AND UPPER(pair) LIKE ? ORDER BY ts DESC",
                    (f"%-{quote_currency.upper()}",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM simulated_trades WHERE status = 'open' ORDER BY ts DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def close_simulated_trade(
        self,
        sim_id: int,
        close_price: float,
    ) -> Optional[dict]:
        """
        Mark a simulated trade as closed, compute and store final PnL.
        Returns the updated row dict, or None if not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM simulated_trades WHERE id = ? AND status = 'open'",
            (sim_id,),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        quantity = row["quantity"]
        entry_price = row["entry_price"]
        # Direction-aware PnL: short when from_currency is the base (sold base)
        pair_base = row["pair"].split("-")[0]
        is_short = row.get("from_currency", "") == pair_base
        if is_short:
            pnl_abs = (entry_price - close_price) * quantity
            pnl_pct = ((entry_price / close_price) - 1) * 100 if close_price > 0 else 0.0
        else:
            pnl_abs = (close_price - entry_price) * quantity
            pnl_pct = ((close_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
        closed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE simulated_trades
               SET status='closed', closed_at=?, close_price=?,
                   close_pnl_abs=?, close_pnl_pct=?
               WHERE id=?""",
            (closed_at, close_price, round(pnl_abs, 6), round(pnl_pct, 4), sim_id),
        )
        conn.commit()
        row.update(
            status="closed",
            closed_at=closed_at,
            close_price=close_price,
            close_pnl_abs=round(pnl_abs, 6),
            close_pnl_pct=round(pnl_pct, 4),
        )
        return row

    # ─── Universe Scan Results ─────────────────────────────────────────────

    def save_scan_results(
        self,
        universe_size: int,
        scanned_pairs: int,
        results_json: dict,
        top_movers: list[dict] | None = None,
        summary_text: str = "",
    ) -> int:
        """Persist a universe scan snapshot (technicals per pair)."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO scan_results
               (universe_size, scanned_pairs, results_json, top_movers, summary_text)
               VALUES (?, ?, ?, ?, ?)""",
            (
                universe_size,
                scanned_pairs,
                json.dumps(results_json, default=str),
                json.dumps(top_movers or [], default=str),
                summary_text,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def get_latest_scan_results(self) -> Optional[dict]:
        """Get the most recent universe scan results."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM scan_results ORDER BY ts DESC LIMIT 1",
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["results_json"] = json.loads(result.get("results_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            result["results_json"] = {}
        try:
            result["top_movers"] = json.loads(result.get("top_movers", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["top_movers"] = []
        # Guard: json.loads of a JSON-encoded string returns a str, not a list
        # (old data stored top_movers as a plain string before M4 fix)
        if not isinstance(result["top_movers"], list):
            result["top_movers"] = []
        return result

    # ─── Prediction Accuracy ───────────────────────────────────────────────

    def get_prediction_accuracy(self, days: int = 30, quote_currency: str | None = None) -> dict:
        """
        Compute signal prediction accuracy by comparing market_analyst signals
        with actual price movements over subsequent hours.

        Uses the current_prices stored in portfolio_snapshots to determine what
        actually happened after each prediction.

        If *quote_currency* is given (e.g. "EUR"), only pairs ending in
        "-EUR" are included.
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 1. Get all market_analyst predictions with signal details
        if quote_currency:
            suffix = f"-{quote_currency.upper()}"
            predictions = conn.execute(
                """SELECT
                    ar.ts, ar.pair, ar.signal_type, ar.confidence,
                    ar.reasoning_json, ar.cycle_id
                   FROM agent_reasoning ar
                   WHERE ar.agent_name = 'market_analyst'
                     AND ar.ts >= ?
                     AND UPPER(ar.pair) LIKE ?
                   ORDER BY ar.ts ASC""",
                (cutoff, f"%{suffix}"),
            ).fetchall()
        else:
            predictions = conn.execute(
                """SELECT
                    ar.ts, ar.pair, ar.signal_type, ar.confidence,
                    ar.reasoning_json, ar.cycle_id
                   FROM agent_reasoning ar
                   WHERE ar.agent_name = 'market_analyst'
                     AND ar.ts >= ?
                   ORDER BY ar.ts ASC""",
                (cutoff,),
            ).fetchall()

        if not predictions:
            return {
                "predictions": [],
                "per_pair": {},
                "overall": {
                    "total": 0, "correct_24h": 0, "evaluated_24h": 0,
                    "correct_1h": 0, "evaluated_1h": 0,
                    "accuracy_24h_pct": None, "accuracy_1h_pct": None,
                },
                "by_signal_type": {},
                "confidence_calibration": [],
                "daily_accuracy": [],
            }

        # 2. Build a price lookup from portfolio snapshots (current_prices JSON)
        snapshots = conn.execute(
            """SELECT ts, current_prices
               FROM portfolio_snapshots
               WHERE ts >= ? AND current_prices IS NOT NULL AND current_prices != '{}'
               ORDER BY ts ASC""",
            (cutoff,),
        ).fetchall()

        # Parse into list of (ts, prices_dict) — sample every ~5 min
        price_timeline: list[tuple[str, dict]] = []
        for snap in snapshots:
            try:
                prices = json.loads(snap["current_prices"] or "{}")
                if prices:
                    price_timeline.append((snap["ts"], prices))
            except (json.JSONDecodeError, TypeError):
                continue

        def _find_price(pair: str, target_ts: str) -> float | None:
            """Find the closest price for a pair at or after target_ts."""
            for ts, prices in price_timeline:
                if ts >= target_ts:
                    # Try exact pair, then common variants
                    for key in [pair, pair.replace("-", "/"), pair.replace("/", "-")]:
                        if key in prices:
                            val = prices[key]
                            return float(val) if val else None
                    return None
            return None

        def _ts_plus_hours(ts_str: str, hours: int) -> str:
            """Add hours to an ISO timestamp string."""
            from datetime import datetime, timedelta
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return (dt + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
            except Exception:
                return ts_str

        # 3. Evaluate each prediction
        results: list[dict] = []
        for pred in predictions:
            try:
                reasoning = json.loads(pred["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                reasoning = {}

            signal = pred["signal_type"] or "neutral"
            confidence = pred["confidence"] or 0.0
            pair = pred["pair"]
            pred_ts = pred["ts"]

            # Price at prediction time
            price_at_signal = None
            for key in ["suggested_entry", "current_price"]:
                val = reasoning.get(key)
                if val and float(val) > 0:
                    price_at_signal = float(val)
                    break
            if not price_at_signal:
                price_at_signal = _find_price(pair, pred_ts)

            if not price_at_signal:
                continue

            # Direction the bot predicted
            is_bullish = signal in ("strong_buy", "buy", "weak_buy")
            is_bearish = signal in ("strong_sell", "sell", "weak_sell")
            if not is_bullish and not is_bearish:
                continue  # neutral predictions can't be evaluated

            # Check outcome at multiple horizons
            horizons = {"1h": 1, "4h": 4, "24h": 24, "7d": 168}
            outcomes: dict[str, dict | None] = {}
            for label, hours in horizons.items():
                future_ts = _ts_plus_hours(pred_ts, hours)
                actual_price = _find_price(pair, future_ts)
                if actual_price and actual_price > 0:
                    pct_change = (actual_price - price_at_signal) / price_at_signal * 100
                    price_went_up = actual_price > price_at_signal
                    correct = (is_bullish and price_went_up) or (is_bearish and not price_went_up)
                    outcomes[label] = {
                        "actual_price": round(actual_price, 6),
                        "pct_change": round(pct_change, 4),
                        "correct": correct,
                    }
                else:
                    outcomes[label] = None

            results.append({
                "ts": pred_ts,
                "pair": pair,
                "signal_type": signal,
                "confidence": round(confidence, 3),
                "entry_price": round(price_at_signal, 6),
                "suggested_tp": reasoning.get("suggested_take_profit"),
                "suggested_sl": reasoning.get("suggested_stop_loss"),
                "outcomes": outcomes,
            })

        # 4. Aggregate per-pair accuracy
        per_pair: dict[str, dict] = {}
        for r in results:
            p = r["pair"]
            if p not in per_pair:
                per_pair[p] = {"total": 0, "correct_24h": 0, "correct_1h": 0, "evaluated_24h": 0, "evaluated_1h": 0}
            per_pair[p]["total"] += 1
            if r["outcomes"].get("24h"):
                per_pair[p]["evaluated_24h"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    per_pair[p]["correct_24h"] += 1
            if r["outcomes"].get("1h"):
                per_pair[p]["evaluated_1h"] += 1
                if r["outcomes"]["1h"]["correct"]:
                    per_pair[p]["correct_1h"] += 1
        for p in per_pair:
            s = per_pair[p]
            s["accuracy_24h_pct"] = round(s["correct_24h"] / s["evaluated_24h"] * 100, 1) if s["evaluated_24h"] else None
            s["accuracy_1h_pct"] = round(s["correct_1h"] / s["evaluated_1h"] * 100, 1) if s["evaluated_1h"] else None

        # 5. Overall accuracy
        overall = {"total": len(results), "correct_24h": 0, "evaluated_24h": 0, "correct_1h": 0, "evaluated_1h": 0}
        for r in results:
            if r["outcomes"].get("24h"):
                overall["evaluated_24h"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    overall["correct_24h"] += 1
            if r["outcomes"].get("1h"):
                overall["evaluated_1h"] += 1
                if r["outcomes"]["1h"]["correct"]:
                    overall["correct_1h"] += 1
        overall["accuracy_24h_pct"] = round(overall["correct_24h"] / overall["evaluated_24h"] * 100, 1) if overall["evaluated_24h"] else None
        overall["accuracy_1h_pct"] = round(overall["correct_1h"] / overall["evaluated_1h"] * 100, 1) if overall["evaluated_1h"] else None

        # 6. By signal type
        by_signal: dict[str, dict] = {}
        for r in results:
            st = r["signal_type"]
            if st not in by_signal:
                by_signal[st] = {"total": 0, "correct_24h": 0, "evaluated_24h": 0}
            by_signal[st]["total"] += 1
            if r["outcomes"].get("24h"):
                by_signal[st]["evaluated_24h"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    by_signal[st]["correct_24h"] += 1
        for st in by_signal:
            s = by_signal[st]
            s["accuracy_pct"] = round(s["correct_24h"] / s["evaluated_24h"] * 100, 1) if s["evaluated_24h"] else None

        # 7. Confidence calibration buckets (0-20%, 20-40%, …, 80-100%)
        buckets: dict[str, dict] = {}
        for r in results:
            bucket_idx = min(int(r['confidence'] * 100 // 20), 4)  # clamp to 0-4
            bucket = f"{bucket_idx * 20}-{bucket_idx * 20 + 20}%"
            if bucket not in buckets:
                buckets[bucket] = {"confidence_range": bucket, "total": 0, "correct": 0, "evaluated": 0}
            buckets[bucket]["total"] += 1
            if r["outcomes"].get("24h"):
                buckets[bucket]["evaluated"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    buckets[bucket]["correct"] += 1
        calibration = []
        for b in sorted(buckets.values(), key=lambda x: x["confidence_range"]):
            b["accuracy_pct"] = round(b["correct"] / b["evaluated"] * 100, 1) if b["evaluated"] else None
            calibration.append(b)

        # 8. Daily accuracy time-series
        from collections import defaultdict
        daily: dict[str, dict] = defaultdict(lambda: {"date": "", "total": 0, "correct": 0, "evaluated": 0})
        for r in results:
            date = r["ts"][:10]
            daily[date]["date"] = date
            daily[date]["total"] += 1
            if r["outcomes"].get("24h"):
                daily[date]["evaluated"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    daily[date]["correct"] += 1
        daily_list = []
        for d in sorted(daily.values(), key=lambda x: x["date"]):
            d["accuracy_pct"] = round(d["correct"] / d["evaluated"] * 100, 1) if d["evaluated"] else None
            daily_list.append(d)

        return {
            "predictions": results[-200:],  # last 200 for detail view
            "per_pair": per_pair,
            "overall": overall,
            "by_signal_type": by_signal,
            "confidence_calibration": calibration,
            "daily_accuracy": daily_list,
        }

    def get_pair_prediction_history(self, pair: str, days: int = 30) -> dict:
        """Return price time-series with prediction markers for a single pair.

        Used by the Prediction Overlay chart. Returns:
          - price_history: [{ts, price}] from portfolio snapshots
          - predictions: [{ts, signal_type, confidence, entry_price, suggested_tp,
                          suggested_sl, is_bullish, outcomes}]
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 1. Price history from portfolio snapshots
        snapshots = conn.execute(
            """SELECT ts, current_prices
               FROM portfolio_snapshots
               WHERE ts >= ? AND current_prices IS NOT NULL AND current_prices != '{}'
               ORDER BY ts ASC""",
            (cutoff,),
        ).fetchall()

        price_history: list[dict] = []
        pair_upper = pair.upper()
        seen_hours: set[str] = set()  # deduplicate to ~hourly
        for snap in snapshots:
            try:
                prices = json.loads(snap["current_prices"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            price = None
            for key in [pair_upper, pair_upper.replace("-", "/"), pair_upper.replace("/", "-")]:
                if key in prices and prices[key]:
                    price = float(prices[key])
                    break
            if price and price > 0:
                hour_key = snap["ts"][:13]  # YYYY-MM-DDTHH
                if hour_key not in seen_hours:
                    seen_hours.add(hour_key)
                    price_history.append({"ts": snap["ts"], "price": round(price, 8)})

        # 2. Prediction markers
        predictions_raw = conn.execute(
            """SELECT ts, signal_type, confidence, reasoning_json
               FROM agent_reasoning
               WHERE agent_name = 'market_analyst'
                 AND UPPER(pair) = ?
                 AND ts >= ?
               ORDER BY ts ASC""",
            (pair_upper, cutoff),
        ).fetchall()

        predictions: list[dict] = []
        for pred in predictions_raw:
            try:
                reasoning = json.loads(pred["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                reasoning = {}

            signal = pred["signal_type"] or "neutral"
            confidence = pred["confidence"] or 0.0

            entry_price = None
            for key in ["suggested_entry", "current_price"]:
                val = reasoning.get(key)
                if val and float(val) > 0:
                    entry_price = float(val)
                    break

            # Try to find price from snapshot if not in reasoning
            if not entry_price:
                pred_ts = pred["ts"]
                for ph in price_history:
                    if ph["ts"] >= pred_ts:
                        entry_price = ph["price"]
                        break

            if not entry_price:
                continue

            is_bullish = signal in ("strong_buy", "buy", "weak_buy")
            is_bearish = signal in ("strong_sell", "sell", "weak_sell")

            # Find outcome prices at various horizons
            def _find_price_at(target_ts: str) -> float | None:
                for ph in price_history:
                    if ph["ts"] >= target_ts:
                        return ph["price"]
                return None

            def _ts_plus_hours(ts_str: str, hours: int) -> str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    return (dt + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
                except Exception:
                    return ts_str

            pred_ts = pred["ts"]
            outcomes: dict[str, dict | None] = {}
            for label, hours in {"1h": 1, "4h": 4, "24h": 24, "7d": 168}.items():
                future_ts = _ts_plus_hours(pred_ts, hours)
                actual_price = _find_price_at(future_ts)
                if actual_price and actual_price > 0:
                    pct_change = (actual_price - entry_price) / entry_price * 100
                    price_went_up = actual_price > entry_price
                    correct = (is_bullish and price_went_up) or (is_bearish and not price_went_up)
                    outcomes[label] = {
                        "actual_price": round(actual_price, 8),
                        "pct_change": round(pct_change, 4),
                        "correct": correct,
                    }
                else:
                    outcomes[label] = None

            predictions.append({
                "ts": pred_ts,
                "signal_type": signal,
                "confidence": round(confidence, 3),
                "entry_price": round(entry_price, 8),
                "suggested_tp": reasoning.get("suggested_take_profit"),
                "suggested_sl": reasoning.get("suggested_stop_loss"),
                "is_bullish": is_bullish,
                "outcomes": outcomes,
            })

        return {
            "pair": pair_upper,
            "price_history": price_history,
            "predictions": predictions,
            "total_predictions": len(predictions),
        }

    def cleanup_bad_snapshots(self) -> int:
        """One-time cleanup: delete portfolio snapshots with anomalous values.

        Removes rows where portfolio_value is 0 or wildly inconsistent with
        recent stable values (paper-mode bleed-through from initial_balance).
        Returns the number of deleted rows.
        """
        conn = self._get_conn()

        # Find the median of the last 500 snapshots (most recent / stable)
        recent = conn.execute(
            """SELECT portfolio_value FROM portfolio_snapshots
               WHERE portfolio_value > 0
               ORDER BY ts DESC LIMIT 500"""
        ).fetchall()
        if not recent:
            return 0

        values = sorted(r["portfolio_value"] for r in recent)
        median_val = values[len(values) // 2]
        if median_val <= 0:
            return 0

        threshold = max(median_val * 10, 100)

        # Delete zero-value and anomalously high snapshots
        cursor = conn.execute(
            """DELETE FROM portfolio_snapshots
               WHERE portfolio_value = 0 OR portfolio_value > ?""",
            (threshold,),
        )
        deleted = cursor.rowcount
        conn.commit()
        logger.info(f"Cleaned up {deleted} bad portfolio snapshots (threshold={threshold:.2f})")
        return deleted

    def get_tracked_pairs(self, quote_currency: str | None = None) -> dict:
        """Return pairs the LLM system has analyzed, grouped by asset class.

        Looks at agent_reasoning entries to see what pairs were actually
        predicted on, and classifies them as crypto or equity.

        If *quote_currency* is given (e.g. "EUR"), only pairs ending in
        that currency suffix are returned.
        """
        conn = self._get_conn()

        # Get all pairs with prediction counts from last 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        if quote_currency:
            suffix = f"-{quote_currency.upper()}"
            rows = conn.execute(
                """SELECT pair, COUNT(*) as prediction_count,
                          MAX(ts) as last_predicted,
                          GROUP_CONCAT(DISTINCT signal_type) as signal_types
                   FROM agent_reasoning
                   WHERE agent_name = 'market_analyst' AND ts >= ?
                     AND UPPER(pair) LIKE ?
                   GROUP BY pair
                   ORDER BY prediction_count DESC""",
                (cutoff, f"%{suffix}"),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT pair, COUNT(*) as prediction_count,
                          MAX(ts) as last_predicted,
                          GROUP_CONCAT(DISTINCT signal_type) as signal_types
                   FROM agent_reasoning
                   WHERE agent_name = 'market_analyst' AND ts >= ?
                   GROUP BY pair
                   ORDER BY prediction_count DESC""",
                (cutoff,),
            ).fetchall()

        # Classify pairs
        crypto_suffixes = {"-USD", "-EUR", "-BTC", "-ETH", "-USDT", "-USDC", "-GBP"}
        equity_suffixes = {"-SEK", "-NOK", "-DKK"}  # Nordnet equities

        crypto_pairs = []
        equity_pairs = []
        for r in rows:
            pair = r["pair"]
            item = {
                "pair": pair,
                "prediction_count": r["prediction_count"],
                "last_predicted": r["last_predicted"],
                "signal_types": (r["signal_types"] or "").split(","),
            }
            # Classify by suffix
            is_equity = any(pair.upper().endswith(s) for s in equity_suffixes)
            if is_equity:
                equity_pairs.append(item)
            else:
                crypto_pairs.append(item)

        return {
            "crypto": crypto_pairs,
            "equity": equity_pairs,
            "total_pairs": len(rows),
        }

    # ─── Pair Follows ──────────────────────────────────────────────────────

    def get_pair_follows(self, exchange: str | None = None, quote_currency: str | None = None) -> list[dict]:
        """Get all followed pairs, optionally filtered by exchange or quote currency."""
        conn = self._get_conn()
        sql = "SELECT pair, exchange, followed_by, ts FROM pair_follows"
        conditions: list[str] = []
        params: list = []
        if exchange:
            conditions.append("exchange = ?")
            params.append(exchange)
        if quote_currency:
            conditions.append("UPPER(pair) LIKE ?")
            params.append(f"%-{quote_currency.upper()}")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY pair, followed_by"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def follow_pair(self, pair: str, followed_by: str = "human", exchange: str = "coinbase") -> bool:
        """Add a pair follow. Returns True if newly added, False if already existed."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO pair_follows (pair, exchange, followed_by)
                   VALUES (?, ?, ?)""",
                (pair.upper(), exchange, followed_by),
            )
            conn.commit()
            return conn.total_changes > 0
        except Exception:
            return False

    def unfollow_pair(self, pair: str, followed_by: str = "human") -> bool:
        """Remove a pair follow. Returns True if actually deleted."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM pair_follows WHERE pair = ? AND followed_by = ?",
            (pair.upper(), followed_by),
        )
        conn.commit()
        return cursor.rowcount > 0

    def get_followed_pairs_set(self, followed_by: str | None = None, quote_currency: str | None = None) -> set[str]:
        """Return a set of followed pair names for quick lookup."""
        conn = self._get_conn()
        sql = "SELECT DISTINCT pair FROM pair_follows"
        conditions: list[str] = []
        params: list = []
        if followed_by:
            conditions.append("followed_by = ?")
            params.append(followed_by)
        if quote_currency:
            conditions.append("UPPER(pair) LIKE ?")
            params.append(f"%-{quote_currency.upper()}")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        rows = conn.execute(sql, params).fetchall()
        return {r["pair"] for r in rows}

