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
    "fear_greed_max_chars": 300,
    "multi_timeframe_max_chars": 500,
    "sentiment_max_chars": 300,
    "strategic_context_max_chars": 800,
    "recent_outcomes_n": 10,
    "strategist_skip_signals": ["neutral", "weak_buy", "weak_sell"],
    "articles_for_analysis": 8,
    "analyst_skip_llm_neutral": True,
    "trader_tool_payload_max_chars": 2400,
    "trader_news_excerpt_chars": 220,
    "trader_recent_outcomes_chars": 300,
    "trader_context_excerpt_chars": 300,
    "trader_max_contributors": 4,
    "trader_max_edges": 4,
    "trader_max_positions": 8,
    "trader_retry_on_veto": True,
    "trader_hard_veto_skip_enabled": True,
    "trader_tier3_notional_threshold": 750.0,
    "trader_tier3_portfolio_pct": 0.10,
    "trader_tier3_ambiguous_confidence": 0.70,
    "reasoning_judge_sample_pct": 0.005,
    "reasoning_judge_max_judgments": 15,
    "reasoning_judge_reasoning_max_chars": 1200,
    # ── Adaptive Learning Engine (ALE) ────────────────────────────────────
    "learning_enabled": True,
    "calibration_min_samples": 50,
    "ensemble_max_shift": 0.05,
    "prompt_supplement_max_tokens": 500,
    "wfo_min_wfe": 0.5,
    "finetune_min_examples": 50,
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
    "fear_greed_max_chars": {
        "label": "Fear/Greed cap (chars)",
        "description": "Maximum Fear & Greed text included in analyst prompts.",
        "min": 0,
        "max": 2000,
        "step": 50,
        "type": "int",
        "impact_category": "context",
        "token_weight": 0.03,
    },
    "multi_timeframe_max_chars": {
        "label": "Multi-timeframe cap (chars)",
        "description": "Maximum multi-timeframe confluence text included in analyst prompts.",
        "min": 0,
        "max": 3000,
        "step": 50,
        "type": "int",
        "impact_category": "context",
        "token_weight": 0.08,
    },
    "sentiment_max_chars": {
        "label": "Sentiment cap (chars)",
        "description": "Maximum sentiment summary text included in analyst prompts.",
        "min": 0,
        "max": 2000,
        "step": 50,
        "type": "int",
        "impact_category": "context",
        "token_weight": 0.04,
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
    "analyst_skip_llm_neutral": {
        "label": "Skip neutral analyst LLM",
        "description": "Use deterministic technical-only output for quiet neutral setups with no catalyst or strategy signal.",
        "type": "bool",
        "impact_category": "skip",
    },
    "trader_tool_payload_max_chars": {
        "label": "Trader toolkit cap (chars)",
        "description": "Maximum deterministic toolkit JSON embedded in TraderAgent prompts.",
        "min": 800,
        "max": 6000,
        "step": 100,
        "type": "int",
        "impact_category": "context",
        "token_weight": 0.20,
    },
    "trader_news_excerpt_chars": {
        "label": "Trader news excerpt cap (chars)",
        "description": "Maximum news excerpt included in TraderAgent toolkit snapshots.",
        "min": 0,
        "max": 1000,
        "step": 50,
        "type": "int",
        "impact_category": "news",
        "token_weight": 0.05,
    },
    "trader_recent_outcomes_chars": {
        "label": "Trader outcomes cap (chars)",
        "description": "Maximum recent-outcomes excerpt included in TraderAgent toolkit snapshots.",
        "min": 0,
        "max": 1500,
        "step": 50,
        "type": "int",
        "impact_category": "outcomes",
        "token_weight": 0.08,
    },
    "trader_context_excerpt_chars": {
        "label": "Trader plan context cap (chars)",
        "description": "Maximum strategic-context excerpt included in TraderAgent toolkit snapshots.",
        "min": 0,
        "max": 1500,
        "step": 50,
        "type": "int",
        "impact_category": "context",
        "token_weight": 0.08,
    },
    "trader_max_contributors": {
        "label": "Trader strategy contributors",
        "description": "Maximum deterministic strategy contributors included in TraderAgent prompts.",
        "min": 1,
        "max": 12,
        "step": 1,
        "type": "int",
        "impact_category": "context",
    },
    "trader_max_edges": {
        "label": "Trader edge rows",
        "description": "Maximum edge-library rows included in TraderAgent prompts.",
        "min": 0,
        "max": 12,
        "step": 1,
        "type": "int",
        "impact_category": "context",
    },
    "trader_max_positions": {
        "label": "Trader position rows",
        "description": "Maximum open positions included in TraderAgent portfolio snapshots.",
        "min": 1,
        "max": 30,
        "step": 1,
        "type": "int",
        "impact_category": "context",
    },
    "trader_retry_on_veto": {
        "label": "Trader retry on adjustable veto",
        "description": "Allow one TraderAgent retry only when a deterministic veto can plausibly be fixed by size/stop changes.",
        "type": "bool",
        "impact_category": "skip",
    },
    "trader_hard_veto_skip_enabled": {
        "label": "Skip hard-veto trader calls",
        "description": "Skip TraderAgent LLM calls when deterministic state already forces a hold.",
        "type": "bool",
        "impact_category": "skip",
    },
    "trader_tier3_notional_threshold": {
        "label": "Tier 3 notional threshold",
        "description": "Escalate TraderAgent decisions to the strongest hosted tier when proposed notional meets or exceeds this amount.",
        "min": 0,
        "max": 100000,
        "step": 50,
        "type": "float",
        "impact_category": "routing",
    },
    "trader_tier3_portfolio_pct": {
        "label": "Tier 3 portfolio share",
        "description": "Escalate TraderAgent decisions to the strongest hosted tier when proposed notional reaches this portfolio share.",
        "min": 0,
        "max": 0.50,
        "step": 0.01,
        "type": "float",
        "impact_category": "routing",
    },
    "trader_tier3_ambiguous_confidence": {
        "label": "Tier 3 ambiguity confidence",
        "description": "Signals below this confidence are treated as ambiguous and routed to the strongest hosted tier.",
        "min": 0.50,
        "max": 0.95,
        "step": 0.01,
        "type": "float",
        "impact_category": "routing",
    },
    "reasoning_judge_sample_pct": {
        "label": "Reasoning judge sample rate",
        "description": "Fraction of recent reasoning rows sampled for LLM-as-judge audits.",
        "min": 0.0,
        "max": 0.05,
        "step": 0.001,
        "type": "float",
        "impact_category": "learning",
    },
    "reasoning_judge_max_judgments": {
        "label": "Reasoning judge max rows",
        "description": "Maximum LLM judgments per reasoning-audit run.",
        "min": 0,
        "max": 50,
        "step": 1,
        "type": "int",
        "impact_category": "learning",
    },
    "reasoning_judge_reasoning_max_chars": {
        "label": "Reasoning judge input cap (chars)",
        "description": "Maximum reasoning JSON characters sent to each judge call.",
        "min": 300,
        "max": 2500,
        "step": 100,
        "type": "int",
        "impact_category": "learning",
    },
    # ── Adaptive Learning Engine (ALE) ────────────────────────────────────
    "learning_enabled": {
        "label": "Enable Adaptive Learning",
        "description": "Master switch for the Adaptive Learning Engine. Disabling stops all learning subsystems.",
        "type": "bool",
        "impact_category": "learning",
    },
    "calibration_min_samples": {
        "label": "Calibration min samples",
        "description": "Minimum signal scores required before retraining the confidence calibrator.",
        "min": 20,
        "max": 500,
        "step": 10,
        "type": "int",
        "impact_category": "learning",
    },
    "ensemble_max_shift": {
        "label": "Ensemble max weight shift",
        "description": "Maximum change in strategy weight per update cycle. Prevents sudden swings.",
        "min": 0.01,
        "max": 0.20,
        "step": 0.01,
        "type": "float",
        "impact_category": "learning",
    },
    "prompt_supplement_max_tokens": {
        "label": "Prompt supplement cap (tokens)",
        "description": "Maximum tokens of learned lessons injected into agent prompts.",
        "min": 0,
        "max": 2000,
        "step": 50,
        "type": "int",
        "impact_category": "learning",
        "token_weight": 0.05,
    },
    "wfo_min_wfe": {
        "label": "WFO minimum robustness",
        "description": "Walk-Forward Efficiency threshold. Parameters below this are not promoted.",
        "min": 0.3,
        "max": 0.9,
        "step": 0.05,
        "type": "float",
        "impact_category": "learning",
    },
    "finetune_min_examples": {
        "label": "Fine-tune min examples",
        "description": "Minimum curated examples required before exporting a fine-tuning dataset.",
        "min": 10,
        "max": 500,
        "step": 10,
        "type": "int",
        "impact_category": "learning",
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
            elif meta.get("type") == "bool":
                if isinstance(value, bool):
                    pass
                elif isinstance(value, str) and value.lower() in {"true", "false"}:
                    value = value.lower() == "true"
                else:
                    errors.append(f"{key}: expected bool")
                    continue
            elif meta.get("type") == "float":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    errors.append(f"{key}: expected float")
                    continue
                mn, mx = meta.get("min", float("-inf")), meta.get("max", float("inf"))
                if not (mn <= value <= mx):
                    errors.append(f"{key}: must be between {mn} and {mx}")
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
