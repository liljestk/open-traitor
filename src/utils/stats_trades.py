from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.qc_filter import qc_where


class TradesMixin:
    """Mixin providing trade, event, and scheduled-report persistence."""

    # --- Trades -------------------------------------------------------------

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
        entry_score: Optional[float] = None,
    ) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO trades
                   (exchange, pair, action, quantity, price, quote_amount, confidence,
                    signal_type, stop_loss, take_profit, reasoning, pnl,
                    fee_quote, is_rotation, approved_by, entry_score)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    exchange, pair, action, quantity, price, quote_amount, confidence,
                    signal_type, stop_loss, take_profit, reasoning, pnl,
                    fee_quote, 1 if is_rotation else 0, approved_by, entry_score,
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"]

    def record_synced_trade(
        self,
        *,
        ts: str,
        external_id: str,
        pair: str,
        action: str,
        price: float,
        quantity: float = 0,
        quote_amount: float = 0,
        fee_quote: float = 0,
        signal_type: str = "exchange_sync",
        approved_by: str = "exchange",
        exchange: str = "coinbase",
    ) -> int | None:
        """Insert a trade from an external exchange sync, skipping duplicates via external_id."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO trades
                   (ts, exchange, pair, action, quantity, price, quote_amount,
                    confidence, signal_type, stop_loss, take_profit, reasoning,
                    pnl, fee_quote, is_rotation, approved_by, external_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, 0, 0, '', NULL, %s, 0, %s, %s)
                   ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO NOTHING
                   RETURNING id""",
                (ts, exchange, pair, action, quantity, price, quote_amount,
                 signal_type, fee_quote, approved_by, external_id),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"] if row else None

    def get_trades(self, hours: int = 24, pair: Optional[str] = None, limit: int = 50, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> list[dict]:
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            # LEFT JOIN agent_reasoning to fetch the cycle_id that produced each trade
            _select = """SELECT t.*,
                                (SELECT ar.cycle_id FROM agent_reasoning ar
                                 WHERE ar.trade_id = t.id LIMIT 1) AS cycle_id
                         FROM trades t"""
            if pair:
                rows = conn.execute(
                    _select + " WHERE t.ts >= %s AND t.pair = %s ORDER BY t.ts DESC LIMIT %s",
                    (cutoff, pair, limit),
                ).fetchall()
            else:
                qc_frag, qc_params = qc_where(quote_currency, col="t.pair")
                exch_frag = " AND t.exchange = %s" if exchange else ""
                exch_params = [exchange] if exchange else []
                rows = conn.execute(
                    _select + " WHERE t.ts >= %s" + qc_frag + exch_frag + " ORDER BY t.ts DESC LIMIT %s",
                    (cutoff, *qc_params, *exch_params, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_trade_stats(self, hours: int = 24, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """Get aggregate trade statistics."""
        with self._get_conn() as conn:
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
                   FROM trades WHERE ts >= %s"""
            qc_frag, qc_params = qc_where(quote_currency)
            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            row = conn.execute(base_sql + qc_frag + exch_frag, (cutoff, *qc_params, *exch_params)).fetchone()
            return dict(row) if row else {}

    def get_pair_stats(self, pair: str, hours: int = 168) -> dict:
        """Get stats for a specific trading pair (default 7 days)."""
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            row = conn.execute(
                """SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN action='buy' THEN 1 ELSE 0 END) as buys,
                    SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END) as sells,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(confidence), 0) as avg_confidence,
                    COALESCE(SUM(quote_amount), 0) as total_volume
                   FROM trades WHERE ts >= %s AND pair = %s""",
                (cutoff, pair),
            ).fetchone()
            return dict(row) if row else {}

    def get_win_loss_stats(self, hours: int = 720, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """
        Get win rate and average win/loss for Kelly Criterion position sizing.
        Default look-back: 30 days (720 hours).

        Returns:
            win_rate: fraction of profitable trades (0-1)
            avg_win: average profit on winning trades (absolute)
            avg_loss: average loss on losing trades (absolute, positive number)
            sample_size: number of closed trades used
        """
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            base_sql = """SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win,
                    COALESCE(AVG(CASE WHEN pnl < 0 THEN ABS(pnl) END), 0) as avg_loss
                   FROM trades WHERE ts >= %s AND pnl IS NOT NULL AND pnl != 0"""
            qc_frag, qc_params = qc_where(quote_currency)
            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            row = conn.execute(base_sql + qc_frag + exch_frag, (cutoff, *qc_params, *exch_params)).fetchone()
            if not row or row["total"] == 0:
                return {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0}
            return {
                "win_rate": row["wins"] / row["total"],
                "avg_win": row["avg_win"],
                "avg_loss": row["avg_loss"],
                "sample_size": row["total"],
            }

    def get_backtest_kelly_stats(self, pair: str, exchange: str | None = None) -> dict:
        """Get win rate and avg win/loss from the most recent backtest run for a pair.

        Used to supplement live Kelly stats when the live sample size is too
        small (< 20 trades).  Returns the same dict shape as get_win_loss_stats.
        """
        with self._get_conn() as conn:
            # Check table exists
            exists = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backtest_runs')"
            ).fetchone()
            if not exists or not exists[0]:
                return {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0, "source": "backtest"}

            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            row = conn.execute(
                f"""SELECT result_json, win_rate, total_trades
                    FROM backtest_runs
                    WHERE pair = %s AND total_trades > 0{exch_frag}
                    ORDER BY run_ts DESC LIMIT 1""",
                (pair, *exch_params),
            ).fetchone()
            if not row or not row["total_trades"]:
                return {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0, "source": "backtest"}

            # Parse avg_win/avg_loss from result_json
            import json
            try:
                result = json.loads(row["result_json"]) if row["result_json"] else {}
            except (json.JSONDecodeError, TypeError):
                result = {}

            return {
                "win_rate": (row["win_rate"] or 0) / 100.0,  # stored as percentage
                "avg_win": result.get("avg_win", 0),
                "avg_loss": abs(result.get("avg_loss", 0)),
                "sample_size": row["total_trades"],
                "source": "backtest",
            }

    def update_trade_pnl(self, trade_id: int, pnl: float, fee_quote: float | None = None) -> None:
        """Back-fill PNL (and optionally fee) for a trade recorded without it.

        Called after FIFO cost-basis calculation so that analytics queries
        (which filter WHERE pnl IS NOT NULL) can include the trade.
        """
        with self._get_conn() as conn:
            if fee_quote is not None:
                conn.execute(
                    "UPDATE trades SET pnl = %s, fee_quote = %s WHERE id = %s",
                    (pnl, fee_quote, trade_id),
                )
            else:
                conn.execute("UPDATE trades SET pnl = %s WHERE id = %s", (pnl, trade_id))
            conn.commit()

    # --- Events -------------------------------------------------------------

    def record_event(
        self,
        event_type: str,
        message: str,
        severity: str = "info",
        pair: Optional[str] = None,
        data: Optional[dict] = None,
        exchange: str = "coinbase",
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO events (exchange, event_type, severity, pair, message, data)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (exchange, event_type, severity, pair, message, json.dumps(data or {}, default=str)),
            )
            conn.commit()

    def get_events(self, hours: int = 24, event_type: Optional[str] = None, limit: int = 50, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> list[dict]:
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            qc_frag, qc_params = qc_where(quote_currency)
            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM events WHERE ts >= %s AND event_type = %s" + qc_frag + exch_frag + " ORDER BY ts DESC LIMIT %s",
                    (cutoff, event_type, *qc_params, *exch_params, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE ts >= %s" + qc_frag + exch_frag + " ORDER BY ts DESC LIMIT %s",
                    (cutoff, *qc_params, *exch_params, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_event_counts(self, hours: int = 24) -> dict:
        """Get event counts by type."""
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                """SELECT event_type, COUNT(*) as count, severity
                   FROM events WHERE ts >= %s
                   GROUP BY event_type, severity ORDER BY count DESC""",
                (cutoff,),
            ).fetchall()
            return {f"{r['event_type']}({r['severity']})": r["count"] for r in rows}

    # --- Scheduled Reports --------------------------------------------------

    def add_scheduled_report(
        self,
        name: str,
        description: str,
        cron_expression: str,
        query_type: str,
        query_params: Optional[dict] = None,
    ) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO scheduled_reports
                   (name, description, cron_expression, query_type, query_params)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""",
                (name, description, cron_expression, query_type,
                 json.dumps(query_params or {})),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"]

    def get_active_schedules(self) -> list[dict]:
        with self._get_conn() as conn:
            # LOW-10: column is INTEGER 0/1; compare with != 0 which works
            # regardless of whether the column is ever migrated to BOOLEAN.
            rows = conn.execute(
                "SELECT * FROM scheduled_reports WHERE is_active != 0 ORDER BY id",
            ).fetchall()
            return [dict(r) for r in rows]

    def update_schedule_last_run(self, schedule_id: int) -> None:
        with self._get_conn() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE scheduled_reports SET last_run_ts = %s WHERE id = %s",
                (now, schedule_id),
            )
            conn.commit()

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM scheduled_reports WHERE id = %s", (schedule_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
