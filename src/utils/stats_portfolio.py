from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger
from src.utils.qc_filter import qc_where

logger = get_logger("stats")


class PortfolioMixin:
    """Portfolio-related methods extracted from StatsDB."""

    # --- Portfolio Snapshots ------------------------------------------------

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
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO portfolio_snapshots
                   (exchange, portfolio_value, cash_balance, return_pct, total_pnl, max_drawdown,
                    open_positions, current_prices, fear_greed_value, high_stakes_active)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE ts >= %s ORDER BY ts DESC LIMIT %s",
                (cutoff, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_portfolio_range(self, hours: int = 24, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """Get min/max/avg portfolio value over a period.

        Uses the same anomaly-filtering logic as get_portfolio_history to exclude
        paper-mode bleed-through values.
        """
        history = self.get_portfolio_history(hours=hours, quote_currency=quote_currency, exchange=exchange)
        if not history:
            return {"low": 0, "high": 0, "avg": 0, "samples": 0}
        values = [h["portfolio_value"] for h in history]
        return {
            "low": min(values),
            "high": max(values),
            "avg": sum(values) / len(values),
            "samples": len(values),
        }

    # --- Daily Summaries ----------------------------------------------------

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
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO daily_summaries
                   (date, opening_value, closing_value, high_value, low_value,
                    total_trades, winning_trades, losing_trades, total_pnl,
                    best_trade, worst_trade, events_count, summary_text, plan_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (date) DO UPDATE SET
                    opening_value = EXCLUDED.opening_value,
                    closing_value = EXCLUDED.closing_value,
                    high_value = EXCLUDED.high_value,
                    low_value = EXCLUDED.low_value,
                    total_trades = EXCLUDED.total_trades,
                    winning_trades = EXCLUDED.winning_trades,
                    losing_trades = EXCLUDED.losing_trades,
                    total_pnl = EXCLUDED.total_pnl,
                    best_trade = EXCLUDED.best_trade,
                    worst_trade = EXCLUDED.worst_trade,
                    events_count = EXCLUDED.events_count,
                    summary_text = EXCLUDED.summary_text,
                    plan_text = EXCLUDED.plan_text""",
                (
                    date, opening_value, closing_value, high_value, low_value,
                    total_trades, winning_trades, losing_trades, total_pnl,
                    best_trade, worst_trade, events_count, summary_text, plan_text,
                ),
            )
            conn.commit()

    def get_daily_summary(self, date: str) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM daily_summaries WHERE date = %s", (date,),
            ).fetchone()
            return dict(row) if row else None

    def get_daily_summaries(self, days: int = 7, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> list[dict]:
        """Get daily summaries.  When *exchange* is provided and the table has
        an ``exchange`` column, filter by exchange.  Falls back gracefully when
        the column doesn't exist (legacy DBs)."""
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            if exchange:
                try:
                    rows = conn.execute(
                        "SELECT * FROM daily_summaries WHERE date >= %s AND exchange = %s ORDER BY date DESC",
                        (cutoff, exchange),
                    ).fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    conn.rollback()  # Reset aborted transaction before next query
                    # MED-3: log so misconfigured/legacy DBs are visible in logs.
                    from src.utils.logger import get_logger as _get_logger
                    _get_logger("stats.portfolio").warning(
                        "get_daily_summaries: exchange filter failed (column may not exist) — "
                        "returning unfiltered results"
                    )
            rows = conn.execute(
                "SELECT * FROM daily_summaries WHERE date >= %s ORDER BY date DESC",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Analytics Queries --------------------------------------------------

    def get_performance_summary(self, hours: int = 24, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """Get a comprehensive performance summary for the LLM."""
        return {
            "trade_stats": self.get_trade_stats(hours, quote_currency=quote_currency, exchange=exchange),
            "portfolio_range": self.get_portfolio_range(hours, quote_currency=quote_currency, exchange=exchange),
            "event_counts": self.get_event_counts(hours),
            "recent_trades": self.get_trades(hours, limit=10, quote_currency=quote_currency, exchange=exchange),
        }

    def get_portfolio_history(self, hours: int = 24, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> list[dict]:
        """Get portfolio value over time (for trend analysis).

        Downsamples at the DB level based on the time range to keep response
        sizes reasonable regardless of how many snapshots exist:
          ≤24h  → 5-minute buckets  (~288 points max)
          ≤168h → 1-hour buckets    (~168 points max)
          ≤720h → 6-hour buckets    (~120 points max)
          >720h → 24-hour buckets   (~365 points max)

        Filters out anomalous snapshots (portfolio_value == 0 or outliers).
        When *exchange* is given, only snapshots for that exchange are returned.
        """
        if hours <= 24:
            bucket = '5 minutes'
        elif hours <= 168:
            bucket = '1 hour'
        elif hours <= 720:
            bucket = '6 hours'
        else:
            bucket = '1 day'

        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []

            rows = conn.execute(
                f"""SELECT
                        MIN(ts) AS ts,
                        AVG(portfolio_value) AS portfolio_value,
                        AVG(return_pct) AS return_pct,
                        AVG(total_pnl) AS total_pnl
                    FROM portfolio_snapshots
                    WHERE ts >= %s AND portfolio_value > 0{exch_frag}
                    GROUP BY date_bin(%s::interval, ts::timestamptz, TIMESTAMPTZ '2000-01-01')
                    ORDER BY MIN(ts)""",
                (cutoff, *exch_params, bucket),
            ).fetchall()

            if not rows:
                return []

            # Remove anomalous portfolio values (first-boot / paper-mode bleed-through).
            # Uses median ± 20× to handle bimodal distributions (e.g. paper 10 000 mixed
            # with live 10) where the classic IQR fence would pass all outliers.
            values = sorted(r["portfolio_value"] for r in rows)
            n = len(values)
            if n >= 4:
                median = values[n // 2]
                if median > 0:
                    lower_fence = median / 20
                    upper_fence = median * 20
                    rows = [r for r in rows if lower_fence <= r["portfolio_value"] <= upper_fence]

            return [dict(r) for r in rows]

    def get_best_worst_trades(self, hours: int = 168, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """Get best and worst trades in a period."""
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            base_best = """SELECT pair, action, pnl, price, quote_amount, ts
                   FROM trades WHERE ts >= %s AND pnl IS NOT NULL"""
            base_worst = base_best  # same WHERE clause
            qc_frag, qc_params = qc_where(quote_currency)
            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            best = conn.execute(base_best + qc_frag + exch_frag + " ORDER BY pnl DESC LIMIT 3", (cutoff, *qc_params, *exch_params)).fetchall()
            worst = conn.execute(base_worst + qc_frag + exch_frag + " ORDER BY pnl ASC LIMIT 3", (cutoff, *qc_params, *exch_params)).fetchall()
            return {
                "best": [dict(r) for r in best],
                "worst": [dict(r) for r in worst],
            }

    # --- Cleanup ------------------------------------------------------------

    def cleanup_bad_snapshots(self) -> int:
        """One-time cleanup: delete portfolio snapshots with anomalous values.

        Removes rows where portfolio_value is 0 or wildly inconsistent with
        recent stable values (paper-mode bleed-through from initial_balance).
        Returns the number of deleted rows.
        """
        with self._get_conn() as conn:
            # Find the median of the last 500 snapshots (most recent / stable)
            recent = conn.execute(
                """SELECT portfolio_value FROM portfolio_snapshots
                   WHERE portfolio_value > 0
                   ORDER BY ts DESC LIMIT 500"""
            ).fetchall()
            if not recent:
                return 0

            values = sorted(r["portfolio_value"] for r in recent)
            n = len(values)
            if n < 10:
                return 0

            q1 = values[n // 4]
            q3 = values[(3 * n) // 4]
            iqr = q3 - q1
            median_val = values[n // 2]
            min_range = median_val * 0.5 if median_val > 0 else 1.0
            effective_iqr = max(iqr, min_range)
            threshold = q3 + 3 * effective_iqr

            # Delete zero-value and anomalously high snapshots
            cursor = conn.execute(
                """DELETE FROM portfolio_snapshots
                   WHERE portfolio_value = 0 OR portfolio_value > %s""",
                (threshold,),
            )
            deleted = cursor.rowcount
            conn.commit()
            logger.info(f"Cleaned up {deleted} bad portfolio snapshots (threshold={threshold:.2f})")
            return deleted
