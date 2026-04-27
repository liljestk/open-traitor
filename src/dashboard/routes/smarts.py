"""Smarts dashboard routes (Phases 1-8).

Read-only views of the new ``smarts.*`` tables. All endpoints are
domain-isolated by the ``profile`` query parameter.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.smarts")

router = APIRouter(tags=["Smarts"])


def _profile_to_exchange(profile: str) -> str:
    cfg = deps.get_config_for_profile(profile)
    return (cfg.get("trading", {}).get("exchange") or profile or "").lower()


def _resolve(profile: str) -> tuple[Any, str]:
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exch = _profile_to_exchange(resolved)
    if not exch:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")
    return db, exch


def _clean(v: Any) -> Any:
    if isinstance(v, float):
        return None if not math.isfinite(v) else v
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _query(db, sql: str, params: tuple) -> list[dict]:
    try:
        with db._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall()
        return [_clean(dict(zip(cols, r))) for r in rows]
    except Exception as e:
        logger.warning(f"smarts query failed: {e}")
        return []


@router.get("/api/smarts/feature-brier")
def feature_brier(
    profile: str = Query(...),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT feature_name,
               COUNT(*) AS samples,
               AVG(brier) AS brier_score,
               AVG(confidence) AS avg_confidence,
               MAX(ts) AS last_seen
        FROM feature_attribution
        WHERE exchange = %s AND brier IS NOT NULL
        GROUP BY feature_name
        ORDER BY brier_score ASC
        LIMIT %s
        """,
        (exch, limit),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/bandit")
def bandit_state(profile: str = Query(...)) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT regime, strategy, alpha, beta,
               n_pulls AS samples, last_update
        FROM bandit_state
        WHERE exchange = %s
        ORDER BY regime, strategy
        """,
        (exch,),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/counterfactual")
def counterfactual(
    profile: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT cycle_id, pair, ts AS replayed_at,
               actual_action, replay_action,
               actual_conf  AS original_confidence,
               replay_conf  AS replay_confidence,
               actual_pnl_pct, replay_pnl_pct,
               (actual_action = replay_action) AS agreed
        FROM counterfactual_replays
        WHERE exchange = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (exch, limit),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/lead-lag/{follower}")
def lead_lag_for(
    follower: str,
    profile: str = Query(...),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT leader, follower, lag_minutes, beta, t_stat,
               r_squared, sample_count AS samples, computed_at
        FROM lead_lag_matrix
        WHERE exchange = %s AND follower = %s
        ORDER BY ABS(t_stat) DESC
        """,
        (exch, follower),
    )
    return {"profile": profile, "exchange": exch, "follower": follower, "rows": rows}


@router.get("/api/smarts/upcoming-events")
def upcoming_events(
    profile: str = Query(...),
    within_hours: int = Query(168, ge=1, le=720),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT id, event_type, symbol AS asset,
               event_ts AS scheduled_at,
               importance AS severity, source, title, metadata_json
        FROM upcoming_events
        WHERE exchange = %s
          AND event_ts BETWEEN NOW() AND NOW() + (%s || ' hours')::INTERVAL
        ORDER BY event_ts ASC
        """,
        (exch, within_hours),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/decision-drift")
def decision_drift(
    profile: str = Query(...),
    limit: int = Query(60, ge=1, le=400),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT snapshot_date, agent, n_decisions, mean_conf,
               p10_conf, p50_conf, p90_conf,
               baseline_mean, baseline_std, z_score, alert, computed_at
        FROM decision_drift
        WHERE exchange = %s
        ORDER BY computed_at DESC
        LIMIT %s
        """,
        (exch, limit),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/reasoning-judge")
def reasoning_judge(
    profile: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT cycle_id, agent, pair, judged_at, verdict, score, rationale
        FROM reasoning_judge
        WHERE exchange = %s
        ORDER BY judged_at DESC
        LIMIT %s
        """,
        (exch, limit),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/onchain/{asset}/{metric}")
def onchain(
    asset: str,
    metric: str,
    profile: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT asset, metric, value, ts AS observed_at, source
        FROM onchain_signals
        WHERE exchange = %s AND asset = %s AND metric = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (exch, asset.upper(), metric),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/shadow")
def shadow(
    profile: str = Query(...),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT cycle_id, variant, pair, action, confidence,
               live_action, live_confidence, diff_action,
               ts AS decided_at, reasoning
        FROM shadow_decisions
        WHERE exchange = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (exch, limit),
    )
    return {"profile": profile, "exchange": exch, "rows": rows}


@router.get("/api/smarts/l2-snapshots/{symbol}")
def l2_snapshots(
    symbol: str,
    profile: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    db, exch = _resolve(profile)
    rows = _query(
        db,
        """
        SELECT cycle_id, symbol, ts, mid, spread_bps,
               bid_depth_5, ask_depth_5, obi
        FROM l2_snapshots
        WHERE exchange = %s AND symbol = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (exch, symbol, limit),
    )
    return {"profile": profile, "exchange": exch, "symbol": symbol, "rows": rows}
