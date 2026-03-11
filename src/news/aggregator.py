"""
Crypto & Market News Aggregator — Fetches news from multiple open sources.
Sources: Reddit, RSS feeds (CoinTelegraph, CoinDesk, Decrypt, etc.)

Includes lightweight NLP enrichment (sentiment, ticker extraction, relevance
scoring, noise filtering) so the dashboard receives actionable articles.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import feedparser
import requests

from src.utils.logger import get_logger
from src.utils.security import sanitize_input

logger = get_logger("news.aggregator")

# ── Keyword-based sentiment analysis ────────────────────────────────────────

_BULLISH_WORDS = frozenset([
    "surge", "surges", "surging", "soar", "soars", "soaring", "rally", "rallies",
    "rallying", "pump", "pumps", "pumping", "moon", "mooning", "breakout",
    "bullish", "all-time high", "ath", "record high", "skyrocket", "boom",
    "gain", "gains", "gained", "uptick", "uptrend", "recovery", "recovers",
    "rebound", "rebounds", "outperform", "outperforms", "beat", "beats",
    "upgrade", "upgraded", "buy", "accumulate", "undervalued", "growth",
    "profit", "profits", "green", "optimism", "optimistic", "positive",
    "strong", "strength", "momentum", "breakthrough", "milestone", "adoption",
])

_BEARISH_WORDS = frozenset([
    "crash", "crashes", "crashing", "plunge", "plunges", "plunging", "dump",
    "dumps", "dumping", "tank", "tanks", "tanking", "bearish", "sell-off",
    "selloff", "sell off", "decline", "declines", "declining", "drop", "drops",
    "dropping", "slump", "slumps", "downturn", "downtrend", "correction",
    "fear", "panic", "capitulation", "liquidation", "liquidated", "hack",
    "hacked", "exploit", "scam", "fraud", "rug pull", "rugpull", "ban",
    "banned", "lawsuit", "sued", "sec charges", "warning", "risk", "risky",
    "overvalued", "bubble", "collapse", "collapses", "loss", "losses", "red",
    "negative", "weak", "weakness", "downgrade", "underperform",
])

# Generic tickers — broadly relevant names for serendipitous discovery.
# The bulk of ticker matching is built dynamically from the user's watchlist.
_GENERIC_TICKERS = frozenset([
    # Major indices/ETFs
    "SPY", "QQQ", "VOO", "VTI", "ARKK",
    # Macro-significant crypto (always worth catching)
    "BTC", "ETH", "SOL", "XRP", "DOGE",
    # Tech bellwethers (market-moving)
    "AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "META", "TSLA",
])


def _pair_to_tickers(pair: str) -> set[str]:
    """Extract searchable ticker symbols from a trading pair string.

    Examples:
        "BTC-EUR"     → {"BTC"}
        "ASML.AS-EUR" → {"ASML"}
        "ETH-EURC"    → {"ETH"}
    """
    base = pair.split("-")[0] if "-" in pair else pair
    base_short = base.split(".")[0]  # ASML.AS → ASML
    tickers = set()
    for t in (base.upper(), base_short.upper()):
        if 2 <= len(t) <= 6 and t.isalpha():
            tickers.add(t)
    return tickers


def build_ticker_set_from_config(config: dict) -> set[str]:
    """Build a ticker set from config trading pairs."""
    tickers: set[str] = set()
    for pair in config.get("trading", {}).get("pairs", []):
        tickers |= _pair_to_tickers(pair)
    return tickers

# Regex: $TICKER or standalone 2-5 uppercase letters bounded by word boundaries
_TICKER_RE = re.compile(
    r'(?:\$([A-Z]{1,5}))|(?<![A-Za-z])([A-Z]{2,5})(?![A-Za-z])'
)

# Noise patterns — skip generic meta/discussion posts
_NOISE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"^daily.*(?:discussion|thread|general)",
        r"^weekly.*(?:discussion|thread|general)",
        r"^monthly.*(?:discussion|thread)",
        r"^(?:mega)?thread",
        r"^(?:investing|trading).*scam.*(?:reminder|warning|alert)",
        r"^rate my (?:portfolio|picks)",
        r"^moronic monday",
        r"^mentor monday",
        r"^weekend",
        r"^mod.*post",
        r"^rule[s]? (?:update|change|reminder)",
        r"^community (?:update|announcement|guidelines)",
    ]
]


def _classify_sentiment(text: str) -> str:
    """Keyword-based sentiment: bullish / bearish / neutral."""
    lower = text.lower()
    bull = sum(1 for w in _BULLISH_WORDS if w in lower)
    bear = sum(1 for w in _BEARISH_WORDS if w in lower)
    if bull > bear and bull >= 1:
        return "bullish"
    if bear > bull and bear >= 1:
        return "bearish"
    return "neutral"


def _extract_tickers(text: str, known_tickers: frozenset[str] | set[str] = _GENERIC_TICKERS) -> list[str]:
    """Extract likely stock/crypto ticker symbols from text."""
    found: list[str] = []
    # 1. Regex: match $TICKER or standalone uppercase tickers
    for m in _TICKER_RE.finditer(text):
        ticker = m.group(1) or m.group(2)
        if ticker and ticker in known_tickers:
            found.append(ticker)
    # 2. Case-insensitive whole-word search for known tickers (catches
    #    mixed-case mentions like "Nokia" → NOKIA, "Asml" → ASML)
    text_upper = text.upper()
    for ticker in known_tickers:
        if ticker in found:
            continue
        if len(ticker) < 2:
            continue
        # Use word-boundary search on uppercased text
        if re.search(rf'(?<![A-Z]){re.escape(ticker)}(?![A-Z])', text_upper):
            found.append(ticker)
    return list(dict.fromkeys(found))  # dedup, preserve order


def _relevance_score(article: "NewsArticle", known_tickers: frozenset[str] | set[str] = _GENERIC_TICKERS) -> float:
    """Score 0-1 based on specificity & actionability."""
    score = 0.3  # base score for any article

    text = f"{article.title} {article.summary}".lower()

    # Has specific tickers → more relevant
    tickers = _extract_tickers(article.title + " " + article.summary, known_tickers)
    if tickers:
        score += min(0.3, len(tickers) * 0.1)

    # Non-neutral sentiment → more relevant
    if article.sentiment and article.sentiment != "neutral":
        score += 0.1

    # From RSS (actual news sites) → more relevant than Reddit self-posts
    if "rss" in article.tags:
        score += 0.1

    # Has a URL that isn't just a Reddit self-post
    if article.url and "reddit.com" not in article.url:
        score += 0.05

    # Recency bonus — articles < 2h old get a boost
    if article.published:
        age = (datetime.now(timezone.utc) - article.published).total_seconds()
        if age < 7200:
            score += 0.1
        elif age < 14400:
            score += 0.05

    # Penalty for very short content (probably low-effort)
    if len(text) < 50:
        score -= 0.1

    return max(0.0, min(1.0, round(score, 3)))


def _is_noise(title: str) -> bool:
    """Return True if the post is a generic/meta discussion thread."""
    for pattern in _NOISE_PATTERNS:
        if pattern.search(title):
            return True
    return False


def _enrich_article(article: "NewsArticle", known_tickers: frozenset[str] | set[str] = _GENERIC_TICKERS) -> "NewsArticle":
    """Add sentiment, tickers, relevance score to an article in-place."""
    text = f"{article.title} {article.summary}"

    # Sentiment
    if not article.sentiment or article.sentiment == "neutral":
        article.sentiment = _classify_sentiment(text)

    # Ticker extraction → add to tags
    tickers = _extract_tickers(text, known_tickers)
    existing_tags = set(t.lower() for t in article.tags)
    for ticker in tickers:
        if ticker.lower() not in existing_tags:
            article.tags.append(ticker)

    # Relevance score
    article.relevance_score = _relevance_score(article, known_tickers)

    return article


@dataclass
class NewsArticle:
    """A single news article from any source."""
    id: str = ""
    title: str = ""
    summary: str = ""
    source: str = ""
    url: str = ""
    published: Optional[datetime] = None
    sentiment: Optional[str] = None  # bullish, bearish, neutral
    relevance_score: float = 0.0
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = hashlib.md5(f"{self.title}{self.url}".encode()).hexdigest()[:12]


class NewsAggregator:
    """
    Aggregates crypto news from Reddit and RSS feeds.
    Runs as a background process, storing articles in memory and Redis.
    """

    def __init__(
        self,
        config: dict,
        redis_client=None,
        reddit_client_id: str = "",
        reddit_client_secret: str = "",
        reddit_user_agent: str = "auto-traitor-bot/0.1",
        profile: str = "",
        exchange_client=None,
    ):
        self.config = config
        self.redis = redis_client
        self.profile = profile  # e.g. "coinbase", "ibkr" — used for Redis key
        self.exchange_client = exchange_client

        # Reddit config
        self.reddit_client_id = reddit_client_id
        self.reddit_client_secret = reddit_client_secret
        self.reddit_user_agent = reddit_user_agent
        self.subreddits = config.get("reddit_subreddits", [
            "cryptocurrency", "bitcoin", "ethereum", "CryptoMarkets",
        ])

        # RSS config
        self.rss_feeds = config.get("rss_feeds", [
            "https://cointelegraph.com/rss",
            "https://cryptonews.com/news/feed/",
        ])

        # Storage
        self.articles: list[NewsArticle] = []
        self.max_articles = config.get("max_articles", 100)
        self._lock = threading.Lock()
        self._praw_reddit = None

        # Dynamic ticker set: generic base + config pairs + Redis-published watchlist
        self._known_tickers: set[str] = set(_GENERIC_TICKERS)
        self._known_tickers |= build_ticker_set_from_config(config)
        self._refresh_tickers_from_redis()

        # Stats
        self.last_fetch_time: Optional[datetime] = None
        self.total_fetched = 0

        logger.info(
            f"📰 News Aggregator initialized | "
            f"Subreddits: {len(self.subreddits)} | RSS feeds: {len(self.rss_feeds)} | "
            f"Tracked tickers: {len(self._known_tickers)}"
        )

    def _refresh_tickers_from_redis(self) -> None:
        """Read watched tickers published by orchestrator instances."""
        if not self.redis:
            return
        try:
            for key in self.redis.keys("*:news:watched_tickers") + [b"news:watched_tickers"]:
                raw = self.redis.get(key)
                if raw:
                    tickers = json.loads(raw if isinstance(raw, str) else raw.decode())
                    if isinstance(tickers, list):
                        self._known_tickers.update(t for t in tickers if isinstance(t, str))
        except Exception as e:
            logger.debug(f"Could not read watched tickers from Redis: {e}")

    def _init_reddit(self) -> None:
        """Initialize the Reddit client (praw)."""
        if self._praw_reddit:
            return
        if not self.reddit_client_id or not self.reddit_client_secret:
            logger.warning("Reddit credentials not set, skipping Reddit scraping")
            return
        try:
            import praw
            self._praw_reddit = praw.Reddit(
                client_id=self.reddit_client_id,
                client_secret=self.reddit_client_secret,
                user_agent=self.reddit_user_agent,
            )
            logger.info("✅ Reddit client initialized")
        except Exception as e:
            logger.error(f"Failed to init Reddit client: {e}")

    def fetch_reddit(self) -> list[NewsArticle]:
        """Fetch top posts from crypto subreddits."""
        self._init_reddit()
        articles = []

        if not self._praw_reddit:
            # Fallback: use Reddit JSON API (no auth required)
            return self._fetch_reddit_json()

        try:
            for sub_name in self.subreddits:
                try:
                    subreddit = self._praw_reddit.subreddit(sub_name)
                    for post in subreddit.hot(limit=10):
                        article = NewsArticle(
                            title=sanitize_input(post.title, max_length=300),
                            summary=sanitize_input(post.selftext[:500], max_length=500) if post.selftext else "",
                            source=f"reddit/r/{sub_name}",
                            url=f"https://reddit.com{post.permalink}",
                            published=datetime.fromtimestamp(post.created_utc, tz=timezone.utc),
                            tags=[sub_name, "reddit"],
                        )
                        articles.append(article)
                except Exception as e:
                    logger.warning(f"Failed to fetch r/{sub_name}: {e}")
        except Exception as e:
            logger.error(f"Reddit fetch error: {e}")

        logger.info(f"📰 Fetched {len(articles)} Reddit posts")
        return articles

    def _fetch_reddit_json(self) -> list[NewsArticle]:
        """Fallback: Fetch Reddit posts using the public JSON API."""
        articles = []
        headers = {"User-Agent": self.reddit_user_agent}

        for sub_name in self.subreddits:
            try:
                url = f"https://www.reddit.com/r/{sub_name}/hot.json?limit=10"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                for item in data.get("data", {}).get("children", []):
                    post = item.get("data", {})
                    article = NewsArticle(
                        title=sanitize_input(post.get("title", ""), max_length=300),
                        summary=sanitize_input(post.get("selftext", "")[:500], max_length=500),
                        source=f"reddit/r/{sub_name}",
                        url=f"https://reddit.com{post.get('permalink', '')}",
                        published=datetime.fromtimestamp(
                            post.get("created_utc", time.time()), tz=timezone.utc
                        ),
                        tags=[sub_name, "reddit"],
                    )
                    articles.append(article)
            except Exception as e:
                logger.warning(f"Reddit JSON fallback failed for r/{sub_name}: {e}")

        logger.info(f"📰 Fetched {len(articles)} Reddit posts (JSON fallback)")
        return articles

    @staticmethod
    def _domain_tag(url: str) -> str:
        """Extract a domain-based tag from a URL for routing.

        Example: 'https://feeds.bloomberg.com/markets/news.rss' → 'feeds_bloomberg_com'
        """
        m = re.search(r'//(?:www\.)?([^/]+)', url)
        return m.group(1).lower().replace(".", "_") if m else ""

    def fetch_rss(self) -> list[NewsArticle]:
        """Fetch articles from RSS feeds."""
        articles = []

        for feed_url in self.rss_feeds:
            try:
                # SSRF protection: only allow http/https schemes
                if not feed_url.lower().startswith(("https://", "http://")):
                    logger.warning(f"RSS feed URL rejected (invalid scheme): {feed_url}")
                    continue

                # Use requests with User-Agent to avoid being blocked,
                # then parse the content with feedparser.
                resp = requests.get(
                    feed_url,
                    timeout=15,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; auto-traitor-bot/0.1)"},
                )
                if resp.status_code != 200:
                    logger.warning(f"RSS HTTP {resp.status_code} for {feed_url}")
                    continue

                feed = feedparser.parse(resp.content)
                source_name = feed.feed.get("title", feed_url)
                domain_tag = self._domain_tag(feed_url)

                for entry in feed.entries[:15]:
                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

                    summary = entry.get("summary", "")
                    # Strip HTML tags from summary
                    summary = re.sub(r"<[^>]+>", "", summary)[:500]

                    # Tags include: generic "rss" marker, the domain-based id
                    # (used by profile routing), and the human-readable source name.
                    tags = ["rss"]
                    if domain_tag:
                        tags.append(domain_tag)
                    title_tag = source_name.lower().replace(" ", "_")
                    if title_tag not in tags:
                        tags.append(title_tag)

                    article = NewsArticle(
                        title=sanitize_input(entry.get("title", ""), max_length=300),
                        summary=sanitize_input(summary, max_length=500),
                        source=source_name,
                        url=entry.get("link", ""),
                        published=published,
                        tags=tags,
                    )
                    articles.append(article)
            except Exception as e:
                logger.warning(f"RSS fetch failed for {feed_url}: {e}")

        logger.info(f"📰 Fetched {len(articles)} RSS articles")
        return articles

    def fetch_coingecko_trending(self) -> list[NewsArticle]:
        """Fetch trending coins from CoinGecko (free, no API key)."""
        articles = []
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/search/trending",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                coins = data.get("coins", [])
                trending_names = [c["item"]["name"] for c in coins[:7]]
                trending_symbols = [c["item"].get("symbol", "").upper() for c in coins[:7]]
                tags = ["coingecko", "trending"] + [s for s in trending_symbols if s]
                article = NewsArticle(
                    title="CoinGecko Trending Coins",
                    summary=f"Currently trending: {', '.join(trending_names)}",
                    source="coingecko",
                    url="https://www.coingecko.com/en/trending",
                    published=datetime.now(timezone.utc),
                    sentiment="bullish",
                    relevance_score=0.6,
                    tags=tags,
                )
                articles.append(article)
        except Exception as e:
            logger.warning(f"CoinGecko fetch failed: {e}")

        return articles

    def fetch_ibkr_news(self) -> list[NewsArticle]:
        """Fetch news from IBKR if available and configured."""
        articles = []
        if not getattr(self, 'exchange_client', None):
            return articles
            
        if self.exchange_client.exchange_id != "ibkr" or getattr(self.exchange_client, 'paper_mode', True):
            return articles
            
        try:
            # We fetch news for configured trading pairs
            pairs = self.config.get("trading", {}).get("pairs", [])
            for pair in pairs:
                ib_news = self.exchange_client.get_news(pair, limit=3)
                for n in ib_news:
                    title = n.get("headline", "")
                    if not title:
                        continue
                        
                    # Parse time if possible — MUST be timezone-aware (UTC)
                    pub_time = datetime.now(timezone.utc)
                    if "time" in n:
                        try:
                            time_val = n["time"]
                            if isinstance(time_val, str):
                                pub_time = datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                            elif hasattr(time_val, "isoformat"):
                                pub_time = time_val
                            # Ensure timezone-aware (naive → UTC)
                            if pub_time.tzinfo is None:
                                pub_time = pub_time.replace(tzinfo=timezone.utc)
                        except Exception:
                            pass

                    articles.append(NewsArticle(
                        title=sanitize_input(title, max_length=300),
                        summary=f"IBKR News for {pair}",
                        source=f"IBKR-{n.get('provider', 'News')}",
                        url=n.get("article_id", ""),
                        published=pub_time,
                        tags=[pair.split("-")[0]],
                        relevance_score=0.8
                    ))
        except Exception as e:
            logger.warning(f"IBKR News fetch failed: {e}")
            
        return articles

    def fetch_all(self) -> list[NewsArticle]:
        """Fetch news from all sources, enrich and filter."""
        # Refresh dynamic tickers from Redis each cycle
        self._refresh_tickers_from_redis()

        all_articles = []

        all_articles.extend(self.fetch_reddit())
        all_articles.extend(self.fetch_rss())
        all_articles.extend(self.fetch_coingecko_trending())
        all_articles.extend(self.fetch_ibkr_news())

        # Filter noise (generic/meta discussion threads)
        before_filter = len(all_articles)
        all_articles = [a for a in all_articles if not _is_noise(a.title)]
        if before_filter != len(all_articles):
            logger.info(f"📰 Filtered {before_filter - len(all_articles)} noise posts")

        # Enrich: sentiment, tickers, relevance (using dynamic ticker set)
        tickers = self._known_tickers
        for article in all_articles:
            _enrich_article(article, tickers)

        # Deduplicate by ID
        seen = set()
        unique_articles = []
        for article in all_articles:
            if article.id not in seen:
                seen.add(article.id)
                unique_articles.append(article)

        # Sort by relevance (highest first), then by recency
        unique_articles.sort(
            key=lambda a: (a.relevance_score, a.published or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )

        # Store
        with self._lock:
            self.articles = unique_articles[:self.max_articles]
            self.total_fetched += len(unique_articles)
            self.last_fetch_time = datetime.now(timezone.utc)

        # Store in Redis if available; publish update signal for subscribers
        if self.redis:
            try:
                payload = json.dumps([asdict(a) for a in self.articles[-80:]], default=str)
                # Always write to global key for backward compat / "All Systems" view
                self.redis.set(
                    "news:latest",
                    payload,
                    ex=600,  # 10 min TTL
                )
                # NOTE: profile-specific keys (news:{profile}:latest) are written
                # by the news worker with proper routing logic — do NOT write them
                # here to avoid race conditions and incorrect filtering.

                # Notify any subscribers (e.g. orchestrator) that fresh news is available.
                # Non-blocking: if the channel has no subscribers the message is silently dropped.
                self.redis.publish(
                    "news:updates",
                    json.dumps({"count": len(unique_articles), "profile": self.profile}, default=str),
                )
            except Exception as e:
                logger.warning(f"Redis store failed: {e}")

        logger.info(
            f"📰 Total aggregated: {len(unique_articles)} articles from all sources"
        )
        return unique_articles

    def get_latest(self, count: int = 15) -> list[NewsArticle]:
        """Get the latest N articles.

        Prefers in-memory cache. Falls back to Redis ``news:latest`` key when
        the cache is empty — this lets the agent pick up articles fetched by
        the separate news-worker process without duplicating the fetching work.
        """
        with self._lock:
            local = self.articles[-count:]

        if local:
            return local

        # Cache miss — try to hydrate from Redis (written by news-worker)
        # Prefer profile-scoped key to prevent cross-domain news bleed
        if self.redis:
            try:
                raw = None
                if self.profile:
                    raw = self.redis.get(f"news:{self.profile}:latest")
                if not raw:
                    raw = self.redis.get("news:latest")
                if raw:
                    data = json.loads(raw)
                    articles = []
                    for d in data[-count:]:
                        pub = d.get("published")
                        if isinstance(pub, str):
                            try:
                                d["published"] = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                            except ValueError:
                                d["published"] = None
                        articles.append(NewsArticle(**d))
                    logger.debug(f"📰 Loaded {len(articles)} articles from Redis (news-worker cache)")
                    return articles
            except Exception as e:
                logger.warning(f"Redis news fallback failed: {e}")

        return []

    def get_headlines(self, count: int = 10) -> str:
        """Get a formatted string of recent headlines for LLM consumption."""
        articles = self.get_latest(count)
        if not articles:
            return "No recent news available."

        lines = []
        for i, article in enumerate(articles, 1):
            age = ""
            if article.published:
                delta = datetime.now(timezone.utc) - article.published
                if delta.total_seconds() < 3600:
                    age = f" ({int(delta.total_seconds() / 60)}m ago)"
                elif delta.total_seconds() < 86400:
                    age = f" ({int(delta.total_seconds() / 3600)}h ago)"
                else:
                    age = f" ({delta.days}d ago)"

            lines.append(
                f"{i}. [{article.source}]{age} {article.title}"
            )
            if article.summary:
                lines.append(f"   {article.summary[:150]}")

        return "\n".join(lines)

    def get_summary(self, count: int = 10) -> str:
        """Get a summary of recent news for LLM consumption."""
        return self.get_headlines(count)

    def get_stats(self) -> dict:
        """Get aggregator statistics."""
        return {
            "total_articles": len(self.articles),
            "total_fetched": self.total_fetched,
            "last_fetch": self.last_fetch_time.isoformat() if self.last_fetch_time else None,
            "sources": {
                "subreddits": len(self.subreddits),
                "rss_feeds": len(self.rss_feeds),
            },
        }
