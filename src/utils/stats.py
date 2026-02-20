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

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("stats")

DB_PATH = os.path.join("data", "stats.db")


class StatsDB:
    """Thread-safe SQLite statistics database."""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()
        logger.info(f"📊 Stats DB initialized: {db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO portfolio_snapshots
               (portfolio_value, cash_balance, return_pct, total_pnl, max_drawdown,
                open_positions, current_prices, fear_greed_value, high_stakes_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                portfolio_value, cash_balance, return_pct, total_pnl, max_drawdown,
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

    def get_portfolio_range(self, hours: int = 24) -> dict:
        """Get min/max/avg portfolio value over a period."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            """SELECT MIN(portfolio_value) as low, MAX(portfolio_value) as high,
                      AVG(portfolio_value) as avg, COUNT(*) as samples
               FROM portfolio_snapshots WHERE ts >= ?""",
            (cutoff,),
        ).fetchone()
        return dict(row) if row else {}

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
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO trades
               (pair, action, quantity, price, quote_amount, confidence,
                signal_type, stop_loss, take_profit, reasoning, pnl,
                fee_quote, is_rotation, approved_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pair, action, quantity, price, quote_amount, confidence,
                signal_type, stop_loss, take_profit, reasoning, pnl,
                fee_quote, 1 if is_rotation else 0, approved_by,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def get_trades(self, hours: int = 24, pair: Optional[str] = None, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if pair:
            rows = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? AND pair = ? ORDER BY ts DESC LIMIT ?",
                (cutoff, pair, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_stats(self, hours: int = 24) -> dict:
        """Get aggregate trade statistics."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            """SELECT
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
               FROM trades WHERE ts >= ?""",
            (cutoff,),
        ).fetchone()
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

    def get_win_loss_stats(self, hours: int = 720) -> dict:
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
        row = conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
                COALESCE(AVG(CASE WHEN pnl < 0 THEN ABS(pnl) END), 0) as avg_loss
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL AND pnl != 0""",
            (cutoff,),
        ).fetchone()
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

    def get_events(self, hours: int = 24, event_type: Optional[str] = None, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if event_type:
            rows = conn.execute(
                "SELECT * FROM events WHERE ts >= ? AND event_type = ? ORDER BY ts DESC LIMIT ?",
                (cutoff, event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (cutoff, limit),
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

    def get_daily_summaries(self, days: int = 7) -> list[dict]:
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM daily_summaries WHERE date >= ? ORDER BY date DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Analytics Queries ─────────────────────────────────────────────────

    def get_performance_summary(self, hours: int = 24) -> dict:
        """Get a comprehensive performance summary for the LLM."""
        return {
            "trade_stats": self.get_trade_stats(hours),
            "portfolio_range": self.get_portfolio_range(hours),
            "event_counts": self.get_event_counts(hours),
            "recent_trades": self.get_trades(hours, limit=10),
        }

    def get_portfolio_history(self, hours: int = 24) -> list[dict]:
        """Get portfolio value over time (for trend analysis)."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """SELECT ts, portfolio_value, return_pct, total_pnl
               FROM portfolio_snapshots WHERE ts >= ?
               ORDER BY ts""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_best_worst_trades(self, hours: int = 168) -> dict:
        """Get best and worst trades in a period."""
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        best = conn.execute(
            """SELECT pair, action, pnl, price, quote_amount, ts
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL
               ORDER BY pnl DESC LIMIT 3""",
            (cutoff,),
        ).fetchall()
        worst = conn.execute(
            """SELECT pair, action, pnl, price, quote_amount, ts
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL
               ORDER BY pnl ASC LIMIT 3""",
            (cutoff,),
        ).fetchall()
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
    ) -> int:
        """Persist a full LLM reasoning trace for one agent call."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO agent_reasoning
               (cycle_id, pair, agent_name, reasoning_json, signal_type, confidence,
                trade_id, langfuse_trace_id, langfuse_span_id,
                prompt_tokens, completion_tokens, latency_ms, raw_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cycle_id, pair, agent_name,
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
    ) -> int:
        """Record a new simulated (paper) trade and return its id."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO simulated_trades
               (pair, from_currency, from_amount, entry_price, quantity, to_currency, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pair, from_currency, from_amount, entry_price, quantity, to_currency, notes),
        )
        conn.commit()
        return cursor.lastrowid

    def get_simulated_trades(self, include_closed: bool = False) -> list[dict]:
        """Return all (open, or all including closed) simulated trades."""
        conn = self._get_conn()
        if include_closed:
            rows = conn.execute(
                "SELECT * FROM simulated_trades ORDER BY ts DESC"
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

