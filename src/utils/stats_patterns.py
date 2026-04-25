"""
Pattern-engine persistence mixin for StatsDB.

Owns four tables introduced by the Catalyst Pattern Engine:

* ``historical_candles``     — long-history OHLCV per (exchange, symbol, granularity).
* ``catalyst_events``        — generic event calendar (earnings/launches/halvings/listings).
* ``pattern_fingerprints``   — pgvector embeddings of price windows around past events,
  with labelled forward returns at 1d / 5d / 20d horizons.
* ``backfill_progress``      — per-(exchange, symbol, granularity) high-water marks for
  resumable bulk historical backfill.

All read methods enforce profile/domain isolation by requiring an ``exchange``
filter (no cross-domain reads). All write methods accept exchange and refuse
to interleave domains via UPSERT (PK includes exchange).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

import psycopg2
import psycopg2.extras

from src.utils.logger import get_logger

logger = get_logger("stats.patterns")


# Allowed granularities (kept narrow on purpose so tooling stays predictable).
ALLOWED_GRANULARITIES: frozenset[str] = frozenset({
    "ONE_MINUTE",
    "FIVE_MINUTE",
    "FIFTEEN_MINUTE",
    "ONE_HOUR",
    "SIX_HOUR",
    "ONE_DAY",
})

# Embedding dimension for pattern fingerprints (see src/analysis/pattern_engine.py).
PATTERN_VECTOR_DIM: int = 64


class PatternsMixin:
    """Mixin owning the catalyst-pattern persistence layer."""

    # ─── DDL ────────────────────────────────────────────────────────────────

    _PATTERN_DDL_STATEMENTS: tuple[str, ...] = (
        # pgvector extension is required for similarity search. Created at the
        # database level; idempotent. If the role lacks CREATE EXTENSION rights
        # (e.g. non-superuser in managed Postgres), the operator must run it
        # once manually — _init_pattern_schema downgrades the failure to a log
        # warning rather than crashing the trading loop.
        "CREATE EXTENSION IF NOT EXISTS vector",
        # Long-history OHLCV. PK includes exchange so domain rows never collide.
        """
        CREATE TABLE IF NOT EXISTS historical_candles (
            exchange     TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            granularity  TEXT NOT NULL,
            ts           TIMESTAMPTZ NOT NULL,
            o            DOUBLE PRECISION NOT NULL,
            h            DOUBLE PRECISION NOT NULL,
            l            DOUBLE PRECISION NOT NULL,
            c            DOUBLE PRECISION NOT NULL,
            v            DOUBLE PRECISION NOT NULL DEFAULT 0,
            source       TEXT NOT NULL DEFAULT 'unknown',
            inserted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, symbol, granularity, ts)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_hc_exchange_symbol_ts "
        "ON historical_candles(exchange, symbol, ts)",
        "CREATE INDEX IF NOT EXISTS idx_hc_exchange_ts "
        "ON historical_candles(exchange, ts)",
        # Catalyst events — generic schema for equity (earnings/dividends/macro)
        # and crypto (halving/listing/regulatory).
        """
        CREATE TABLE IF NOT EXISTS catalyst_events (
            id            BIGSERIAL PRIMARY KEY,
            exchange      TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            event_type    TEXT NOT NULL,
            event_ts      TIMESTAMPTZ NOT NULL,
            source        TEXT NOT NULL,
            confidence    REAL NOT NULL DEFAULT 1.0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            inserted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (exchange, symbol, event_type, event_ts, source)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ce_exchange_symbol_ts "
        "ON catalyst_events(exchange, symbol, event_ts)",
        "CREATE INDEX IF NOT EXISTS idx_ce_exchange_ts "
        "ON catalyst_events(exchange, event_ts)",
        "CREATE INDEX IF NOT EXISTS idx_ce_exchange_type_ts "
        "ON catalyst_events(exchange, event_type, event_ts)",
        # Pattern fingerprints — pgvector embedding + labelled forward returns.
        # vector(N) literal must be inlined; PATTERN_VECTOR_DIM is hard-coded
        # below to keep DDL static (asserted equal at class load).
        """
        CREATE TABLE IF NOT EXISTS pattern_fingerprints (
            id                 BIGSERIAL PRIMARY KEY,
            exchange           TEXT NOT NULL,
            symbol             TEXT NOT NULL,
            event_id           BIGINT REFERENCES catalyst_events(id) ON DELETE CASCADE,
            event_type         TEXT NOT NULL,
            anchor_ts          TIMESTAMPTZ NOT NULL,
            window_pre_days    INTEGER NOT NULL,
            window_post_days   INTEGER NOT NULL,
            vector             vector(64) NOT NULL,
            forward_return_1d  DOUBLE PRECISION,
            forward_return_5d  DOUBLE PRECISION,
            forward_return_20d DOUBLE PRECISION,
            sample_meta_json   TEXT NOT NULL DEFAULT '{}',
            inserted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (exchange, symbol, event_id, window_pre_days, window_post_days)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pf_exchange_event_type "
        "ON pattern_fingerprints(exchange, event_type)",
        "CREATE INDEX IF NOT EXISTS idx_pf_exchange_symbol "
        "ON pattern_fingerprints(exchange, symbol)",
        # IVFFLAT index for fast cosine ANN search. ``lists`` of 100 is a
        # sane default for ≤1M rows; can be re-tuned later.
        "CREATE INDEX IF NOT EXISTS idx_pf_vector_cos "
        "ON pattern_fingerprints USING ivfflat (vector vector_cosine_ops) "
        "WITH (lists = 100)",
        # Backfill resumability — high-water mark per (exchange, symbol, granularity).
        """
        CREATE TABLE IF NOT EXISTS backfill_progress (
            exchange       TEXT NOT NULL,
            symbol         TEXT NOT NULL,
            granularity    TEXT NOT NULL,
            earliest_ts    TIMESTAMPTZ,
            latest_ts      TIMESTAMPTZ,
            last_source    TEXT,
            last_run_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            row_count      BIGINT NOT NULL DEFAULT 0,
            status         TEXT NOT NULL DEFAULT 'pending',
            error_message  TEXT,
            PRIMARY KEY (exchange, symbol, granularity)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bp_exchange_status "
        "ON backfill_progress(exchange, status)",
    )

    def _init_pattern_schema(self) -> None:
        """Create pattern-engine tables. Safe to call at every startup."""
        # Asserted invariant — keep DDL string in sync with PATTERN_VECTOR_DIM.
        assert "vector(64)" in self._PATTERN_DDL_STATEMENTS[8], (
            "Pattern DDL vector dim must match PATTERN_VECTOR_DIM"
        )
        assert PATTERN_VECTOR_DIM == 64, "PATTERN_VECTOR_DIM hard-coded to 64"

        with self._get_conn() as conn:
            for stmt in self._PATTERN_DDL_STATEMENTS:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT pattern_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT pattern_ddl_sp")
                    except psycopg2.errors.InsufficientPrivilege as e:
                        cur.execute("ROLLBACK TO SAVEPOINT pattern_ddl_sp")
                        logger.warning(
                            "Pattern schema stmt skipped — insufficient "
                            f"privilege (run as superuser once): {e}"
                        )
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT pattern_ddl_sp")
                        logger.warning(f"Pattern DDL failed: {stmt[:60]}…  → {e}")
            conn.commit()

    # ─── historical_candles ────────────────────────────────────────────────

    def upsert_candles(
        self,
        exchange: str,
        symbol: str,
        granularity: str,
        candles: Iterable[dict[str, Any]],
        source: str = "unknown",
    ) -> int:
        """Upsert OHLCV rows. Returns the count of rows actually written.

        Each candle dict must have keys ``ts`` (datetime|str|int), ``o``, ``h``,
        ``l``, ``c``, ``v``. ``ts`` may be a unix timestamp (seconds), an ISO
        string, or a ``datetime``.
        """
        if granularity not in ALLOWED_GRANULARITIES:
            raise ValueError(f"Invalid granularity: {granularity!r}")
        if not exchange or not symbol:
            raise ValueError("exchange and symbol are required")

        rows: list[tuple] = []
        for c in candles:
            ts = _coerce_ts(c.get("ts") or c.get("start") or c.get("time"))
            if ts is None:
                continue
            try:
                rows.append((
                    exchange, symbol, granularity, ts,
                    float(c["o"] if "o" in c else c["open"]),
                    float(c["h"] if "h" in c else c["high"]),
                    float(c["l"] if "l" in c else c["low"]),
                    float(c["c"] if "c" in c else c["close"]),
                    float(c.get("v", c.get("volume", 0)) or 0),
                    source,
                ))
            except (KeyError, TypeError, ValueError):
                continue

        if not rows:
            return 0

        sql = (
            "INSERT INTO historical_candles "
            "(exchange, symbol, granularity, ts, o, h, l, c, v, source) "
            "VALUES %s "
            "ON CONFLICT (exchange, symbol, granularity, ts) DO NOTHING"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
                written = cur.rowcount
            conn.commit()
        return max(written, 0)

    def get_candles_range(
        self,
        exchange: str,
        symbol: str,
        granularity: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Read OHLCV between ``start`` and ``end`` inclusive (ascending)."""
        if granularity not in ALLOWED_GRANULARITIES:
            raise ValueError(f"Invalid granularity: {granularity!r}")
        clauses = ["exchange = %s", "symbol = %s", "granularity = %s"]
        params: list = [exchange, symbol, granularity]
        if start is not None:
            clauses.append("ts >= %s")
            params.append(start)
        if end is not None:
            clauses.append("ts <= %s")
            params.append(end)
        sql = (
            "SELECT ts, o, h, l, c, v, source FROM historical_candles "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts ASC"
        )
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_candles_coverage(
        self, exchange: str, symbol: str, granularity: str
    ) -> dict:
        """Return ``{first_ts, last_ts, count}`` for a series."""
        if granularity not in ALLOWED_GRANULARITIES:
            raise ValueError(f"Invalid granularity: {granularity!r}")
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS n "
                "FROM historical_candles "
                "WHERE exchange = %s AND symbol = %s AND granularity = %s",
                (exchange, symbol, granularity),
            ).fetchone()
        if not row:
            return {"first_ts": None, "last_ts": None, "count": 0}
        return {
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "count": int(row["n"] or 0),
        }

    # ─── catalyst_events ──────────────────────────────────────────────────

    def upsert_catalyst_events(
        self, events: Sequence[dict[str, Any]]
    ) -> int:
        """Upsert event rows. Returns count of NEW rows (existing skipped)."""
        rows: list[tuple] = []
        for e in events:
            try:
                ts = _coerce_ts(e["event_ts"])
                if ts is None:
                    continue
                rows.append((
                    str(e["exchange"]),
                    str(e["symbol"]),
                    str(e["event_type"]),
                    ts,
                    str(e.get("source", "unknown")),
                    float(e.get("confidence", 1.0)),
                    json.dumps(e.get("metadata", {}), default=str),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not rows:
            return 0
        sql = (
            "INSERT INTO catalyst_events "
            "(exchange, symbol, event_type, event_ts, source, confidence, metadata_json) "
            "VALUES %s "
            "ON CONFLICT (exchange, symbol, event_type, event_ts, source) DO NOTHING"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
                written = cur.rowcount
            conn.commit()
        return max(written, 0)

    def get_catalyst_events(
        self,
        exchange: str,
        symbol: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        event_type: Optional[str] = None,
        event_id: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            clauses.append("symbol = %s")
            params.append(symbol)
        if start is not None:
            clauses.append("event_ts >= %s")
            params.append(start)
        if end is not None:
            clauses.append("event_ts <= %s")
            params.append(end)
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if event_id:
            clauses.append("id = %s")
            params.append(event_id)
        sql = (
            "SELECT id, exchange, symbol, event_type, event_ts, source, "
            "confidence, metadata_json, inserted_at FROM catalyst_events "
            f"WHERE {' AND '.join(clauses)} ORDER BY event_ts ASC LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.pop("metadata_json", "{}") or "{}")
            except (TypeError, ValueError):
                d["metadata"] = {}
            out.append(d)
        return out

    def get_upcoming_catalysts(
        self,
        exchange: str,
        horizon_days: int = 21,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        """Events with ``event_ts`` between now and now+horizon_days."""
        now = datetime.now(timezone.utc)
        return self.get_catalyst_events(
            exchange=exchange,
            symbol=symbol,
            start=now,
            end=now + timedelta(days=int(horizon_days)),
            limit=1000,
        )

    # ─── pattern_fingerprints ─────────────────────────────────────────────

    def upsert_pattern_fingerprint(
        self,
        exchange: str,
        symbol: str,
        event_id: Optional[int],
        event_type: str,
        anchor_ts: datetime,
        window_pre_days: int,
        window_post_days: int,
        vector: Sequence[float],
        forward_returns: dict[str, Optional[float]],
        sample_meta: Optional[dict] = None,
    ) -> int:
        """Upsert a fingerprint. Returns id of inserted/existing row."""
        if len(vector) != PATTERN_VECTOR_DIM:
            raise ValueError(
                f"vector dim {len(vector)} != {PATTERN_VECTOR_DIM}"
            )
        vec_literal = "[" + ",".join(f"{float(x):.8f}" for x in vector) + "]"
        sql = (
            "INSERT INTO pattern_fingerprints "
            "(exchange, symbol, event_id, event_type, anchor_ts, "
            " window_pre_days, window_post_days, vector, "
            " forward_return_1d, forward_return_5d, forward_return_20d, "
            " sample_meta_json) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s) "
            "ON CONFLICT (exchange, symbol, event_id, window_pre_days, window_post_days) "
            "DO UPDATE SET vector = EXCLUDED.vector, "
            "forward_return_1d = EXCLUDED.forward_return_1d, "
            "forward_return_5d = EXCLUDED.forward_return_5d, "
            "forward_return_20d = EXCLUDED.forward_return_20d, "
            "sample_meta_json = EXCLUDED.sample_meta_json "
            "RETURNING id"
        )
        params = (
            exchange, symbol, event_id, event_type, anchor_ts,
            int(window_pre_days), int(window_post_days), vec_literal,
            forward_returns.get("1d"),
            forward_returns.get("5d"),
            forward_returns.get("20d"),
            json.dumps(sample_meta or {}, default=str),
        )
        with self._get_conn() as conn:
            row = conn.execute(sql, params).fetchone()
            conn.commit()
        return int(row["id"])

    def find_similar_fingerprints(
        self,
        exchange: str,
        query_vector: Sequence[float],
        k: int = 20,
        event_type: Optional[str] = None,
        exclude_symbol: Optional[str] = None,
        exclude_anchor_after: Optional[datetime] = None,
    ) -> list[dict]:
        """Return top-k cosine-nearest fingerprints (same exchange only)."""
        if len(query_vector) != PATTERN_VECTOR_DIM:
            raise ValueError(
                f"query_vector dim {len(query_vector)} != {PATTERN_VECTOR_DIM}"
            )
        vec_literal = "[" + ",".join(f"{float(x):.8f}" for x in query_vector) + "]"
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if exclude_symbol:
            clauses.append("symbol <> %s")
            params.append(exclude_symbol)
        if exclude_anchor_after is not None:
            # Avoid look-ahead bias: exclude fingerprints whose anchor is
            # after the query's reference time.
            clauses.append("anchor_ts <= %s")
            params.append(exclude_anchor_after)

        sql = (
            "SELECT id, exchange, symbol, event_id, event_type, anchor_ts, "
            "window_pre_days, window_post_days, "
            "forward_return_1d, forward_return_5d, forward_return_20d, "
            "sample_meta_json, "
            "1 - (vector <=> %s::vector) AS similarity "
            "FROM pattern_fingerprints "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY vector <=> %s::vector ASC LIMIT %s"
        )
        full_params = [vec_literal, *params, vec_literal, int(k)]
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(full_params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["sample_meta"] = json.loads(d.pop("sample_meta_json", "{}") or "{}")
            except (TypeError, ValueError):
                d["sample_meta"] = {}
            out.append(d)
        return out

    def count_fingerprints(self, exchange: str) -> int:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pattern_fingerprints WHERE exchange = %s",
                (exchange,),
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    # ─── backfill_progress ────────────────────────────────────────────────

    def update_backfill_progress(
        self,
        exchange: str,
        symbol: str,
        granularity: str,
        earliest_ts: Optional[datetime],
        latest_ts: Optional[datetime],
        row_count: int,
        last_source: str,
        status: str = "ok",
        error_message: Optional[str] = None,
    ) -> None:
        if granularity not in ALLOWED_GRANULARITIES:
            raise ValueError(f"Invalid granularity: {granularity!r}")
        if status not in {"pending", "running", "ok", "error", "rate_limited"}:
            raise ValueError(f"Invalid status: {status!r}")
        sql = (
            "INSERT INTO backfill_progress "
            "(exchange, symbol, granularity, earliest_ts, latest_ts, "
            " row_count, last_source, status, error_message, last_run_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (exchange, symbol, granularity) DO UPDATE SET "
            "earliest_ts = LEAST(COALESCE(backfill_progress.earliest_ts, EXCLUDED.earliest_ts), EXCLUDED.earliest_ts), "
            "latest_ts = GREATEST(COALESCE(backfill_progress.latest_ts, EXCLUDED.latest_ts), EXCLUDED.latest_ts), "
            "row_count = backfill_progress.row_count + EXCLUDED.row_count, "
            "last_source = EXCLUDED.last_source, "
            "status = EXCLUDED.status, "
            "error_message = EXCLUDED.error_message, "
            "last_run_at = now()"
        )
        with self._get_conn() as conn:
            conn.execute(sql, (
                exchange, symbol, granularity, earliest_ts, latest_ts,
                int(row_count), last_source, status, error_message,
            ))
            conn.commit()

    def get_backfill_progress(
        self, exchange: str, symbol: Optional[str] = None
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            clauses.append("symbol = %s")
            params.append(symbol)
        sql = (
            "SELECT * FROM backfill_progress "
            f"WHERE {' AND '.join(clauses)} ORDER BY symbol, granularity"
        )
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def _coerce_ts(v: Any) -> Optional[datetime]:
    """Best-effort conversion of mixed timestamp inputs to aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        # Treat values > 1e12 as milliseconds.
        seconds = float(v) / 1000.0 if float(v) > 1e12 else float(v)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # Numeric string?
        try:
            return _coerce_ts(float(s))
        except ValueError:
            pass
        # ISO-8601 (handle trailing Z).
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
