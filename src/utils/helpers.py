"""Utility helper functions for Auto-Traitor."""

import os
from datetime import datetime, timezone


def get_data_dir() -> str:
    """Return the profile-scoped data directory.

    Uses ``AUTO_TRAITOR_PROFILE`` to isolate state files when multiple
    instances run side-by-side (e.g. crypto vs shares).

    Falls back to the flat ``data/`` directory when no profile is set.
    """
    profile = os.environ.get("AUTO_TRAITOR_PROFILE", "")
    base = os.path.join("data", profile) if profile else "data"
    os.makedirs(base, exist_ok=True)
    return base


def get_log_dir() -> str:
    """Return the profile-scoped log directory.

    Mirrors :func:`get_data_dir` logic for the ``logs/`` tree.
    """
    profile = os.environ.get("AUTO_TRAITOR_PROFILE", "")
    base = os.path.join("logs", profile) if profile else "logs"
    os.makedirs(base, exist_ok=True)
    return base


def format_currency(value: float, symbol: str = "$") -> str:
    """Format a value as currency."""
    if abs(value) >= 1_000_000:
        return f"{symbol}{value:,.0f}"
    elif abs(value) >= 1:
        return f"{symbol}{value:,.2f}"
    else:
        return f"{symbol}{value:.6f}"


def format_percentage(value: float, decimals: int = 2) -> str:
    """Format a value as percentage."""
    return f"{value * 100:.{decimals}f}%"


def timestamp_now() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(timezone.utc)


def safe_float(value, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def truncate(text: str, max_length: int = 200) -> str:
    """Truncate text to max length."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def calculate_pct_change(old_value: float, new_value: float) -> float:
    """Calculate percentage change between two values."""
    if old_value == 0:
        return 0.0
    return (new_value - old_value) / old_value
