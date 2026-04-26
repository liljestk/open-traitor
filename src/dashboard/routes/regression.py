"""Event–price regression dashboard routes.

Exposes the rows persisted by ``EventRegressionWorkflow`` so the
RegressionAI page can show which event types historically move price
for which symbols. Always domain-isolated by exchange.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.regression")

router = APIRouter(tags=["RegressionAI"])


def _profile_to_exchange(profile: str) -> str:
    cfg = deps.get_config_for_profile(profile)
    return (cfg.get("trading", {}).get("exchange") or profile or "").lower()


def _serialise(row: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


@router.get(
    "/api/regression/models",
    summary="List event–price regression models",
)
def list_regression_models(
    profile: str = Query("", description="Exchange profile (e.g. 'coinbase', 'ibkr')"),
    event_type: str = Query("", description="Filter by event_type"),
    symbol: str = Query("", description="Filter by symbol"),
    min_samples: int = Query(0, ge=0, le=10_000),
    order_by: str = Query("r_squared"),
    limit: int = Query(100, ge=1, le=500),
):
    """List fitted regressions for the active profile, ordered for the dashboard."""
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    try:
        rows = db.get_event_regressions(
            exchange=exchange,
            symbol=symbol or None,
            event_type=event_type or None,
            min_samples=min_samples,
            order_by=order_by,
            limit=limit,
        )
    except Exception as e:
        logger.warning(f"get_event_regressions failed: {e}")
        rows = []

    total = 0
    try:
        total = db.count_event_regressions(exchange=exchange)
    except Exception as e:
        logger.debug(f"count_event_regressions failed: {e}")

    return {
        "profile": resolved,
        "exchange": exchange,
        "total": total,
        "count": len(rows),
        "rows": [_serialise(r) for r in rows],
    }


@router.get(
    "/api/regression/models/{symbol}",
    summary="Regression models for one symbol",
)
def get_regression_models_for_symbol(
    symbol: str,
    profile: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    try:
        rows = db.get_event_regressions(
            exchange=exchange,
            symbol=symbol,
            order_by="computed_at",
            limit=limit,
        )
    except Exception as e:
        logger.warning(f"get_event_regressions({symbol}) failed: {e}")
        rows = []

    return {
        "profile": resolved,
        "exchange": exchange,
        "symbol": symbol,
        "count": len(rows),
        "rows": [_serialise(r) for r in rows],
    }


__all__ = ["router"]
