"""
Settings Manager — Read, write, and hot-reload config/settings.yaml.

Provides:
  - Atomic writes (write to temp → rename) so a crash never corrupts the file.
  - Validation schemas for ALL config sections.
  - "Enable trading" / "Disable trading" presets with smart defaults.
  - Generic update_section() for any YAML section.
  - Persist + runtime push: update YAML *and* live config in one call.
  - Telegram safety tiers: SAFE / SEMI_SAFE / BLOCKED.
"""

from __future__ import annotations

import os
import copy
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

from src.utils.logger import get_logger

logger = get_logger("utils.settings_manager")

def get_settings_path() -> str:
    """Return the active settings path, overridden by AUTO_TRAITOR_CONFIG env var if set."""
    return os.environ.get("AUTO_TRAITOR_CONFIG", os.path.join("config", "settings.yaml"))
_lock = threading.RLock()


# ═══════════════════════════════════════════════════════════════════════════
# Validation schemas — per-section, per-field
# ═══════════════════════════════════════════════════════════════════════════

# H1 fix: Add minimum bounds to prevent LLM from lowering safety limits to near-zero
_RULE_SCHEMA: dict[str, dict[str, Any]] = {
    "max_single_trade":           {"type": float, "min": 1.0, "max": 1_000_000},      # H1: min $1 per trade
    "max_daily_spend":            {"type": float, "min": 10.0, "max": 10_000_000},    # H1: min $10/day
    "max_daily_loss":             {"type": float, "min": 10.0, "max": 1_000_000},     # H1: min $10 loss limit
    "max_portfolio_risk_pct":     {"type": float, "min": 0.005, "max": 1.0},          # H1: min 0.5%
    "require_approval_above":     {"type": float, "min": 0, "max": 1_000_000},        # 0 is valid (approve all)
    "never_trade_pairs":          {"type": list},
    "only_trade_pairs":           {"type": list},
    "min_trade_interval_seconds": {"type": int,   "min": 0, "max": 86_400},           # 0 is valid
    "max_trades_per_day":         {"type": int,   "min": 1, "max": 10_000},           # H1: at least 1 trade
    "max_cash_per_trade_pct":     {"type": float, "min": 0.005, "max": 1.0},          # H1: min 0.5%
    "emergency_stop_portfolio":   {"type": float, "min": 0, "max": 100_000_000},      # 0 is valid (disabled)
    "always_use_stop_loss":       {"type": bool},
    "max_stop_loss_pct":          {"type": float, "min": 0.005, "max": 1.0},          # H1: min 0.5%
}

_TRADING_SCHEMA: dict[str, dict[str, Any]] = {
    "mode":                         {"type": str, "enum": ["paper", "live"]},
    "exchange":                     {"type": str, "enum": ["coinbase", "ibkr"]},
    "pairs":                        {"type": list},
    "pair_discovery":               {"type": str, "enum": ["all", "configured"]},
    "quote_currency":               {"type": str},
    "quote_currencies":             {"type": list},
    "interval":                     {"type": int,   "min": 10, "max": 86_400},
    "min_confidence":               {"type": float, "min": 0.0, "max": 2.0},
    "max_open_positions":           {"type": int,   "min": 0, "max": 100},
    "reconcile_every_cycles":       {"type": int,   "min": 1, "max": 1000},
    "paper_slippage_pct":           {"type": float, "min": 0.0, "max": 0.1},
    "live_holdings_sync":           {"type": bool},
    "holdings_refresh_seconds":     {"type": int,   "min": 5, "max": 3600},
    "holdings_dust_threshold":      {"type": float, "min": 0.0, "max": 100.0},
    "invalidate_strategic_context": {"type": bool},
    "watchlist_pairs":               {"type": list},
    "pair_universe_refresh_seconds": {"type": int,   "min": 300, "max": 86400},
    "max_active_pairs":              {"type": int,   "min": 1, "max": 30},
    "include_crypto_quotes":         {"type": bool},
    "scan_volume_threshold":         {"type": float, "min": 0, "max": 1_000_000_000},
    "scan_movement_threshold_pct":   {"type": float, "min": 0.0, "max": 100.0},
    "screener_interval_cycles":      {"type": int,   "min": 1, "max": 100},
}

_RISK_SCHEMA: dict[str, dict[str, Any]] = {
    "max_position_pct":        {"type": float, "min": 0.01, "max": 1.0},   # H1: min 1%
    "max_total_exposure_pct":  {"type": float, "min": 0.05, "max": 1.0},   # H1: min 5%
    "max_drawdown_pct":        {"type": float, "min": 0.01, "max": 1.0},   # H1: min 1%
    "stop_loss_pct":           {"type": float, "min": 0.005, "max": 1.0},  # H1: min 0.5%
    "take_profit_pct":         {"type": float, "min": 0.005, "max": 1.0},  # H1: min 0.5%
    "trailing_stop_pct":       {"type": float, "min": 0.005, "max": 1.0},  # H1: min 0.5%
    "max_trades_per_hour":     {"type": int,   "min": 1, "max": 1000},     # H1: at least 1
    "loss_cooldown_seconds":   {"type": int,   "min": 0, "max": 86_400},   # 0 is valid (disabled)
}

_ROTATION_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":                   {"type": bool},
    "autonomous_allocation_pct": {"type": float, "min": 0.0, "max": 1.0},
    "min_score_delta":           {"type": float, "min": 0.0, "max": 2.0},
    "min_confidence":            {"type": float, "min": 0.0, "max": 1.0},
    "high_impact_confidence":    {"type": float, "min": 0.0, "max": 1.0},
    "approval_threshold":        {"type": float, "min": 0, "max": 1_000_000},
    "swap_cooldown_seconds":     {"type": int,   "min": 0, "max": 86_400},
}

_FEES_SCHEMA: dict[str, dict[str, Any]] = {
    "trade_fee_pct":           {"type": float, "min": 0.0, "max": 0.1},
    "maker_fee_pct":           {"type": float, "min": 0.0, "max": 0.1},
    "safety_margin":           {"type": float, "min": 1.0, "max": 10.0},
    "min_gain_after_fees_pct": {"type": float, "min": 0.0, "max": 1.0},
    "min_trade_quote":         {"type": float, "min": 0, "max": 100_000},
    "min_trade_pct":           {"type": float, "min": 0.0, "max": 1.0},
    "swap_cooldown_seconds":   {"type": int,   "min": 0, "max": 86_400},
}

_HIGH_STAKES_SCHEMA: dict[str, dict[str, Any]] = {
    "trade_size_multiplier":      {"type": float, "min": 1.0, "max": 10.0},
    "swap_allocation_multiplier": {"type": float, "min": 1.0, "max": 10.0},
    "min_confidence":             {"type": float, "min": 0.0, "max": 1.0},
    "min_swap_gain_pct":          {"type": float, "min": 0.0, "max": 1.0},
    "auto_approve_up_to":         {"type": float, "min": 0, "max": 1_000_000},
}

_TELEGRAM_SCHEMA: dict[str, dict[str, Any]] = {
    "mode":                        {"type": str, "enum": ["controller", "reporting"]},
    "bot_token":                   {"type": str},
    "chat_id":                     {"type": str},
    "authorized_users":            {"type": list},
    # Trade & Signal Alerts
    "notify_on_trade":             {"type": bool},
    "notify_on_signal":            {"type": bool},
    "notify_on_signal_confidence": {"type": float, "min": 0.0, "max": 1.0},
    # Win / Loss Highlights
    "notify_on_big_win":           {"type": bool},
    "big_win_threshold":           {"type": float, "min": 0, "max": 1_000_000},
    "notify_on_big_loss":          {"type": bool},
    "big_loss_threshold":          {"type": float, "min": 0, "max": 1_000_000},
    # Price Movement Alerts
    "notify_on_price_move":        {"type": bool},
    "price_move_threshold_pct":    {"type": float, "min": 0.5, "max": 50.0},
    "price_move_cooldown_minutes": {"type": int,   "min": 1, "max": 1440},
    # Scheduled Messages
    "notify_morning_plan":         {"type": bool},
    "notify_evening_summary":      {"type": bool},
    "notify_periodic_update":      {"type": bool},
    "status_update_interval":      {"type": int,   "min": 0, "max": 86_400},
    "daily_summary":               {"type": bool},
    "daily_summary_hour":          {"type": int,   "min": 0, "max": 23},
}

_NEWS_SCHEMA: dict[str, dict[str, Any]] = {
    "fetch_interval":        {"type": int,  "min": 30, "max": 86_400},
    "reddit_subreddits":     {"type": list},
    "rss_feeds":             {"type": list},
    "max_articles":          {"type": int,  "min": 1, "max": 10_000},
    "articles_for_analysis": {"type": int,  "min": 1, "max": 1000},
}

_FEAR_GREED_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":   {"type": bool},
    "cache_ttl": {"type": int, "min": 60, "max": 86_400},
}

_MULTI_TIMEFRAME_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":       {"type": bool},
    "min_alignment": {"type": int, "min": 1, "max": 10},
}

_JOURNAL_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":  {"type": bool},
    "data_dir": {"type": str},
}

_AUDIT_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":  {"type": bool},
    "data_dir": {"type": str},
}

_LLM_SCHEMA: dict[str, dict[str, Any]] = {
    "model":       {"type": str},
    "temperature": {"type": float, "min": 0.0, "max": 2.0},
    "max_tokens":  {"type": int,   "min": 100, "max": 100_000},
    "max_retries": {"type": int,   "min": 0, "max": 20},
    "timeout":     {"type": int,   "min": 5, "max": 600},
    "persona":     {"type": str},
}

_LOGGING_SCHEMA: dict[str, dict[str, Any]] = {
    "level":         {"type": str, "enum": ["DEBUG", "INFO", "WARNING", "ERROR"]},
    "file_enabled":  {"type": bool},
    "directory":     {"type": str},
    "max_file_size": {"type": int, "min": 1, "max": 1000},
    "backup_count":  {"type": int, "min": 0, "max": 100},
}

_HEALTH_SCHEMA: dict[str, dict[str, Any]] = {
    "port": {"type": int, "min": 1, "max": 65535},
}

_DASHBOARD_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":          {"type": bool},
    "port":             {"type": int, "min": 1, "max": 65535},
    "langfuse_host":    {"type": str},
    "langfuse_enabled": {"type": bool},
}

_ANALYSIS_TECHNICAL_SCHEMA: dict[str, dict[str, Any]] = {
    "rsi_period":         {"type": int,   "min": 2, "max": 100},
    "rsi_overbought":     {"type": int,   "min": 50, "max": 100},
    "rsi_oversold":       {"type": int,   "min": 0, "max": 50},
    "macd_fast":          {"type": int,   "min": 2, "max": 100},
    "macd_slow":          {"type": int,   "min": 5, "max": 200},
    "macd_signal":        {"type": int,   "min": 2, "max": 50},
    "bb_period":          {"type": int,   "min": 5, "max": 100},
    "bb_std":             {"type": float, "min": 0.5, "max": 5.0},
    "ema_periods":        {"type": list},
    "candle_count":       {"type": int,   "min": 50, "max": 1000},
    "candle_granularity": {"type": str},
}

_ANALYSIS_SENTIMENT_SCHEMA: dict[str, dict[str, Any]] = {
    "enabled":     {"type": bool},
    "sample_size": {"type": int, "min": 1, "max": 100},
}

# ── Master registry ──────────────────────────────────────────────────────

SECTION_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "absolute_rules":  _RULE_SCHEMA,
    "trading":         _TRADING_SCHEMA,
    "risk":            _RISK_SCHEMA,
    "rotation":        _ROTATION_SCHEMA,
    "fees":            _FEES_SCHEMA,
    "high_stakes":     _HIGH_STAKES_SCHEMA,
    "telegram":        _TELEGRAM_SCHEMA,
    "news":            _NEWS_SCHEMA,
    "fear_greed":      _FEAR_GREED_SCHEMA,
    "multi_timeframe": _MULTI_TIMEFRAME_SCHEMA,
    "journal":         _JOURNAL_SCHEMA,
    "audit":           _AUDIT_SCHEMA,
    "llm":             _LLM_SCHEMA,
    "logging":         _LOGGING_SCHEMA,
    "health":          _HEALTH_SCHEMA,
    "dashboard":       _DASHBOARD_SCHEMA,
}

NESTED_SCHEMAS: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    "analysis": {
        "technical": _ANALYSIS_TECHNICAL_SCHEMA,
        "sentiment": _ANALYSIS_SENTIMENT_SCHEMA,
    },
}

SECTION_LABELS: dict[str, str] = {
    "absolute_rules":  "Absolute Rules",
    "trading":         "Trading",
    "risk":            "Risk Management",
    "rotation":        "Portfolio Rotation",
    "fees":            "Fee Management",
    "high_stakes":     "High-Stakes Mode",
    "telegram":        "Telegram",
    "news":            "News",
    "fear_greed":      "Fear & Greed Index",
    "multi_timeframe": "Multi-Timeframe Analysis",
    "journal":         "Journal",
    "audit":           "Audit",
    "llm":             "LLM",
    "logging":         "Logging",
    "health":          "Health Check",
    "dashboard":       "Dashboard",
    "analysis":        "Analysis",
}


# ═══════════════════════════════════════════════════════════════════════════
# Telegram Safety Tiers
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_SAFE_SECTIONS = frozenset({
    "absolute_rules", "trading", "risk", "rotation", "fees", "high_stakes",
})

TELEGRAM_SEMI_SAFE_SECTIONS = frozenset({
    "telegram", "news", "fear_greed", "multi_timeframe",
})

TELEGRAM_BLOCKED_SECTIONS = frozenset({
    "llm", "logging", "health", "dashboard", "analysis", "journal", "audit",
})

TELEGRAM_SAFETY_TIERS: dict[str, dict[str, Any]] = {
    "safe": {
        "sections": sorted(TELEGRAM_SAFE_SECTIONS),
        "description": (
            "Core trading parameters, limits, and risk settings. "
            "Changes take effect immediately and are persisted to disk."
        ),
    },
    "semi_safe": {
        "sections": sorted(TELEGRAM_SEMI_SAFE_SECTIONS),
        "description": (
            "Notification, news, and feature-toggle settings. "
            "Could affect alerting behavior. Change with care."
        ),
    },
    "blocked": {
        "sections": sorted(TELEGRAM_BLOCKED_SECTIONS),
        "description": (
            "Infrastructure settings (LLM model, ports, analysis params, paths). "
            "Use the Dashboard UI to change these."
        ),
    },
}


def is_telegram_allowed(section: str) -> str:
    """Return 'safe', 'semi_safe', or 'blocked' for a given section name."""
    if section in TELEGRAM_SAFE_SECTIONS:
        return "safe"
    if section in TELEGRAM_SEMI_SAFE_SECTIONS:
        return "semi_safe"
    return "blocked"


# ═══════════════════════════════════════════════════════════════════════════
# Autonomous LLM Agent Tier
# ═══════════════════════════════════════════════════════════════════════════
# The trading LLM can adjust these sections to adapt to market conditions.
# "on/off trading" is OFF LIMITS — enforced via floor values that prevent
# the LLM from setting parameters to values that effectively disable trading.

AUTONOMOUS_ALLOWED_SECTIONS = frozenset({
    "risk", "trading", "rotation", "fees", "high_stakes", "absolute_rules",
})

# Per-field floor/ceiling overrides for autonomous updates.
# These OVERRIDE the normal schema min/max to keep trading alive.
AUTONOMOUS_FIELD_GUARDS: dict[str, dict[str, dict[str, Any]]] = {
    "absolute_rules": {
        "max_single_trade":           {"min": 5},      # can't zero out
        "max_daily_spend":            {"min": 10},     # can't zero out
        "max_daily_loss":             {"min": 5},      # can't zero out
        "max_portfolio_risk_pct":     {"min": 0.01},   # can't zero out
        "max_trades_per_day":         {"min": 1},      # at least 1 trade/day
        "max_cash_per_trade_pct":     {"min": 0.01},   # can't zero out
        "require_approval_above":     {"max": 50000},  # C5: LLM cannot set absurdly high
        "never_trade_pairs":          {},              # LLM can manage exclusion list
        "only_trade_pairs":           {},              # LLM can manage inclusion list
        "min_trade_interval_seconds": {"max": 7200},   # can't slow to >2h
        "emergency_stop_portfolio":   {"min": 0.01},   # C5: LLM cannot disable emergency stop
        "always_use_stop_loss":       {},              # C5: guarded in AUTONOMOUS_BLOCKED_FIELDS
        "max_stop_loss_pct":          {},              # free to adjust
    },
    "trading": {
        "min_confidence":              {"min": 0.3, "max": 0.95},  # can't set >0.95 (effective disable)
        "max_open_positions":          {"min": 1},                  # can't zero out
        "paper_slippage_pct":          {},
        "interval":                    {"min": 30, "max": 3600},
        "pairs":                       {},                            # LLM can add/remove pairs
        "pair_discovery":              {},                            # LLM can switch discovery mode
        "quote_currencies":            {},                            # LLM can adjust quote currencies
        "include_crypto_quotes":       {},
        "scan_volume_threshold":       {},
        "scan_movement_threshold_pct": {},
        "screener_interval_cycles":    {"min": 2, "max": 50},
    },
    "risk": {
        "stop_loss_pct":           {"min": 0.005, "max": 0.20},
        "take_profit_pct":         {"min": 0.01, "max": 0.50},
        "trailing_stop_pct":       {"min": 0.005, "max": 0.20},
        "max_position_pct":        {"min": 0.01},
        "max_total_exposure_pct":  {"min": 0.05},
        "max_drawdown_pct":        {"min": 0.02},
        "max_trades_per_hour":     {"min": 1},
        "loss_cooldown_seconds":   {"max": 7200},
    },
    "rotation": {
        "enabled":                   {},
        "autonomous_allocation_pct": {"max": 0.5},
        "min_score_delta":           {},
        "min_confidence":            {"min": 0.3},
        "high_impact_confidence":    {"min": 0.5},
        "approval_threshold":        {},
        "swap_cooldown_seconds":     {"max": 7200},
    },
    "fees": {
        "safety_margin":           {},
        "min_gain_after_fees_pct": {},
        "min_trade_quote":         {},
        "min_trade_pct":           {},
    },
    "high_stakes": {
        "trade_size_multiplier":      {"max": 5.0},
        "swap_allocation_multiplier": {"max": 5.0},
        "min_confidence":             {"min": 0.5},
        "min_swap_gain_pct":          {},
        "auto_approve_up_to":         {},
    },
}

# List-type fields where the autonomous LLM can only ADD items, never remove.
# This prevents the LLM from clearing safety-critical blacklists/whitelists.
_AUTONOMOUS_APPEND_ONLY_LISTS = frozenset({
    "never_trade_pairs",
    "only_trade_pairs",
})

# Fields the autonomous LLM may NOT touch even within allowed sections
AUTONOMOUS_BLOCKED_FIELDS = frozenset({
    ("trading", "mode"),
    ("trading", "quote_currency"),
    ("trading", "live_holdings_sync"),
    ("trading", "holdings_refresh_seconds"),
    ("trading", "holdings_dust_threshold"),
    ("trading", "reconcile_every_cycles"),
    ("trading", "invalidate_strategic_context"),
    ("trading", "pair_universe_refresh_seconds"),
    ("trading", "max_active_pairs"),          # human-only: RPM guardrail enforces upper bound
    ("absolute_rules", "always_use_stop_loss"),  # C5: LLM must NOT disable stop losses
    ("fees", "trade_fee_pct"),
    ("fees", "maker_fee_pct"),
    ("fees", "swap_cooldown_seconds"),
})


def validate_autonomous_update(
    section: str,
    updates: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any]]:
    """
    Validate a proposed autonomous (LLM-agent) settings change.

    Enforces:
      1. Section is in AUTONOMOUS_ALLOWED_SECTIONS
      2. Field is not in AUTONOMOUS_BLOCKED_FIELDS
      3. Field has an entry in AUTONOMOUS_FIELD_GUARDS
      4. Value passes normal schema validation
      5. Value respects autonomous floor/ceiling guards

    Returns (ok, errors, clamped_updates).
    """
    if section not in AUTONOMOUS_ALLOWED_SECTIONS:
        return False, [f"Section '{section}' is not allowed for autonomous updates"], {}

    section_guards = AUTONOMOUS_FIELD_GUARDS.get(section, {})
    errors: list[str] = []
    clamped: dict[str, Any] = {}

    for field, raw_value in updates.items():
        if (section, field) in AUTONOMOUS_BLOCKED_FIELDS:
            errors.append(f"{section}.{field} is blocked for autonomous updates")
            continue

        if field not in section_guards:
            errors.append(f"{section}.{field} is not in the autonomous-allowed field list")
            continue

        ok, err, cast_val = validate_field(section, field, raw_value)
        if not ok:
            errors.append(f"{section}.{field}: {err}")
            continue

        # Clamp to autonomous guard ranges (don't reject — just cap)
        guards = section_guards[field]
        if isinstance(cast_val, (int, float)):
            guard_min = guards.get("min")
            guard_max = guards.get("max")
            if guard_min is not None and cast_val < guard_min:
                cast_val = type(cast_val)(guard_min)
                logger.info(f"  ↳ Autonomous guard: clamped {section}.{field} to floor {guard_min}")
            if guard_max is not None and cast_val > guard_max:
                cast_val = type(cast_val)(guard_max)
                logger.info(f"  ↳ Autonomous guard: clamped {section}.{field} to ceiling {guard_max}")

        # List-field guardrails: LLM can add items but cannot clear/shrink
        # safety-critical lists (never_trade_pairs, only_trade_pairs).
        if isinstance(cast_val, list) and field in _AUTONOMOUS_APPEND_ONLY_LISTS:
            current_settings = load_settings()
            current_list = current_settings.get(section, {}).get(field, [])
            if isinstance(current_list, list):
                current_set = set(current_list)
                new_set = set(cast_val)
                removed = current_set - new_set
                if removed:
                    # LLM tried to remove items — re-add them
                    cast_val = list(new_set | current_set)
                    logger.warning(
                        f"  ↳ Autonomous guard: blocked removal of {removed} from "
                        f"{section}.{field} — LLM can only add, not remove"
                    )

        clamped[field] = cast_val

    if errors:
        return False, errors, {}
    if not clamped:
        return True, [], {}
    return True, [], clamped


def get_autonomous_schema_summary() -> dict[str, dict[str, Any]]:
    """
    Return a summary of what the autonomous LLM agent is allowed to change,
    including field types, effective ranges, and guard values.
    """
    result: dict[str, dict[str, Any]] = {}
    for section, fields in AUTONOMOUS_FIELD_GUARDS.items():
        section_schema = SECTION_SCHEMAS.get(section, {})
        section_info: dict[str, Any] = {}
        for field, guards in fields.items():
            if (section, field) in AUTONOMOUS_BLOCKED_FIELDS:
                continue
            field_schema = section_schema.get(field, {})
            info: dict[str, Any] = {"type": field_schema.get("type", str).__name__}
            schema_min = field_schema.get("min")
            schema_max = field_schema.get("max")
            guard_min = guards.get("min")
            guard_max = guards.get("max")
            candidates_min = [x for x in [schema_min, guard_min] if x is not None]
            candidates_max = [x for x in [schema_max, guard_max] if x is not None]
            if candidates_min:
                info["min"] = max(candidates_min)
            if candidates_max:
                info["max"] = min(candidates_max)
            if "enum" in field_schema:
                info["enum"] = field_schema["enum"]
            section_info[field] = info
        if section_info:
            result[section] = section_info
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Smart Presets
# ═══════════════════════════════════════════════════════════════════════════

PRESET_DISABLED: dict[str, Any] = {
    "absolute_rules": {
        "max_single_trade": 0,
        "max_daily_spend": 0,
        "max_daily_loss": 0,
        "max_portfolio_risk_pct": 0.0,
        "require_approval_above": 0,
        "max_trades_per_day": 0,
        "max_cash_per_trade_pct": 0.0,
    },
    "trading": {
        "min_confidence": 1.1,
        "max_open_positions": 0,
    },
}

PRESET_CONSERVATIVE: dict[str, Any] = {
    "absolute_rules": {
        "max_single_trade": 50,
        "max_daily_spend": 200,
        "max_daily_loss": 100,
        "max_portfolio_risk_pct": 0.05,
        "require_approval_above": 25,
        "max_trades_per_day": 10,
        "max_cash_per_trade_pct": 0.05,
    },
    "trading": {
        "min_confidence": 0.75,
        "max_open_positions": 3,
    },
}

PRESET_MODERATE: dict[str, Any] = {
    "absolute_rules": {
        "max_single_trade": 150,
        "max_daily_spend": 500,
        "max_daily_loss": 250,
        "max_portfolio_risk_pct": 0.10,
        "require_approval_above": 75,
        "max_trades_per_day": 20,
        "max_cash_per_trade_pct": 0.10,
    },
    "trading": {
        "min_confidence": 0.65,
        "max_open_positions": 5,
    },
}

PRESET_AGGRESSIVE: dict[str, Any] = {
    "absolute_rules": {
        "max_single_trade": 500,
        "max_daily_spend": 2000,
        "max_daily_loss": 500,
        "max_portfolio_risk_pct": 0.20,
        "require_approval_above": 200,
        "max_trades_per_day": 50,
        "max_cash_per_trade_pct": 0.25,
    },
    "trading": {
        "min_confidence": 0.55,
        "max_open_positions": 8,
    },
}

PRESETS = {
    "disabled":     PRESET_DISABLED,
    "conservative": PRESET_CONSERVATIVE,
    "moderate":     PRESET_MODERATE,
    "aggressive":   PRESET_AGGRESSIVE,
}


# ═══════════════════════════════════════════════════════════════════════════
# Core I/O
# ═══════════════════════════════════════════════════════════════════════════

def load_settings(path: str = None) -> dict:
    """Load the full settings.yaml as a dict."""
    path = path or get_settings_path()
    with _lock:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}


def save_settings(settings: dict, path: str = None) -> None:
    """Atomically write settings to YAML (write to temp, then rename)."""
    path = path or get_settings_path()
    with _lock:
        dir_path = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(suffix=".yaml", dir=dir_path)
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(
                    settings, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            if os.path.exists(path):
                os.replace(tmp_path, path)
            else:
                os.rename(tmp_path, path)
            logger.info(f"💾 Settings saved to {path}")
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def get_full_settings(path: str = None) -> dict:
    """Return the full settings dict plus metadata for the API."""
    path = path or get_settings_path()
    cfg = load_settings(path)
    return {
        "settings": cfg,
        "trading_enabled": is_trading_enabled(path),
        "sections": list(SECTION_SCHEMAS.keys()) + list(NESTED_SCHEMAS.keys()),
        "section_labels": SECTION_LABELS,
        "telegram_tiers": TELEGRAM_SAFETY_TIERS,
    }


def get_section(section: str, path: str = None) -> dict:
    """Return a single section from settings.yaml."""
    path = path or get_settings_path()
    cfg = load_settings(path)
    if "." in section:
        parts = section.split(".", 1)
        return cfg.get(parts[0], {}).get(parts[1], {})
    return cfg.get(section, {})


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════

def validate_field(section: str, key: str, value: Any) -> tuple[bool, str, Any]:
    """
    Validate a single field for a given section.
    Returns (ok, error_msg, cast_value).
    """
    if "." in section:
        parts = section.split(".", 1)
        nested = NESTED_SCHEMAS.get(parts[0], {})
        schema_dict = nested.get(parts[1], {})
    else:
        schema_dict = SECTION_SCHEMAS.get(section, {})

    field_schema = schema_dict.get(key)
    if field_schema is None:
        # M4 fix: warn on unknown fields so typos are visible in logs
        _logger = None
        try:
            from src.utils.logger import get_logger
            _logger = get_logger("settings")
        except Exception:
            pass
        if _logger:
            _logger.warning(f"Unknown settings field '{section}.{key}' — passing through without validation")
        return True, "", value  # unknown fields pass through

    expected_type = field_schema["type"]

    if expected_type == bool:
        if isinstance(value, bool):
            return True, "", value
        if isinstance(value, str):
            if value.lower() in ("true", "1", "yes", "on"):
                return True, "", True
            if value.lower() in ("false", "0", "no", "off"):
                return True, "", False
        return False, f"{key} must be a boolean", value

    if expected_type == list:
        if isinstance(value, list):
            return True, "", value
        return False, f"{key} must be a list", value

    if expected_type == str:
        value = str(value)
        if "enum" in field_schema and value not in field_schema["enum"]:
            return False, f"{key} must be one of {field_schema['enum']}", value
        return True, "", value

    try:
        cast_val = expected_type(value)
    except (ValueError, TypeError):
        return False, f"{key} must be {expected_type.__name__}", value

    if "min" in field_schema and cast_val < field_schema["min"]:
        return False, f"{key} must be >= {field_schema['min']}", value
    if "max" in field_schema and cast_val > field_schema["max"]:
        return False, f"{key} must be <= {field_schema['max']}", value

    return True, "", cast_val


def validate_section(section: str, updates: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    """
    Validate all fields in an update dict for a section.
    Returns (ok, errors, cast_updates).
    """
    errors: list[str] = []
    cast_updates: dict[str, Any] = {}

    for key, value in updates.items():
        ok, err, cast_val = validate_field(section, key, value)
        if not ok:
            errors.append(err)
        else:
            cast_updates[key] = cast_val

    return len(errors) == 0, errors, cast_updates


# ═══════════════════════════════════════════════════════════════════════════
# Generic update (any section)
# ═══════════════════════════════════════════════════════════════════════════

def update_section(
    section: str,
    updates: dict[str, Any],
    path: str = None,
) -> tuple[bool, str, dict]:
    """
    Update one or more fields in any settings section.
    Validates, persists to YAML, and returns changes.

    Supports dotted sections: ``"analysis.technical"`` updates
    ``cfg["analysis"]["technical"]``.

    Returns (ok, error_message, applied_changes).
    """
    path = path or get_settings_path()
    ok, errors, cast_updates = validate_section(section, updates)
    if not ok:
        return False, "; ".join(errors), {}

    if not cast_updates:
        return True, "No changes", {}

    with _lock:
        cfg = load_settings(path)

        if "." in section:
            parts = section.split(".", 1)
            parent = cfg.setdefault(parts[0], {})
            target = parent.setdefault(parts[1], {})
        else:
            target = cfg.setdefault(section, {})

        for key, new_val in cast_updates.items():
            target[key] = new_val

        save_settings(cfg, path)
    logger.warning(f"🔧 [{section}] updated (persisted): {cast_updates}")
    return True, "", cast_updates


# ═══════════════════════════════════════════════════════════════════════════
# Convenience wrappers (backwards-compatible)
# ═══════════════════════════════════════════════════════════════════════════

def get_absolute_rules(path: str = None) -> dict:
    return get_section("absolute_rules", path)


def get_trading_section(path: str = None) -> dict:
    return get_section("trading", path)


def validate_rule(key: str, value: Any) -> tuple[bool, str]:
    ok, err, _ = validate_field("absolute_rules", key, value)
    return ok, err


def update_absolute_rules(
    updates: dict[str, Any],
    path: str = None,
) -> tuple[bool, str, dict]:
    return update_section("absolute_rules", updates, path)


def update_trading_params(
    updates: dict[str, Any],
    path: str = None,
) -> tuple[bool, str, dict]:
    return update_section("trading", updates, path)


# ═══════════════════════════════════════════════════════════════════════════
# Presets
# ═══════════════════════════════════════════════════════════════════════════

def apply_preset(
    preset_name: str,
    path: str = None,
) -> tuple[bool, str, dict]:
    """
    Apply a named preset (disabled, conservative, moderate, aggressive).
    Returns (ok, error, applied_changes).
    """
    path = path or get_settings_path()
    preset = PRESETS.get(preset_name)
    if preset is None:
        return (
            False,
            f"Unknown preset: {preset_name!r}. Options: {', '.join(PRESETS.keys())}",
            {},
        )

    # M26 fix: hold _lock for the entire load→modify→save to prevent TOCTOU
    with _lock:
        cfg = load_settings(path)
        changes: dict[str, Any] = {}

        for section_name, section_updates in preset.items():
            target = cfg.setdefault(section_name, {})
            for k, v in section_updates.items():
                target[k] = v
                changes[f"{section_name}.{k}"] = v

        save_settings(cfg, path)
    logger.warning(f"🔧 Preset '{preset_name}' applied (persisted): {changes}")
    return True, "", changes


# ═══════════════════════════════════════════════════════════════════════════
# Runtime push — hot-reload without restart
# ═══════════════════════════════════════════════════════════════════════════

# Allowlist of rule attributes that may be hot-reloaded via setattr.
# Prevents YAML field names from setting arbitrary attributes.
_RULES_SETTABLE_ATTRS = frozenset({
    "max_single_trade", "max_daily_spend", "max_daily_loss",
    "max_portfolio_risk_pct", "require_approval_above",
    "never_trade_pairs", "only_trade_pairs",
    "min_trade_interval_seconds", "max_trades_per_day",
    "max_cash_per_trade_pct", "emergency_stop_portfolio",
    "always_use_stop_loss", "max_stop_loss_pct",
})


def push_to_runtime(
    rules_instance,
    config: dict,
    changes: dict[str, Any],
) -> None:
    """
    Push persisted changes to the live AbsoluteRules instance and config dict.

    ``changes`` keys can be:
      - Flat (absolute_rules assumed): ``{"max_single_trade": 100}``
      - Dotted: ``{"absolute_rules.max_single_trade": 100, "risk.stop_loss_pct": 0.03}``
    """
    for key, value in changes.items():
        if "." in key:
            section, attr = key.split(".", 1)
        else:
            section, attr = "absolute_rules", key

        if section == "absolute_rules":
            if rules_instance and attr in _RULES_SETTABLE_ATTRS and hasattr(rules_instance, attr):
                with rules_instance._lock:
                    setattr(rules_instance, attr, value)
                logger.info(f"  ↳ Runtime rule {attr} → {value!r}")
            elif rules_instance and attr not in _RULES_SETTABLE_ATTRS:
                logger.warning(f"  ↳ Blocked setattr for disallowed rule attribute: {attr}")
        else:
            section_cfg = config.setdefault(section, {})
            section_cfg[attr] = value
            logger.info(f"  ↳ Runtime {section}.{attr} → {value!r}")


def push_section_to_runtime(
    section: str,
    updates: dict[str, Any],
    rules_instance,
    config: dict,
) -> None:
    """Push a section update to the live runtime config."""
    dotted = {f"{section}.{k}": v for k, v in updates.items()}
    push_to_runtime(rules_instance, config, dotted)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def is_trading_enabled(path: str = None) -> bool:
    """Quick check: is trading effectively enabled (non-zero limits)?"""
    path = path or get_settings_path()
    rules = get_absolute_rules(path)
    return (
        rules.get("max_single_trade", 0) > 0
        and rules.get("max_daily_spend", 0) > 0
        and rules.get("max_trades_per_day", 0) > 0
    )


def get_preset_summary(preset_name: str) -> str:
    """Return a human-readable summary of a preset for Telegram."""
    preset = PRESETS.get(preset_name)
    if not preset:
        return f"Unknown preset: {preset_name}"

    rules = preset.get("absolute_rules", {})
    trading = preset.get("trading", {})

    if preset_name == "disabled":
        return (
            "🔴 *Trading Disabled*\n"
            "All limits set to zero. No trades can execute."
        )

    lines = [f"*{preset_name.title()} Preset:*\n"]
    if "max_single_trade" in rules:
        lines.append(f"• Max single trade: {rules['max_single_trade']:,.0f}")
    if "max_daily_spend" in rules:
        lines.append(f"• Max daily spend: {rules['max_daily_spend']:,.0f}")
    if "max_daily_loss" in rules:
        lines.append(f"• Max daily loss: {rules['max_daily_loss']:,.0f}")
    if "max_portfolio_risk_pct" in rules:
        lines.append(f"• Max portfolio risk: {rules['max_portfolio_risk_pct']:.0%}")
    if "require_approval_above" in rules:
        lines.append(f"• Approval above: {rules['require_approval_above']:,.0f}")
    if "max_trades_per_day" in rules:
        lines.append(f"• Max trades/day: {rules['max_trades_per_day']}")
    if "max_cash_per_trade_pct" in rules:
        lines.append(f"• Max cash/trade: {rules['max_cash_per_trade_pct']:.0%}")
    if "min_confidence" in trading:
        lines.append(f"• Min confidence: {trading['min_confidence']:.0%}")
    if "max_open_positions" in trading:
        lines.append(f"• Max open positions: {trading['max_open_positions']}")

    return "\n".join(lines)


def get_schema_metadata() -> dict[str, Any]:
    """Return schema info for the frontend UI (field types, ranges)."""
    meta: dict[str, Any] = {}

    for section, schema in SECTION_SCHEMAS.items():
        meta[section] = {
            "label": SECTION_LABELS.get(section, section),
            "telegram_tier": is_telegram_allowed(section),
            "fields": {},
        }
        for field_name, field_def in schema.items():
            field_info: dict[str, Any] = {"type": field_def["type"].__name__}
            if "min" in field_def:
                field_info["min"] = field_def["min"]
            if "max" in field_def:
                field_info["max"] = field_def["max"]
            if "enum" in field_def:
                field_info["enum"] = field_def["enum"]
            meta[section]["fields"][field_name] = field_info

    for parent, children in NESTED_SCHEMAS.items():
        meta[parent] = {
            "label": SECTION_LABELS.get(parent, parent),
            "telegram_tier": is_telegram_allowed(parent),
            "nested": {},
        }
        for child_name, child_schema in children.items():
            child_meta: dict[str, Any] = {"fields": {}}
            for field_name, field_def in child_schema.items():
                field_info = {"type": field_def["type"].__name__}
                if "min" in field_def:
                    field_info["min"] = field_def["min"]
                if "max" in field_def:
                    field_info["max"] = field_def["max"]
                if "enum" in field_def:
                    field_info["enum"] = field_def["enum"]
                child_meta["fields"][field_name] = field_info
            meta[parent]["nested"][child_name] = child_meta

    return meta


# ═══════════════════════════════════════════════════════════════════════════
# LLM Provider validation & management
# ═══════════════════════════════════════════════════════════════════════════

_LLM_PROVIDER_SCHEMA: dict[str, dict[str, Any]] = {
    "name":              {"type": str,  "required": True},
    "enabled":           {"type": bool},
    "model":             {"type": str,  "required": True},
    "base_url":          {"type": str},
    "base_url_env":      {"type": str},
    "api_key_env":       {"type": str},
    "model_env":         {"type": str},
    "timeout":           {"type": int,  "min": 5,  "max": 600},
    "rpm_limit":         {"type": int,  "min": 0,  "max": 10_000},
    "daily_token_limit": {"type": int,  "min": 0,  "max": 100_000_000},
    "cooldown_seconds":  {"type": int,  "min": 5,  "max": 3600},
    "is_local":          {"type": bool},
    "tier":              {"type": str},   # "free" or "paid"
}


def validate_provider(provider: dict) -> tuple[bool, str]:
    """Validate a single LLM provider config dict."""
    errors: list[str] = []
    name = provider.get("name", "<unnamed>")

    for field_name, field_def in _LLM_PROVIDER_SCHEMA.items():
        if field_def.get("required") and field_name not in provider:
            errors.append(f"Provider '{name}': missing required field '{field_name}'")
            continue

        if field_name not in provider:
            continue

        value = provider[field_name]
        expected = field_def["type"]

        if expected == bool:
            if not isinstance(value, bool):
                errors.append(f"Provider '{name}': {field_name} must be boolean")
        elif expected == str:
            if not isinstance(value, str):
                errors.append(f"Provider '{name}': {field_name} must be string")
        elif expected == int:
            try:
                cast = int(value)
            except (ValueError, TypeError):
                errors.append(f"Provider '{name}': {field_name} must be int")
                continue
            if "min" in field_def and cast < field_def["min"]:
                errors.append(f"Provider '{name}': {field_name} must be >= {field_def['min']}")
            if "max" in field_def and cast > field_def["max"]:
                errors.append(f"Provider '{name}': {field_name} must be <= {field_def['max']}")

    if errors:
        return False, "; ".join(errors)
    return True, ""


def validate_providers_list(providers: list[dict]) -> tuple[bool, str]:
    """Validate a full ordered providers list."""
    if not isinstance(providers, list):
        return False, "providers must be a list"

    names: set[str] = set()
    for i, p in enumerate(providers):
        if not isinstance(p, dict):
            return False, f"Provider at index {i} must be a dict"

        ok, err = validate_provider(p)
        if not ok:
            return False, err

        name = p.get("name", "")
        if name in names:
            return False, f"Duplicate provider name: '{name}'"
        names.add(name)

    return True, ""


def update_llm_providers(
    providers: list[dict],
    path: str = None,
) -> tuple[bool, str, list[dict]]:
    """
    Validate and persist a new LLM providers list.
    Returns (ok, error_message, saved_providers).
    """
    path = path or get_settings_path()
    ok, err = validate_providers_list(providers)
    if not ok:
        return False, err, []

    # H6 fix: single lock scope for load→modify→save to prevent TOCTOU
    with _lock:
        cfg = load_settings(path)
        llm_section = cfg.setdefault("llm", {})
        llm_section["providers"] = providers
        save_settings(cfg, path)

    logger.warning(f"🔧 LLM providers updated: {[p.get('name') for p in providers]}")
    return True, "", providers


def get_llm_providers(path: str = None) -> list[dict]:
    """Return the current providers list from settings."""
    path = path or get_settings_path()
    cfg = load_settings(path)
    return cfg.get("llm", {}).get("providers", [])
