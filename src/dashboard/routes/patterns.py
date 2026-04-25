"""Catalyst Pattern Engine routes.

Exposes upcoming catalysts + historical analog summaries for the
dashboard PatternsPage. All queries are filtered by the resolved
exchange so equity (ibkr) and crypto (coinbase) data never mix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.patterns")

router = APIRouter(tags=["Patterns"])


def _profile_to_exchange(profile: str) -> str:
    cfg = deps.get_config_for_profile(profile)
    return (cfg.get("trading", {}).get("exchange") or profile or "").lower()


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _serialise_event(ev: dict) -> dict:
    out = {k: _iso(v) for k, v in ev.items()}
    return out


@router.get(
    "/api/patterns/upcoming",
    summary="Upcoming catalysts + pattern-engine summary",
)
def get_upcoming_patterns(
    profile: str = Query("", description="Exchange profile (e.g. 'coinbase', 'ibkr')"),
    horizon_days: int = Query(30, ge=1, le=365),
    granularity: str = Query("ONE_DAY"),
    k: int = Query(20, ge=1, le=100),
    limit: int = Query(25, ge=1, le=200),
):
    """Return upcoming catalyst events for symbols in the active universe
    along with a pattern-engine outcome summary derived from historical
    analogs (no LLM call). Results are domain-isolated by exchange."""
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    # Build the symbol set from configured pairs ∪ followed pairs.
    cfg = deps.get_config_for_profile(resolved)
    pairs: list[str] = list(cfg.get("trading", {}).get("pairs", []) or [])
    try:
        followed = db.get_followed_pairs_set(exchange=exchange) or set()
        for sym in followed:
            if sym and sym not in pairs:
                pairs.append(sym)
    except Exception as e:
        logger.debug(f"get_followed_pairs_set failed: {e}")

    # Lazy-import the engine so the dashboard process doesn't load numpy
    # at startup unless this route is hit.
    from src.analysis.pattern_engine import predict_for_upcoming

    rows: list[dict] = []
    for sym in pairs:
        try:
            upcoming = db.get_upcoming_catalysts(
                exchange=exchange, horizon_days=horizon_days, symbol=sym
            )
        except Exception as e:
            logger.debug(f"get_upcoming_catalysts({sym}) failed: {e}")
            continue
        if not upcoming:
            continue
        next_event = upcoming[0]
        anchor_ts = next_event["event_ts"]
        try:
            outcome = predict_for_upcoming(
                db=db,
                exchange=exchange,
                symbol=sym,
                upcoming_event_ts=anchor_ts,
                event_type=next_event["event_type"],
                granularity=granularity,
                sentiment_score=None,
                k=k,
            )
            outcome_payload = {
                "direction": outcome.direction,
                "expected_drift": outcome.expected_drift,
                "dispersion": outcome.dispersion,
                "n_matches": outcome.n_matches,
                "confidence": outcome.confidence,
            }
        except Exception as e:
            logger.debug(f"predict_for_upcoming({sym}) failed: {e}")
            outcome_payload = {
                "direction": "neutral",
                "expected_drift": {},
                "dispersion": {},
                "n_matches": 0,
                "confidence": 0.0,
                "error": str(e)[:200],
            }
        rows.append({
            "symbol": sym,
            "exchange": exchange,
            "upcoming_event": _serialise_event(next_event),
            "outcome": outcome_payload,
        })

    rows.sort(key=lambda r: r["upcoming_event"].get("event_ts") or "")
    return {
        "profile": resolved,
        "exchange": exchange,
        "horizon_days": horizon_days,
        "count": len(rows[:limit]),
        "items": rows[:limit],
    }


@router.get(
    "/api/patterns/{event_id}/matches",
    summary="Top historical analogs for a catalyst event",
)
def get_event_matches(
    event_id: str,
    profile: str = Query("", description="Exchange profile"),
    granularity: str = Query("ONE_DAY"),
    k: int = Query(20, ge=1, le=100),
):
    """Return top-k historical pattern matches for a given upcoming
    catalyst ``event_id``. Domain-isolated by exchange."""
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    try:
        events = db.get_catalyst_events(exchange=exchange, event_id=event_id)
    except Exception as e:
        logger.debug(f"get_catalyst_events({event_id}) failed: {e}")
        raise HTTPException(status_code=500, detail="catalyst lookup failed") from e
    if not events:
        raise HTTPException(status_code=404, detail="event not found")
    event = events[0]
    symbol = event.get("symbol")
    if not symbol or symbol == "_MACRO_":
        raise HTTPException(status_code=400, detail="event has no per-symbol context")

    from src.analysis.pattern_engine import predict_for_upcoming

    try:
        outcome = predict_for_upcoming(
            db=db,
            exchange=exchange,
            symbol=symbol,
            upcoming_event_ts=event["event_ts"],
            event_type=event["event_type"],
            granularity=granularity,
            sentiment_score=None,
            k=k,
        )
    except Exception as e:
        logger.debug(f"predict_for_upcoming({symbol}, {event_id}) failed: {e}")
        raise HTTPException(status_code=500, detail="pattern engine failed") from e

    return {
        "profile": resolved,
        "exchange": exchange,
        "event": _serialise_event(event),
        "outcome": {
            "direction": outcome.direction,
            "expected_drift": outcome.expected_drift,
            "dispersion": outcome.dispersion,
            "n_matches": outcome.n_matches,
            "confidence": outcome.confidence,
            "matches": outcome.matches,
        },
    }


@router.get(
    "/api/patterns/status",
    summary="Pattern engine readiness — table counts + recent backfill progress",
)
def get_pattern_engine_status(
    profile: str = Query("", description="Exchange profile"),
):
    """Cheap snapshot used by the dashboard PatternsPage to communicate
    whether the engine is warm (has data + fingerprints), still
    backfilling, or completely empty."""
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    # Counts (per-exchange).
    counts: dict[str, int] = {
        "catalyst_events": 0,
        "pattern_fingerprints": 0,
        "historical_candles_symbols": 0,
        "backfill_progress_rows": 0,
    }
    try:
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM catalyst_events WHERE exchange = %s",
                (exchange,),
            ).fetchone()
            counts["catalyst_events"] = int(row["n"] or 0) if row else 0
    except Exception as e:
        logger.debug(f"catalyst_events count failed: {e}")
    try:
        counts["pattern_fingerprints"] = db.count_fingerprints(exchange=exchange)
    except Exception as e:
        logger.debug(f"count_fingerprints failed: {e}")
    try:
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS n FROM historical_candles WHERE exchange = %s",
                (exchange,),
            ).fetchone()
            counts["historical_candles_symbols"] = int(row["n"] or 0) if row else 0
    except Exception as e:
        logger.debug(f"historical_candles count failed: {e}")

    # Recent backfill progress rows (latest 25 by run time).
    progress: list[dict] = []
    try:
        rows = db.get_backfill_progress(exchange=exchange) or []
        # Sort newest-run-first for the dashboard view.
        rows.sort(
            key=lambda r: (r.get("last_run_at") or datetime.min).isoformat()
            if isinstance(r.get("last_run_at"), datetime)
            else "",
            reverse=True,
        )
        for r in rows[:25]:
            progress.append({k: _iso(v) for k, v in r.items()})
        counts["backfill_progress_rows"] = len(rows)
    except Exception as e:
        logger.debug(f"get_backfill_progress failed: {e}")

    ready = (
        counts["pattern_fingerprints"] > 0
        and counts["catalyst_events"] > 0
        and counts["historical_candles_symbols"] > 0
    )
    return {
        "profile": resolved,
        "exchange": exchange,
        "ready": ready,
        "counts": counts,
        "recent_backfills": progress,
    }
