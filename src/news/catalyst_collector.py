"""
Catalyst event collector.

Populates the ``catalyst_events`` table with upcoming and recent catalysts
that the Pattern Engine will key off:

Equity (ibkr profile):
  * Earnings dates    via ``src.core.equity_feed.get_earnings_calendar``.
  * Ex-dividend dates via ``src.core.equity_feed.get_dividend_calendar``.
  * Macro events      via ``src.core.equity_feed.get_macro_calendar``.

Crypto (coinbase profile):
  * Halvings          — deterministic schedule (BTC + a few major chains).
  * Listings          — Coinbase blog "Asset listing" RSS feed.
  * Regulatory        — regex-tagged articles from
    ``src.news.aggregator.NewsAggregator`` (free, already aggregated).

All adapters are best-effort and never raise — empty list on any error so
the orchestrator can keep running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests

from src.utils.logger import get_logger
from src.utils.stats import StatsDB

logger = get_logger("news.catalyst_collector")


# ─── Domain mapping ────────────────────────────────────────────────────────

_PROFILE_EXCHANGE: dict[str, str] = {
    "coinbase": "coinbase",
    "coinbase_paper": "coinbase_paper",
    "ibkr": "ibkr",
}


def _profile_exchange(profile: str) -> str:
    return _PROFILE_EXCHANGE.get(profile.lower(), profile.lower())


# ─── Equity catalysts ──────────────────────────────────────────────────────


def collect_equity_catalysts(
    profile: str,
    pairs: Iterable[str],
    days_ahead: int = 90,
    stats_db: Optional[StatsDB] = None,
) -> int:
    """Collect earnings/dividend/macro events for an equity profile.

    Returns the number of new event rows written.
    """
    if profile.lower() != "ibkr":
        return 0
    db = stats_db or StatsDB()
    exchange = _profile_exchange(profile)
    pair_list = list(pairs)
    if not pair_list:
        return 0

    try:
        from src.core.equity_feed import (
            get_dividend_calendar,
            get_earnings_calendar,
            get_macro_calendar,
            pair_to_yahoo,
        )
    except Exception as e:
        logger.warning(f"equity_feed import failed: {e}")
        return 0

    # Map pair → yahoo ticker so we can write back the original pair.
    pair_by_ticker: dict[str, str] = {}
    tickers: list[str] = []
    for p in pair_list:
        try:
            t = pair_to_yahoo(p)
            if t:
                pair_by_ticker[t] = p
                tickers.append(t)
        except Exception:
            continue

    rows: list[dict] = []
    try:
        for ticker, info in get_earnings_calendar(tickers, days_ahead=days_ahead).items():
            ts = _coerce_event_ts(info.get("earnings_date"))
            if ts is None:
                continue
            rows.append({
                "exchange": exchange,
                "symbol": pair_by_ticker.get(ticker, ticker),
                "event_type": "earnings",
                "event_ts": ts,
                "source": "yfinance.earnings",
                "confidence": 0.95,
                "metadata": info,
            })
    except Exception as e:
        logger.warning(f"earnings calendar failed: {e}")
    try:
        for ticker, info in get_dividend_calendar(tickers, days_ahead=days_ahead).items():
            ts = _coerce_event_ts(info.get("ex_div_date"))
            if ts is None:
                continue
            rows.append({
                "exchange": exchange,
                "symbol": pair_by_ticker.get(ticker, ticker),
                "event_type": "ex_dividend",
                "event_ts": ts,
                "source": "yfinance.dividends",
                "confidence": 0.95,
                "metadata": info,
            })
    except Exception as e:
        logger.warning(f"dividend calendar failed: {e}")
    try:
        for ev in get_macro_calendar(days_ahead=days_ahead) or []:
            ts = _coerce_event_ts(ev.get("date"))
            if ts is None:
                continue
            # Macro events have no ticker → use "_MACRO_" so per-symbol
            # queries skip them but per-exchange queries still see them.
            rows.append({
                "exchange": exchange,
                "symbol": "_MACRO_",
                "event_type": "macro",
                "event_ts": ts,
                "source": ev.get("source", "macro"),
                "confidence": 0.9,
                "metadata": ev,
            })
    except Exception as e:
        logger.warning(f"macro calendar failed: {e}")

    if not rows:
        return 0
    return db.upsert_catalyst_events(rows)


# ─── Crypto catalysts ──────────────────────────────────────────────────────


# Bitcoin halving schedule. The 4 most recent past + the next two predicted
# halvings — each cycle is 210,000 blocks ≈ ~4 years.
_BTC_HALVINGS_UTC: tuple[datetime, ...] = (
    datetime(2012, 11, 28, 15, 24, tzinfo=timezone.utc),
    datetime(2016, 7, 9, 16, 46, tzinfo=timezone.utc),
    datetime(2020, 5, 11, 19, 23, tzinfo=timezone.utc),
    datetime(2024, 4, 19, 12, 9, tzinfo=timezone.utc),
    datetime(2028, 4, 17, 0, 0, tzinfo=timezone.utc),
    datetime(2032, 4, 12, 0, 0, tzinfo=timezone.utc),
)

# Litecoin halvings (every ~840,000 blocks, ~4 years).
_LTC_HALVINGS_UTC: tuple[datetime, ...] = (
    datetime(2015, 8, 25, 0, 0, tzinfo=timezone.utc),
    datetime(2019, 8, 5, 0, 0, tzinfo=timezone.utc),
    datetime(2023, 8, 2, 0, 0, tzinfo=timezone.utc),
    datetime(2027, 8, 2, 0, 0, tzinfo=timezone.utc),
)

# Bitcoin Cash halvings.
_BCH_HALVINGS_UTC: tuple[datetime, ...] = (
    datetime(2020, 4, 8, 0, 0, tzinfo=timezone.utc),
    datetime(2024, 4, 4, 0, 0, tzinfo=timezone.utc),
    datetime(2028, 4, 1, 0, 0, tzinfo=timezone.utc),
)


_HALVING_SCHEDULES: dict[str, tuple[datetime, ...]] = {
    "BTC": _BTC_HALVINGS_UTC,
    "LTC": _LTC_HALVINGS_UTC,
    "BCH": _BCH_HALVINGS_UTC,
}


# Regulatory keywords (case-insensitive) — articles whose title/summary
# matches at least one tag are recorded as a catalyst event.
_REGULATORY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("sec_action", re.compile(r"\b(sec|securities and exchange commission)\b.*\b(sue|charge|enforce|settle|approve|reject|deny)\w*", re.IGNORECASE)),
    ("etf_decision", re.compile(r"\b(etf)\b.*\b(approv|reject|deny|delay|file|list)\w*", re.IGNORECASE)),
    ("ban", re.compile(r"\b(ban|prohibit|outlaw|crackdown)\b.*\b(crypto|bitcoin|ethereum|stablecoin)\b", re.IGNORECASE)),
    ("regulation", re.compile(r"\b(mica|fit21|stablecoin|cbdc|treasury|finra|cftc)\b.*\b(regulation|rule|law|guidance|propos|enact)\w*", re.IGNORECASE)),
    ("hack", re.compile(r"\b(hack|exploit|breach|stolen|drained)\b.*\b(exchange|protocol|wallet|bridge|defi)\b", re.IGNORECASE)),
)


def collect_crypto_catalysts(
    profile: str,
    pairs: Iterable[str],
    horizon_days: int = 365,
    stats_db: Optional[StatsDB] = None,
    news_articles: Optional[Iterable] = None,
) -> int:
    """Collect halvings, listings, and regulatory events for crypto.

    Returns the number of new rows written.
    """
    if profile.lower() not in {"coinbase", "coinbase_paper"}:
        return 0
    db = stats_db or StatsDB()
    exchange = _profile_exchange(profile)
    pair_list = list(pairs)
    rows: list[dict] = []

    # 1. Halvings — for any tracked pair whose base maps to a halving schedule.
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=int(horizon_days))
    history = now - timedelta(days=int(horizon_days))
    for p in pair_list:
        base = p.split("-", 1)[0].upper()
        schedule = _HALVING_SCHEDULES.get(base)
        if not schedule:
            continue
        for ts in schedule:
            if history <= ts <= horizon:
                rows.append({
                    "exchange": exchange,
                    "symbol": p,
                    "event_type": "halving",
                    "event_ts": ts,
                    "source": "deterministic.halving_schedule",
                    "confidence": 1.0,
                    "metadata": {"asset": base},
                })

    # 2. Listings via Coinbase blog RSS (free, no key).
    try:
        rows.extend(_fetch_coinbase_listings(exchange, pair_list))
    except Exception as e:
        logger.warning(f"Coinbase listings fetch failed: {e}")

    # 3. Regulatory tags from already-aggregated news.
    try:
        rows.extend(_tag_regulatory_articles(exchange, pair_list, news_articles))
    except Exception as e:
        logger.warning(f"Regulatory tagging failed: {e}")

    if not rows:
        return 0
    return db.upsert_catalyst_events(rows)


def _fetch_coinbase_listings(
    exchange: str, pairs: Iterable[str]
) -> list[dict]:
    """Pull recent posts from the Coinbase asset-listings RSS feed and emit
    one event per known pair mentioned in the post title."""
    url = "https://blog.coinbase.com/feed"
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException:
        return []
    if resp.status_code != 200 or not resp.text:
        return []

    # Lightweight parse — feedparser is the project's standard, used by news.
    try:
        import feedparser  # type: ignore
    except ImportError:
        logger.warning("feedparser not installed — skipping listings fetch")
        return []
    parsed = feedparser.parse(resp.text)
    bases = {p.split("-", 1)[0].upper() for p in pairs}
    pair_by_base = {p.split("-", 1)[0].upper(): p for p in pairs}
    out: list[dict] = []
    for entry in parsed.entries[:100]:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        # Heuristic: titles that mention "is now available" / "now trading" /
        # "experimental" are listing announcements.
        lt = title.lower()
        if not any(k in lt for k in ("now available", "now trading", "is launching", "is live", "experimental")):
            continue
        # Find any tracked base that appears as a whole word in the title.
        matched: list[str] = []
        for b in bases:
            if re.search(rf"\b{re.escape(b)}\b", title, re.IGNORECASE):
                matched.append(b)
        if not matched:
            continue
        ts = _coerce_event_ts(
            entry.get("published") or entry.get("updated") or entry.get("pubDate")
        )
        if ts is None:
            continue
        for b in matched:
            out.append({
                "exchange": exchange,
                "symbol": pair_by_base[b],
                "event_type": "listing",
                "event_ts": ts,
                "source": "coinbase.blog.rss",
                "confidence": 0.85,
                "metadata": {
                    "title": title,
                    "url": entry.get("link", ""),
                },
            })
    return out


def _tag_regulatory_articles(
    exchange: str, pairs: Iterable[str], news_articles: Optional[Iterable]
) -> list[dict]:
    """Scan provided NewsArticle objects for regulatory keywords; emit one
    event per matching article × applicable pair (max 3 pairs per article
    to bound cardinality)."""
    if not news_articles:
        return []
    bases = {p.split("-", 1)[0].upper(): p for p in pairs}
    out: list[dict] = []
    for art in news_articles:
        text = f"{getattr(art, 'title', '')} {getattr(art, 'summary', '')}"
        if not text.strip():
            continue
        tag = None
        for name, pat in _REGULATORY_PATTERNS:
            if pat.search(text):
                tag = name
                break
        if tag is None:
            continue
        ts = getattr(art, "published", None)
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # Apply to up to three pairs whose base appears in the text.
        applicable: list[str] = []
        for base, pair in bases.items():
            if re.search(rf"\b{re.escape(base)}\b", text, re.IGNORECASE):
                applicable.append(pair)
            if len(applicable) >= 3:
                break
        # If nothing matched, attribute to the broadest pair (usually BTC-USD).
        if not applicable:
            preferred = next((p for b, p in bases.items() if b == "BTC"), None)
            if preferred:
                applicable = [preferred]
            else:
                continue
        for pair in applicable:
            out.append({
                "exchange": exchange,
                "symbol": pair,
                "event_type": f"regulatory.{tag}",
                "event_ts": ts,
                "source": f"news.aggregator/{getattr(art, 'source', 'rss')}",
                "confidence": 0.6,
                "metadata": {
                    "title": getattr(art, "title", ""),
                    "url": getattr(art, "url", ""),
                    "tag": tag,
                },
            })
    return out


# ─── Helpers ───────────────────────────────────────────────────────────────


def _coerce_event_ts(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        # Try RFC 822 (RSS timestamps).
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            return None
    return None


# ─── Top-level entrypoint ──────────────────────────────────────────────────


def collect_catalysts(
    profile: str,
    pairs: Iterable[str],
    stats_db: Optional[StatsDB] = None,
    news_articles: Optional[Iterable] = None,
    days_ahead: int = 90,
) -> dict:
    """Run all adapters appropriate for ``profile``. Returns
    ``{equity: n_new, crypto: n_new}``."""
    pair_list = list(pairs)
    p = profile.lower()
    out = {"equity": 0, "crypto": 0}
    if p == "ibkr":
        out["equity"] = collect_equity_catalysts(
            profile, pair_list, days_ahead=days_ahead, stats_db=stats_db
        )
    elif p in {"coinbase", "coinbase_paper"}:
        out["crypto"] = collect_crypto_catalysts(
            profile, pair_list, horizon_days=days_ahead, stats_db=stats_db,
            news_articles=news_articles,
        )
    return out
