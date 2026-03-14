"""
Shared candle-fetching utility for backtests.

Extracted from dashboard routes so that planning activities and the
SettingsAdvisor can run backtests without importing the dashboard layer.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("backtesting.candle_fetch")

_COINBASE_MAX_CANDLES = 300  # Coinbase API hard limit is ~350; use 300 for safety


def fetch_candles(
    pair: str,
    days: int,
    is_equity: bool,
    exchange_client=None,
) -> list[dict]:
    """Fetch historical hourly candles for backtesting.

    Args:
        pair: Trading pair (e.g. "BTC-USD" or "ABB.ST-SEK").
        days: Number of days of history to fetch.
        is_equity: Whether the pair is an equity (Yahoo Finance) vs crypto (Coinbase).
        exchange_client: Optional Coinbase client instance. If None for crypto,
                         attempts to build one from environment.

    Returns:
        List of candle dicts sorted by timestamp ascending.
    """
    limit = days * 24  # hourly candles

    if is_equity:
        return _fetch_equity_candles(pair, limit)

    return _fetch_crypto_candles(pair, limit, exchange_client)


def _fetch_equity_candles(pair: str, limit: int) -> list[dict]:
    """Fetch equity candles via Yahoo Finance."""
    try:
        from src.core.equity_feed import get_candles as yf_candles
        candles = yf_candles(pair, granularity="ONE_HOUR", limit=limit)
        candles.sort(
            key=lambda c: int(c.get("time") or c.get("start") or 0)
            if isinstance(c, dict)
            else 0
        )
        return candles
    except Exception as e:
        logger.warning(f"Yahoo Finance candle fetch failed for {pair}: {e}")
        return []


def _fetch_crypto_candles(
    pair: str, limit: int, exchange_client=None
) -> list[dict]:
    """Fetch crypto candles via Coinbase, paginating as needed."""
    client = exchange_client
    if client is None:
        client = _build_coinbase_client()
    if client is None:
        logger.warning("No exchange client available for candle fetch")
        return []

    try:
        if limit <= _COINBASE_MAX_CANDLES:
            candles = client.get_candles(pair, granularity="ONE_HOUR", limit=limit)
            candles.sort(
                key=lambda c: int(c.get("start") or c.get("time") or 0)
                if isinstance(c, dict)
                else 0
            )
            return candles

        # Paginate: fetch chunks walking backwards from now
        all_candles: list[dict] = []
        remaining = limit
        end_ts = int(time.time())
        while remaining > 0:
            chunk_size = min(remaining, _COINBASE_MAX_CANDLES)
            start_ts = end_ts - (chunk_size * 3600)
            candles = client.get_candles(
                pair,
                granularity="ONE_HOUR",
                limit=chunk_size,
                start_time=start_ts,
                end_time=end_ts,
            )
            if not candles:
                break
            all_candles.extend(candles)
            remaining -= len(candles)
            end_ts = start_ts
            if len(candles) < chunk_size:
                break

        all_candles.sort(
            key=lambda c: int(c.get("start") or c.get("time") or 0)
            if isinstance(c, dict)
            else 0
        )
        return all_candles
    except Exception as e:
        logger.warning(f"Coinbase candle fetch failed for {pair}: {e}")
        return []


def _build_coinbase_client():
    """Attempt to build a Coinbase client from env vars (for headless use)."""
    try:
        api_key = os.environ.get("COINBASE_API_KEY", "")
        api_secret = os.environ.get("COINBASE_API_SECRET", "")
        if not api_key or not api_secret:
            return None
        from src.core.coinbase_client import CoinbaseClient
        return CoinbaseClient(api_key=api_key, api_secret=api_secret)
    except Exception:
        return None


def is_equity_pair(pair: str) -> bool:
    """Heuristic: equity pairs are plain ticker symbols without a crypto quote currency."""
    if not pair:
        return False
    crypto_quotes = {"USD", "EUR", "GBP", "USDT", "USDC", "BTC", "ETH"}
    parts = pair.split("-")
    if len(parts) == 2 and parts[1] in crypto_quotes:
        return False  # Crypto pair like BTC-EUR
    return True  # Likely equity ticker
