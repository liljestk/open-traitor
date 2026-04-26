"""
Recommendations Mixin — Backtest-derived parameter change suggestions.

Each row is a single pending change (e.g. "add pair X to active rotation",
"adjust stop_loss_pct from 0.04 to 0.03 on pair Y"). Status transitions:

    pending  ── operator approves ──>  approved
    pending  ── operator rejects  ──>  rejected
    pending  ── 14 days no action ──>  expired (set lazily on read)

The trading loop never *applies* recommendations automatically; the
``approved`` flag is purely a signal for downstream consumers (dashboard,
deployment scripts) that the operator endorses the change.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2

from src.utils.logger import get_logger

logger = get_logger("utils.stats_recommendations")

ALLOWED_STATUS: frozenset[str] = frozenset({"pending", "approved", "rejected", "expired"})
DEFAULT_EXPIRY_DAYS: int = 14


class RecommendationsMixin:
    """Mixin owning the ``backtest_recommendations`` table."""

    _RECOMMENDATIONS_DDL: tuple[str, ...] = (
        """
        CREATE TABLE IF NOT EXISTS backtest_recommendations (
            id              BIGSERIAL PRIMARY KEY,
            exchange        TEXT NOT NULL,
            kind            TEXT NOT NULL,
            symbol          TEXT NOT NULL DEFAULT '',
            summary         TEXT NOT NULL,
            rationale       TEXT NOT NULL DEFAULT '',
            payload_json    TEXT NOT NULL DEFAULT '{}',
            metric_name     TEXT NOT NULL DEFAULT '',
            metric_value    DOUBLE PRECISION,
            status          TEXT NOT NULL DEFAULT 'pending',
            decided_by      TEXT NOT NULL DEFAULT '',
            decided_at      TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at      TIMESTAMPTZ NOT NULL
                            DEFAULT (now() + INTERVAL '14 days'),
            source          TEXT NOT NULL DEFAULT 'nightly_backtest',
            UNIQUE (exchange, kind, symbol, metric_name, source)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_br_exchange_status "
        "ON backtest_recommendations(exchange, status)",
        "CREATE INDEX IF NOT EXISTS idx_br_exchange_created "
        "ON backtest_recommendations(exchange, created_at DESC)",
    )

    def _init_recommendations_schema(self) -> None:
        with self._get_conn() as conn:
            for stmt in self._RECOMMENDATIONS_DDL:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT rec_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT rec_ddl_sp")
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT rec_ddl_sp")
                        logger.warning(f"Recommendations DDL skipped: {e}")
            conn.commit()

    # ─── Writes ────────────────────────────────────────────────────────────

    def upsert_recommendation(
        self,
        exchange: str,
        kind: str,
        summary: str,
        symbol: str = "",
        rationale: str = "",
        payload: Optional[dict] = None,
        metric_name: str = "",
        metric_value: Optional[float] = None,
        source: str = "nightly_backtest",
        expires_in_days: int = DEFAULT_EXPIRY_DAYS,
    ) -> dict[str, Any]:
        """Create or refresh a recommendation.

        Conflict key: (exchange, kind, symbol, metric_name, source). On
        conflict the row is refreshed with the latest summary/payload/metric
        and the expiry is extended; the status is reset to ``pending`` only
        when the previous status was ``expired``.
        """
        if not exchange or not kind or not summary:
            raise ValueError("exchange, kind, and summary are required")
        payload_str = json.dumps(payload or {}, default=str)
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(expires_in_days))
        with self._get_conn() as conn:
            row = conn.execute(
                """
                INSERT INTO backtest_recommendations
                    (exchange, kind, symbol, summary, rationale, payload_json,
                     metric_name, metric_value, source, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (exchange, kind, symbol, metric_name, source)
                DO UPDATE SET
                    summary      = EXCLUDED.summary,
                    rationale    = EXCLUDED.rationale,
                    payload_json = EXCLUDED.payload_json,
                    metric_value = EXCLUDED.metric_value,
                    expires_at   = EXCLUDED.expires_at,
                    status       = CASE
                        WHEN backtest_recommendations.status = 'expired'
                        THEN 'pending'
                        ELSE backtest_recommendations.status
                    END,
                    created_at   = CASE
                        WHEN backtest_recommendations.status = 'expired'
                        THEN now()
                        ELSE backtest_recommendations.created_at
                    END
                RETURNING id, exchange, kind, symbol, summary, rationale,
                          payload_json, metric_name, metric_value, status,
                          decided_by, decided_at, created_at, expires_at, source
                """,
                (
                    exchange, kind, symbol or "", summary, rationale or "",
                    payload_str, metric_name or "", metric_value,
                    source or "nightly_backtest", expires_at,
                ),
            ).fetchone()
            conn.commit()
        return _row_to_dict(row)

    def decide_recommendation(
        self, rec_id: int, status: str, decided_by: str = ""
    ) -> Optional[dict]:
        """Set ``status`` to ``approved`` or ``rejected``. Returns the row."""
        status_norm = (status or "").strip().lower()
        if status_norm not in {"approved", "rejected"}:
            raise ValueError("status must be 'approved' or 'rejected'")
        try:
            rid = int(rec_id)
        except (TypeError, ValueError) as e:
            raise ValueError(f"rec_id must be an integer: {rec_id!r}") from e
        with self._get_conn() as conn:
            row = conn.execute(
                """
                UPDATE backtest_recommendations
                   SET status = %s,
                       decided_by = %s,
                       decided_at = now()
                 WHERE id = %s
                RETURNING id, exchange, kind, symbol, summary, rationale,
                          payload_json, metric_name, metric_value, status,
                          decided_by, decided_at, created_at, expires_at, source
                """,
                (status_norm, decided_by or "", rid),
            ).fetchone()
            conn.commit()
        return _row_to_dict(row) if row else None

    def expire_old_recommendations(self, exchange: str) -> int:
        """Lazily flip ``pending`` rows past ``expires_at`` to ``expired``.

        Returns the number of rows that flipped. Cheap; safe to call before
        every read.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                """
                WITH flipped AS (
                    UPDATE backtest_recommendations
                       SET status = 'expired'
                     WHERE exchange = %s
                       AND status = 'pending'
                       AND expires_at <= now()
                    RETURNING id
                )
                SELECT COUNT(*) AS n FROM flipped
                """,
                (exchange,),
            ).fetchone()
            conn.commit()
        return int((row or {}).get("n", 0) or 0)

    # ─── Reads ─────────────────────────────────────────────────────────────

    def list_recommendations(
        self,
        exchange: str,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """List recommendations, newest first."""
        # Auto-expire before listing so the operator never sees stale rows.
        try:
            self.expire_old_recommendations(exchange)
        except Exception as _e:  # pragma: no cover — best-effort
            logger.debug(f"expire_old_recommendations failed: {_e}")

        sql = (
            "SELECT id, exchange, kind, symbol, summary, rationale, "
            "       payload_json, metric_name, metric_value, status, "
            "       decided_by, decided_at, created_at, expires_at, source "
            "FROM backtest_recommendations WHERE exchange = %s"
        )
        params: list = [exchange]
        if status:
            status_norm = status.strip().lower()
            if status_norm not in ALLOWED_STATUS:
                raise ValueError(f"status must be one of {sorted(ALLOWED_STATUS)}")
            sql += " AND status = %s"
            params.append(status_norm)
        if kind:
            sql += " AND kind = %s"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(1, min(int(limit), 500)))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def count_recommendations_by_status(self, exchange: str) -> dict[str, int]:
        try:
            self.expire_old_recommendations(exchange)
        except Exception:
            pass
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM backtest_recommendations "
                "WHERE exchange = %s GROUP BY status",
                (exchange,),
            ).fetchall()
        out: dict[str, int] = {s: 0 for s in ALLOWED_STATUS}
        total = 0
        for r in rows:
            n = int(r["n"] or 0)
            out[str(r["status"])] = n
            total += n
        out["total"] = total
        return out

    def get_recommendation(self, rec_id: int) -> Optional[dict]:
        try:
            rid = int(rec_id)
        except (TypeError, ValueError):
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, exchange, kind, symbol, summary, rationale, "
                "       payload_json, metric_name, metric_value, status, "
                "       decided_by, decided_at, created_at, expires_at, source "
                "FROM backtest_recommendations WHERE id = %s",
                (rid,),
            ).fetchone()
        return _row_to_dict(row) if row else None


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    raw = d.get("payload_json") or "{}"
    try:
        d["payload"] = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        d["payload"] = {}
    # Don't return both forms (frontend expects ``payload``)
    d.pop("payload_json", None)
    return d
