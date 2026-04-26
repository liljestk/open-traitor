"""
Labels Mixin — Human-in-the-loop trade feedback.

Stores operator-supplied labels on closed trades (``win``/``loss``/``skip``)
plus an optional free-text note. Used by the fine-tuning pipeline to
upweight high-confidence training examples and by the dashboard to surface
operator-reviewed performance.

Schema is intentionally narrow: one row per (exchange, trade_id) with
last-write-wins semantics.

Public surface:
    - add_trade_label(trade_id, label, exchange, ...)
    - delete_trade_label(trade_id, exchange)
    - get_trade_label(trade_id, exchange) -> dict | None
    - get_recent_unlabeled_trades(exchange, limit=10) -> list[dict]
    - get_trade_labels(exchange, label=None, since_iso=None, limit=100) -> list[dict]
    - count_trade_labels(exchange) -> dict[str, int]

Authorized labels: ``win``, ``loss``, ``skip``, ``unsure``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2

from src.utils.logger import get_logger

logger = get_logger("utils.stats_labels")

ALLOWED_LABELS: frozenset[str] = frozenset({"win", "loss", "skip", "unsure"})


class LabelsMixin:
    """Mixin owning the ``trade_labels`` table.

    Labels are *advisory* — the trading loop never reads them at decision
    time. They feed the fine-tuning pipeline and the dashboard.
    """

    _LABELS_DDL_STATEMENTS: tuple[str, ...] = (
        """
        CREATE TABLE IF NOT EXISTS trade_labels (
            id            BIGSERIAL PRIMARY KEY,
            exchange      TEXT NOT NULL,
            trade_id      BIGINT NOT NULL,
            label         TEXT NOT NULL,
            note          TEXT NOT NULL DEFAULT '',
            user_id       TEXT NOT NULL DEFAULT '',
            source        TEXT NOT NULL DEFAULT 'telegram',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (exchange, trade_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tl_exchange_label "
        "ON trade_labels(exchange, label)",
        "CREATE INDEX IF NOT EXISTS idx_tl_exchange_created "
        "ON trade_labels(exchange, created_at DESC)",
    )

    def _init_labels_schema(self) -> None:
        """Create ``trade_labels`` table. Safe to call at every startup."""
        with self._get_conn() as conn:
            for stmt in self._LABELS_DDL_STATEMENTS:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT labels_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT labels_ddl_sp")
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT labels_ddl_sp")
                        logger.warning(f"Labels DDL skipped: {e}")
            conn.commit()

    # ─── Writes ────────────────────────────────────────────────────────────

    def add_trade_label(
        self,
        trade_id: int,
        label: str,
        exchange: str,
        note: str = "",
        user_id: str = "",
        source: str = "telegram",
    ) -> dict[str, Any]:
        """Upsert a label. Last write wins per (exchange, trade_id).

        Returns the persisted row as a dict. Raises ``ValueError`` if the
        label is not in :data:`ALLOWED_LABELS` or if the trade does not
        exist for the given exchange.
        """
        label_norm = (label or "").strip().lower()
        if label_norm not in ALLOWED_LABELS:
            raise ValueError(
                f"label must be one of {sorted(ALLOWED_LABELS)}, got {label!r}"
            )
        if not exchange:
            raise ValueError("exchange is required")
        try:
            tid = int(trade_id)
        except (TypeError, ValueError) as e:
            raise ValueError(f"trade_id must be an integer, got {trade_id!r}") from e

        with self._get_conn() as conn:
            # Verify the trade exists for this exchange (prevents typos
            # silently labelling a stranger's trade in another profile).
            row = conn.execute(
                "SELECT id FROM trades WHERE id = %s AND (exchange = %s OR exchange = %s)",
                (tid, exchange, f"{exchange}_paper"),
            ).fetchone()
            if not row:
                raise ValueError(
                    f"trade_id={tid} not found for exchange={exchange!r}"
                )
            inserted = conn.execute(
                """
                INSERT INTO trade_labels
                    (exchange, trade_id, label, note, user_id, source, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (exchange, trade_id) DO UPDATE SET
                    label      = EXCLUDED.label,
                    note       = EXCLUDED.note,
                    user_id    = EXCLUDED.user_id,
                    source     = EXCLUDED.source,
                    updated_at = now()
                RETURNING id, exchange, trade_id, label, note, user_id, source,
                          created_at, updated_at
                """,
                (exchange, tid, label_norm, note or "", user_id or "", source or "telegram"),
            ).fetchone()
            conn.commit()
        return dict(inserted) if inserted else {}

    def delete_trade_label(self, trade_id: int, exchange: str) -> bool:
        """Remove a label. Returns ``True`` when a row was deleted."""
        try:
            tid = int(trade_id)
        except (TypeError, ValueError):
            return False
        with self._get_conn() as conn:
            row = conn.execute(
                "DELETE FROM trade_labels WHERE exchange = %s AND trade_id = %s "
                "RETURNING id",
                (exchange, tid),
            ).fetchone()
            conn.commit()
        return bool(row)

    # ─── Reads ─────────────────────────────────────────────────────────────

    def get_trade_label(self, trade_id: int, exchange: str) -> Optional[dict]:
        try:
            tid = int(trade_id)
        except (TypeError, ValueError):
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, exchange, trade_id, label, note, user_id, source, "
                "       created_at, updated_at "
                "FROM trade_labels WHERE exchange = %s AND trade_id = %s",
                (exchange, tid),
            ).fetchone()
        return dict(row) if row else None

    def get_recent_unlabeled_trades(
        self, exchange: str, limit: int = 10, hours: int = 720
    ) -> list[dict]:
        """Return up to ``limit`` recent closed trades that have no label.

        A closed trade has a non-NULL ``pnl``. Unlabeled = no row in
        ``trade_labels`` for the same (exchange, trade_id).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT t.id, t.ts, t.pair, t.action, t.price, t.quantity,
                       t.pnl, t.confidence, t.signal_type
                FROM trades t
                LEFT JOIN trade_labels tl
                    ON tl.exchange = t.exchange AND tl.trade_id = t.id
                WHERE (t.exchange = %s OR t.exchange = %s)
                  AND t.ts >= %s
                  AND t.pnl IS NOT NULL
                  AND tl.id IS NULL
                ORDER BY t.ts DESC
                LIMIT %s
                """,
                (exchange, f"{exchange}_paper", cutoff, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_labels(
        self,
        exchange: str,
        label: Optional[str] = None,
        since_iso: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """List labels. Optional filter by ``label`` and ``since_iso`` window."""
        sql = (
            "SELECT tl.id, tl.exchange, tl.trade_id, tl.label, tl.note, "
            "       tl.user_id, tl.source, tl.created_at, tl.updated_at, "
            "       t.pair, t.action, t.pnl, t.confidence, t.signal_type "
            "FROM trade_labels tl "
            "LEFT JOIN trades t "
            "    ON t.id = tl.trade_id "
            "   AND (t.exchange = tl.exchange OR t.exchange = tl.exchange || '_paper') "
            "WHERE tl.exchange = %s"
        )
        params: list = [exchange]
        if label:
            label_norm = label.strip().lower()
            if label_norm not in ALLOWED_LABELS:
                raise ValueError(f"label filter must be one of {sorted(ALLOWED_LABELS)}")
            sql += " AND tl.label = %s"
            params.append(label_norm)
        if since_iso:
            sql += " AND tl.created_at >= %s"
            params.append(since_iso)
        sql += " ORDER BY tl.created_at DESC LIMIT %s"
        params.append(max(1, min(int(limit), 1000)))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def count_trade_labels(self, exchange: str) -> dict[str, int]:
        """Return ``{label: count, total: N}`` for an exchange."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT label, COUNT(*) AS n FROM trade_labels "
                "WHERE exchange = %s GROUP BY label",
                (exchange,),
            ).fetchall()
        out: dict[str, int] = {lbl: 0 for lbl in ALLOWED_LABELS}
        total = 0
        for r in rows:
            n = int(r["n"] or 0)
            out[str(r["label"])] = n
            total += n
        out["total"] = total
        return out
