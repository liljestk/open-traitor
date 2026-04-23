"""
Quant observability dashboard routes (Phase 8).

Exposes read-only endpoints for the Phase 1-7 substrate:

  • GET /api/quant/allocator?profile=...   — capital weights per strategy
  • GET /api/quant/edges?profile=...       — signal edge leaderboard
  • GET /api/quant/healing?profile=...     — strategy health + tier heartbeats
  • GET /api/quant/promotions?profile=...  — recent WFO promotion log

All endpoints degrade gracefully: when the underlying singleton has not been
wired into ``deps`` yet (Phases 4/7 still pending orchestrator integration),
they return a structured stub with ``available: false`` so the frontend can
render an empty-state panel instead of erroring.

Domain separation: every endpoint takes a ``profile`` query param and uses
it to scope file paths (e.g. ``data/<profile>/audit/wfo_promotions.jsonl``).
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


def _safe_call(fn, default):
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
    profile = deps.resolve_profile(profile) if hasattr(deps, "resolve_profile") else profile
    allocator = getattr(deps, "capital_allocator", None)
    if allocator is None:
        return {"available": False, "profile": profile, "weights": {}, "state": None}

    weights = _safe_call(allocator.weights, {})
    state = getattr(allocator, "state", None)
    return {
        "available": True,
        "profile": profile,
        "weights": weights,
        "state": {
            "cumulative_pnl": getattr(state, "cumulative_pnl", {}) if state else {},
            "sample_count": getattr(state, "sample_count", 0) if state else 0,
            "last_updated": getattr(state, "last_updated", 0.0) if state else 0.0,
        },
    }


# --------------------------------------------------------------------- #
# Signal edge leaderboard
# --------------------------------------------------------------------- #

@router.get("/api/quant/edges", summary="Signal edge leaderboard")
def get_signal_edges(
    profile: str = Query("coinbase"),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    profile = deps.resolve_profile(profile) if hasattr(deps, "resolve_profile") else profile
    library = getattr(deps, "signal_edge_library", None)
    if library is None:
        return {"available": False, "profile": profile, "edges": []}

    # Library API: assume `.leaderboard(limit) -> list[dict]` or similar.
    edges = _safe_call(
        lambda: library.leaderboard(limit) if hasattr(library, "leaderboard") else [],
        [],
    )
    return {"available": True, "profile": profile, "edges": list(edges)}


# --------------------------------------------------------------------- #
# Self-healing status
# --------------------------------------------------------------------- #

@router.get("/api/quant/healing", summary="Self-healing controller status")
def get_healing_status(profile: str = Query("coinbase")) -> dict:
    profile = deps.resolve_profile(profile) if hasattr(deps, "resolve_profile") else profile
    healer = getattr(deps, "self_healing", None)
    if healer is None:
        return {
            "available": False,
            "profile": profile,
            "tiers": {},
            "strategies": [],
        }

    tiers = _safe_call(healer.tier_status, {})
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
    return {
        "available": True,
        "profile": profile,
        "tiers": tiers,
        "strategies": strategies,
    }


# --------------------------------------------------------------------- #
# WFO promotion history (file-based; works even without singleton)
# --------------------------------------------------------------------- #

@router.get("/api/quant/promotions", summary="Recent WFO promotion decisions")
def get_promotions(
    profile: str = Query("coinbase"),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    profile = deps.resolve_profile(profile) if hasattr(deps, "resolve_profile") else profile
    audit = Path("data") / profile / "audit" / "wfo_promotions.jsonl"
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

    return {
        "available": True,
        "profile": profile,
        "promotions": items,
        "count": len(items),
    }
