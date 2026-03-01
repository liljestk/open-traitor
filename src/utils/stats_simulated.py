from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from src.utils.qc_filter import qc_where


class SimulatedMixin:
    """Mixin for simulated-trade, scan-result and pair-follow helpers."""

    # --- Simulated Trades ---------------------------------------------------

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
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO simulated_trades
                   (exchange, pair, from_currency, from_amount, entry_price, quantity, to_currency, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (exchange, pair, from_currency, from_amount, entry_price, quantity, to_currency, notes),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"]

    def get_simulated_trades(self, include_closed: bool = False, quote_currency: str | list[str] | None = None) -> list[dict]:
        """Return all (open, or all including closed) simulated trades."""
        with self._get_conn() as conn:
            qc_frag, qc_params = qc_where(quote_currency)
            if include_closed:
                rows = conn.execute(
                    "SELECT * FROM simulated_trades WHERE 1=1" + qc_frag + " ORDER BY ts DESC",
                    qc_params,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM simulated_trades WHERE status = 'open'" + qc_frag + " ORDER BY ts DESC",
                    qc_params,
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
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM simulated_trades WHERE id = %s AND status = 'open'",
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
                   SET status='closed', closed_at=%s, close_price=%s,
                       close_pnl_abs=%s, close_pnl_pct=%s
                   WHERE id=%s""",
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

    # --- Universe Scan Results ----------------------------------------------

    def save_scan_results(
        self,
        universe_size: int,
        scanned_pairs: int,
        results_json: dict,
        top_movers: list[dict] | None = None,
        summary_text: str = "",
    ) -> int:
        """Persist a universe scan snapshot (technicals per pair)."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO scan_results
                   (universe_size, scanned_pairs, results_json, top_movers, summary_text)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    universe_size,
                    scanned_pairs,
                    json.dumps(results_json, default=str),
                    json.dumps(top_movers or [], default=str),
                    summary_text,
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"]

    def get_latest_scan_results(self) -> Optional[dict]:
        """Get the most recent universe scan results."""
        with self._get_conn() as conn:
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
        if not isinstance(result["top_movers"], list):
            result["top_movers"] = []
        return result

    # --- Pair Follows -------------------------------------------------------

    def get_pair_follows(self, exchange: str | None = None, quote_currency: str | list[str] | None = None) -> list[dict]:
        """Get all followed pairs, optionally filtered by exchange or quote currency."""
        with self._get_conn() as conn:
            sql = "SELECT pair, exchange, followed_by, ts FROM pair_follows WHERE 1=1"
            params: list = []
            if exchange:
                sql += " AND exchange = %s"
                params.append(exchange)
            qc_frag, qc_params = qc_where(quote_currency)
            sql += qc_frag
            params.extend(qc_params)
            sql += " ORDER BY pair, followed_by"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def follow_pair(self, pair: str, followed_by: str = "human", exchange: str = "coinbase") -> bool:
        """Add a pair follow. Returns True if newly added, False if already existed."""
        with self._get_conn() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO pair_follows (pair, exchange, followed_by)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (pair, followed_by) DO NOTHING""",
                    (pair.upper(), exchange, followed_by),
                )
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                return False

    def unfollow_pair(self, pair: str, followed_by: str = "human") -> bool:
        """Remove a pair follow. Returns True if actually deleted."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM pair_follows WHERE pair = %s AND followed_by = %s",
                (pair.upper(), followed_by),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_followed_pairs_set(self, followed_by: str | None = None, quote_currency: str | list[str] | None = None) -> set[str]:
        """Return a set of followed pair names for quick lookup."""
        with self._get_conn() as conn:
            sql = "SELECT DISTINCT pair FROM pair_follows WHERE 1=1"
            params: list = []
            if followed_by:
                sql += " AND followed_by = %s"
                params.append(followed_by)
            qc_frag, qc_params = qc_where(quote_currency)
            sql += qc_frag
            params.extend(qc_params)
            rows = conn.execute(sql, params).fetchall()
            return {r["pair"] for r in rows}
