"""
On-demand / nightly OHLCV gap-fill.

Lighter sibling of ``history_bulk_backfill``: keeps the most recent N days
hot for each tracked symbol. Designed to run frequently (cycle hook /
nightly cron) without saturating public APIs — it only fetches the gap
between the latest persisted ``ts`` and ``now``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from src.analysis.history_bulk_backfill import (
    _profile_exchange,
    bulk_backfill_symbol,
)
from src.utils.logger import get_logger
from src.utils.stats import StatsDB

logger = get_logger("analysis.history_backfill")


def gapfill_symbol(
    profile: str,
    symbol: str,
    granularities: Iterable[str] = ("ONE_HOUR", "ONE_DAY"),
    lookback_days: int = 30,
    stats_db: Optional[StatsDB] = None,
) -> dict:
    """Gap-fill the last ``lookback_days`` for a symbol. Returns
    ``{granularity: rows_written}``."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(lookback_days))
    return bulk_backfill_symbol(
        profile=profile,
        symbol=symbol,
        granularities=granularities,
        since=start,
        until=end,
        stats_db=stats_db,
    )


def gapfill_universe(
    profile: str,
    symbols: Iterable[str],
    granularities: Iterable[str] = ("ONE_HOUR", "ONE_DAY"),
    lookback_days: int = 30,
    stats_db: Optional[StatsDB] = None,
) -> dict:
    """Gap-fill many symbols. Returns ``{symbol: {granularity: rows}}``."""
    out: dict = {}
    for sym in symbols:
        try:
            out[sym] = gapfill_symbol(
                profile, sym, granularities, lookback_days, stats_db
            )
        except Exception as e:
            logger.warning(f"gapfill {sym} failed: {e}")
            out[sym] = {}
    return out


def coverage_summary(profile: str, stats_db: Optional[StatsDB] = None) -> list[dict]:
    """Return a list of ``{symbol, granularity, first_ts, last_ts, count}``
    rows for the current profile. Useful for ops / dashboard."""
    db = stats_db or StatsDB()
    exchange = _profile_exchange(profile)
    return db.get_backfill_progress(exchange=exchange)
