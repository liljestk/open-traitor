"""News feed routes."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.news")

router = APIRouter(tags=["News"])


@router.get("/api/news", summary="Recent news headlines with sentiment")
def get_news(
    count: int = Query(30, ge=1, le=100),
    profile: str = Query("", description="Exchange profile"),
    db=Depends(deps.get_profile_db),
):
    """Returns recent news articles from Redis cache (populated by news worker).

    When a profile is set, try the profile-specific key first
    (``news:{profile}:latest``), then fall back to the global ``news:latest``
    key and filter articles by tags matching the profile's news sources.

    Human-followed pairs (from the watchlist) boost relevance: articles whose
    tags or title contain the base symbol of a followed pair are included even
    if they don't match the profile's source config.
    """
    if not deps.redis_client:
        return {"articles": [], "count": 0, "source": "unavailable"}
    try:
        resolved = deps.resolve_profile(profile)
        qc = deps.quote_currency_for(profile)

        # 1) Try profile-specific Redis key
        raw = None
        if resolved:
            raw = deps.redis_client.get(f"news:{resolved}:latest")

        # 2) Fall back to global key
        if not raw:
            raw = deps.redis_client.get("news:latest")

        if not raw:
            return {"articles": [], "count": 0, "source": "redis_empty"}

        articles = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        if not isinstance(articles, list):
            articles = []

        # Build set of base symbols from human-followed pairs for relevance matching
        followed_symbols: set[str] = set()
        try:
            human_pairs = db.get_followed_pairs_set(followed_by="human", quote_currency=qc)
            for p in human_pairs:
                base = p.split("-")[0].lower() if "-" in p else p.lower()
                followed_symbols.add(base)
        except Exception:
            pass  # non-critical

        # 3) Filter articles by profile's news sources when using global key
        if resolved and articles:
            cfg = deps.get_config_for_profile(profile)
            news_cfg = cfg.get("news", {})
            # Build a set of expected source identifiers from the profile's config
            expected_subs = {s.lower() for s in news_cfg.get("reddit_subreddits", [])}
            expected_rss = set()
            for url in news_cfg.get("rss_feeds", []):
                # Extract domain-like identifier from RSS URL
                m = re.search(r'//(?:www\.)?([^/]+)', url)
                if m:
                    expected_rss.add(m.group(1).lower().replace(".", "_"))

            # CoinGecko trending is crypto-specific
            crypto_profiles = {"coinbase", "crypto"}
            has_coingecko = resolved in crypto_profiles

            def _matches_profile(article: dict) -> bool:
                tags = {t.lower() for t in article.get("tags", [])}
                source = (article.get("source") or "").lower()
                title = (article.get("title") or "").lower()
                # Match by subreddit tag
                if tags & expected_subs:
                    return True
                # Match by RSS source tag
                if tags & expected_rss:
                    return True
                # Match coingecko for crypto profiles
                if has_coingecko and "coingecko" in tags:
                    return True
                # Match by source field containing expected identifiers
                for sub in expected_subs:
                    if sub in source:
                        return True
                # Match by human-followed pair symbols appearing in tags or title
                if followed_symbols:
                    if tags & followed_symbols:
                        return True
                    for sym in followed_symbols:
                        if sym in title:
                            return True
                return False

            # Only filter if we have source config or followed symbols; otherwise show all
            if expected_subs or expected_rss or followed_symbols:
                articles = [a for a in articles if _matches_profile(a)]

        articles = articles[:count]
        return {"articles": articles, "count": len(articles), "source": "redis"}
    except Exception as exc:
        logger.warning(f"news endpoint error: {exc}")
        return {"articles": [], "count": 0, "source": "error"}
