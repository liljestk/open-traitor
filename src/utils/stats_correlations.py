"""
Cross-asset correlation, taxonomy, and cross-event regression persistence.

Owns four new tables that turn the per-symbol catalyst/pattern stack into
a cross-asset reaction engine:

* ``asset_taxonomy``           — per-symbol classification (asset_class,
  ecosystem, sector, custom tag list). Drives cluster grouping and
  spillover scoring.
* ``asset_correlations``       — pairwise rolling correlations + lead-lag
  scores. One row per (exchange, base_symbol, peer_symbol, window_days).
* ``asset_clusters``           — agglomerative clusters derived from the
  correlation matrix; surfaces "ETH-family" / "stablecoin" cohorts
  automatically without hand-tagging.
* ``cross_event_regressions``  — OLS of ``forward_return_target ~
  pre_features_driver`` for high-correlation pairs, per (driver_event_type,
  horizon). The cross-asset complement to ``event_price_regressions``.

All read methods enforce profile/domain isolation (exchange always
required). Writes are idempotent UPSERTs.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

import psycopg2
import psycopg2.extras

from src.utils.logger import get_logger

logger = get_logger("stats.correlations")


# Allowed event categories (normalised across equity & crypto). Kept narrow
# so cross-asset queries can group by category without a free-text fanout.
ALLOWED_EVENT_CATEGORIES: frozenset[str] = frozenset({
    "earnings",
    "dividend",
    "product",
    "macro",
    "onchain",
    "regulatory",
    "listing",
    "halving",
    "other",
})


def categorise_event_type(event_type: str) -> str:
    """Map a raw ``event_type`` to a normalised category."""
    if not event_type:
        return "other"
    et = event_type.strip().lower()
    if et in {"earnings", "earnings_release"}:
        return "earnings"
    if et in {"ex_dividend", "dividend"}:
        return "dividend"
    if et in {"product", "product_release", "launch"}:
        return "product"
    if et in {"macro", "fomc", "cpi", "ppi", "nfp", "gdp"}:
        return "macro"
    if et in {"halving"}:
        return "halving"
    if et in {"listing", "delisting"}:
        return "listing"
    if et in {"regulatory", "sec", "lawsuit", "etf"}:
        return "regulatory"
    if et in {"onchain", "upgrade", "fork", "burn"}:
        return "onchain"
    return "other"


class CorrelationsMixin:
    """Mixin owning cross-asset correlation, taxonomy & cross-event tables."""

    _CORRELATIONS_DDL_STATEMENTS: tuple[str, ...] = (
        # ── asset_taxonomy ───────────────────────────────────────────────
        # One row per symbol per exchange. ``ecosystem`` is the coarse
        # cluster label (ETH-L1, ETH-L2, BTC-family, stablecoin, equity-tech…).
        # ``tags`` is a free-form JSON object for additional classifications
        # (e.g. {"defi": true, "layer": 2, "industry": "Software"}).
        """
        CREATE TABLE IF NOT EXISTS asset_taxonomy (
            exchange     TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            asset_class  TEXT NOT NULL,
            ecosystem    TEXT,
            sector       TEXT,
            tags         JSONB NOT NULL DEFAULT '{}'::jsonb,
            source       TEXT NOT NULL DEFAULT 'manual',
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, symbol)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_at_exchange_ecosystem "
        "ON asset_taxonomy(exchange, ecosystem)",
        "CREATE INDEX IF NOT EXISTS idx_at_exchange_sector "
        "ON asset_taxonomy(exchange, sector)",
        # ── asset_correlations ───────────────────────────────────────────
        # Pairwise correlation snapshot. base_symbol < peer_symbol enforced
        # by the writer for canonical ordering.
        """
        CREATE TABLE IF NOT EXISTS asset_correlations (
            exchange         TEXT NOT NULL,
            base_symbol      TEXT NOT NULL,
            peer_symbol      TEXT NOT NULL,
            window_days      INTEGER NOT NULL,
            pearson          DOUBLE PRECISION,
            spearman         DOUBLE PRECISION,
            lead_lag_days    INTEGER NOT NULL DEFAULT 0,
            lead_lag_score   DOUBLE PRECISION,
            sample_count     INTEGER NOT NULL DEFAULT 0,
            computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, base_symbol, peer_symbol, window_days)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ac_exchange_pearson "
        "ON asset_correlations(exchange, ABS(pearson) DESC NULLS LAST)",
        "CREATE INDEX IF NOT EXISTS idx_ac_exchange_base "
        "ON asset_correlations(exchange, base_symbol)",
        "CREATE INDEX IF NOT EXISTS idx_ac_exchange_peer "
        "ON asset_correlations(exchange, peer_symbol)",
        # ── asset_clusters ───────────────────────────────────────────────
        # Each cluster gets a deterministic id per (exchange, computed_at)
        # batch. A symbol can belong to at most one cluster per batch.
        """
        CREATE TABLE IF NOT EXISTS asset_clusters (
            exchange     TEXT NOT NULL,
            cluster_id   INTEGER NOT NULL,
            symbol       TEXT NOT NULL,
            cohesion     DOUBLE PRECISION,
            label        TEXT,
            computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, cluster_id, symbol)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_acl_exchange_symbol "
        "ON asset_clusters(exchange, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_acl_exchange_computed "
        "ON asset_clusters(exchange, computed_at DESC)",
        # ── cross_event_regressions ──────────────────────────────────────
        # Driver: the symbol whose event fires.
        # Target: the related symbol whose forward return is being predicted.
        """
        CREATE TABLE IF NOT EXISTS cross_event_regressions (
            exchange              TEXT NOT NULL,
            driver_symbol         TEXT NOT NULL,
            driver_event_type     TEXT NOT NULL,
            target_symbol         TEXT NOT NULL,
            horizon_days          INTEGER NOT NULL,
            sample_count          INTEGER NOT NULL,
            beta                  DOUBLE PRECISION,
            intercept             DOUBLE PRECISION,
            r_squared             DOUBLE PRECISION,
            t_stat_beta           DOUBLE PRECISION,
            mean_forward_return   DOUBLE PRECISION,
            hit_rate              DOUBLE PRECISION,
            notes                 TEXT NOT NULL DEFAULT '',
            computed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, driver_symbol, driver_event_type,
                         target_symbol, horizon_days)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_xer_exchange_driver "
        "ON cross_event_regressions(exchange, driver_symbol, driver_event_type)",
        "CREATE INDEX IF NOT EXISTS idx_xer_exchange_target "
        "ON cross_event_regressions(exchange, target_symbol)",
        "CREATE INDEX IF NOT EXISTS idx_xer_exchange_r2 "
        "ON cross_event_regressions(exchange, r_squared DESC NULLS LAST)",
    )

    _CORRELATIONS_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        # Add event_category column to existing catalyst_events table.
        ("catalyst_events", "event_category", "TEXT NOT NULL DEFAULT 'other'"),
    )

    def _init_correlations_schema(self) -> None:
        """Create cross-asset tables and run additive migrations."""
        with self._get_conn() as conn:
            for stmt in self._CORRELATIONS_DDL_STATEMENTS:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT corr_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT corr_ddl_sp")
                    except psycopg2.errors.InsufficientPrivilege as e:
                        cur.execute("ROLLBACK TO SAVEPOINT corr_ddl_sp")
                        logger.warning(
                            f"Correlations DDL skipped — insufficient privilege: {e}"
                        )
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT corr_ddl_sp")
                        logger.warning(
                            f"Correlations DDL failed: {stmt[:60]}…  → {e}"
                        )
            for table, column, col_type in self._CORRELATIONS_MIGRATIONS:
                with conn.cursor() as cur:
                    sp = f"corr_mig_{table}_{column}"
                    try:
                        cur.execute(f"SAVEPOINT {sp}")
                        cur.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                        )
                        cur.execute(f"RELEASE SAVEPOINT {sp}")
                        logger.info(
                            f"Correlations migration: added {table}.{column}"
                        )
                    except psycopg2.errors.DuplicateColumn:
                        cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    except psycopg2.Error as e:
                        cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        logger.warning(
                            f"Correlations migration {table}.{column} failed: {e}"
                        )
            conn.commit()

    # ─── asset_taxonomy ──────────────────────────────────────────────────

    def upsert_asset_taxonomy(
        self,
        rows: Sequence[dict[str, Any]],
    ) -> int:
        """Upsert taxonomy rows. Returns count of writes attempted.

        Each row needs ``exchange``, ``symbol``, ``asset_class``. Optional:
        ``ecosystem``, ``sector``, ``tags`` (dict), ``source``.
        """
        prepared: list[tuple] = []
        for r in rows:
            try:
                prepared.append((
                    str(r["exchange"]),
                    str(r["symbol"]),
                    str(r["asset_class"]),
                    r.get("ecosystem"),
                    r.get("sector"),
                    json.dumps(r.get("tags") or {}, default=str),
                    str(r.get("source", "manual")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not prepared:
            return 0
        sql = (
            "INSERT INTO asset_taxonomy "
            "(exchange, symbol, asset_class, ecosystem, sector, tags, source) "
            "VALUES %s "
            "ON CONFLICT (exchange, symbol) DO UPDATE SET "
            "asset_class = EXCLUDED.asset_class, "
            "ecosystem = COALESCE(EXCLUDED.ecosystem, asset_taxonomy.ecosystem), "
            "sector = COALESCE(EXCLUDED.sector, asset_taxonomy.sector), "
            "tags = asset_taxonomy.tags || EXCLUDED.tags, "
            "source = EXCLUDED.source, "
            "updated_at = now()"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, prepared, page_size=200)
            conn.commit()
        return len(prepared)

    def get_asset_taxonomy(
        self,
        exchange: str,
        symbol: Optional[str] = None,
        ecosystem: Optional[str] = None,
        sector: Optional[str] = None,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            clauses.append("symbol = %s")
            params.append(symbol)
        if ecosystem:
            clauses.append("ecosystem = %s")
            params.append(ecosystem)
        if sector:
            clauses.append("sector = %s")
            params.append(sector)
        sql = (
            "SELECT exchange, symbol, asset_class, ecosystem, sector, "
            "tags, source, updated_at FROM asset_taxonomy "
            f"WHERE {' AND '.join(clauses)} ORDER BY symbol"
        )
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            tags = d.get("tags")
            if isinstance(tags, str):
                try:
                    d["tags"] = json.loads(tags)
                except (TypeError, ValueError):
                    d["tags"] = {}
            elif tags is None:
                d["tags"] = {}
            out.append(d)
        return out

    # ─── asset_correlations ──────────────────────────────────────────────

    def upsert_asset_correlations(
        self,
        exchange: str,
        rows: Sequence[dict[str, Any]],
    ) -> int:
        """Bulk upsert correlation rows. Canonicalises (base, peer) order.

        Each row: ``base_symbol``, ``peer_symbol``, ``window_days``,
        ``pearson``, ``spearman`` (optional), ``lead_lag_days`` (optional),
        ``lead_lag_score`` (optional), ``sample_count`` (optional).
        """
        prepared: list[tuple] = []
        for r in rows:
            try:
                a = str(r["base_symbol"])
                b = str(r["peer_symbol"])
                if a == b:
                    continue
                base, peer = (a, b) if a < b else (b, a)
                prepared.append((
                    exchange,
                    base,
                    peer,
                    int(r["window_days"]),
                    _opt_float(r.get("pearson")),
                    _opt_float(r.get("spearman")),
                    int(r.get("lead_lag_days") or 0),
                    _opt_float(r.get("lead_lag_score")),
                    int(r.get("sample_count") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not prepared:
            return 0
        sql = (
            "INSERT INTO asset_correlations "
            "(exchange, base_symbol, peer_symbol, window_days, pearson, "
            " spearman, lead_lag_days, lead_lag_score, sample_count) "
            "VALUES %s "
            "ON CONFLICT (exchange, base_symbol, peer_symbol, window_days) DO UPDATE SET "
            "pearson = EXCLUDED.pearson, "
            "spearman = EXCLUDED.spearman, "
            "lead_lag_days = EXCLUDED.lead_lag_days, "
            "lead_lag_score = EXCLUDED.lead_lag_score, "
            "sample_count = EXCLUDED.sample_count, "
            "computed_at = now()"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, prepared, page_size=500)
            conn.commit()
        return len(prepared)

    def get_asset_correlations(
        self,
        exchange: str,
        symbol: Optional[str] = None,
        window_days: Optional[int] = None,
        min_abs_pearson: float = 0.0,
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            clauses.append("(base_symbol = %s OR peer_symbol = %s)")
            params.extend([symbol, symbol])
        if window_days is not None:
            clauses.append("window_days = %s")
            params.append(int(window_days))
        if min_abs_pearson > 0.0:
            clauses.append("ABS(COALESCE(pearson, 0)) >= %s")
            params.append(float(min_abs_pearson))
        sql = (
            "SELECT exchange, base_symbol, peer_symbol, window_days, "
            "pearson, spearman, lead_lag_days, lead_lag_score, "
            "sample_count, computed_at FROM asset_correlations "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ABS(COALESCE(pearson, 0)) DESC NULLS LAST LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ─── asset_clusters ──────────────────────────────────────────────────

    def replace_asset_clusters(
        self,
        exchange: str,
        clusters: Sequence[dict[str, Any]],
    ) -> int:
        """Atomically replace the cluster snapshot for an exchange.

        Each cluster: ``cluster_id``, ``symbols`` (iterable[str]),
        ``cohesion`` (float), ``label`` (optional).
        """
        rows: list[tuple] = []
        for c in clusters:
            try:
                cid = int(c["cluster_id"])
                cohesion = _opt_float(c.get("cohesion"))
                label = c.get("label")
                for sym in c.get("symbols", []) or []:
                    rows.append((exchange, cid, str(sym), cohesion, label))
            except (KeyError, TypeError, ValueError):
                continue
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM asset_clusters WHERE exchange = %s",
                    (exchange,),
                )
                if rows:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO asset_clusters "
                        "(exchange, cluster_id, symbol, cohesion, label) "
                        "VALUES %s",
                        rows,
                        page_size=500,
                    )
            conn.commit()
        return len(rows)

    def get_asset_clusters(
        self,
        exchange: str,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        """Return rows grouped by cluster_id."""
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            # Find the cluster(s) that contain this symbol, then return all
            # members of those clusters.
            sql_inner = (
                "SELECT DISTINCT cluster_id FROM asset_clusters "
                "WHERE exchange = %s AND symbol = %s"
            )
            with self._get_conn() as conn:
                inner_rows = conn.execute(sql_inner, (exchange, symbol)).fetchall()
            cids = [int(r["cluster_id"]) for r in inner_rows]
            if not cids:
                return []
            clauses.append("cluster_id = ANY(%s)")
            params.append(cids)
        sql = (
            "SELECT exchange, cluster_id, symbol, cohesion, label, computed_at "
            f"FROM asset_clusters WHERE {' AND '.join(clauses)} "
            "ORDER BY cluster_id, symbol"
        )
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_cluster_for_symbol(
        self,
        exchange: str,
        symbol: str,
    ) -> list[str]:
        """Return symbol's cluster mates (excluding itself). Empty if none."""
        members = self.get_asset_clusters(exchange=exchange, symbol=symbol)
        return [m["symbol"] for m in members if m["symbol"] != symbol]

    # ─── cross_event_regressions ─────────────────────────────────────────

    def upsert_cross_event_regression(self, row: dict) -> None:
        """Persist one cross-asset regression. Idempotent on the composite PK."""
        sql = (
            "INSERT INTO cross_event_regressions "
            "(exchange, driver_symbol, driver_event_type, target_symbol, "
            " horizon_days, sample_count, beta, intercept, r_squared, "
            " t_stat_beta, mean_forward_return, hit_rate, notes, computed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (exchange, driver_symbol, driver_event_type, "
            "target_symbol, horizon_days) DO UPDATE SET "
            "sample_count = EXCLUDED.sample_count, "
            "beta = EXCLUDED.beta, "
            "intercept = EXCLUDED.intercept, "
            "r_squared = EXCLUDED.r_squared, "
            "t_stat_beta = EXCLUDED.t_stat_beta, "
            "mean_forward_return = EXCLUDED.mean_forward_return, "
            "hit_rate = EXCLUDED.hit_rate, "
            "notes = EXCLUDED.notes, "
            "computed_at = now()"
        )
        with self._get_conn() as conn:
            conn.execute(
                sql,
                (
                    row["exchange"],
                    row["driver_symbol"],
                    row["driver_event_type"],
                    row["target_symbol"],
                    int(row["horizon_days"]),
                    int(row.get("sample_count") or 0),
                    _opt_float(row.get("beta")),
                    _opt_float(row.get("intercept")),
                    _opt_float(row.get("r_squared")),
                    _opt_float(row.get("t_stat_beta")),
                    _opt_float(row.get("mean_forward_return")),
                    _opt_float(row.get("hit_rate")),
                    row.get("notes") or "",
                ),
            )
            conn.commit()

    def get_cross_event_regressions(
        self,
        exchange: str,
        *,
        driver_symbol: Optional[str] = None,
        target_symbol: Optional[str] = None,
        driver_event_type: Optional[str] = None,
        min_samples: int = 0,
        min_abs_beta: float = 0.0,
        limit: int = 200,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if driver_symbol:
            clauses.append("driver_symbol = %s")
            params.append(driver_symbol)
        if target_symbol:
            clauses.append("target_symbol = %s")
            params.append(target_symbol)
        if driver_event_type:
            clauses.append("driver_event_type = %s")
            params.append(driver_event_type)
        if min_samples > 0:
            clauses.append("sample_count >= %s")
            params.append(int(min_samples))
        if min_abs_beta > 0.0:
            clauses.append("ABS(COALESCE(beta, 0)) >= %s")
            params.append(float(min_abs_beta))
        sql = (
            "SELECT exchange, driver_symbol, driver_event_type, target_symbol, "
            "horizon_days, sample_count, beta, intercept, r_squared, "
            "t_stat_beta, mean_forward_return, hit_rate, notes, computed_at "
            f"FROM cross_event_regressions WHERE {' AND '.join(clauses)} "
            "ORDER BY r_squared DESC NULLS LAST LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None
