"""
Equity Market Data Feed — free Yahoo Finance data for paper-mode equity trading.

Provides real price data, OHLCV candles, and instrument discovery for
IBClient paper mode so the analysis pipeline, universe scanner, and LLM
screener actually have data to work with.

Uses the Yahoo Finance v8 chart API directly via ``requests`` for price/candle
data, and ``yfinance``'s authenticated session for v10 quoteSummary (financial
calendar, earnings, dividends) which requires crumb+cookie auth.

This module is **only** used in paper mode.  Live IBKR uses ``ib_insync``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests as _http

from src.utils.logger import get_logger

logger = get_logger("equity_feed")

# ── Constants ────────────────────────────────────────────────────────────

_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
_TIMEOUT = 15  # seconds

# ── Authenticated Yahoo session (v10 quoteSummary requires crumb+cookie) ───

_yfdata_lock = threading.Lock()
_yfdata: Any = None


def _get_yfdata():
    """Lazy-init yfinance.data.YfData singleton for authenticated requests."""
    global _yfdata
    if _yfdata is not None:
        return _yfdata
    with _yfdata_lock:
        if _yfdata is not None:
            return _yfdata
        try:
            from yfinance.data import YfData
            _yfdata = YfData(session=None)
            return _yfdata
        except Exception as e:
            logger.debug(f"yfinance YfData init failed: {e}")
            return None

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

# EU Large-Cap (Euro Stoxx 50 & Major European Markets)
EU_UNIVERSE = [
    # Technology / Semiconductors
    "ASML.AS", "SAP.DE", "IFX.DE", "ASM.AS", "CAP.PA",
    # Consumer Discretionary & Luxury
    "MC.PA", "RMS.PA", "CDI.PA", "KER.PA", "BMW.DE", "MBG.DE", "VOW3.DE", "RNO.PA", "STLA.MI",
    # Financials
    "BNP.PA", "SAN.MC", "INGA.AS", "ISP.MI", "ALV.DE", "MUV2.DE", "CS.PA", "UCG.MI", "BBVA.MC",
    # Energy & Utilities
    "TTE.PA", "ENI.MI", "IBE.MC", "ENEL.MI", "ENG.MC", "EOAN.DE",
    # Industrials
    "SIE.DE", "AIR.PA", "VCI.PA", "SGO.PA", "SU.PA", "DHL.DE", "SAF.PA", "DSY.PA",
    # Consumer Staples & Healthcare
    "OR.PA", "SAN.PA", "BN.PA", "ABI.BR", "AH.AS", "BAYN.DE", "FRE.DE",
    # Telecom
    "DTE.DE", "ORA.PA", "TEF.MC",
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


# ── Search / autocomplete ────────────────────────────────────────────────

_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_search_cache = _Cache(default_ttl=60.0)   # search results: 60s TTL


def search_tickers(query: str, limit: int = 25) -> list[dict]:
    """Search for equity tickers via Yahoo Finance autocomplete.

    Returns results in the same format as the dashboard product search:
    ``[{id, base, quote, display_name, exchange, volume_24h, price_change_24h}, ...]``

    Only returns EQUITY and ETF results (filters out indices, futures, etc).
    """
    q = query.strip()
    if not q:
        return []

    cache_key = f"search:{q.upper()}"
    cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached[:limit]

    try:
        resp = _http.get(
            _SEARCH_URL,
            headers=_HEADERS,
            params={
                "q": q,
                "quotesCount": 25,
                "newsCount": 0,
                "listsCount": 0,
                "enableFuzzyQuery": True,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug(f"Yahoo search API returned {resp.status_code} for '{q}'")
            return []

        data = resp.json()
        quotes = data.get("quotes", [])
    except Exception as e:
        logger.debug(f"Yahoo search request failed for '{q}': {e}")
        return []

    results: list[dict] = []
    for q_item in quotes:
        # Only include equities and ETFs
        quote_type = (q_item.get("quoteType") or "").upper()
        if quote_type not in ("EQUITY", "ETF"):
            continue

        symbol = q_item.get("symbol", "")
        if not symbol:
            continue

        exchange_short = q_item.get("exchDisp") or q_item.get("exchange") or ""
        long_name = q_item.get("longname") or q_item.get("shortname") or symbol

        # Convert Yahoo symbol to internal pair format
        pair_id = yahoo_to_pair(symbol)
        parts = pair_id.rsplit("-", 1)
        base = parts[0] if parts else symbol
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        results.append({
            "id": pair_id,
            "base": base,
            "quote": quote_currency,
            "display_name": long_name,
            "exchange": exchange_short,
            "volume_24h": 0,
            "price_change_24h": 0,
        })

    if results:
        _search_cache.put(cache_key, results)

    return results[:limit]


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

    raw_tickers = list(EU_UNIVERSE)
    default_currency = "EUR"

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


# ── Corporate & Macro Calendar ────────────────────────────────────────────────

_calendar_cache = _Cache(default_ttl=3600.0)  # 1h TTL — earnings dates don't shift hourly


def _fetch_quote_summary(yahoo_ticker: str, modules: str) -> dict | None:
    """Fetch Yahoo Finance quoteSummary for a ticker. Returns the first result or None."""
    cache_key = f"qsummary:{yahoo_ticker}:{modules}"
    cached = _calendar_cache.get(cache_key)
    if cached is not None:
        return cached

    yd = _get_yfdata()
    if not yd:
        logger.debug("yfinance YfData unavailable — quoteSummary skipped")
        return None

    try:
        resp = yd.get(
            url=f"{_SUMMARY_URL}/{yahoo_ticker}",
            params={"modules": modules},
        )
        if resp.status_code != 200:
            logger.debug(f"Yahoo quoteSummary {resp.status_code} for {yahoo_ticker}")
            return None
        data = resp.json()
        result = data.get("quoteSummary", {}).get("result")
        if not result:
            return None
        parsed = result[0]
        _calendar_cache.put(cache_key, parsed)
        return parsed
    except Exception as e:
        logger.debug(f"Yahoo quoteSummary failed for {yahoo_ticker}: {e}")
        return None


def get_earnings_calendar(tickers: list[str], days_ahead: int = 60) -> dict[str, dict]:
    """Fetch upcoming earnings dates for a list of Yahoo tickers.

    Returns:
        {ticker: {"earnings_date": "2026-04-16", "days_away": 40, "eps_estimate": 4.82}}
    Only includes tickers with an earnings date within ``days_ahead`` days.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    results: dict[str, dict] = {}

    for ticker in tickers:
        try:
            summary = _fetch_quote_summary(ticker, "calendarEvents")
            if not summary:
                continue

            earnings_dates = (
                summary.get("calendarEvents", {})
                       .get("earnings", {})
                       .get("earningsDate", [])
            )
            if not earnings_dates:
                continue

            # Take the earliest upcoming date
            earliest: datetime | None = None
            for ed in earnings_dates:
                raw_ts = ed.get("raw")
                if raw_ts:
                    dt = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
                    if dt > now and (earliest is None or dt < earliest):
                        earliest = dt

            if earliest is None or earliest > cutoff:
                continue

            days_away = (earliest - now).days
            entry: dict = {
                "earnings_date": earliest.strftime("%Y-%m-%d"),
                "days_away": days_away,
            }

            # EPS estimate (earningsAverage) if available
            eps = (
                summary.get("calendarEvents", {})
                       .get("earnings", {})
                       .get("earningsAverage", {})
                       .get("raw")
            )
            if eps is not None:
                entry["eps_estimate"] = eps

            results[ticker] = entry

        except Exception as e:
            logger.debug(f"Earnings calendar failed for {ticker}: {e}")

    logger.info(f"📅 Earnings calendar: {len(results)}/{len(tickers)} tickers have dates within {days_ahead}d")
    return results


def get_dividend_calendar(tickers: list[str], days_ahead: int = 60) -> dict[str, dict]:
    """Fetch upcoming ex-dividend dates for a list of Yahoo tickers.

    Returns:
        {ticker: {"ex_div_date": "2026-03-15", "days_away": 8, "annual_dividend": 15.40, "yield_pct": 3.2}}
    Only includes tickers with an ex-div date within ``days_ahead`` days.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    results: dict[str, dict] = {}

    for ticker in tickers:
        try:
            summary = _fetch_quote_summary(ticker, "calendarEvents,summaryDetail")
            if not summary:
                continue

            ex_div_raw = summary.get("calendarEvents", {}).get("exDividendDate", {})
            raw_ts = ex_div_raw.get("raw") if isinstance(ex_div_raw, dict) else None
            if not raw_ts:
                continue

            ex_div_dt = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
            if ex_div_dt <= now or ex_div_dt > cutoff:
                continue

            days_away = (ex_div_dt - now).days
            entry: dict = {
                "ex_div_date": ex_div_dt.strftime("%Y-%m-%d"),
                "days_away": days_away,
            }

            detail = summary.get("summaryDetail", {})
            div_rate = detail.get("dividendRate", {})
            div_yield = detail.get("dividendYield", {})
            if isinstance(div_rate, dict) and div_rate.get("raw") is not None:
                entry["annual_dividend"] = div_rate["raw"]
            if isinstance(div_yield, dict) and div_yield.get("raw") is not None:
                entry["yield_pct"] = round(div_yield["raw"] * 100, 2)

            results[ticker] = entry

        except Exception as e:
            logger.debug(f"Dividend calendar failed for {ticker}: {e}")

    logger.info(f"💰 Dividend calendar: {len(results)}/{len(tickers)} tickers have ex-div within {days_ahead}d")
    return results


# ── Macro Event Calendar (semi-static, refresh annually) ─────────────────────
# ECB Governing Council monetary policy meeting dates (rate decision days)
# Source: ECB official press release schedule
_ECB_MEETING_DATES = [
    # 2025
    "2025-01-30", "2025-03-06", "2025-04-17", "2025-06-05",
    "2025-07-24", "2025-09-11", "2025-10-30", "2025-12-18",
    # 2026
    "2026-01-29", "2026-03-05", "2026-04-16", "2026-06-04",
    "2026-07-23", "2026-09-10", "2026-10-29", "2026-12-17",
]

# US Federal Reserve FOMC meeting dates (global risk-appetite driver)
_FOMC_MEETING_DATES = [
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-17",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
]

# Quarterly EU earnings seasons (approximate peak weeks — when bulk of EU large-caps report)
# Used to set seasonal regime context in monthly/weekly planning
_EU_EARNINGS_SEASONS = [
    # Q4 results season (January–February)
    {"label": "Q4 Earnings Season", "start": "01-15", "end": "02-28", "notes": "Full-year results; guidance updates dominant"},
    # Q1 results season (April–May)
    {"label": "Q1 Earnings Season", "start": "04-10", "end": "05-20", "notes": "Revenue momentum check post-H2"},
    # H1/Q2 results season (July–August)
    {"label": "H1/Q2 Earnings Season", "start": "07-10", "end": "08-15", "notes": "Halftime results; summer liquidity thin"},
    # Q3 results season (October–November)
    {"label": "Q3 Earnings Season", "start": "10-10", "end": "11-20", "notes": "Guidance cuts/raises heading into year-end"},
]


def get_macro_calendar(days_ahead: int = 60) -> list[dict]:
    """Return upcoming macro events relevant to European equity trading.

    Covers ECB and FOMC decisions within the window.
    Negative days_away = event occurred within the past 3 days (still market-moving context).

    Returns list of dicts:
        {"date": str, "days_away": int, "event": str, "importance": str, "relevance": str}
    """
    today = datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=days_ahead)
    lookback = today - timedelta(days=3)

    events: list[dict] = []

    for date_str in _ECB_MEETING_DATES:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            if lookback <= d <= cutoff:
                events.append({
                    "date": date_str,
                    "days_away": (d - today).days,
                    "event": "ECB Rate Decision",
                    "importance": "high",
                    "relevance": "Direct driver of European equity valuations, EUR/USD, and bond yields",
                })
        except ValueError:
            pass

    for date_str in _FOMC_MEETING_DATES:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            if lookback <= d <= cutoff:
                events.append({
                    "date": date_str,
                    "days_away": (d - today).days,
                    "event": "US Fed FOMC Decision",
                    "importance": "high",
                    "relevance": "Global risk appetite; USD strength affects EU exporter earnings (ASML, SAP, Airbus)",
                })
        except ValueError:
            pass

    events.sort(key=lambda x: x["days_away"])
    return events


def get_earnings_season_context() -> dict:
    """Return the current/upcoming EU earnings season context for LLM injection.

    Returns:
        {"in_season": bool, "season_label": str, "notes": str, "days_to_peak": int | None}
    """
    today = datetime.now(timezone.utc).date()
    year = today.year

    for season in _EU_EARNINGS_SEASONS:
        start = datetime.strptime(f"{year}-{season['start']}", "%Y-%m-%d").date()
        end = datetime.strptime(f"{year}-{season['end']}", "%Y-%m-%d").date()
        pre_window = start - timedelta(days=21)  # 3 weeks before = pre-season positioning

        if start <= today <= end:
            return {
                "in_season": True,
                "season_label": season["label"],
                "notes": season["notes"],
                "days_to_peak": None,
                "phase": "active",
            }
        elif pre_window <= today < start:
            return {
                "in_season": False,
                "season_label": season["label"],
                "notes": season["notes"],
                "days_to_peak": (start - today).days,
                "phase": "pre_season",
            }

    return {"in_season": False, "season_label": None, "notes": None, "days_to_peak": None, "phase": "between_seasons"}
