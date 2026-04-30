"""Quantitative Analytics dashboard routes.

Read-only views of the rows persisted by ``QuantAnalyticsManager``:

* ``/api/quant/factor-loadings``       — per-symbol factor regression rows.
* ``/api/quant/har-rv``                — HAR-RV one-step-ahead vol forecasts.
* ``/api/quant/granger``               — Granger-significant lead-lag edges.
* ``/api/quant/slippage-model``        — current size+vol slippage regression.
* ``/api/quant/correlation-regime``    — universe-wide correlation regime
                                         events + history time series.

All endpoints are exchange-scoped via ``profile``. None mutate state.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.quant_analytics")

router = APIRouter(tags=["QuantAnalytics"])


# ─── Helpers ────────────────────────────────────────────────────────────


def _profile_to_exchange(profile: str) -> str:
    cfg = deps.get_config_for_profile(profile)
    return (cfg.get("trading", {}).get("exchange") or profile or "").lower()


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _resolve_db_exchange(profile: str) -> tuple[Any, str]:
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")
    return db, exchange


# ─── Factor loadings ────────────────────────────────────────────────────


@router.get(
    "/api/quant/factor-loadings",
    summary="Per-symbol multi-factor regression loadings",
)
def list_factor_loadings(
    profile: str = Query("", description="Exchange profile"),
    symbol: str = Query("", description="Filter by symbol"),
    factor: str = Query("", description="Filter by factor"),
    min_abs_t_stat: float = Query(0.0, ge=0.0, description="Filter |t_stat| ≥ this"),
    limit: int = Query(500, ge=1, le=5000),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_market_factor_loadings(
        exchange=exchange,
        symbol=symbol or None,
        factor=factor or None,
        min_abs_t_stat=float(min_abs_t_stat),
        limit=int(limit),
    )
    return {
        "exchange": exchange,
        "rows": [_clean(r) for r in rows],
    }


# ─── HAR-RV forecasts ───────────────────────────────────────────────────


@router.get(
    "/api/quant/har-rv",
    summary="HAR-RV one-step-ahead realised-volatility forecasts",
)
def list_har_rv(
    profile: str = Query(""),
    symbol: str = Query(""),
    horizon_days: int = Query(1, ge=1, le=30),
    limit: int = Query(500, ge=1, le=5000),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_har_rv_forecasts(
        exchange=exchange,
        symbol=symbol or None,
        horizon_days=int(horizon_days),
        limit=int(limit),
    )
    return {
        "exchange": exchange,
        "horizon_days": int(horizon_days),
        "rows": [_clean(r) for r in rows],
    }


# ─── Granger causality ──────────────────────────────────────────────────


@router.get(
    "/api/quant/granger",
    summary="Granger causality lead-lag edges (significant only)",
)
def list_granger(
    profile: str = Query(""),
    leader: str = Query(""),
    follower: str = Query(""),
    max_p_value: float = Query(0.05, ge=0.0, le=1.0),
    limit: int = Query(500, ge=1, le=5000),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_granger_results(
        exchange=exchange,
        leader=leader or None,
        follower=follower or None,
        max_p_value=float(max_p_value),
        limit=int(limit),
    )
    return {
        "exchange": exchange,
        "rows": [_clean(r) for r in rows],
    }


# ─── Slippage impact model ──────────────────────────────────────────────


@router.get(
    "/api/quant/slippage-model",
    summary="Current size + vol slippage impact regression",
)
def get_slippage_model(profile: str = Query("")):
    db, exchange = _resolve_db_exchange(profile)
    row = db.get_slippage_impact_model(exchange=exchange)
    return {"exchange": exchange, "model": _clean(row) if row else None}


# ─── Correlation regime ─────────────────────────────────────────────────


@router.get(
    "/api/quant/correlation-regime",
    summary="Universe-wide correlation regime events + recent history",
)
def get_correlation_regime(
    profile: str = Query(""),
    limit: int = Query(200, ge=1, le=2000),
):
    db, exchange = _resolve_db_exchange(profile)
    events = db.get_correlation_regime_events(exchange=exchange, limit=int(limit))
    # Latest snapshot at the top of the list (DESC). Provide convenience:
    latest = events[0] if events else None
    return {
        "exchange": exchange,
        "latest": _clean(latest) if latest else None,
        "events": [_clean(e) for e in events],
    }
