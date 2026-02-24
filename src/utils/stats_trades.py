from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional


class TradesMixin:
    """Mixin providing trade, event, and scheduled-report persistence."""

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
