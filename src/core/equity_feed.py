"""
Equity Market Data Feed — free Yahoo Finance data for paper-mode equity trading.

Provides real price data, OHLCV candles, and instrument discovery for
IBClient and NordnetClient paper modes so the analysis pipeline, universe
scanner, and LLM screener actually have data to work with.

Uses the Yahoo Finance v8 chart API directly via ``requests`` (no yfinance
dependency needed).  This avoids the ``curl_cffi`` / ``fc.yahoo.com`` cookie
issues that plague ``yfinance`` inside Docker containers.

This module is **only** used in paper mode.  Live IBKR uses ``ib_insync``
and live Nordnet will use its own REST API.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests as _http

from src.utils.logger import get_logger

logger = get_logger("equity_feed")

# ── Constants ────────────────────────────────────────────────────────────

_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
_TIMEOUT = 15  # seconds

# ── Pair ↔ Ticker conversion helpers ─────────────────────────────────────

# Internal pair format: "AAPL-USD", "VOLV-B.ST-SEK"
# Yahoo ticker format:  "AAPL",     "VOLV-B.ST"

_YAHOO_EXCHANGE_SUFFIXES = {
    "ST": "SEK",   # OMX Stockholm
    "HE": "EUR",   # Helsinki
    "CO": "DKK",   # Copenhagen
    "OL": "NOK",   # Oslo
    "L": "GBP",    # London
    "DE": "EUR",   # XETRA
    "PA": "EUR",   # Paris
    "MI": "EUR",   # Milan
    "AS": "EUR",   # Amsterdam
    "SW": "CHF",   # Swiss
    "TO": "CAD",   # Toronto
    "AX": "AUD",   # Australia
    "T": "JPY",    # Tokyo
    "HK": "HKD",   # Hong Kong
}


def pair_to_yahoo(pair: str) -> str:
    """Convert internal pair format to a Yahoo Finance ticker.

    ``"AAPL-USD"`` → ``"AAPL"``
    ``"VOLV-B.ST-SEK"`` → ``"VOLV-B.ST"``
    ``"ABB.ST-SEK"``    → ``"ABB.ST"``
    """
    parts = pair.upper().split("-")
    if len(parts) <= 1:
        return pair.upper()
    # If last segment looks like a currency code (3 letters), strip it
    if len(parts[-1]) == 3 and parts[-1].isalpha():
        return "-".join(parts[:-1])
    return pair.upper()


def yahoo_to_pair(ticker: str, default_currency: str = "USD") -> str:
    """Convert a Yahoo ticker back to internal pair format.

    ``"AAPL"``      → ``"AAPL-USD"``
    ``"VOLV-B.ST"`` → ``"VOLV-B.ST-SEK"``
    """
    upper = ticker.upper()
    # Check for exchange suffix to infer currency
    for suffix, currency in _YAHOO_EXCHANGE_SUFFIXES.items():
        if upper.endswith(f".{suffix}"):
            return f"{upper}-{currency}"
    return f"{upper}-{default_currency}"


# ── Granularity mapping (internal → Yahoo API) ──────────────────────────

_GRANULARITY_MAP: dict[str, tuple[str, str]] = {
    "ONE_MINUTE": ("1m", "5d"),
    "FIVE_MINUTE": ("5m", "60d"),
    "FIFTEEN_MINUTE": ("15m", "60d"),
    "ONE_HOUR": ("1h", "730d"),
    "ONE_DAY": ("1d", "2y"),
}


# ── Default universe lists (well-known liquid tickers) ───────────────────

# US Large-Cap (S&P 500 core)
US_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "V", "JNJ", "UNH", "XOM", "PG", "MA", "HD", "CVX", "MRK",
    "ABBV", "PEP", "KO", "COST", "AVGO", "LLY", "TMO", "MCD", "WMT",
    "CSCO", "ACN", "ABT", "DHR", "NEE", "PM", "TXN", "UPS", "RTX",
    "LOW", "MS", "BMY", "AMGN", "HON", "IBM", "INTC", "QCOM", "CAT",
    "GE", "DE", "BA", "AMAT", "ADP",
]

# OMX Stockholm (top Nordic blue chips)
OMX_UNIVERSE = [
    "VOLV-B.ST", "ERIC-B.ST", "ABB.ST", "ASSA-B.ST", "ATCO-A.ST",
    "SEB-A.ST", "SHB-A.ST", "SWED-A.ST", "SAND.ST", "SKF-B.ST",
    "HEXA-B.ST", "ALFA.ST", "INVE-B.ST", "ESSITY-B.ST", "BOL.ST",
    "ELUX-B.ST", "TELIA.ST", "KINV-B.ST", "HM-B.ST", "SINCH.ST",
    "SAAB-B.ST", "NIBE-B.ST", "SWMA.ST", "EVO.ST", "GETI-B.ST",
]


# ── Caching layer (avoid hammering Yahoo on every cycle) ─────────────────

class _Cache:
    """Simple thread-safe TTL cache for market data."""

    def __init__(self, default_ttl: float = 120.0):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.time() - ts > self._default_ttl:
                del self._store[key]
                return None
            return val

    def put(self, key: str, val: Any, ttl: float | None = None) -> None:
        with self._lock:
            self._store[key] = (time.time(), val)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_price_cache = _Cache(default_ttl=60.0)    # prices: 60s TTL
_candle_cache = _Cache(default_ttl=300.0)   # candles: 5min TTL
_detail_cache = _Cache(default_ttl=600.0)   # detailed discovery: 10min TTL


# ── Low-level Yahoo Finance API caller ───────────────────────────────────

def _fetch_chart(
    yahoo_ticker: str,
    interval: str = "1d",
    range_str: str = "1mo",
) -> dict | None:
    """Call Yahoo Finance v8 chart API. Returns parsed JSON or None on failure."""
    url = f"{_BASE_URL}/{yahoo_ticker}"
    params = {"interval": interval, "range": range_str}
    try:
        resp = _http.get(url, headers=_HEADERS, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.debug(f"Yahoo API {resp.status_code} for {yahoo_ticker}")
            return None
        data = resp.json()
        results = data.get("chart", {}).get("result")
        if not results:
            error = data.get("chart", {}).get("error")
            if error:
                logger.debug(f"Yahoo API error for {yahoo_ticker}: {error}")
            return None
        return results[0]
    except Exception as e:
        logger.debug(f"Yahoo API request failed for {yahoo_ticker}: {e}")
        return None


# ── Public API ───────────────────────────────────────────────────────────

def is_available() -> bool:
    """Return True — the feed uses ``requests`` which is always available."""
    return True


def get_current_price(pair: str) -> float:
    """Fetch current price for an equity pair via Yahoo Finance.

    Returns 0.0 on failure (same contract as ExchangeClient).
    """
    cached = _price_cache.get(f"price:{pair}")
    if cached is not None:
        return cached

    yahoo_sym = pair_to_yahoo(pair)
    result = _fetch_chart(yahoo_sym, interval="1d", range_str="1d")
    if result is None:
        return 0.0

    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice", 0)
    if price and price > 0:
        price = float(price)
        _price_cache.put(f"price:{pair}", price)
        return price

    # Fallback: last close from the quote data
    quotes = result.get("indicators", {}).get("quote", [{}])[0]
    closes = quotes.get("close", [])
    for c in reversed(closes):
        if c is not None and c > 0:
            price = float(c)
            _price_cache.put(f"price:{pair}", price)
            return price

    return 0.0


def get_candles(
    pair: str, granularity: str = "ONE_HOUR", limit: int = 200
) -> list[dict]:
    """Fetch OHLCV candles via Yahoo Finance.

    Returns list of dicts matching the ExchangeClient candle format:
    ``[{time, open, high, low, close, volume}, ...]``
    """
    cache_key = f"candles:{pair}:{granularity}:{limit}"
    cached = _candle_cache.get(cache_key)
    if cached is not None:
        return cached

    yahoo_sym = pair_to_yahoo(pair)
    interval, range_str = _GRANULARITY_MAP.get(granularity, ("1d", "2y"))

    result = _fetch_chart(yahoo_sym, interval=interval, range_str=range_str)
    if result is None:
        return []

    timestamps = result.get("timestamp", [])
    quotes = result.get("indicators", {}).get("quote", [{}])[0]
    opens = quotes.get("open", [])
    highs = quotes.get("high", [])
    lows = quotes.get("low", [])
    closes = quotes.get("close", [])
    volumes = quotes.get("volume", [])

    candles: list[dict] = []
    for i, ts in enumerate(timestamps):
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        lo = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        v = volumes[i] if i < len(volumes) else 0

        # Skip candles with missing data
        if any(x is None for x in (o, h, lo, c)):
            continue

        candles.append({
            "time": int(ts),
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
            "volume": float(v or 0),
        })

    # Trim to requested limit (most recent candles)
    if len(candles) > limit:
        candles = candles[-limit:]

    if candles:
        _candle_cache.put(cache_key, candles)

    return candles


def discover_pairs(
    exchange_id: str,
    quote_currencies: list[str] | None = None,
    never_trade: list[str] | None = None,
    only_trade: list[str] | None = None,
) -> list[str]:
    """Return a list of tradable equity pairs for the given exchange.

    Uses hardcoded universe lists of liquid, well-known tickers and
    validates them against Yahoo Finance.
    """
    if only_trade:
        return list(only_trade)

    never = set(never_trade or [])

    # Pick the right universe based on exchange
    if exchange_id == "nordnet":
        raw_tickers = list(OMX_UNIVERSE)
        default_currency = "SEK"
    else:
        raw_tickers = list(US_UNIVERSE)
        default_currency = "USD"

    # Convert to internal pair format
    pairs = [yahoo_to_pair(t, default_currency) for t in raw_tickers]

    # Apply currency filter
    if quote_currencies:
        qc_set = {c.upper() for c in quote_currencies}
        pairs = [p for p in pairs if p.split("-")[-1] in qc_set]

    # Apply never-trade filter
    if never:
        pairs = [p for p in pairs if p not in never]

    return pairs


def discover_pairs_detailed(
    exchange_id: str,
    quote_currencies: list[str] | None = None,
    never_trade: list[str] | None = None,
    only_trade: list[str] | None = None,
) -> list[dict]:
    """Return detailed metadata for equity pairs (volume, 24h change etc).

    This powers the universe scanner's Stage 1 for equity exchanges.
    Uses batch requests to Yahoo Finance to minimize API calls.
    """
    cache_key = f"detailed:{exchange_id}"
    cached = _detail_cache.get(cache_key)
    if cached is not None:
        return cached

    pairs = discover_pairs(
        exchange_id=exchange_id,
        quote_currencies=quote_currencies,
        never_trade=never_trade,
        only_trade=only_trade,
    )

    if not pairs:
        return []

    results: list[dict] = []

    # Fetch metadata for each pair (batched with pauses to avoid rate limits)
    _BATCH_SIZE = 8
    _BATCH_PAUSE = 1.5  # seconds between batches

    for batch_idx in range(0, len(pairs), _BATCH_SIZE):
        if batch_idx > 0:
            time.sleep(_BATCH_PAUSE)

        batch = pairs[batch_idx : batch_idx + _BATCH_SIZE]
        for pair in batch:
            yahoo_sym = pair_to_yahoo(pair)
            chart = _fetch_chart(yahoo_sym, interval="1d", range_str="5d")

            parts = pair.split("-")
            base = "-".join(parts[:-1]) if len(parts) > 1 else parts[0]
            quote = parts[-1] if len(parts) > 1 else "USD"

            if chart is None:
                # Include pair with zero metadata (better than skipping)
                results.append({
                    "product_id": pair,
                    "base_currency_id": base,
                    "quote_currency_id": quote,
                    "base_min_size": "1",
                    "quote_min_size": "1",
                    "volume_24h": "0",
                    "price_percentage_change_24h": "0",
                })
                continue

            meta = chart.get("meta", {})
            last_price = float(meta.get("regularMarketPrice", 0) or 0)
            prev_close = float(meta.get("chartPreviousClose", 0) or 0)
            # Volume from the most recent trading session
            quotes = chart.get("indicators", {}).get("quote", [{}])[0]
            vol_list = quotes.get("volume", [])
            volume = float(vol_list[-1]) if vol_list and vol_list[-1] else 0

            pct_change = 0.0
            if prev_close > 0 and last_price > 0:
                pct_change = ((last_price - prev_close) / prev_close) * 100

            results.append({
                "product_id": pair,
                "base_currency_id": base,
                "quote_currency_id": quote,
                "base_min_size": "1",
                "quote_min_size": "1",
                "volume_24h": str(volume * last_price),  # approx notional volume
                "price_percentage_change_24h": str(round(pct_change, 4)),
            })

    if results:
        _detail_cache.put(cache_key, results)

    logger.info(
        f"📊 Equity discovery ({exchange_id}): {len(results)} instruments with metadata"
    )
    return results
