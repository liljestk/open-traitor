"""
Historical OHLCV source adapters for the Catalyst Pattern Engine.

Defines a single ``HistoricalSource`` protocol and concrete adapters for
free public sources:

Equity (ibkr profile):
  * ``YahooFinanceSource``  — daily back to inception, hourly back ~2 years.
  * ``StooqSource``         — daily decades of history, no API key.

Crypto (coinbase profile):
  * ``CoinbaseSource``      — paginates back to product inception via the
    project's existing ``CoinbaseClient.get_candles``.
  * ``CryptoCompareSource`` — free public ``histoday``/``histohour`` endpoints.
  * ``BinanceSource``       — public ``/api/v3/klines`` (no API key).

All adapters return ``list[dict]`` rows with keys
``ts (datetime, UTC) | o | h | l | c | v``. They never raise on empty results
or transport errors — empty list is the failure signal so the bulk backfill
loop can fall through to the next source.

Domain isolation is enforced by a registry in ``select_sources(profile)``:
ibkr → equity sources only; coinbase → crypto sources only.
"""

from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Optional, Protocol

import requests

from src.utils.logger import get_logger

logger = get_logger("analysis.history_sources")

# Granularity → seconds (matches Coinbase + project conventions).
GRAN_SECONDS: dict[str, int] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "ONE_HOUR": 3600,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}

# Map our granularity labels onto Yahoo Finance intervals.
_YF_INTERVAL: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "ONE_HOUR": "60m",
    "SIX_HOUR": "1h",  # Yahoo has no 6h; resample upstream if needed.
    "ONE_DAY": "1d",
}

# Map onto Binance kline intervals.
_BINANCE_INTERVAL: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "ONE_HOUR": "1h",
    "SIX_HOUR": "6h",
    "ONE_DAY": "1d",
}


# ─── Token-bucket rate limiter ─────────────────────────────────────────────


@dataclass
class TokenBucket:
    """Trivial token-bucket — 1 token = 1 request. Thread-unsafe (single
    bulk-backfill worker; no contention)."""

    rate_per_sec: float
    capacity: int
    _tokens: float = 0.0
    _last: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)

    def take(self, n: int = 1) -> None:
        now = time.monotonic()
        self._tokens = min(
            self.capacity,
            self._tokens + (now - self._last) * self.rate_per_sec,
        )
        self._last = now
        if self._tokens >= n:
            self._tokens -= n
            return
        wait = (n - self._tokens) / self.rate_per_sec
        time.sleep(max(wait, 0.0))
        self._tokens = 0.0
        self._last = time.monotonic()


# ─── Protocol ──────────────────────────────────────────────────────────────


class HistoricalSource(Protocol):
    """Adapter contract for fetching historical OHLCV from a public source."""

    name: str
    domain: str  # 'equity' | 'crypto'

    def fetch(
        self,
        symbol: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        ...


# ─── Yahoo Chart API (equity, primary) ─────────────────────────────────────


class YahooChartSource:
    """Direct Yahoo chart-API client (no `yfinance` / no `fc.yahoo.com`).

    The ``yfinance`` library performs a cookie/crumb negotiation against
    ``fc.yahoo.com`` on every download. That host is blocked on many
    networks (corporate firewalls, container egress policies), which
    causes ``yfinance.download`` to silently return empty frames even
    though ``query{1,2}.finance.yahoo.com/v8/finance/chart`` is reachable
    and serves the same OHLCV without any auth.

    This adapter calls the chart endpoint directly with a browser-ish
    User-Agent and returns the same row schema the rest of the engine
    expects. It is intentionally listed *before* :class:`YahooFinanceSource`
    in :func:`select_sources` so equity backfills succeed even when
    ``fc.yahoo.com`` is unreachable.
    """

    name = "yahoo_chart"
    domain = "equity"

    _BASES = (
        "https://query2.finance.yahoo.com/v8/finance/chart/",
        "https://query1.finance.yahoo.com/v8/finance/chart/",
    )
    _INTERVAL: dict[str, str] = {
        "ONE_MINUTE": "1m",
        "FIVE_MINUTE": "5m",
        "FIFTEEN_MINUTE": "15m",
        "ONE_HOUR": "60m",
        "ONE_DAY": "1d",
    }

    def __init__(self, rate_per_sec: float = 1.0, timeout: float = 20.0) -> None:
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=2)
        self._timeout = timeout

    @staticmethod
    def _normalise_symbol(symbol: str) -> str:
        """``NOKIA.HE-EUR`` -> ``NOKIA.HE``. See ``YahooFinanceSource``."""
        if not symbol:
            return symbol
        parts = symbol.upper().split("-")
        if len(parts) > 1 and len(parts[-1]) == 3 and parts[-1].isalpha():
            return "-".join(parts[:-1])
        return symbol.upper()

    def fetch(
        self, symbol: str, granularity: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        interval = self._INTERVAL.get(granularity)
        if interval is None:
            return []
        ticker = self._normalise_symbol(symbol)
        params = {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
        headers = {
            # Yahoo treats the verbose Chrome UA used by yfinance as bot
            # traffic and returns 429 for many IP ranges. A minimal
            # "Mozilla/5.0" UA is accepted by the chart endpoint.
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        }
        payload = None
        max_429_retries = 3
        for base in self._BASES:
            for attempt in range(max_429_retries + 1):
                self._bucket.take()
                try:
                    resp = requests.get(
                        f"{base}{ticker}",
                        params=params,
                        headers=headers,
                        timeout=self._timeout,
                    )
                except requests.RequestException as e:
                    logger.warning(f"YahooChart request failed for {ticker}: {e}")
                    break
                if resp.status_code == 429:
                    # Honour Retry-After if present, otherwise exponential backoff.
                    ra = resp.headers.get("Retry-After")
                    delay = 2.0 * (2 ** attempt)
                    if ra:
                        try:
                            delay = max(delay, float(ra))
                        except ValueError:
                            pass
                    delay = min(delay, 60.0)
                    logger.debug(
                        f"YahooChart {ticker} 429 from {base} "
                        f"(attempt {attempt + 1}/{max_429_retries + 1}); sleeping {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                if resp.status_code != 200:
                    logger.debug(
                        f"YahooChart {ticker}: HTTP {resp.status_code} from {base}"
                    )
                    break
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                break
            if payload is not None:
                break
        if not payload:
            return []
        try:
            chart = payload.get("chart") or {}
            err = chart.get("error")
            if err:
                logger.debug(f"YahooChart {ticker}: API error {err}")
                return []
            results = chart.get("result") or []
            if not results:
                return []
            res = results[0]
            timestamps = res.get("timestamp") or []
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            o = quote.get("open") or []
            h = quote.get("high") or []
            l = quote.get("low") or []  # noqa: E741
            c = quote.get("close") or []
            v = quote.get("volume") or []
        except (KeyError, TypeError):
            return []
        out: list[dict[str, Any]] = []
        n = min(len(timestamps), len(o), len(h), len(l), len(c))
        for i in range(n):
            try:
                # Yahoo zero-pads holidays as `null`; skip rows with no close.
                if c[i] is None or o[i] is None or h[i] is None or l[i] is None:
                    continue
                out.append({
                    "ts": datetime.fromtimestamp(int(timestamps[i]), tz=timezone.utc),
                    "o": float(o[i]),
                    "h": float(h[i]),
                    "l": float(l[i]),
                    "c": float(c[i]),
                    "v": float(v[i]) if i < len(v) and v[i] is not None else 0.0,
                })
            except (TypeError, ValueError):
                continue
        return out


# ─── Yahoo Finance (equity, fallback via yfinance) ─────────────────────────


class YahooFinanceSource:
    """Yahoo Finance via ``yfinance``. Daily back to inception is reliable;
    intraday is limited (60m → ~730 days; 1m → ~30 days).

    Accepts the project's ``BASE.EXCH-CCY`` pair format (e.g.
    ``NOKIA.HE-EUR``) and strips the trailing currency suffix before
    calling Yahoo, since Yahoo tickers don't carry the quote currency.
    """

    name = "yfinance"
    domain = "equity"

    # Process-wide reachability cache for ``fc.yahoo.com`` (yfinance's
    # cookie/crumb endpoint). When this host is blocked — common in
    # container egress policies — every ``yfinance.download`` call fails
    # with a curl error, which yfinance logs as ERROR per ticker. We
    # probe once and short-circuit further calls when unreachable.
    _fc_reachable: bool | None = None
    _fc_lock = threading.Lock()

    def __init__(self, rate_per_sec: float = 2.0) -> None:
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=4)

    @classmethod
    def _fc_yahoo_reachable(cls) -> bool:
        """Cached probe: is ``fc.yahoo.com:443`` reachable from this process?"""
        if cls._fc_reachable is not None:
            return cls._fc_reachable
        with cls._fc_lock:
            if cls._fc_reachable is not None:
                return cls._fc_reachable
            import socket
            try:
                with socket.create_connection(("fc.yahoo.com", 443), timeout=2.0):
                    cls._fc_reachable = True
            except OSError:
                cls._fc_reachable = False
                logger.info(
                    "YahooFinanceSource: fc.yahoo.com unreachable — "
                    "disabling yfinance fallback for this process "
                    "(YahooChartSource + StooqSource still active)"
                )
            return cls._fc_reachable

    @staticmethod
    def _normalise_symbol(symbol: str) -> str:
        """Strip a trailing 3-letter currency suffix (``-EUR``, ``-USD``).

        Mirrors :func:`src.core.equity_feed.pair_to_yahoo` but kept local
        so this module has no upward import dependency on ``src.core``.
        """
        if not symbol:
            return symbol
        parts = symbol.upper().split("-")
        if len(parts) > 1 and len(parts[-1]) == 3 and parts[-1].isalpha():
            return "-".join(parts[:-1])
        return symbol.upper()

    def fetch(
        self, symbol: str, granularity: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        if not self._fc_yahoo_reachable():
            return []
        try:
            import yfinance  # type: ignore
        except ImportError:
            logger.warning("yfinance not installed — skipping YahooFinanceSource")
            return []
        interval = _YF_INTERVAL.get(granularity)
        if interval is None:
            return []
        ticker = self._normalise_symbol(symbol)
        self._bucket.take()
        try:
            df = yfinance.download(
                ticker,
                start=start.date().isoformat(),
                end=(end + timedelta(days=1)).date().isoformat(),
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception as e:
            logger.warning(f"yfinance.download({ticker}) failed: {e}")
            return []
        if df is None or df.empty:
            return []
        # yfinance may return a MultiIndex when single-ticker; flatten.
        try:
            if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
        except Exception:
            pass
        out: list[dict[str, Any]] = []
        for ts, row in df.iterrows():
            try:
                t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                if not isinstance(t, datetime):
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                else:
                    t = t.astimezone(timezone.utc)
                out.append({
                    "ts": t,
                    "o": float(row["Open"]),
                    "h": float(row["High"]),
                    "l": float(row["Low"]),
                    "c": float(row["Close"]),
                    "v": float(row.get("Volume", 0) or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out


# ─── Stooq (equity fallback) ───────────────────────────────────────────────


class StooqSource:
    """Stooq daily CSV — no API key, decades of history.

    URL: https://stooq.com/q/d/l/?s={symbol}&d1=YYYYMMDD&d2=YYYYMMDD&i=d
    Symbols: US tickers must be suffixed ``.us`` (e.g. ``aapl.us``); EU
    tickers use Stooq's own exchange codes (e.g. Helsinki ``.fi``,
    Frankfurt ``.de``, Stockholm ``.sf``).
    """

    name = "stooq"
    domain = "equity"
    _BASE = "https://stooq.com/q/d/l/"

    # Yahoo exchange suffix → Stooq exchange suffix.
    # Add new mappings as new venues come online.
    _YAHOO_TO_STOOQ_EXCHANGE: dict[str, str] = {
        "HE": "fi",   # Nasdaq Helsinki
        "ST": "sf",   # Stockholm
        "DE": "de",   # Xetra
        "F": "de",    # Frankfurt
        "PA": "fr",   # Euronext Paris
        "AS": "nl",   # Euronext Amsterdam
        "BR": "be",   # Euronext Brussels
        "MI": "it",   # Borsa Italiana
        "MC": "es",   # Bolsa de Madrid
        "L": "uk",    # London
        "VI": "at",   # Vienna
        "SW": "ch",   # SIX Swiss
        "CO": "dk",   # Copenhagen
        "OL": "no",   # Oslo
        "WA": "pl",   # Warsaw
        "LS": "pt",   # Lisbon
    }

    def __init__(self, rate_per_sec: float = 1.0, timeout: float = 20.0) -> None:
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=2)
        self._timeout = timeout

    @classmethod
    def _normalise_symbol(cls, symbol: str) -> str:
        """Convert project pair / Yahoo ticker into a Stooq symbol.

        ``NOKIA.HE-EUR`` → ``nokia.fi``
        ``AAPL-USD``    → ``aapl.us``
        ``VOLV-B.ST``   → ``volv-b.sf``
        ``aapl``        → ``aapl.us``
        """
        s = symbol.strip()
        # Strip trailing 3-letter currency suffix (project pair format).
        parts = s.split("-")
        if len(parts) > 1 and len(parts[-1]) == 3 and parts[-1].isalpha():
            s = "-".join(parts[:-1])
        s = s.lower()
        if "." in s:
            base, _, exch = s.rpartition(".")
            mapped = cls._YAHOO_TO_STOOQ_EXCHANGE.get(exch.upper())
            if mapped:
                return f"{base}.{mapped}"
            return s  # already a Stooq-style suffix or unknown — try as-is
        return f"{s}.us"

    def fetch(
        self, symbol: str, granularity: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        if granularity != "ONE_DAY":
            return []  # only daily supported
        params = {
            "s": self._normalise_symbol(symbol),
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
        self._bucket.take()
        try:
            resp = requests.get(self._BASE, params=params, timeout=self._timeout)
        except requests.RequestException as e:
            logger.warning(f"Stooq request failed for {symbol}: {e}")
            return []
        if resp.status_code != 200 or not resp.text or resp.text.startswith("No data"):
            return []
        return _parse_stooq_csv(resp.text)


def _parse_stooq_csv(text: str) -> list[dict[str, Any]]:
    """Parse a Stooq daily CSV (Date,Open,High,Low,Close,Volume)."""
    out: list[dict[str, Any]] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].lower().split(",")
    try:
        idx_date = header.index("date")
        idx_o = header.index("open")
        idx_h = header.index("high")
        idx_l = header.index("low")
        idx_c = header.index("close")
        idx_v = header.index("volume") if "volume" in header else None
    except ValueError:
        return out
    for raw in lines[1:]:
        parts = raw.split(",")
        if len(parts) < 5:
            continue
        try:
            ts = datetime.strptime(parts[idx_date], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            out.append({
                "ts": ts,
                "o": float(parts[idx_o]),
                "h": float(parts[idx_h]),
                "l": float(parts[idx_l]),
                "c": float(parts[idx_c]),
                "v": float(parts[idx_v]) if idx_v is not None and parts[idx_v] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    return out


# ─── Coinbase (crypto, primary) ────────────────────────────────────────────


class CoinbaseSource:
    """Wraps the project's CoinbaseClient.get_candles with backward pagination
    to product inception."""

    name = "coinbase"
    domain = "crypto"

    def __init__(self, client=None, rate_per_sec: float = 5.0) -> None:
        self._client = client
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=8)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        from src.backtesting.candle_fetch import _build_coinbase_client  # noqa
        self._client = _build_coinbase_client()
        return self._client

    def fetch(
        self, symbol: str, granularity: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        client = self._ensure_client()
        if client is None:
            return []
        seconds = GRAN_SECONDS.get(granularity)
        if seconds is None:
            return []
        # Page backwards in 300-candle windows from `end` to `start`.
        out: list[dict[str, Any]] = []
        page_span = 300 * seconds
        end_ts = int(end.timestamp())
        start_ts = int(start.timestamp())
        while end_ts > start_ts:
            page_start = max(start_ts, end_ts - page_span)
            self._bucket.take()
            try:
                page = client.get_candles(
                    symbol,
                    granularity=granularity,
                    limit=300,
                    start_time=page_start,
                    end_time=end_ts,
                )
            except Exception as e:
                logger.warning(f"Coinbase get_candles({symbol}) failed: {e}")
                break
            if not page:
                break
            for c in page:
                try:
                    raw_ts = c.get("start") or c.get("time")
                    if raw_ts is None:
                        continue
                    ts = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
                    out.append({
                        "ts": ts,
                        "o": float(c["open"]),
                        "h": float(c["high"]),
                        "l": float(c["low"]),
                        "c": float(c["close"]),
                        "v": float(c.get("volume", 0) or 0),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            if len(page) < 300:
                # Likely hit product inception or sparse history.
                end_ts = page_start
                if page_start <= start_ts:
                    break
                continue
            end_ts = page_start
        return out


# ─── CryptoCompare (crypto fallback, no key) ───────────────────────────────


class CryptoCompareSource:
    """CryptoCompare public histoday/histohour endpoints.

    Symbol convention: project pairs are ``BASE-QUOTE`` (e.g. ``BTC-USD``);
    CryptoCompare wants ``fsym=BASE&tsym=QUOTE``.
    """

    name = "cryptocompare"
    domain = "crypto"
    _BASE = "https://min-api.cryptocompare.com/data/v2/"

    _PATH = {
        "ONE_DAY": "histoday",
        "ONE_HOUR": "histohour",
        "ONE_MINUTE": "histominute",
    }

    def __init__(self, rate_per_sec: float = 2.0, timeout: float = 20.0) -> None:
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=4)
        self._timeout = timeout

    def fetch(
        self, symbol: str, granularity: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        path = self._PATH.get(granularity)
        if path is None:
            return []
        if "-" not in symbol:
            return []
        fsym, tsym = symbol.split("-", 1)
        # CryptoCompare returns up to 2000 rows per call walking backwards
        # from `toTs`. Loop until we cover [start, end].
        out: list[dict[str, Any]] = []
        to_ts = int(end.timestamp())
        start_ts = int(start.timestamp())
        seconds = GRAN_SECONDS[granularity]
        while to_ts > start_ts:
            self._bucket.take()
            try:
                resp = requests.get(
                    f"{self._BASE}{path}",
                    params={
                        "fsym": fsym,
                        "tsym": tsym,
                        "limit": 2000,
                        "toTs": to_ts,
                    },
                    timeout=self._timeout,
                )
            except requests.RequestException as e:
                logger.warning(f"CryptoCompare request failed for {symbol}: {e}")
                break
            if resp.status_code == 429:
                _backoff(resp)
                continue
            if resp.status_code != 200:
                break
            try:
                payload = resp.json()
            except ValueError:
                break
            if payload.get("Response") != "Success":
                break
            data = payload.get("Data", {}).get("Data", [])
            if not data:
                break
            page_min_ts = int(data[0].get("time", to_ts))
            for row in data:
                try:
                    ts = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
                    # Skip rows where there's no real data (CryptoCompare
                    # zero-pads pre-inception history).
                    if not row.get("close") and not row.get("high"):
                        continue
                    out.append({
                        "ts": ts,
                        "o": float(row.get("open", 0) or 0),
                        "h": float(row.get("high", 0) or 0),
                        "l": float(row.get("low", 0) or 0),
                        "c": float(row.get("close", 0) or 0),
                        "v": float(row.get("volumefrom", 0) or 0),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            if page_min_ts <= start_ts or page_min_ts >= to_ts:
                break
            to_ts = page_min_ts - seconds
        return out


# ─── Binance (crypto, alternative coverage) ────────────────────────────────


class BinanceSource:
    """Binance public ``/api/v3/klines`` — no key required.

    Project pairs ``BASE-QUOTE`` are mapped to ``BASEQUOTE`` (e.g. BTCUSDT).
    USD pairs are remapped to USDT since Binance has no USD spot.
    """

    name = "binance"
    domain = "crypto"
    _BASE = "https://api.binance.com/api/v3/klines"

    def __init__(self, rate_per_sec: float = 5.0, timeout: float = 20.0) -> None:
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=10)
        self._timeout = timeout

    @staticmethod
    def _normalise_symbol(symbol: str) -> Optional[str]:
        if "-" not in symbol:
            return None
        base, quote = symbol.split("-", 1)
        if quote.upper() == "USD":
            quote = "USDT"
        return f"{base.upper()}{quote.upper()}"

    def fetch(
        self, symbol: str, granularity: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        bsym = self._normalise_symbol(symbol)
        interval = _BINANCE_INTERVAL.get(granularity)
        if bsym is None or interval is None:
            return []
        seconds_ms = GRAN_SECONDS[granularity] * 1000
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out: list[dict[str, Any]] = []
        cursor = start_ms
        while cursor < end_ms:
            self._bucket.take()
            try:
                resp = requests.get(
                    self._BASE,
                    params={
                        "symbol": bsym,
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": end_ms,
                        "limit": 1000,
                    },
                    timeout=self._timeout,
                )
            except requests.RequestException as e:
                logger.warning(f"Binance request failed for {symbol}: {e}")
                break
            if resp.status_code == 429 or resp.status_code == 418:
                _backoff(resp)
                continue
            if resp.status_code != 200:
                break
            try:
                rows = resp.json()
            except ValueError:
                break
            if not rows:
                break
            for r in rows:
                try:
                    ts = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc)
                    out.append({
                        "ts": ts,
                        "o": float(r[1]),
                        "h": float(r[2]),
                        "l": float(r[3]),
                        "c": float(r[4]),
                        "v": float(r[5]),
                    })
                except (IndexError, TypeError, ValueError):
                    continue
            last_ms = int(rows[-1][0])
            if last_ms <= cursor:
                break
            cursor = last_ms + seconds_ms
        return out


def _backoff(resp: requests.Response) -> None:
    """Honour Retry-After and apply a small base delay."""
    delay = 1.0
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            delay = max(delay, float(ra))
        except ValueError:
            pass
    time.sleep(min(delay, 30.0))


# ─── Source registry / domain isolation ────────────────────────────────────


def select_sources(profile: str) -> list[HistoricalSource]:
    """Return the ordered source list for a profile. Domain-isolated."""
    p = (profile or "").lower()
    if p in {"coinbase", "coinbase_paper"}:
        return [CoinbaseSource(), CryptoCompareSource(), BinanceSource()]
    if p == "ibkr":
        return [YahooChartSource(), YahooFinanceSource(), StooqSource()]
    return []
