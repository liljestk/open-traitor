"""Macro-factor candle universe.

The :mod:`market_factors` module fits asset returns onto a fixed basket
of macro factors (``^GSPC``, ``^VIX``, ``DX-Y.NYB``, ``BTC-USD`` by
default). For the regression to produce data, daily candles for these
factor symbols must be present in ``historical_candles``.

We persist them under a dedicated ``_factors_`` exchange so the same
candles can be reused by *both* the coinbase and ibkr profiles without
polluting either real-exchange namespace.

This module provides :func:`ensure_factor_candles` — a small, idempotent
helper that:

* Checks whether daily candles for each factor exist in
  ``historical_candles`` under ``exchange='_factors_'`` covering the
  requested window.
* If missing or stale, fetches them via Yahoo Finance
  (:func:`src.core.equity_feed.get_candles`) and upserts them.

Designed to be safe to call on every nightly run *and* on every new
follow event — the upsert is cheap when no new rows are required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from src.analysis.market_factors import DEFAULT_FACTORS
from src.utils.logger import get_logger

logger = get_logger("analysis.factor_universe")


FACTOR_EXCHANGE: str = "_factors_"
FACTOR_GRANULARITY: str = "ONE_DAY"
DEFAULT_LOOKBACK_DAYS: int = 400  # ~1.5y, covers the 252-day fit window.
STALENESS_DAYS: int = 3  # refetch if newest candle older than this.


def _newest_candle_age_days(stats_db, symbol: str) -> float | None:
    """Days since the most recent stored candle (None if symbol absent)."""
    try:
        rows = stats_db.get_candles_range(
            FACTOR_EXCHANGE, symbol, FACTOR_GRANULARITY,
            start=datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS * 2),
            limit=1,
        )
    except Exception as exc:
        logger.debug(f"factor_universe: range probe failed {symbol}: {exc}")
        return None
    if not rows:
        return None
    ts = rows[-1].get("ts")
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)


def _fetch_yahoo_daily(symbol: str, days: int) -> list[dict]:
    """Best-effort daily fetch from Yahoo Finance via equity_feed.

    Returns the candle dicts in the canonical
    ``{time, open, high, low, close, volume}`` shape, or ``[]`` on failure.
    Imports lazily so non-equity environments (no ``yfinance``) still work
    when this helper is unused.
    """
    try:
        from src.core.equity_feed import _fetch_chart  # type: ignore
    except Exception as exc:
        logger.warning(f"factor_universe: equity_feed unavailable: {exc}")
        return []

    # Choose the smallest range string covering ``days``.
    if days <= 35:
        range_str = "1mo"
    elif days <= 100:
        range_str = "3mo"
    elif days <= 200:
        range_str = "6mo"
    elif days <= 380:
        range_str = "1y"
    elif days <= 760:
        range_str = "2y"
    else:
        range_str = "5y"

    try:
        result = _fetch_chart(symbol, interval="1d", range_str=range_str)
    except Exception as exc:
        logger.warning(f"factor_universe: yahoo fetch failed {symbol}: {exc}")
        return []
    if not result:
        return []

    timestamps = result.get("timestamp", []) or []
    quotes = (result.get("indicators", {}) or {}).get("quote", [{}])[0] or {}
    opens = quotes.get("open", []) or []
    highs = quotes.get("high", []) or []
    lows = quotes.get("low", []) or []
    closes = quotes.get("close", []) or []
    volumes = quotes.get("volume", []) or []

    candles: list[dict] = []
    for i, ts in enumerate(timestamps):
        try:
            o = opens[i] if i < len(opens) else None
            h = highs[i] if i < len(highs) else None
            lo = lows[i] if i < len(lows) else None
            c = closes[i] if i < len(closes) else None
            v = volumes[i] if i < len(volumes) else 0
            if any(x is None for x in (o, h, lo, c)):
                continue
            candles.append({
                "ts": int(ts),
                "o": float(o),
                "h": float(h),
                "l": float(lo),
                "c": float(c),
                "v": float(v or 0),
            })
        except (TypeError, ValueError):
            continue
    return candles


def ensure_factor_candles(
    stats_db,
    *,
    factors: Sequence[str] = DEFAULT_FACTORS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    force: bool = False,
) -> dict[str, int]:
    """Make sure factor daily candles are loaded under ``_factors_``.

    Returns ``{symbol: rows_written}`` (0 means already fresh / nothing
    fetched). Network failures are logged and the symbol is reported with
    ``-1`` so callers can detect partial degradation without raising.
    """
    out: dict[str, int] = {}
    for sym in factors:
        try:
            age = _newest_candle_age_days(stats_db, sym)
            if not force and age is not None and age <= STALENESS_DAYS:
                out[sym] = 0
                continue
            candles = _fetch_yahoo_daily(sym, days=lookback_days)
            if not candles:
                logger.info(
                    f"factor_universe: no candles fetched for {sym} "
                    f"(age={age!r})"
                )
                out[sym] = -1
                continue
            written = stats_db.upsert_candles(
                exchange=FACTOR_EXCHANGE,
                symbol=sym,
                granularity=FACTOR_GRANULARITY,
                candles=candles,
                source="yahoo.factor",
            )
            out[sym] = int(written or 0)
            logger.info(
                f"factor_universe: {sym} written={written} "
                f"(was age={age!r}d, fetched={len(candles)})"
            )
        except Exception as exc:
            logger.warning(f"factor_universe: ensure failed {sym}: {exc}")
            out[sym] = -1
    return out


__all__ = [
    "FACTOR_EXCHANGE",
    "FACTOR_GRANULARITY",
    "DEFAULT_LOOKBACK_DAYS",
    "STALENESS_DAYS",
    "ensure_factor_candles",
]
