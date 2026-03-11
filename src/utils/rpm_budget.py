"""
RPM Budget Calculator — Computes the maximum safe number of tracked entities
based on the LLM provider's RPM (requests-per-minute) limit and the trading
cycle interval.

This module enforces a hard guardrail: the configured ``max_active_pairs``
can never exceed the RPM-derived maximum, preventing LLM quota exhaustion
and forced fallback to technical-only signals.

Formula
-------
    available_calls_per_cycle = primary_rpm × (interval_seconds / 60)
    overhead_per_cycle        ≈ 2  (screener amortised + rotator + advisor)
    max_safe_entities         = (available_calls_per_cycle - overhead) // 2

The divisor of 2 accounts for the worst case where every entity triggers
both a MarketAnalyst call AND a Strategist call in the same cycle.
"""

from __future__ import annotations

import math
from typing import Any

from src.utils.logger import get_logger

logger = get_logger("utils.rpm_budget")

# Overhead calls per cycle that are NOT per-entity:
#   - LLM screener: ~1 call every 5 cycles → 0.2 amortised
#   - Portfolio rotator: ~1 call per cycle
#   - Settings advisor: ~1 call every 10 cycles → 0.1 amortised
# We round up to 2 for a safe margin.
_OVERHEAD_CALLS_PER_CYCLE: int = 2

# Worst-case LLM calls per entity per cycle:
#   - MarketAnalyst: 1 call (always)
#   - Strategist: 1 call (conditional, but budget for worst case)
_CALLS_PER_ENTITY: int = 2


def compute_rpm_entity_cap(
    llm_providers: list[dict[str, Any]],
    interval_seconds: int,
    *,
    overhead_per_cycle: int = _OVERHEAD_CALLS_PER_CYCLE,
    calls_per_entity: int = _CALLS_PER_ENTITY,
) -> tuple[int, dict[str, Any]]:
    """Return the maximum number of entities the RPM budget allows.

    Parameters
    ----------
    llm_providers:
        The ``llm_providers`` list from ``settings.yaml``.
    interval_seconds:
        The trading cycle interval in seconds (``trading.interval``).
    overhead_per_cycle:
        Fixed non-entity LLM calls per cycle.
    calls_per_entity:
        Worst-case LLM calls per entity per cycle.

    Returns
    -------
    (max_entities, breakdown)
        *max_entities* is the hard cap (minimum 1).
        *breakdown* is a dict for logging / diagnostics.
    """
    primary = _find_primary_provider(llm_providers)

    if primary is None:
        # No cloud provider with an API key — local-only (Ollama).
        # Local providers have no RPM limits; return a generous default.
        return 30, {
            "provider": "local-only",
            "rpm": 0,
            "interval": interval_seconds,
            "available_per_cycle": None,
            "overhead": overhead_per_cycle,
            "entity_budget": None,
            "max_entities": 30,
            "note": "No cloud RPM limit — local models only, cap set to schema max.",
        }

    rpm = int(primary.get("rpm_limit", 0))
    if rpm <= 0:
        # Provider exists but has no RPM limit configured → uncapped.
        return 30, {
            "provider": primary.get("name", "unknown"),
            "rpm": 0,
            "interval": interval_seconds,
            "available_per_cycle": None,
            "overhead": overhead_per_cycle,
            "entity_budget": None,
            "max_entities": 30,
            "note": "Primary provider has no RPM limit configured.",
        }

    # Budget math
    available_per_cycle = rpm * (interval_seconds / 60.0)
    entity_budget = available_per_cycle - overhead_per_cycle
    max_entities = max(1, math.floor(entity_budget / calls_per_entity))

    breakdown = {
        "provider": primary.get("name", "unknown"),
        "model": primary.get("model", "unknown"),
        "tier": primary.get("tier", "unknown"),
        "rpm": rpm,
        "interval": interval_seconds,
        "available_per_cycle": round(available_per_cycle, 1),
        "overhead": overhead_per_cycle,
        "entity_budget": round(entity_budget, 1),
        "calls_per_entity": calls_per_entity,
        "max_entities": max_entities,
    }

    return max_entities, breakdown


def _find_primary_provider(
    providers_config: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Identify the primary (first enabled, non-local, API-key-present) provider.

    This mirrors the priority order used by ``build_providers`` in ``llm_client.py``:
    iterate the config list in order; the first cloud provider whose API key env var
    resolves to a non-empty value wins.
    """
    import os

    for pc in providers_config:
        if not pc.get("enabled", True):
            continue
        if pc.get("is_local", False):
            continue

        api_key_env = pc.get("api_key_env", "")
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not api_key:
            continue

        return pc

    return None
