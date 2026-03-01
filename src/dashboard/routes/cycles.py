from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.dashboard import deps

router = APIRouter(tags=["Cycles"])


@router.get("/api/cycles", summary="List trading cycles (Cycle Explorer)")
def list_cycles(
    pair: Optional[str] = Query(None, description="Filter by pair e.g. BTC-USD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """
    Returns a paginated list of trading cycles with outcome summary.
    Each item represents one unique `cycle_id` across all agent spans.
    """
    qc = deps.quote_currency_for(profile)
    exchange = deps.resolve_profile(profile) or None
    cycles = db.get_cycles(pair=pair, limit=limit, offset=offset, quote_currency=qc, exchange=exchange)
    for c in cycles:
        c["langfuse_url"] = deps.langfuse_url(c.get("langfuse_trace_id"))
        # Compute wall-clock duration from first→last agent span timestamps
        try:
            if c.get("started_at") and c.get("finished_at"):
                _s = datetime.fromisoformat(c["started_at"])
                _f = datetime.fromisoformat(c["finished_at"])
                c["cycle_duration_ms"] = round((_f - _s).total_seconds() * 1000, 1)
            else:
                c["cycle_duration_ms"] = None
        except Exception:
            c["cycle_duration_ms"] = None
    return {"cycles": cycles, "limit": limit, "offset": offset, "count": len(cycles)}


@router.get("/api/cycles/{cycle_id}", summary="Full span chain for one cycle (Playback)")
def get_cycle(cycle_id: str, db=Depends(deps.get_profile_db)):
    """
    Returns the complete trace: all agent spans with token counts, latency,
    LLM prompt/output, plus the resulting trade (if any).
    Powers the animated Waterfall timeline on the Playback page.
    """
    cycle = db.get_cycle_full(cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail=f"Cycle {cycle_id!r} not found")
    cycle["langfuse_url"] = deps.langfuse_url(cycle.get("langfuse_trace_id"))
    return cycle


@router.get("/api/events", summary="List system events")
def list_events(
    event_type: Optional[str] = Query(None),
    hours: int = Query(24 * 7, ge=1),
    limit: int = Query(500, ge=1, le=5000),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Returns a list of system events/logs from the database."""
    qc = deps.quote_currency_for(profile)
    exchange = deps.resolve_profile(profile) or None
    events = db.get_events(hours=hours, event_type=event_type, limit=limit, quote_currency=qc, exchange=exchange)
    # Parse event data json if possible
    for e in events:
        if isinstance(e.get("data"), str):
            try:
                e["data"] = json.loads(e["data"])
            except Exception:
                pass
    return deps.sanitize_floats({"events": events, "count": len(events)})
