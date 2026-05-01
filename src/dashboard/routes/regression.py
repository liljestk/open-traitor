"""Event–price regression dashboard routes.

Exposes the rows persisted by ``EventRegressionWorkflow`` so the
RegressionAI page can show which event types historically move price
for which symbols. Always domain-isolated by exchange.
"""

from __future__ import annotations

import threading
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


@router.get(
    "/api/regression/coverage",
    summary="Followed-asset regression coverage",
)
def get_regression_coverage(
    profile: str = Query("", description="Exchange profile (e.g. 'coinbase', 'ibkr')"),
):
    """Return follow-vs-modeled counts for the active profile.

    A symbol is considered *modeled* when it has at least one row in
    either ``event_price_regressions`` (excluding the ``_MACRO_``
    placeholder) or ``market_factor_loadings`` for the resolved exchange.
    """
    from src.analysis.regression_coverage import compute_coverage_stats

    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")
    try:
        stats = compute_coverage_stats(db, exchange)
    except Exception as exc:
        logger.warning(f"regression coverage failed: {exc}")
        raise HTTPException(status_code=500, detail="coverage probe failed")
    return {"profile": resolved, **stats}


@router.post(
    "/api/regression/refresh",
    summary="Refresh regressions for the followed-asset universe",
)
def refresh_regression_followed(profile: str = Query("")):
    """Synchronously refresh regressions for every followed symbol.

    Used by the dashboard "Refresh coverage" button and by the on-follow
    hook below. Idempotent. Logs and returns the per-symbol summary.
    """
    from src.analysis.regression_coverage import refresh_regression_for_followed

    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")
    try:
        result = refresh_regression_for_followed(stats_db=db, exchange=exchange)
    except Exception as exc:
        logger.warning(f"refresh regression failed: {exc}")
        raise HTTPException(status_code=500, detail="refresh failed")
    return {"profile": resolved, **result}


def trigger_followed_refresh_in_background(profile: str, symbols: list[str]) -> None:
    """Best-effort background regression refresh for ``symbols``.

    Called from the watchlist follow hook + the universe-scanner LLM
    follow hook so a freshly-followed asset has factor coverage on the
    next dashboard load. Exceptions are swallowed — the trade pipeline
    must never block on regression maintenance.
    """
    if not symbols:
        return

    def _run() -> None:
        try:
            from src.utils.stats import StatsDB
            from src.analysis.regression_coverage import (
                refresh_regression_for_symbols,
            )

            resolved = deps.resolve_profile(profile)
            exchange = _profile_to_exchange(resolved)
            if not exchange:
                logger.debug(
                    f"trigger_followed_refresh: profile {profile!r} -> no exchange"
                )
                return
            db = StatsDB()
            res = refresh_regression_for_symbols(
                stats_db=db,
                exchange=exchange,
                symbols=symbols,
            )
            logger.info(
                f"on-follow regression refresh: exchange={exchange} "
                f"symbols={symbols} factor_rows={res.get('factor_rows')} "
                f"event_models={res.get('event_models')}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"on-follow regression refresh failed: {exc}")

    threading.Thread(
        target=_run,
        name=f"regression-refresh-{','.join(symbols)[:32]}",
        daemon=True,
    ).start()


__all__ = ["router", "trigger_followed_refresh_in_background"]
