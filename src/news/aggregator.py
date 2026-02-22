"""
Crypto News Aggregator — Fetches news from multiple open sources.
Sources: Reddit, RSS feeds (CoinTelegraph, CoinDesk, Decrypt, etc.)
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

logger = get_logger("news.aggregator")


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
    ):
        self.config = config
        self.redis = redis_client
        self.profile = profile  # e.g. "coinbase", "nordnet" — used for Redis key

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

        # Stats
        self.last_fetch_time: Optional[datetime] = None
        self.total_fetched = 0

        logger.info(
            f"📰 News Aggregator initialized | "
            f"Subreddits: {len(self.subreddits)} | RSS feeds: {len(self.rss_feeds)}"
        )

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
                            title=post.title,
                            summary=post.selftext[:500] if post.selftext else "",
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
                        title=post.get("title", ""),
                        summary=post.get("selftext", "")[:500],
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

    def fetch_rss(self) -> list[NewsArticle]:
        """Fetch articles from RSS feeds."""
        articles = []

        for feed_url in self.rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                source_name = feed.feed.get("title", feed_url)

                for entry in feed.entries[:10]:
                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

                    summary = entry.get("summary", "")
                    # Strip HTML tags from summary
                    summary = re.sub(r"<[^>]+>", "", summary)[:500]

                    article = NewsArticle(
                        title=entry.get("title", ""),
                        summary=summary,
                        source=source_name,
                        url=entry.get("link", ""),
                        published=published,
                        tags=["rss", source_name.lower().replace(" ", "_")],
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
                article = NewsArticle(
                    title="CoinGecko Trending Coins",
                    summary=f"Currently trending: {', '.join(trending_names)}",
                    source="coingecko",
                    url="https://www.coingecko.com/en/trending",
                    published=datetime.now(timezone.utc),
                    tags=["coingecko", "trending"],
                )
                articles.append(article)
        except Exception as e:
            logger.warning(f"CoinGecko fetch failed: {e}")

        return articles

    def fetch_all(self) -> list[NewsArticle]:
        """Fetch news from all sources."""
        all_articles = []

        all_articles.extend(self.fetch_reddit())
        all_articles.extend(self.fetch_rss())
        all_articles.extend(self.fetch_coingecko_trending())

        # Deduplicate by ID
        seen = set()
        unique_articles = []
        for article in all_articles:
            if article.id not in seen:
                seen.add(article.id)
                unique_articles.append(article)

        # Store
        with self._lock:
            self.articles = unique_articles[-self.max_articles:]
            self.total_fetched += len(unique_articles)
            self.last_fetch_time = datetime.now(timezone.utc)

        # Store in Redis if available; publish update signal for subscribers
        if self.redis:
            try:
                payload = json.dumps([asdict(a) for a in self.articles[-20:]], default=str)
                # Always write to global key for backward compat / "All Systems" view
                self.redis.set(
                    "news:latest",
                    payload,
                    ex=600,  # 10 min TTL
                )
                # Also write to profile-specific key when running under a profile
                if self.profile:
                    self.redis.set(
                        f"news:{self.profile}:latest",
                        payload,
                        ex=600,
                    )
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
        if self.redis:
            try:
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
