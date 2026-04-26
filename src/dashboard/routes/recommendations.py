"""Backtest recommendations dashboard routes.

Surfaces the ``backtest_recommendations`` rows produced by
``run_nightly_backtests`` so the operator has an actionable inbox of
parameter changes to review. All mutations are domain-isolated by
exchange and require an authenticated session.

Recommendations are NEVER auto-applied. ``approved`` is purely a signal
the operator endorses the change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.recommendations")

router = APIRouter(tags=["Recommendations"])


def _profile_to_exchange(profile: str) -> str:
    cfg = deps.get_config_for_profile(profile)
    return (cfg.get("trading", {}).get("exchange") or profile or "").lower()


def _serialise(row: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _resolve_user(request: Request) -> str:
    """Best-effort caller identity for the audit trail."""
    try:
        sess = getattr(request.state, "session", None) or {}
        for key in ("user", "username", "user_id", "sub"):
            if sess.get(key):
                return str(sess[key])
    except Exception:
        pass
    return "dashboard"


@router.get(
    "/api/recommendations",
    summary="List backtest-derived recommendations",
)
def list_recommendations(
    profile: str = Query(""),
    status: str = Query("", description="pending|approved|rejected|expired"),
    kind: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
):
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")
    try:
        rows = db.list_recommendations(
            exchange=exchange,
            status=status or None,
            kind=kind or None,
            limit=limit,
        )
        counts = db.count_recommendations_by_status(exchange=exchange)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"list_recommendations failed: {e}")
        rows, counts = [], {}
    return {
        "profile": resolved,
        "exchange": exchange,
        "counts": counts,
        "count": len(rows),
        "rows": [_serialise(r) for r in rows],
    }


@router.post(
    "/api/recommendations/{rec_id}/decision",
    summary="Approve or reject a recommendation",
)
def decide_recommendation(
    rec_id: int,
    request: Request,
    profile: str = Query(""),
    body: dict = Body(...),
):
    status = str(body.get("status") or "").strip().lower()
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be approved|rejected")
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    # Domain-isolation guard: confirm the recommendation belongs to this
    # exchange before any mutation.
    existing = db.get_recommendation(rec_id)
    if not existing:
        raise HTTPException(status_code=404, detail="recommendation not found")
    if str(existing.get("exchange") or "").lower() != exchange:
        raise HTTPException(
            status_code=403,
            detail="recommendation belongs to a different exchange",
        )

    try:
        decided_by = _resolve_user(request)
        row = db.decide_recommendation(rec_id, status, decided_by=decided_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"decide_recommendation failed: {e}")
        raise HTTPException(status_code=500, detail=f"internal error: {e}")
    if not row:
        raise HTTPException(status_code=404, detail="recommendation not found")
    return {"recommendation": _serialise(row)}
