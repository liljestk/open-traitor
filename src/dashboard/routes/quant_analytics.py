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
from fastapi.responses import PlainTextResponse

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


# ─── Model card + attribution + export ─────────────────────────────────


def _build_model_card_for(profile: str, *, include_attribution: bool = True) -> dict:
    from src.utils.quant_model_card import build_model_card
    from src.utils.quant_attribution import attribute_quant_signals

    db, exchange = _resolve_db_exchange(profile)
    cfg = deps.get_config_for_profile(deps.resolve_profile(profile))
    universe = (
        list(cfg.get("trading", {}).get("pairs") or [])
    )
    attribution = None
    if include_attribution:
        try:
            attribution = attribute_quant_signals(db, exchange, lookback_days=30)
        except Exception:
            attribution = None
    return build_model_card(
        db, exchange, universe=universe, attribution=attribution,
    )


@router.get(
    "/api/quant/model-card",
    summary="Self-describing JSON model card across all 5 quant analyzers",
)
def get_model_card(
    profile: str = Query(""),
    include_attribution: bool = Query(True),
):
    card = _build_model_card_for(
        profile, include_attribution=bool(include_attribution),
    )
    return _clean(card)


@router.get(
    "/api/quant/model-card.md",
    summary="Markdown rendering of the quant model card (printable)",
    response_class=PlainTextResponse,
)
def get_model_card_markdown(
    profile: str = Query(""),
    include_attribution: bool = Query(True),
) -> str:
    from src.utils.quant_model_card import format_markdown
    card = _build_model_card_for(
        profile, include_attribution=bool(include_attribution),
    )
    return format_markdown(card)


@router.get(
    "/api/quant/attribution",
    summary=(
        "Hit-rate / avg-PnL per quant-feature bucket — powers self-learning"
    ),
)
def get_quant_attribution(
    profile: str = Query(""),
    lookback_days: int = Query(30, ge=1, le=365),
):
    from src.utils.quant_attribution import (
        attribute_quant_signals,
        derive_learning_adjustments,
    )
    db, exchange = _resolve_db_exchange(profile)
    attribution = attribute_quant_signals(
        db, exchange, lookback_days=int(lookback_days),
    )
    adjustments = derive_learning_adjustments(attribution)
    return {
        "exchange": exchange,
        "attribution": _clean(attribution),
        "adjustments": _clean(adjustments),
    }


@router.get(
    "/api/quant/export",
    summary="Full quant analytics export bundle (JSON, archivable)",
)
def export_quant_bundle(profile: str = Query("")):
    """One-shot export bundle: model card + raw rows from every quant table.

    Stable format suitable for archival, diffing across deploys, and
    handing to a third party (auditor, reviewer, paper).
    """
    from src.utils.quant_attribution import attribute_quant_signals
    db, exchange = _resolve_db_exchange(profile)
    cfg = deps.get_config_for_profile(deps.resolve_profile(profile))
    universe = list(cfg.get("trading", {}).get("pairs") or [])

    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    attribution = _safe(
        lambda: attribute_quant_signals(db, exchange, lookback_days=30), None,
    )
    from src.utils.quant_model_card import build_model_card
    card = build_model_card(
        db, exchange, universe=universe, attribution=attribution,
    )

    bundle = {
        "exchange": exchange,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "model_card": card,
        "raw": {
            "factor_loadings": _safe(
                lambda: db.get_market_factor_loadings(exchange, limit=5000), [],
            ),
            "har_rv_forecasts": _safe(
                lambda: db.get_har_rv_forecasts(exchange, limit=5000), [],
            ),
            "granger_causality": _safe(
                lambda: db.get_granger_results(exchange, max_p_value=1.0, limit=5000), [],
            ),
            "slippage_impact_model": _safe(
                lambda: db.get_slippage_impact_model(exchange), None,
            ),
            "correlation_regime_events": _safe(
                lambda: db.get_correlation_regime_events(exchange, limit=2000), [],
            ),
            "decision_snapshots_recent": _safe(
                lambda: db.get_quant_decision_snapshots(exchange, limit=2000), [],
            ),
        },
    }
    return _clean(bundle)
