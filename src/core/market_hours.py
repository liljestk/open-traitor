"""Equity market trading-hours gating.

Uses the ``exchange_calendars`` library for accurate, holiday-aware market hours.
Calendars are fetched once and cached at module level.

Pairs must include the Yahoo exchange suffix in the ticker for non-US exchanges
(e.g. ``NESTE.HE-EUR``, ``VOLV-B.ST-SEK``).  Plain tickers without a dot-suffix
(e.g. ``GE-USD``, ``AAPL-USD``) default to NYSE hours.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

try:
    import exchange_calendars as xcals
    _XCALS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _XCALS_AVAILABLE = False

from src.utils.logger import get_logger

logger = get_logger("market_hours")

# Yahoo Finance exchange suffix → ISO MIC code used by exchange_calendars.
# Plain tickers with no dot-suffix (e.g. AAPL-USD, GE-USD) → "" → XNYS.
_SUFFIX_TO_MIC: dict[str, str] = {
    "":   "XNYS",  # NYSE / NASDAQ (US, no dot-suffix)
    "ST": "XSTO",  # OMX Stockholm
    "HE": "XHEL",  # Helsinki (Nasdaq Nordic)
    "CO": "XCSE",  # Copenhagen (Nasdaq Nordic)
    "OL": "XOSL",  # Oslo Børs
    "L":  "XLON",  # London Stock Exchange
    "DE": "XETR",  # XETRA / Frankfurt
    "PA": "XPAR",  # Euronext Paris
    "AS": "XAMS",  # Euronext Amsterdam
    "MI": "XMIL",  # Borsa Italiana
    "SW": "XSWX",  # SIX Swiss Exchange
    "TO": "XTSE",  # Toronto Stock Exchange
    "AX": "XASX",  # ASX
    "T":  "XTKS",  # Tokyo Stock Exchange
    "HK": "XHKG",  # Hong Kong Exchange
}

# Calendar objects are expensive to instantiate — cache them
_calendar_cache: dict[str, Any] = {}


def _get_calendar(mic: str) -> Any | None:
    """Return a cached exchange_calendars Calendar for the given MIC code, or None."""
    if mic not in _calendar_cache:
        try:
            _calendar_cache[mic] = xcals.get_calendar(mic)
        except Exception as exc:
            logger.warning(f"market_hours: calendar '{mic}' unavailable: {exc}")
            _calendar_cache[mic] = None
    return _calendar_cache[mic]


def _exchange_suffix(pair: str) -> str | None:
    """Extract the Yahoo exchange suffix from a pair name.

    The exchange is determined by the ``.XX`` part of the ticker — NOT the
    quote currency — so EUR-quoted Nordic pairs are handled correctly:

        "AAPL-USD"       → ""    (US, no dot-suffix → NYSE)
        "GE-USD"         → ""    (US)
        "VOLV-B.ST-SEK"  → "ST"  (OMX Stockholm)
        "NESTE.HE-EUR"   → "HE"  (Helsinki — EUR-quoted Nordic)
        "ABB.SW-CHF"     → "SW"  (SIX Swiss)

    Returns ``None`` for completely unparseable pairs.
    """
    upper = pair.upper()
    parts = upper.split("-")

    # Strip the 3-letter quote currency from the end (USD, EUR, SEK, NOK, DKK…)
    if len(parts) >= 2 and len(parts[-1]) == 3 and parts[-1].isalpha():
        ticker = "-".join(parts[:-1])
    else:
        return None

    if "." in ticker:
        suffix = ticker.rsplit(".", 1)[1]
        return suffix if suffix in _SUFFIX_TO_MIC else None

    # Plain ticker with no dot-suffix → US market
    return ""


def is_market_open(pair: str, asset_class: str = "crypto") -> bool:
    """Return True if the market for this pair is currently open.

    Crypto always returns True (24/7 trading).
    For equity, delegates to exchange_calendars for holiday-aware checking.
    Unknown pairs or unavailable calendars are treated as open (fail-safe).
    """
    if asset_class != "equity":
        return True

    if not _XCALS_AVAILABLE:
        logger.debug("exchange_calendars not installed — allowing all equity pairs")
        return True

    suffix = _exchange_suffix(pair)
    if suffix is None:
        logger.debug(f"market_hours: unknown pair format '{pair}', allowing")
        return True

    mic = _SUFFIX_TO_MIC.get(suffix)
    if mic is None:
        logger.debug(f"market_hours: no MIC for suffix '{suffix}', allowing")
        return True

    cal = _get_calendar(mic)
    if cal is None:
        return True  # calendar unavailable — fail open

    try:
        now = pd.Timestamp.now(tz="UTC")
        return bool(cal.is_open_on_minute(now))
    except Exception as exc:
        logger.debug(f"market_hours: calendar check failed for '{mic}': {exc}")
        return True


def market_status_label(pair: str, asset_class: str = "crypto") -> str:
    """Human-readable status string for logging/debugging."""
    if asset_class != "equity":
        return "open (crypto)"
    suffix = _exchange_suffix(pair)
    mic = _SUFFIX_TO_MIC.get(suffix or "", "") if suffix is not None else ""
    if not mic:
        return "open (unknown exchange)"
    state = "OPEN" if is_market_open(pair, asset_class) else "CLOSED"
    return f"{state} ({mic})"
