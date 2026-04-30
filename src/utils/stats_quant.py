"""
Quant Analytics persistence — multi-factor loadings, HAR-RV forecasts,
Granger causality, slippage impact model, correlation regime events.

Owns five tables, all profile-isolated (every read & write requires
``exchange``). DDL is idempotent and additive — safe to call on every
``StatsDB`` startup.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

import psycopg2
import psycopg2.extras

from src.utils.logger import get_logger

logger = get_logger("stats.quant")


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class QuantAnalyticsMixin:
    """StatsDB mixin: factor loadings, HAR-RV, Granger, slippage, regime."""

    _QUANT_DDL_STATEMENTS: tuple[str, ...] = (
        # ── market_factor_loadings ──────────────────────────────────────
        # One row per (exchange, symbol, factor). UPSERT replaces stale
        # snapshots so the table stays compact (~|universe|·|factors|).
        """
        CREATE TABLE IF NOT EXISTS market_factor_loadings (
            exchange          TEXT NOT NULL,
            symbol            TEXT NOT NULL,
            factor            TEXT NOT NULL,
            beta              DOUBLE PRECISION,
            t_stat            DOUBLE PRECISION,
            r_squared         DOUBLE PRECISION,
            alpha_annualised  DOUBLE PRECISION,
            idio_vol          DOUBLE PRECISION,
            sample_count      INTEGER NOT NULL DEFAULT 0,
            computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, symbol, factor)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_mfl_exchange_symbol "
        "ON market_factor_loadings(exchange, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_mfl_exchange_factor "
        "ON market_factor_loadings(exchange, factor)",
        "CREATE INDEX IF NOT EXISTS idx_mfl_exchange_t_stat "
        "ON market_factor_loadings(exchange, ABS(t_stat) DESC NULLS LAST)",
        # ── har_rv_forecasts ────────────────────────────────────────────
        """
        CREATE TABLE IF NOT EXISTS har_rv_forecasts (
            exchange             TEXT NOT NULL,
            symbol               TEXT NOT NULL,
            horizon_days         INTEGER NOT NULL,
            forecast_vol         DOUBLE PRECISION,
            realized_vol_daily   DOUBLE PRECISION,
            realized_vol_weekly  DOUBLE PRECISION,
            realized_vol_monthly DOUBLE PRECISION,
            beta_daily           DOUBLE PRECISION,
            beta_weekly          DOUBLE PRECISION,
            beta_monthly         DOUBLE PRECISION,
            intercept            DOUBLE PRECISION,
            model_r_squared      DOUBLE PRECISION,
            sample_count         INTEGER NOT NULL DEFAULT 0,
            computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, symbol, horizon_days)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_harrv_exchange_symbol "
        "ON har_rv_forecasts(exchange, symbol)",
        # ── granger_causality ───────────────────────────────────────────
        # A "leader Granger-causes follower" assertion at a single lag.
        # Sparse: only significant rows persisted (caller's responsibility).
        """
        CREATE TABLE IF NOT EXISTS granger_causality (
            exchange      TEXT NOT NULL,
            leader        TEXT NOT NULL,
            follower      TEXT NOT NULL,
            lag_hours     INTEGER NOT NULL,
            f_stat        DOUBLE PRECISION,
            p_value       DOUBLE PRECISION,
            sample_count  INTEGER NOT NULL DEFAULT 0,
            computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, leader, follower, lag_hours)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_granger_exchange_leader "
        "ON granger_causality(exchange, leader)",
        "CREATE INDEX IF NOT EXISTS idx_granger_exchange_follower "
        "ON granger_causality(exchange, follower)",
        "CREATE INDEX IF NOT EXISTS idx_granger_exchange_p_value "
        "ON granger_causality(exchange, p_value ASC NULLS LAST)",
        # ── slippage_impact_models ──────────────────────────────────────
        # One model per exchange (universe-wide fit). Replaced on each
        # nightly recalc.
        """
        CREATE TABLE IF NOT EXISTS slippage_impact_models (
            exchange      TEXT NOT NULL PRIMARY KEY,
            alpha         DOUBLE PRECISION,
            beta_size     DOUBLE PRECISION,
            beta_vol      DOUBLE PRECISION,
            r_squared     DOUBLE PRECISION,
            sample_count  INTEGER NOT NULL DEFAULT 0,
            computed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        # ── correlation_regime_events ───────────────────────────────────
        # Append-only time series of universe-wide correlation snapshots.
        """
        CREATE TABLE IF NOT EXISTS correlation_regime_events (
            id            BIGSERIAL PRIMARY KEY,
            exchange      TEXT NOT NULL,
            avg_corr      DOUBLE PRECISION NOT NULL,
            z_score       DOUBLE PRECISION NOT NULL,
            regime        TEXT NOT NULL,
            n_pairs       INTEGER NOT NULL DEFAULT 0,
            history_n     INTEGER NOT NULL DEFAULT 0,
            computed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_cre_exchange_computed "
        "ON correlation_regime_events(exchange, computed_at DESC)",
    )

    def _init_quant_schema(self) -> None:
        """Create quant-analytics tables. Privilege-tolerant."""
        with self._get_conn() as conn:
            for stmt in self._QUANT_DDL_STATEMENTS:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT quant_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT quant_ddl_sp")
                    except psycopg2.errors.InsufficientPrivilege as e:
                        cur.execute("ROLLBACK TO SAVEPOINT quant_ddl_sp")
                        logger.warning(
                            f"Quant DDL skipped — insufficient privilege: {e}"
                        )
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT quant_ddl_sp")
                        logger.warning(
                            f"Quant DDL failed: {stmt[:60]}…  → {e}"
                        )
            conn.commit()

    # ─── market_factor_loadings ─────────────────────────────────────────

    def upsert_market_factor_loadings(
        self, rows: Sequence[dict[str, Any]],
    ) -> int:
        prepared: list[tuple] = []
        for r in rows:
            try:
                prepared.append((
                    str(r["exchange"]),
                    str(r["symbol"]),
                    str(r["factor"]),
                    _opt_float(r.get("beta")),
                    _opt_float(r.get("t_stat")),
                    _opt_float(r.get("r_squared")),
                    _opt_float(r.get("alpha_annualised")),
                    _opt_float(r.get("idio_vol")),
                    int(r.get("sample_count") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not prepared:
            return 0
        sql = (
            "INSERT INTO market_factor_loadings "
            "(exchange, symbol, factor, beta, t_stat, r_squared, "
            " alpha_annualised, idio_vol, sample_count) "
            "VALUES %s "
            "ON CONFLICT (exchange, symbol, factor) DO UPDATE SET "
            "beta = EXCLUDED.beta, "
            "t_stat = EXCLUDED.t_stat, "
            "r_squared = EXCLUDED.r_squared, "
            "alpha_annualised = EXCLUDED.alpha_annualised, "
            "idio_vol = EXCLUDED.idio_vol, "
            "sample_count = EXCLUDED.sample_count, "
            "computed_at = now()"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, prepared, page_size=200)
            conn.commit()
        return len(prepared)

    def get_market_factor_loadings(
        self,
        exchange: str,
        *,
        symbol: Optional[str] = None,
        factor: Optional[str] = None,
        min_abs_t_stat: float = 0.0,
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            clauses.append("symbol = %s"); params.append(symbol)
        if factor:
            clauses.append("factor = %s"); params.append(factor)
        if min_abs_t_stat > 0:
            clauses.append("ABS(COALESCE(t_stat, 0)) >= %s"); params.append(float(min_abs_t_stat))
        sql = (
            "SELECT exchange, symbol, factor, beta, t_stat, r_squared, "
            "alpha_annualised, idio_vol, sample_count, computed_at "
            "FROM market_factor_loadings "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ABS(COALESCE(t_stat, 0)) DESC NULLS LAST LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ─── har_rv_forecasts ───────────────────────────────────────────────

    def upsert_har_rv_forecasts(
        self, rows: Sequence[dict[str, Any]],
    ) -> int:
        prepared: list[tuple] = []
        for r in rows:
            try:
                prepared.append((
                    str(r["exchange"]),
                    str(r["symbol"]),
                    int(r.get("horizon_days") or 1),
                    _opt_float(r.get("forecast_vol")),
                    _opt_float(r.get("realized_vol_daily")),
                    _opt_float(r.get("realized_vol_weekly")),
                    _opt_float(r.get("realized_vol_monthly")),
                    _opt_float(r.get("beta_daily")),
                    _opt_float(r.get("beta_weekly")),
                    _opt_float(r.get("beta_monthly")),
                    _opt_float(r.get("intercept")),
                    _opt_float(r.get("model_r_squared")),
                    int(r.get("sample_count") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not prepared:
            return 0
        sql = (
            "INSERT INTO har_rv_forecasts "
            "(exchange, symbol, horizon_days, forecast_vol, "
            " realized_vol_daily, realized_vol_weekly, realized_vol_monthly, "
            " beta_daily, beta_weekly, beta_monthly, intercept, "
            " model_r_squared, sample_count) "
            "VALUES %s "
            "ON CONFLICT (exchange, symbol, horizon_days) DO UPDATE SET "
            "forecast_vol = EXCLUDED.forecast_vol, "
            "realized_vol_daily = EXCLUDED.realized_vol_daily, "
            "realized_vol_weekly = EXCLUDED.realized_vol_weekly, "
            "realized_vol_monthly = EXCLUDED.realized_vol_monthly, "
            "beta_daily = EXCLUDED.beta_daily, "
            "beta_weekly = EXCLUDED.beta_weekly, "
            "beta_monthly = EXCLUDED.beta_monthly, "
            "intercept = EXCLUDED.intercept, "
            "model_r_squared = EXCLUDED.model_r_squared, "
            "sample_count = EXCLUDED.sample_count, "
            "computed_at = now()"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, prepared, page_size=200)
            conn.commit()
        return len(prepared)

    def get_har_rv_forecasts(
        self,
        exchange: str,
        *,
        symbol: Optional[str] = None,
        horizon_days: Optional[int] = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if symbol:
            clauses.append("symbol = %s"); params.append(symbol)
        if horizon_days is not None:
            clauses.append("horizon_days = %s"); params.append(int(horizon_days))
        sql = (
            "SELECT exchange, symbol, horizon_days, forecast_vol, "
            "realized_vol_daily, realized_vol_weekly, realized_vol_monthly, "
            "beta_daily, beta_weekly, beta_monthly, intercept, "
            "model_r_squared, sample_count, computed_at "
            "FROM har_rv_forecasts "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY symbol, horizon_days LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_har_rv_forecast_for_symbol(
        self, exchange: str, symbol: str, horizon_days: int = 1,
    ) -> Optional[dict]:
        rows = self.get_har_rv_forecasts(
            exchange, symbol=symbol, horizon_days=horizon_days, limit=1,
        )
        return rows[0] if rows else None

    # ─── granger_causality ──────────────────────────────────────────────

    def upsert_granger_results(
        self, rows: Sequence[dict[str, Any]],
    ) -> int:
        prepared: list[tuple] = []
        for r in rows:
            try:
                prepared.append((
                    str(r["exchange"]),
                    str(r["leader"]),
                    str(r["follower"]),
                    int(r["lag_hours"]),
                    _opt_float(r.get("f_stat")),
                    _opt_float(r.get("p_value")),
                    int(r.get("sample_count") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not prepared:
            return 0
        sql = (
            "INSERT INTO granger_causality "
            "(exchange, leader, follower, lag_hours, f_stat, p_value, sample_count) "
            "VALUES %s "
            "ON CONFLICT (exchange, leader, follower, lag_hours) DO UPDATE SET "
            "f_stat = EXCLUDED.f_stat, "
            "p_value = EXCLUDED.p_value, "
            "sample_count = EXCLUDED.sample_count, "
            "computed_at = now()"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, prepared, page_size=500)
            conn.commit()
        return len(prepared)

    def get_granger_results(
        self,
        exchange: str,
        *,
        leader: Optional[str] = None,
        follower: Optional[str] = None,
        max_p_value: float = 1.0,
        limit: int = 500,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if leader:
            clauses.append("leader = %s"); params.append(leader)
        if follower:
            clauses.append("follower = %s"); params.append(follower)
        if max_p_value < 1.0:
            clauses.append("p_value <= %s"); params.append(float(max_p_value))
        sql = (
            "SELECT exchange, leader, follower, lag_hours, f_stat, "
            "p_value, sample_count, computed_at "
            "FROM granger_causality "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY p_value ASC NULLS LAST LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ─── slippage_impact_models ─────────────────────────────────────────

    def upsert_slippage_impact_model(self, row: dict[str, Any]) -> int:
        try:
            tup = (
                str(row["exchange"]),
                _opt_float(row.get("alpha")),
                _opt_float(row.get("beta_size")),
                _opt_float(row.get("beta_vol")),
                _opt_float(row.get("r_squared")),
                int(row.get("sample_count") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return 0
        sql = (
            "INSERT INTO slippage_impact_models "
            "(exchange, alpha, beta_size, beta_vol, r_squared, sample_count) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (exchange) DO UPDATE SET "
            "alpha = EXCLUDED.alpha, "
            "beta_size = EXCLUDED.beta_size, "
            "beta_vol = EXCLUDED.beta_vol, "
            "r_squared = EXCLUDED.r_squared, "
            "sample_count = EXCLUDED.sample_count, "
            "computed_at = now()"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tup)
            conn.commit()
        return 1

    def get_slippage_impact_model(self, exchange: str) -> Optional[dict]:
        sql = (
            "SELECT exchange, alpha, beta_size, beta_vol, r_squared, "
            "sample_count, computed_at FROM slippage_impact_models "
            "WHERE exchange = %s"
        )
        with self._get_conn() as conn:
            rows = conn.execute(sql, (exchange,)).fetchall()
        return dict(rows[0]) if rows else None

    # ─── correlation_regime_events ──────────────────────────────────────

    def insert_correlation_regime_event(self, row: dict[str, Any]) -> int:
        try:
            tup = (
                str(row["exchange"]),
                float(row["avg_corr"]),
                float(row["z_score"]),
                str(row["regime"]),
                int(row.get("n_pairs") or 0),
                int(row.get("history_n") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return 0
        sql = (
            "INSERT INTO correlation_regime_events "
            "(exchange, avg_corr, z_score, regime, n_pairs, history_n) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tup)
            conn.commit()
        return 1

    def get_correlation_regime_events(
        self,
        exchange: str,
        *,
        limit: int = 200,
        since: Optional[datetime] = None,
    ) -> list[dict]:
        clauses = ["exchange = %s"]
        params: list = [exchange]
        if since is not None:
            clauses.append("computed_at >= %s"); params.append(since)
        sql = (
            "SELECT id, exchange, avg_corr, z_score, regime, n_pairs, "
            "history_n, computed_at FROM correlation_regime_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY computed_at DESC LIMIT %s"
        )
        params.append(int(limit))
        with self._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_correlation_regime_history(
        self, exchange: str, *, window: int = 60,
    ) -> list[float]:
        """Return the last ``window`` avg_corr values (oldest → newest).

        Used by the detector to compute a trailing z-score.
        """
        sql = (
            "SELECT avg_corr FROM correlation_regime_events "
            "WHERE exchange = %s ORDER BY computed_at DESC LIMIT %s"
        )
        with self._get_conn() as conn:
            rows = conn.execute(sql, (exchange, int(window))).fetchall()
        # Reverse so caller gets oldest → newest.
        return [float(r["avg_corr"]) for r in reversed(rows)]
