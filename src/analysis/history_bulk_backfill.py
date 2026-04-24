"""
Bulk historical-OHLCV backfill from public sources.

Fills the ``historical_candles`` table for any tracked symbol on a profile,
walking back to the earliest available data each source supports. Resumable
via the ``backfill_progress`` table — repeated runs only fetch new ground.

Triggered by:
* ``scripts/bulk_backfill.py`` CLI (operator-driven one-shot per profile).
* ``schedule_symbol_backfill()`` from the universe scanner — fires a
  background fill the first time a symbol enters the universe.

Domain isolation: source selection is profile-scoped via
``select_sources(profile)`` and adapters never write rows for an exchange
they don't belong to (``exchange`` is set explicitly on every upsert).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from src.analysis.history_sources import (
    GRAN_SECONDS,
    HistoricalSource,
    select_sources,
)
from src.utils.logger import get_logger
from src.utils.stats import StatsDB

logger = get_logger("analysis.history_bulk_backfill")

# Max gap (in granularity units) before we consider the series "complete".
_GAP_TOLERANCE_BARS: dict[str, int] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 24,
    "FIFTEEN_MINUTE": 16,
    "ONE_HOUR": 6,
    "SIX_HOUR": 2,
    "ONE_DAY": 2,
}


# Map profile → exchange string used in the DB.
_PROFILE_EXCHANGE: dict[str, str] = {
    "coinbase": "coinbase",
    "coinbase_paper": "coinbase_paper",
    "ibkr": "ibkr",
}


def _profile_exchange(profile: str) -> str:
    return _PROFILE_EXCHANGE.get(profile.lower(), profile.lower())


def bulk_backfill_symbol(
    profile: str,
    symbol: str,
    granularities: Iterable[str],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    stats_db: Optional[StatsDB] = None,
    sources: Optional[list[HistoricalSource]] = None,
) -> dict:
    """Fill OHLCV history for a single symbol across the requested granularities.

    Returns ``{granularity: rows_written}``.
    """
    db = stats_db or StatsDB()
    exchange = _profile_exchange(profile)
    src_list = sources or select_sources(profile)
    if not src_list:
        logger.warning(f"No sources for profile={profile!r}")
        return {}
    until_dt = until or datetime.now(timezone.utc)
    since_dt = since or (until_dt - timedelta(days=365 * 5))
    summary: dict[str, int] = {}
    for gran in granularities:
        if gran not in GRAN_SECONDS:
            logger.warning(f"Skipping unknown granularity {gran!r}")
            continue
        # Resume: skip ranges already covered.
        coverage = db.get_candles_coverage(exchange, symbol, gran)
        # Walk from since_dt to (coverage.first_ts) and (coverage.last_ts) to until_dt.
        ranges = _missing_ranges(coverage, since_dt, until_dt, gran)
        rows_written = 0
        for r_start, r_end in ranges:
            db.update_backfill_progress(
                exchange, symbol, gran,
                earliest_ts=None, latest_ts=None,
                row_count=0, last_source="(starting)", status="running",
            )
            for src in src_list:
                try:
                    candles = src.fetch(symbol, gran, r_start, r_end)
                except Exception as e:
                    logger.warning(
                        f"Source {src.name} failed for {symbol} {gran}: {e}"
                    )
                    continue
                if not candles:
                    continue
                written = db.upsert_candles(
                    exchange, symbol, gran, candles, source=src.name
                )
                rows_written += written
                # Record progress per source success.
                ts_values = [c["ts"] for c in candles if c.get("ts")]
                if ts_values:
                    db.update_backfill_progress(
                        exchange, symbol, gran,
                        earliest_ts=min(ts_values),
                        latest_ts=max(ts_values),
                        row_count=written,
                        last_source=src.name,
                        status="ok",
                    )
                logger.info(
                    f"Backfill {profile}/{symbol}/{gran} via {src.name}: "
                    f"{written} new rows ({r_start.date()}→{r_end.date()})"
                )
                # Stop trying further sources once we got data for this range.
                break
            else:
                db.update_backfill_progress(
                    exchange, symbol, gran,
                    earliest_ts=None, latest_ts=None,
                    row_count=0, last_source="none",
                    status="error",
                    error_message="all sources exhausted with no data",
                )
        summary[gran] = rows_written
    return summary


def bulk_backfill_universe(
    profile: str,
    symbols: Iterable[str],
    granularities: Iterable[str],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    stats_db: Optional[StatsDB] = None,
    sources: Optional[list[HistoricalSource]] = None,
) -> dict:
    """Bulk backfill a list of symbols. Returns
    ``{symbol: {granularity: rows_written}}``."""
    out: dict = {}
    for sym in symbols:
        try:
            out[sym] = bulk_backfill_symbol(
                profile, sym, granularities,
                since=since, until=until,
                stats_db=stats_db, sources=sources,
            )
        except Exception as e:
            logger.warning(f"bulk_backfill_symbol({sym}) failed: {e}")
            out[sym] = {}
    return out


def _missing_ranges(
    coverage: dict,
    since: datetime,
    until: datetime,
    granularity: str,
) -> list[tuple[datetime, datetime]]:
    """Compute missing ranges around current coverage."""
    if since >= until:
        return []
    first = coverage.get("first_ts")
    last = coverage.get("last_ts")
    if first is None or last is None:
        return [(since, until)]
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    seconds = GRAN_SECONDS[granularity]
    bars_tol = _GAP_TOLERANCE_BARS.get(granularity, 2)
    tol = timedelta(seconds=seconds * bars_tol)
    ranges: list[tuple[datetime, datetime]] = []
    if since < first - tol:
        ranges.append((since, first))
    if until > last + tol:
        ranges.append((last, until))
    return ranges


# ─── Background scheduling for new symbols ─────────────────────────────────


_active_backfills: set = set()
_active_backfills_lock = threading.Lock()


def schedule_symbol_backfill(
    profile: str,
    symbol: str,
    granularities: Iterable[str] = ("ONE_HOUR", "ONE_DAY"),
    since: Optional[datetime] = None,
) -> bool:
    """Run ``bulk_backfill_symbol`` in a daemon thread. De-duplicates so the
    same symbol can't be backfilled twice concurrently. Returns True if
    scheduled, False if already running."""
    key = f"{profile}:{symbol}:{':'.join(granularities)}"
    with _active_backfills_lock:
        if key in _active_backfills:
            return False
        _active_backfills.add(key)

    def _runner():
        try:
            bulk_backfill_symbol(profile, symbol, granularities, since=since)
        except Exception as e:
            logger.warning(f"Background backfill {key} failed: {e}")
        finally:
            with _active_backfills_lock:
                _active_backfills.discard(key)

    thread = threading.Thread(
        target=_runner, name=f"backfill-{symbol}", daemon=True
    )
    thread.start()
    logger.info(f"📥 Scheduled background backfill: {key}")
    return True
