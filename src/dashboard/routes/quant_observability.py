"""
Quant observability dashboard routes (Phase 8).

Read-only views over the Phase 1-7 substrate, **dual-domain**:

  • GET /api/quant/allocator?profile=coinbase|ibkr     capital weights
  • GET /api/quant/edges?profile=coinbase|ibkr&regime  signal edge leaderboard
  • GET /api/quant/healing?profile=coinbase|ibkr       strategy + tier health
  • GET /api/quant/promotions?profile=coinbase|ibkr    WFO promotion log

Each call resolves a per-profile singleton via deps.get_*_for(profile),
so coinbase (crypto) and ibkr (equity) state never mix. File reads are
scoped to ``data/<profile>/...``. Endpoints degrade gracefully when a
substrate is unavailable (returns ``available: false``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.routes.quant_observability")

router = APIRouter(tags=["QuantOps"])


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:  # pragma: no cover
        logger.warning(f"quant_observability call failed: {e}")
        return default


# --------------------------------------------------------------------- #
# Capital allocator
# --------------------------------------------------------------------- #

@router.get("/api/quant/allocator", summary="Capital allocator weights")
def get_allocator_weights(profile: str = Query("coinbase")) -> dict:
    p = deps.resolve_profile(profile) or profile
    # Prefer per-profile factory; fall back to legacy single slot for tests
    # that monkey-patch deps.capital_allocator directly.
    allocator = deps.capital_allocator or deps.get_capital_allocator_for(p)
    if allocator is None:
        return {"available": False, "profile": p, "weights": {}, "state": None}

    weights = _safe(allocator.weights, {})
    state = getattr(allocator, "state", None)
    return {
        "available": True,
        "profile": p,
        "weights": weights,
        "state": {
            "cumulative_pnl": getattr(state, "cumulative_pnl", {}) if state else {},
            "sample_count": getattr(state, "sample_count", 0) if state else 0,
            "last_updated": getattr(state, "last_updated", 0.0) if state else 0.0,
        },
    }


# --------------------------------------------------------------------- #
# Signal-edge leaderboard
# --------------------------------------------------------------------- #

@router.get("/api/quant/edges", summary="Signal edge leaderboard")
def get_signal_edges(
    profile: str = Query("coinbase"),
    regime: str = Query("ranging"),
    limit: int = Query(20, ge=1, le=200),
    lookback_days: int = Query(30, ge=1, le=365),
) -> dict:
    p = deps.resolve_profile(profile) or profile
    library = deps.signal_edge_library or deps.get_signal_edge_library_for(p)
    if library is None:
        return {"available": False, "profile": p, "regime": regime, "edges": []}

    # Real SignalEdgeLibrary exposes .all_edges(regime, lookback_days) -> list[EdgeStats].
    # Test stubs may expose .leaderboard(limit) -> list[dict]; honour both.
    if hasattr(library, "all_edges"):
        try:
            stats = library.all_edges(regime, lookback_days)
        except TypeError:
            stats = _safe(library.all_edges, [])
        edges = [
            (s.to_dict() if hasattr(s, "to_dict") else dict(s))
            for s in (stats or [])
        ]
        edges.sort(key=lambda e: e.get("sharpe", 0.0), reverse=True)
        edges = edges[:limit]
    elif hasattr(library, "leaderboard"):
        edges = list(_safe(lambda: library.leaderboard(limit), []))
    else:
        edges = []
    return {"available": True, "profile": p, "regime": regime, "edges": edges}


# --------------------------------------------------------------------- #
# Self-healing controller status
# --------------------------------------------------------------------- #

@router.get("/api/quant/healing", summary="Self-healing controller status")
def get_healing_status(profile: str = Query("coinbase")) -> dict:
    p = deps.resolve_profile(profile) or profile
    healer = deps.self_healing or deps.get_self_healing_for(p)
    if healer is None:
        return {"available": False, "profile": p, "tiers": {}, "strategies": []}

    tiers = _safe(healer.tier_status, {})
    strategies = []
    for name, h in getattr(healer, "_strategies", {}).items():
        strategies.append({
            "name": name,
            "disabled": healer.is_disabled(name),
            "disabled_until": h.disabled_until,
            "last_event": h.last_event,
            "last_event_at": h.last_event_at,
            "recent_sharpes": list(h.recent_sharpes)[-10:],
        })
    return {"available": True, "profile": p, "tiers": tiers, "strategies": strategies}


# --------------------------------------------------------------------- #
# WFO promotion history (file-based — works even without a singleton)
# --------------------------------------------------------------------- #

@router.get("/api/quant/promotions", summary="Recent WFO promotion decisions")
def get_promotions(
    profile: str = Query("coinbase"),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    p = deps.resolve_profile(profile) or profile
    audit = Path("data") / p / "audit" / "wfo_promotions.jsonl"
    items: list[dict[str, Any]] = []
    if audit.exists():
        try:
            with audit.open() as f:
                lines = f.readlines()[-limit:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as e:  # pragma: no cover
            logger.warning(f"failed to read promotions audit: {e}")

    return {"available": True, "profile": p, "promotions": items, "count": len(items)}
