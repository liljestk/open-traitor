"""Cross-asset analytics dashboard routes.

Surfaces the rows persisted by ``CrossAssetAnalyticsWorkflow``:
* asset taxonomy (per-symbol classification)
* pairwise correlation matrix + lead-lag scores
* cluster snapshot
* cross-event regressions ("driver event → target reaction")
* live cascade preview ("if I pick this driver event, what does the
  current cluster predict for related symbols?")

All endpoints are domain-isolated by exchange.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.cross_asset")

router = APIRouter(tags=["CrossAsset"])


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


@router.get(
    "/api/cross-asset/taxonomy",
    summary="Per-symbol asset taxonomy (asset_class, ecosystem, sector, tags)",
)
def list_taxonomy(
    profile: str = Query("", description="Exchange profile"),
    symbol: str = Query("", description="Filter by symbol"),
    ecosystem: str = Query("", description="Filter by ecosystem"),
    sector: str = Query("", description="Filter by sector"),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_asset_taxonomy(
        exchange=exchange,
        symbol=symbol or None,
        ecosystem=ecosystem or None,
        sector=sector or None,
    )
    return {"exchange": exchange, "rows": [_clean(r) for r in rows]}


@router.get(
    "/api/cross-asset/correlations",
    summary="Pairwise correlation matrix + lead-lag scores",
)
def list_correlations(
    profile: str = Query("", description="Exchange profile"),
    symbol: str = Query("", description="Filter to pairs containing this symbol"),
    window_days: int | None = Query(None, ge=10, le=720),
    min_abs_pearson: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(500, ge=1, le=5000),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_asset_correlations(
        exchange=exchange,
        symbol=symbol or None,
        window_days=window_days,
        min_abs_pearson=min_abs_pearson,
        limit=limit,
    )
    return {"exchange": exchange, "rows": [_clean(r) for r in rows]}


@router.get(
    "/api/cross-asset/clusters",
    summary="Latest cluster snapshot (members + cohesion + label)",
)
def list_clusters(
    profile: str = Query("", description="Exchange profile"),
    symbol: str = Query("", description="If set, return only the cluster(s) "
                                        "containing this symbol"),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_asset_clusters(exchange=exchange, symbol=symbol or None)
    # Group by cluster_id for the frontend.
    by_cluster: dict[int, dict] = {}
    for r in rows:
        cid = int(r["cluster_id"])
        bucket = by_cluster.setdefault(cid, {
            "cluster_id": cid,
            "label": r.get("label"),
            "cohesion": _clean(r.get("cohesion")),
            "computed_at": _clean(r.get("computed_at")),
            "members": [],
        })
        bucket["members"].append(r["symbol"])
    clusters = sorted(
        by_cluster.values(),
        key=lambda c: (-len(c["members"]), c["cluster_id"]),
    )
    return {"exchange": exchange, "clusters": clusters}


@router.get(
    "/api/cross-asset/regressions",
    summary="Cross-event regressions (driver event → target reaction)",
)
def list_cross_event_regressions(
    profile: str = Query("", description="Exchange profile"),
    driver_symbol: str = Query("", description="Filter by driver symbol"),
    target_symbol: str = Query("", description="Filter by target symbol"),
    driver_event_type: str = Query("", description="Filter by event_type"),
    min_samples: int = Query(0, ge=0, le=10_000),
    min_abs_beta: float = Query(0.0, ge=0.0, le=10.0),
    limit: int = Query(200, ge=1, le=2000),
):
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_cross_event_regressions(
        exchange=exchange,
        driver_symbol=driver_symbol or None,
        target_symbol=target_symbol or None,
        driver_event_type=driver_event_type or None,
        min_samples=min_samples,
        min_abs_beta=min_abs_beta,
        limit=limit,
    )
    return {"exchange": exchange, "rows": [_clean(r) for r in rows]}


@router.get(
    "/api/cross-asset/cascade",
    summary="Predicted reactions across related assets for a driver event_type",
)
def cascade(
    profile: str = Query("", description="Exchange profile"),
    driver_symbol: str = Query(..., description="Driver symbol"),
    driver_event_type: str = Query(..., description="Event type on the driver"),
    horizon_days: int = Query(5, ge=1, le=60),
    min_r2: float = Query(0.0, ge=0.0, le=1.0),
    min_samples: int = Query(0, ge=0, le=10_000),
):
    """Return the per-target predicted reactions for a chosen driver event.

    This is the read-only cascade preview the operator uses to sanity-check
    "if AAPL has earnings tomorrow, what does the model expect for MSFT,
    GOOG, …" without running anything.
    """
    db, exchange = _resolve_db_exchange(profile)
    rows = db.get_cross_event_regressions(
        exchange=exchange,
        driver_symbol=driver_symbol,
        driver_event_type=driver_event_type,
        min_samples=min_samples,
        limit=1000,
    )
    out: list[dict] = []
    for r in rows:
        if int(r["horizon_days"]) != int(horizon_days):
            continue
        r2 = r.get("r_squared") or 0.0
        if r2 < min_r2:
            continue
        beta = r.get("beta") or 0.0
        out.append({
            "target_symbol": r["target_symbol"],
            "horizon_days": int(r["horizon_days"]),
            "beta": _clean(beta),
            "r_squared": _clean(r2),
            "expected_drift": _clean(float(beta) * float(r2)),
            "sample_count": int(r.get("sample_count") or 0),
            "hit_rate": _clean(r.get("hit_rate")),
            "mean_forward_return": _clean(r.get("mean_forward_return")),
        })
    out.sort(key=lambda x: abs(x.get("expected_drift") or 0.0), reverse=True)
    return {
        "exchange": exchange,
        "driver_symbol": driver_symbol,
        "driver_event_type": driver_event_type,
        "horizon_days": horizon_days,
        "predictions": out,
    }
