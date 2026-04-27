"""
SmartsMixin — persistence layer for the "make-the-system-smarter" feature set.

Owns the following tables (all PK includes ``exchange`` for domain isolation):

  * ``feature_attribution``    — per-trade contribution of each feature/agent
                                 to outcome (Brier-style); used to compute
                                 calibration & adjust ensemble weights.
  * ``bandit_state``           — Thompson-sampling alpha/beta per (regime,
                                 strategy) for online strategy weighting.
  * ``counterfactual_replays`` — periodic re-run of strategist on historical
                                 contexts; tracks would-be vs actual P&L.
  * ``lead_lag_matrix``        — nightly OLS asset-A leads asset-B by lag
                                 minutes; (beta, t-stat, sample_count).
  * ``upcoming_events``        — proactive calendar (FOMC, CPI, earnings,
                                 token unlocks, ETF events).
  * ``decision_drift``         — daily strategist confidence-distribution
                                 percentile snapshots.
  * ``reasoning_judge``        — LLM-as-judge scores on a sampled fraction
                                 of agent_reasoning rows.
  * ``l2_snapshots``           — order-book L2 snapshot at decision time
                                 (top-N levels JSON, mid, spread bps).
  * ``onchain_signals``        — generic free-tier on-chain metric series
                                 (e.g. exchange netflow, stablecoin supply).
  * ``shadow_decisions``       — variant-strategist outputs run in parallel
                                 to live strategist, never executed.

All tables are created idempotently in ``_init_smarts_schema``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras

from src.utils.logger import get_logger

logger = get_logger("stats_smarts")


_SMARTS_DDL: tuple[str, ...] = (
    # ── Phase 1: outcome attribution ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS feature_attribution (
        id           BIGSERIAL PRIMARY KEY,
        exchange     TEXT NOT NULL,
        cycle_id     TEXT,
        pair         TEXT NOT NULL,
        feature_name TEXT NOT NULL,
        feature_val  DOUBLE PRECISION,
        confidence   DOUBLE PRECISION,
        action       TEXT,
        outcome      DOUBLE PRECISION,
        brier        DOUBLE PRECISION,
        ts           TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fa_exchange_feature_ts "
    "ON feature_attribution(exchange, feature_name, ts)",
    "CREATE INDEX IF NOT EXISTS idx_fa_exchange_pair_ts "
    "ON feature_attribution(exchange, pair, ts)",

    # ── Phase 1: bandit state ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS bandit_state (
        exchange   TEXT NOT NULL,
        regime     TEXT NOT NULL,
        strategy   TEXT NOT NULL,
        alpha      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        beta       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        n_pulls    BIGINT NOT NULL DEFAULT 0,
        last_update TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (exchange, regime, strategy)
    )
    """,

    # ── Phase 1: counterfactual replays ────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS counterfactual_replays (
        id              BIGSERIAL PRIMARY KEY,
        exchange        TEXT NOT NULL,
        replay_date     DATE NOT NULL,
        cycle_id        TEXT,
        pair            TEXT NOT NULL,
        actual_action   TEXT,
        replay_action   TEXT,
        actual_conf     DOUBLE PRECISION,
        replay_conf     DOUBLE PRECISION,
        actual_pnl_pct  DOUBLE PRECISION,
        replay_pnl_pct  DOUBLE PRECISION,
        notes           TEXT NOT NULL DEFAULT '',
        ts              TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cr_exchange_date "
    "ON counterfactual_replays(exchange, replay_date)",

    # ── Phase 3: lead-lag matrix ───────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS lead_lag_matrix (
        exchange     TEXT NOT NULL,
        leader       TEXT NOT NULL,
        follower     TEXT NOT NULL,
        lag_minutes  INTEGER NOT NULL,
        beta         DOUBLE PRECISION NOT NULL,
        t_stat       DOUBLE PRECISION NOT NULL,
        r_squared    DOUBLE PRECISION,
        sample_count INTEGER NOT NULL,
        computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (exchange, leader, follower, lag_minutes)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ll_exchange_leader "
    "ON lead_lag_matrix(exchange, leader, t_stat DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ll_exchange_follower "
    "ON lead_lag_matrix(exchange, follower, t_stat DESC)",

    # ── Phase 4: forward-looking event calendar ────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS upcoming_events (
        id            BIGSERIAL PRIMARY KEY,
        exchange      TEXT NOT NULL,
        symbol        TEXT NOT NULL DEFAULT '*',
        event_type    TEXT NOT NULL,
        event_ts      TIMESTAMPTZ NOT NULL,
        importance    INTEGER NOT NULL DEFAULT 1,
        source        TEXT NOT NULL,
        title         TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        inserted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (exchange, symbol, event_type, event_ts, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ue_exchange_ts "
    "ON upcoming_events(exchange, event_ts)",
    "CREATE INDEX IF NOT EXISTS idx_ue_exchange_symbol_ts "
    "ON upcoming_events(exchange, symbol, event_ts)",

    # ── Phase 6: decision-distribution drift ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS decision_drift (
        exchange      TEXT NOT NULL,
        snapshot_date DATE NOT NULL,
        agent         TEXT NOT NULL,
        n_decisions   INTEGER NOT NULL,
        mean_conf     DOUBLE PRECISION,
        p10_conf      DOUBLE PRECISION,
        p50_conf      DOUBLE PRECISION,
        p90_conf      DOUBLE PRECISION,
        action_dist_json TEXT NOT NULL DEFAULT '{}',
        baseline_mean DOUBLE PRECISION,
        baseline_std  DOUBLE PRECISION,
        z_score       DOUBLE PRECISION,
        alert         BOOLEAN NOT NULL DEFAULT false,
        computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (exchange, snapshot_date, agent)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dd_exchange_date "
    "ON decision_drift(exchange, snapshot_date)",

    # ── Phase 6: LLM-as-judge sampler ──────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS reasoning_judge (
        id            BIGSERIAL PRIMARY KEY,
        exchange      TEXT NOT NULL,
        cycle_id      TEXT,
        agent         TEXT NOT NULL,
        pair          TEXT,
        score         DOUBLE PRECISION,
        verdict       TEXT,
        rationale     TEXT,
        judged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rj_exchange_judged_at "
    "ON reasoning_judge(exchange, judged_at)",

    # ── Phase 7: L2 snapshots at decision time ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS l2_snapshots (
        id           BIGSERIAL PRIMARY KEY,
        exchange     TEXT NOT NULL,
        symbol       TEXT NOT NULL,
        cycle_id     TEXT,
        ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
        mid          DOUBLE PRECISION,
        spread_bps   DOUBLE PRECISION,
        bid_depth_5  DOUBLE PRECISION,
        ask_depth_5  DOUBLE PRECISION,
        obi          DOUBLE PRECISION,
        levels_json  TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_l2_exchange_symbol_ts "
    "ON l2_snapshots(exchange, symbol, ts)",

    # ── Phase 7: on-chain signals ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS onchain_signals (
        id           BIGSERIAL PRIMARY KEY,
        exchange     TEXT NOT NULL,
        asset        TEXT NOT NULL,
        metric       TEXT NOT NULL,
        ts           TIMESTAMPTZ NOT NULL,
        value        DOUBLE PRECISION NOT NULL,
        source       TEXT NOT NULL,
        inserted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (exchange, asset, metric, ts, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_oc_exchange_asset_metric_ts "
    "ON onchain_signals(exchange, asset, metric, ts)",

    # ── Phase 8: shadow strategist decisions ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS shadow_decisions (
        id            BIGSERIAL PRIMARY KEY,
        exchange      TEXT NOT NULL,
        cycle_id      TEXT,
        variant       TEXT NOT NULL,
        pair          TEXT NOT NULL,
        action        TEXT,
        confidence    DOUBLE PRECISION,
        live_action   TEXT,
        live_confidence DOUBLE PRECISION,
        diff_action   BOOLEAN,
        reasoning     TEXT,
        ts            TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sd_exchange_variant_ts "
    "ON shadow_decisions(exchange, variant, ts)",
)


def _to_iso(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


class SmartsMixin:
    """Tables and accessors for the smarter-system feature set."""

    def _init_smarts_schema(self) -> None:
        """Idempotent DDL for all smarts tables. Safe at every startup."""
        with self._get_conn() as conn:
            for stmt in _SMARTS_DDL:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT smarts_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT smarts_ddl_sp")
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT smarts_ddl_sp")
                        logger.warning(
                            f"Smarts DDL skipped: {stmt[:60]}…  → {e}"
                        )
            conn.commit()

    # ─── feature_attribution ──────────────────────────────────────────────

    def write_feature_attribution(
        self,
        *,
        exchange: str,
        cycle_id: str,
        pair: str,
        rows: Iterable[dict],
    ) -> int:
        """Bulk-insert per-feature attribution rows for a single decision.

        Each ``row`` must have: ``feature_name``, ``feature_val`` (float|None),
        ``confidence`` (float), ``action`` (str), ``outcome`` (1|0|None),
        ``brier`` (float|None).
        """
        if not exchange or not pair:
            raise ValueError("exchange and pair required")
        tup: list[tuple] = []
        for r in rows:
            tup.append((
                exchange, cycle_id, pair,
                str(r.get("feature_name") or "unknown"),
                _f(r.get("feature_val")),
                _f(r.get("confidence")),
                str(r.get("action") or "hold"),
                _f(r.get("outcome")),
                _f(r.get("brier")),
            ))
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO feature_attribution "
                    "(exchange, cycle_id, pair, feature_name, feature_val, "
                    "confidence, action, outcome, brier) VALUES %s",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_feature_brier(
        self, exchange: str, *, lookback_days: int = 30, min_samples: int = 20
    ) -> list[dict]:
        """Per-feature mean Brier score over recent window.

        Lower Brier = better calibration. Useful for ensemble weight tuning.
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT feature_name, AVG(brier) AS mean_brier, "
                "COUNT(*) AS n "
                "FROM feature_attribution "
                "WHERE exchange = %s "
                "AND ts >= now() - (%s || ' days')::interval "
                "AND brier IS NOT NULL "
                "GROUP BY feature_name "
                "HAVING COUNT(*) >= %s "
                "ORDER BY mean_brier ASC",
                (exchange, str(int(lookback_days)), int(min_samples)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── bandit_state ─────────────────────────────────────────────────────

    def get_bandit_state(self, exchange: str, regime: str) -> dict[str, dict]:
        """Return ``{strategy: {alpha, beta, n_pulls}}`` for a (exchange, regime)."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT strategy, alpha, beta, n_pulls "
                "FROM bandit_state "
                "WHERE exchange = %s AND regime = %s",
                (exchange, regime),
            ).fetchall()
        return {r["strategy"]: dict(r) for r in rows}

    def upsert_bandit(
        self,
        *,
        exchange: str,
        regime: str,
        strategy: str,
        alpha: float,
        beta: float,
        n_pulls: int,
    ) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bandit_state "
                    "(exchange, regime, strategy, alpha, beta, n_pulls, last_update) "
                    "VALUES (%s,%s,%s,%s,%s,%s, now()) "
                    "ON CONFLICT (exchange, regime, strategy) DO UPDATE SET "
                    "alpha = EXCLUDED.alpha, beta = EXCLUDED.beta, "
                    "n_pulls = EXCLUDED.n_pulls, last_update = now()",
                    (exchange, regime, strategy, float(alpha),
                     float(beta), int(n_pulls)),
                )
            conn.commit()

    # ─── counterfactual_replays ───────────────────────────────────────────

    def write_counterfactual(
        self,
        *,
        exchange: str,
        replay_date: Any,
        rows: Iterable[dict],
    ) -> int:
        d = _to_iso(replay_date) or datetime.now(timezone.utc)
        tup: list[tuple] = []
        for r in rows:
            tup.append((
                exchange, d.date(),
                r.get("cycle_id"),
                str(r.get("pair") or ""),
                r.get("actual_action"),
                r.get("replay_action"),
                _f(r.get("actual_conf")),
                _f(r.get("replay_conf")),
                _f(r.get("actual_pnl_pct")),
                _f(r.get("replay_pnl_pct")),
                str(r.get("notes") or ""),
            ))
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO counterfactual_replays "
                    "(exchange, replay_date, cycle_id, pair, "
                    "actual_action, replay_action, actual_conf, replay_conf, "
                    "actual_pnl_pct, replay_pnl_pct, notes) VALUES %s",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_counterfactual_summary(
        self, exchange: str, *, lookback_days: int = 30
    ) -> dict:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "AVG(actual_pnl_pct) AS actual_mean, "
                "AVG(replay_pnl_pct) AS replay_mean, "
                "AVG(CASE WHEN actual_action <> replay_action THEN 1.0 "
                "ELSE 0.0 END) AS diff_rate "
                "FROM counterfactual_replays "
                "WHERE exchange = %s "
                "AND replay_date >= (now() - (%s || ' days')::interval)::date",
                (exchange, str(int(lookback_days))),
            ).fetchone()
        return dict(row) if row else {"n": 0}

    # ─── lead_lag_matrix ──────────────────────────────────────────────────

    def upsert_lead_lag(self, exchange: str, rows: Iterable[dict]) -> int:
        tup: list[tuple] = []
        for r in rows:
            try:
                tup.append((
                    exchange, str(r["leader"]), str(r["follower"]),
                    int(r["lag_minutes"]),
                    float(r["beta"]), float(r["t_stat"]),
                    _f(r.get("r_squared")),
                    int(r.get("sample_count") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO lead_lag_matrix "
                    "(exchange, leader, follower, lag_minutes, beta, t_stat, "
                    "r_squared, sample_count) VALUES %s "
                    "ON CONFLICT (exchange, leader, follower, lag_minutes) "
                    "DO UPDATE SET beta = EXCLUDED.beta, "
                    "t_stat = EXCLUDED.t_stat, "
                    "r_squared = EXCLUDED.r_squared, "
                    "sample_count = EXCLUDED.sample_count, "
                    "computed_at = now()",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_lead_lag_for(
        self, exchange: str, follower: str, *, min_abs_t: float = 2.0
    ) -> list[dict]:
        """Return statistically-significant leaders for a follower asset."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT leader, lag_minutes, beta, t_stat, r_squared, sample_count "
                "FROM lead_lag_matrix "
                "WHERE exchange = %s AND follower = %s "
                "AND ABS(t_stat) >= %s "
                "ORDER BY ABS(t_stat) DESC LIMIT 20",
                (exchange, follower, float(min_abs_t)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── upcoming_events ──────────────────────────────────────────────────

    def upsert_upcoming_events(self, events: Iterable[dict]) -> int:
        tup: list[tuple] = []
        for e in events:
            ts = _to_iso(e.get("event_ts"))
            if ts is None:
                continue
            tup.append((
                str(e["exchange"]),
                str(e.get("symbol") or "*"),
                str(e["event_type"]),
                ts,
                int(e.get("importance") or 1),
                str(e.get("source") or "manual"),
                str(e.get("title") or "")[:500],
                json.dumps(e.get("metadata") or {}),
            ))
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO upcoming_events "
                    "(exchange, symbol, event_type, event_ts, importance, "
                    "source, title, metadata_json) VALUES %s "
                    "ON CONFLICT (exchange, symbol, event_type, event_ts, source) "
                    "DO NOTHING",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_upcoming_events(
        self,
        exchange: str,
        *,
        symbol: Optional[str] = None,
        within_hours: int = 72,
        min_importance: int = 1,
    ) -> list[dict]:
        clauses = [
            "exchange = %s",
            "event_ts >= now()",
            "event_ts <= now() + (%s || ' hours')::interval",
            "importance >= %s",
        ]
        params: list = [exchange, str(int(within_hours)), int(min_importance)]
        if symbol:
            clauses.append("(symbol = %s OR symbol = '*')")
            params.append(symbol)
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT symbol, event_type, event_ts, importance, source, "
                "title, metadata_json FROM upcoming_events "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY event_ts ASC LIMIT 100",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── decision_drift ───────────────────────────────────────────────────

    def write_decision_drift(self, exchange: str, rows: Iterable[dict]) -> int:
        tup: list[tuple] = []
        for r in rows:
            d = _to_iso(r.get("snapshot_date"))
            d_date = (d or datetime.now(timezone.utc)).date()
            tup.append((
                exchange, d_date, str(r.get("agent") or "strategist"),
                int(r.get("n_decisions") or 0),
                _f(r.get("mean_conf")),
                _f(r.get("p10_conf")),
                _f(r.get("p50_conf")),
                _f(r.get("p90_conf")),
                json.dumps(r.get("action_dist") or {}),
                _f(r.get("baseline_mean")),
                _f(r.get("baseline_std")),
                _f(r.get("z_score")),
                bool(r.get("alert", False)),
            ))
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO decision_drift "
                    "(exchange, snapshot_date, agent, n_decisions, "
                    "mean_conf, p10_conf, p50_conf, p90_conf, "
                    "action_dist_json, baseline_mean, baseline_std, "
                    "z_score, alert) VALUES %s "
                    "ON CONFLICT (exchange, snapshot_date, agent) DO UPDATE SET "
                    "n_decisions = EXCLUDED.n_decisions, "
                    "mean_conf = EXCLUDED.mean_conf, "
                    "p10_conf = EXCLUDED.p10_conf, "
                    "p50_conf = EXCLUDED.p50_conf, "
                    "p90_conf = EXCLUDED.p90_conf, "
                    "action_dist_json = EXCLUDED.action_dist_json, "
                    "baseline_mean = EXCLUDED.baseline_mean, "
                    "baseline_std = EXCLUDED.baseline_std, "
                    "z_score = EXCLUDED.z_score, "
                    "alert = EXCLUDED.alert, "
                    "computed_at = now()",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_decision_drift(
        self, exchange: str, *, lookback_days: int = 30
    ) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT snapshot_date, agent, n_decisions, mean_conf, "
                "p10_conf, p50_conf, p90_conf, baseline_mean, baseline_std, "
                "z_score, alert FROM decision_drift "
                "WHERE exchange = %s "
                "AND snapshot_date >= (now() - (%s || ' days')::interval)::date "
                "ORDER BY snapshot_date DESC, agent ASC",
                (exchange, str(int(lookback_days))),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── reasoning_judge ──────────────────────────────────────────────────

    def write_reasoning_judge(self, exchange: str, rows: Iterable[dict]) -> int:
        tup: list[tuple] = []
        for r in rows:
            tup.append((
                exchange, r.get("cycle_id"),
                str(r.get("agent") or ""),
                r.get("pair"),
                _f(r.get("score")),
                str(r.get("verdict") or "")[:50],
                str(r.get("rationale") or "")[:2000],
            ))
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO reasoning_judge "
                    "(exchange, cycle_id, agent, pair, score, verdict, rationale) "
                    "VALUES %s",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_reasoning_judge_summary(
        self, exchange: str, *, lookback_days: int = 30
    ) -> dict:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, AVG(score) AS mean_score, "
                "SUM(CASE WHEN verdict='actionable' THEN 1 ELSE 0 END) AS actionable, "
                "SUM(CASE WHEN verdict='generic' THEN 1 ELSE 0 END) AS generic, "
                "SUM(CASE WHEN verdict='confused' THEN 1 ELSE 0 END) AS confused "
                "FROM reasoning_judge "
                "WHERE exchange = %s "
                "AND judged_at >= now() - (%s || ' days')::interval",
                (exchange, str(int(lookback_days))),
            ).fetchone()
        return dict(row) if row else {"n": 0}

    # ─── l2_snapshots ─────────────────────────────────────────────────────

    def write_l2_snapshot(self, *, exchange: str, symbol: str, snap: dict) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO l2_snapshots "
                    "(exchange, symbol, cycle_id, mid, spread_bps, "
                    "bid_depth_5, ask_depth_5, obi, levels_json) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        exchange, symbol, snap.get("cycle_id"),
                        _f(snap.get("mid")),
                        _f(snap.get("spread_bps")),
                        _f(snap.get("bid_depth_5")),
                        _f(snap.get("ask_depth_5")),
                        _f(snap.get("obi")),
                        json.dumps({
                            "bids": (snap.get("bids") or [])[:10],
                            "asks": (snap.get("asks") or [])[:10],
                        }),
                    ),
                )
            conn.commit()
        return 1

    # ─── onchain_signals ──────────────────────────────────────────────────

    def upsert_onchain(self, exchange: str, rows: Iterable[dict]) -> int:
        tup: list[tuple] = []
        for r in rows:
            ts = _to_iso(r.get("ts"))
            if ts is None:
                continue
            try:
                tup.append((
                    exchange, str(r["asset"]), str(r["metric"]),
                    ts, float(r["value"]),
                    str(r.get("source") or "unknown"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if not tup:
            return 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO onchain_signals "
                    "(exchange, asset, metric, ts, value, source) VALUES %s "
                    "ON CONFLICT (exchange, asset, metric, ts, source) "
                    "DO NOTHING",
                    tup,
                    page_size=200,
                )
                n = cur.rowcount
            conn.commit()
        return max(n, 0)

    def get_recent_onchain(
        self, exchange: str, asset: str, metric: str, *, limit: int = 100
    ) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT ts, value, source FROM onchain_signals "
                "WHERE exchange = %s AND asset = %s AND metric = %s "
                "ORDER BY ts DESC LIMIT %s",
                (exchange, asset, metric, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── shadow_decisions ─────────────────────────────────────────────────

    def write_shadow_decision(self, exchange: str, row: dict) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO shadow_decisions "
                    "(exchange, cycle_id, variant, pair, action, confidence, "
                    "live_action, live_confidence, diff_action, reasoning) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        exchange, row.get("cycle_id"),
                        str(row.get("variant") or "default"),
                        str(row.get("pair") or ""),
                        row.get("action"),
                        _f(row.get("confidence")),
                        row.get("live_action"),
                        _f(row.get("live_confidence")),
                        bool(row.get("diff_action", False)),
                        str(row.get("reasoning") or "")[:4000],
                    ),
                )
            conn.commit()

    def get_shadow_summary(
        self, exchange: str, *, lookback_days: int = 14
    ) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT variant, COUNT(*) AS n, "
                "AVG(CASE WHEN diff_action THEN 1.0 ELSE 0.0 END) AS diff_rate, "
                "AVG(confidence) AS mean_conf "
                "FROM shadow_decisions "
                "WHERE exchange = %s "
                "AND ts >= now() - (%s || ' days')::interval "
                "GROUP BY variant ORDER BY n DESC",
                (exchange, str(int(lookback_days))),
            ).fetchall()
        return [dict(r) for r in rows]


def _f(v: Any) -> Optional[float]:
    """Coerce to float-or-None without raising."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
