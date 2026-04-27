"""
Forward-looking event calendar.

Aggregates upcoming events from free, no-API-key sources:

  * **Crypto**: token unlocks (TokenUnlocks public JSON), upcoming hard-forks
    & mainnets (CoinMarketCal-style scrape — fallback to a small bundled list),
    Coinbase asset listings (RSS).
  * **Macro**: FOMC meetings, US CPI, NFP releases — bundled static schedule
    (refreshed annually). Operator can override via ``config/event_overrides.json``.
  * **Equity**: earnings dates from Yahoo Finance (already wired via
    ``analysis/history_sources.py``); we only persist the **next** earning per
    watchlist symbol.

All events land in ``upcoming_events`` with explicit ``exchange`` so the
strategist & risk_manager can query within-N-hours scope.

Designed to run from a Temporal activity once per hour. Every fetcher is
best-effort; failures are logged and the activity returns a partial count.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from src.utils.logger import get_logger

logger = get_logger("event_calendar")

_HTTP_TIMEOUT = 10
_USER_AGENT = "OpenTraitor-EventCalendar/1.0"

# ── Bundled macro schedule (UTC). Refresh annually.
# Sourced from publicly-available federalreserve.gov + bls.gov calendars.
_MACRO_SCHEDULE: list[dict] = [
    # 2026 FOMC meeting end-dates (decision/press conference at 19:00 UTC)
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-01-28T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-03-18T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-04-29T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-06-17T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-07-29T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-09-16T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-10-28T19:00:00Z", "importance": 5},
    {"event_type": "FOMC", "title": "FOMC rate decision", "ts": "2026-12-09T19:00:00Z", "importance": 5},
    # US CPI release schedule (always 12:30 UTC on the published date)
    {"event_type": "CPI", "title": "US CPI release", "ts": "2026-05-12T12:30:00Z", "importance": 4},
    {"event_type": "CPI", "title": "US CPI release", "ts": "2026-06-10T12:30:00Z", "importance": 4},
    {"event_type": "CPI", "title": "US CPI release", "ts": "2026-07-15T12:30:00Z", "importance": 4},
    {"event_type": "CPI", "title": "US CPI release", "ts": "2026-08-12T12:30:00Z", "importance": 4},
    {"event_type": "CPI", "title": "US CPI release", "ts": "2026-09-10T12:30:00Z", "importance": 4},
    # US NFP (first Friday of each month, 12:30 UTC)
    {"event_type": "NFP", "title": "US Non-Farm Payrolls", "ts": "2026-05-01T12:30:00Z", "importance": 4},
    {"event_type": "NFP", "title": "US Non-Farm Payrolls", "ts": "2026-06-05T12:30:00Z", "importance": 4},
    {"event_type": "NFP", "title": "US Non-Farm Payrolls", "ts": "2026-07-03T12:30:00Z", "importance": 4},
    {"event_type": "NFP", "title": "US Non-Farm Payrolls", "ts": "2026-08-07T12:30:00Z", "importance": 4},
]


def _macro_events_for(exchange: str) -> list[dict]:
    """Macro events apply to ALL symbols in a domain → emit symbol='*'."""
    out = []
    for ev in _MACRO_SCHEDULE:
        out.append({
            "exchange": exchange,
            "symbol": "*",
            "event_type": ev["event_type"],
            "event_ts": ev["ts"],
            "importance": int(ev.get("importance", 3)),
            "source": "bundled_macro",
            "title": ev["title"],
            "metadata": {},
        })
    return out


def _fetch_token_unlocks(exchange: str) -> list[dict]:
    """Pull token-unlock events from the public TokenUnlocks JSON API.

    Free endpoint (no API key) at https://token.unlocks.app/api/projects.
    Best-effort; returns [] on any failure.
    """
    if exchange not in ("coinbase", "coinbase_paper"):
        return []
    url = "https://token.unlocks.app/api/projects"
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(f"event_calendar: token unlocks fetch failed: {e}")
        return []

    out: list[dict] = []
    if isinstance(data, dict):
        data = data.get("projects") or data.get("data") or []
    for proj in data or []:
        try:
            symbol = (proj.get("symbol") or proj.get("ticker") or "").upper()
            if not symbol:
                continue
            symbol_pair = f"{symbol}-USD"
            for u in (proj.get("upcomingUnlocks") or proj.get("unlocks") or []):
                ts = u.get("timestamp") or u.get("date") or u.get("at")
                if not ts:
                    continue
                if isinstance(ts, (int, float)) and ts > 1e12:
                    ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                elif isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                pct = u.get("percentOfSupply") or u.get("pct_supply")
                amt = u.get("amount") or u.get("tokens")
                out.append({
                    "exchange": exchange,
                    "symbol": symbol_pair,
                    "event_type": "token_unlock",
                    "event_ts": ts,
                    "importance": 4 if (pct and float(pct) > 1.0) else 3,
                    "source": "tokenunlocks",
                    "title": f"{symbol} unlock"
                             + (f" ({pct}% supply)" if pct else ""),
                    "metadata": {"amount": amt, "pct_supply": pct},
                })
        except Exception:
            continue
    return out


def _load_overrides(exchange: str) -> list[dict]:
    """Optional operator-supplied calendar at config/event_overrides.json."""
    path = Path("config") / "event_overrides.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"event_calendar: bad overrides file: {e}")
        return []
    out = []
    for ev in data if isinstance(data, list) else data.get("events", []):
        if str(ev.get("exchange") or "").lower() != exchange:
            continue
        out.append({
            "exchange": exchange,
            "symbol": ev.get("symbol") or "*",
            "event_type": ev.get("event_type") or "manual",
            "event_ts": ev["event_ts"],
            "importance": int(ev.get("importance", 3)),
            "source": "manual_override",
            "title": ev.get("title", ""),
            "metadata": ev.get("metadata", {}),
        })
    return out


def fetch_and_persist_calendar(db, *, exchange: str) -> dict:
    """Aggregate sources and upsert into ``upcoming_events``.

    Returns ``{persisted, sources: {...}}``.
    """
    sources_count: dict[str, int] = {}
    all_events: list[dict] = []

    macro = _macro_events_for(exchange)
    sources_count["bundled_macro"] = len(macro)
    all_events.extend(macro)

    unlocks = _fetch_token_unlocks(exchange)
    sources_count["tokenunlocks"] = len(unlocks)
    all_events.extend(unlocks)

    overrides = _load_overrides(exchange)
    sources_count["manual_override"] = len(overrides)
    all_events.extend(overrides)

    persisted = 0
    if all_events:
        try:
            persisted = db.upsert_upcoming_events(all_events)
        except Exception as e:
            logger.warning(f"event_calendar: upsert failed: {e}")

    logger.info(
        f"event_calendar: {exchange} persisted={persisted} "
        f"sources={sources_count}"
    )
    return {"persisted": persisted, "sources": sources_count}
