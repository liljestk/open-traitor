"""
LLM Optimizer — Hot-reloadable tunable settings for LLM call cost/quality control.

Settings are persisted to data/llm_optimizer_settings.json and cached for 30 seconds,
so changes applied via the dashboard take effect within one trading cycle.
Every change is appended to data/llm_optimizer_history.json for analytics.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SETTINGS_PATH = Path("data/llm_optimizer_settings.json")
_HISTORY_PATH = Path("data/llm_optimizer_history.json")

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULTS: dict[str, Any] = {
    "news_max_chars": 1500,
    "strategic_context_max_chars": 800,
    "recent_outcomes_n": 10,
    "strategist_skip_signals": ["neutral", "weak_buy", "weak_sell"],
    "articles_for_analysis": 8,
}

# ── Parameter metadata (for UI) ───────────────────────────────────────────────
PARAM_META: dict[str, dict] = {
    "news_max_chars": {
        "label": "News headline cap (chars)",
        "description": "Maximum characters of news fed to the market analyst. Fewer = cheaper; more = richer context.",
        "min": 200,
        "max": 8000,
        "step": 100,
        "type": "int",
        "impact_category": "news",
        "token_weight": 0.22,  # share of avg analyst prompt that is news
    },
    "strategic_context_max_chars": {
        "label": "Strategic context cap (chars)",
        "description": "Maximum characters of planning context sent to each agent. Trimmed symmetrically.",
        "min": 0,
        "max": 3000,
        "step": 50,
        "type": "int",
        "impact_category": "context",
        "token_weight": 0.12,
    },
    "recent_outcomes_n": {
        "label": "Recent trade outcomes (count)",
        "description": "How many past trade outcomes to include in the strategist prompt. Fewer = cheaper.",
        "min": 0,
        "max": 30,
        "step": 1,
        "type": "int",
        "impact_category": "outcomes",
        "token_weight": 0.10,
    },
    "strategist_skip_signals": {
        "label": "Skip strategist LLM for signals",
        "description": "Signal types to skip the strategist LLM call for (below confidence threshold). More = cheaper but fewer trade proposals.",
        "type": "multiselect",
        "options": ["neutral", "weak_buy", "weak_sell", "buy", "sell"],
        "impact_category": "skip",
    },
    "articles_for_analysis": {
        "label": "Articles fetched for analysis",
        "description": "How many news articles to fetch. Fewer articles → fewer tokens across all cycles.",
        "min": 1,
        "max": 30,
        "step": 1,
        "type": "int",
        "impact_category": "news",
        "token_weight": 0.08,
    },
}

# ── Singleton state ───────────────────────────────────────────────────────────
_lock = threading.RLock()
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 30.0  # seconds


def _load_from_disk() -> dict[str, Any]:
    """Load settings from disk, merging with defaults for any missing keys."""
    try:
        if _SETTINGS_PATH.exists():
            raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            return {**DEFAULTS, **raw}
    except Exception:
        pass
    return dict(DEFAULTS)


def get_settings() -> dict[str, Any]:
    """Return current settings, refreshing from disk if cache has expired."""
    global _cache, _cache_ts
    now = time.monotonic()
    with _lock:
        if not _cache or (now - _cache_ts) > _CACHE_TTL:
            _cache = _load_from_disk()
            _cache_ts = now
        return dict(_cache)


def get(key: str, default: Any = None) -> Any:
    """Convenience: get a single setting value."""
    return get_settings().get(key, DEFAULTS.get(key, default))


def save_settings(new_settings: dict[str, Any], changed_by: str = "dashboard") -> dict[str, Any]:
    """
    Persist new settings to disk and refresh the in-memory cache.
    Returns a dict of {key: (old_value, new_value)} for changed keys.
    """
    global _cache, _cache_ts
    with _lock:
        current = _load_from_disk()
        # Only accept known keys; validate types/ranges
        validated: dict[str, Any] = {}
        errors: list[str] = []
        for key, value in new_settings.items():
            if key not in DEFAULTS:
                errors.append(f"Unknown key: {key}")
                continue
            meta = PARAM_META.get(key, {})
            if meta.get("type") == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    errors.append(f"{key}: expected int")
                    continue
                mn, mx = meta.get("min", 0), meta.get("max", 999999)
                if not (mn <= value <= mx):
                    errors.append(f"{key}: must be between {mn} and {mx}")
                    continue
            elif meta.get("type") == "multiselect":
                if not isinstance(value, list):
                    errors.append(f"{key}: expected list")
                    continue
                allowed = set(meta.get("options", []))
                bad = [v for v in value if v not in allowed]
                if bad:
                    errors.append(f"{key}: invalid options {bad}")
                    continue
            validated[key] = value

        if errors:
            raise ValueError("; ".join(errors))

        # Compute diff
        changes: dict[str, tuple] = {}
        merged = {**current, **validated}
        for key, new_val in validated.items():
            old_val = current.get(key)
            if old_val != new_val:
                changes[key] = (old_val, new_val)

        # Write to disk (atomic)
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_SETTINGS_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, _SETTINGS_PATH)

        # Flush cache
        _cache = merged
        _cache_ts = time.monotonic()

        # Log history
        if changes:
            _append_history(changes, changed_by, merged)

        return changes


def _append_history(changes: dict[str, tuple], changed_by: str, settings_snapshot: dict) -> None:
    """Append a change record to the history log."""
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            history: list = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            history = []

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "changed_by": changed_by,
            "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()},
            "snapshot": settings_snapshot,
        }
        history.append(entry)
        # Keep last 500 entries
        if len(history) > 500:
            history = history[-500:]

        tmp = str(_HISTORY_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, _HISTORY_PATH)
    except Exception:
        pass  # never crash the caller


def get_history(limit: int = 50) -> list[dict]:
    """Return the most recent change history entries."""
    try:
        if _HISTORY_PATH.exists():
            history = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            return history[-limit:]
    except Exception:
        pass
    return []


def invalidate_cache() -> None:
    """Force next get_settings() call to reload from disk."""
    global _cache_ts
    with _lock:
        _cache_ts = 0.0
