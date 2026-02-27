"""Equity market trading-hours gating.

Used by the orchestrator to skip analysis cycles when the exchange is closed,
avoiding LLM token waste on stale prices.

Pairs must include the Yahoo exchange suffix in the ticker for non-US exchanges
(e.g. ``NESTE.HE-EUR``, ``VOLV-B.ST-SEK``).  Plain tickers without a dot-suffix
(e.g. ``GE-USD``, ``AAPL-USD``) default to US/NYSE hours.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.utils.logger import get_logger

logger = get_logger("market_hours")


@dataclass(frozen=True)
class _Schedule:
    tz: str       # IANA timezone name
    open_h: int   # local open hour
    open_m: int   # local open minute
    close_h: int  # local close hour
    close_m: int  # local close minute


# Yahoo Finance exchange suffix → trading schedule.
# Key "" = US (no dot-suffix = NYSE / NASDAQ).
_SCHEDULES: dict[str, _Schedule] = {
    "":   _Schedule("America/New_York",   9, 30, 16,  0),  # NYSE / NASDAQ
    "ST": _Schedule("Europe/Stockholm",   9,  0, 17, 30),  # OMX Stockholm
    "HE": _Schedule("Europe/Helsinki",    9,  0, 17, 30),  # Helsinki (Nasdaq Nordic)
    "CO": _Schedule("Europe/Copenhagen",  9,  0, 17,  0),  # Copenhagen (Nasdaq Nordic)
    "OL": _Schedule("Europe/Oslo",        9,  0, 16, 30),  # Oslo Børs
    "L":  _Schedule("Europe/London",      8,  0, 16, 30),  # London Stock Exchange
    "DE": _Schedule("Europe/Berlin",      9,  0, 17, 30),  # XETRA / Frankfurt
    "PA": _Schedule("Europe/Paris",       9,  0, 17, 30),  # Euronext Paris
    "AS": _Schedule("Europe/Amsterdam",   9,  0, 17, 30),  # Euronext Amsterdam
    "MI": _Schedule("Europe/Rome",        9,  0, 17, 30),  # Borsa Italiana
    "SW": _Schedule("Europe/Zurich",      9,  0, 17, 30),  # SIX Swiss Exchange
    "TO": _Schedule("America/Toronto",    9, 30, 16,  0),  # Toronto Stock Exchange
    "AX": _Schedule("Australia/Sydney",  10,  0, 16,  0),  # ASX
    "T":  _Schedule("Asia/Tokyo",         9,  0, 15, 30),  # TSE (lunch break ignored)
    "HK": _Schedule("Asia/Hong_Kong",     9, 30, 16,  0),  # HKEX (lunch break ignored)
}


def _exchange_suffix(pair: str) -> str | None:
    """Extract the Yahoo exchange suffix from a pair name.

    The exchange is determined by the ``.XX`` part of the ticker — NOT the
    quote currency — so EUR-quoted Nordic pairs are handled correctly:

        "AAPL-USD"       → ""    (US, no dot-suffix → NYSE/NASDAQ)
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
        return suffix if suffix in _SCHEDULES else None

    # Plain ticker with no dot-suffix → treat as US market
    return ""


def is_market_open(pair: str, asset_class: str = "crypto") -> bool:
    """Return True if the market for this pair is currently open.

    Crypto always returns True (24/7 trading).
    For equity, checks the exchange's local trading hours Mon–Fri only.
    Unknown pair formats or missing schedules are treated as open (fail-safe).
    """
    if asset_class != "equity":
        return True

    suffix = _exchange_suffix(pair)
    if suffix is None:
        logger.debug(f"market_hours: unknown pair format '{pair}', allowing")
        return True

    schedule = _SCHEDULES.get(suffix)
    if schedule is None:
        logger.debug(f"market_hours: no schedule for suffix '{suffix}', allowing")
        return True

    now = datetime.datetime.now(tz=ZoneInfo(schedule.tz))

    # Weekend check (0=Mon … 6=Sun)
    if now.weekday() >= 5:
        return False

    open_t  = now.replace(hour=schedule.open_h,  minute=schedule.open_m,  second=0, microsecond=0)
    close_t = now.replace(hour=schedule.close_h, minute=schedule.close_m, second=0, microsecond=0)
    return open_t <= now < close_t


def market_status_label(pair: str, asset_class: str = "crypto") -> str:
    """Human-readable status string for logging/debugging."""
    if asset_class != "equity":
        return "open (crypto)"
    suffix = _exchange_suffix(pair)
    schedule = _SCHEDULES.get(suffix or "", None) if suffix is not None else None
    if schedule is None:
        return "open (unknown exchange)"
    tz_label  = schedule.tz.split("/")[-1]
    open_str  = f"{schedule.open_h:02d}:{schedule.open_m:02d}"
    close_str = f"{schedule.close_h:02d}:{schedule.close_m:02d}"
    state = "OPEN" if is_market_open(pair, asset_class) else "CLOSED"
    return f"{state} ({tz_label} {open_str}–{close_str})"
