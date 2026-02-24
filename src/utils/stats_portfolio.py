from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("stats")


class PortfolioMixin:
    """Portfolio-related methods extracted from StatsDB."""

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
            _qc_exchange_map = {"EUR": "coinbase", "USD": "ibkr"}
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
            _qc_exchange_map = {"EUR": "coinbase", "USD": "ibkr"}
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

    # ─── Cleanup ───────────────────────────────────────────────────────────

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
