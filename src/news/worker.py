"""
Background news worker — runs as a separate process in Docker.

Discovers *all* exchange profile configs (coinbase.yaml, ibkr.yaml)
and aggregates news from each profile's configured sources (subreddits + RSS feeds).

Writes to Redis:
  news:latest                — global union of all articles (capped at max_articles)
  news:{profile}:latest      — articles from that profile's sources only
  news:stats                 — aggregator statistics
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml
from dotenv import load_dotenv

from src.news.aggregator import NewsAggregator
from src.utils.logger import setup_logger, get_logger


# Profile configs we attempt to discover (filename stem → profile name)
_PROFILE_FILES: dict[str, str] = {
    "coinbase.yaml": "coinbase",
    "ibkr.yaml": "ibkr",
}


def _discover_profiles(config_dir: str) -> dict[str, dict]:
    """Return {profile_name: news_config} for each discovered config file."""
    profiles: dict[str, dict] = {}
    for filename, profile in _PROFILE_FILES.items():
        path = Path(config_dir) / filename
        if path.exists():
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
                news_cfg = cfg.get("news", {})
                if news_cfg.get("rss_feeds") or news_cfg.get("reddit_subreddits"):
                    profiles[profile] = news_cfg
            except Exception:
                pass
    return profiles


def main():
    # Load config
    load_dotenv("config/.env")

    with open("config/settings.yaml", "r") as f:
        config = yaml.safe_load(f)

    # Setup logging
    log_config = config.get("logging", {})
    setup_logger(
        log_level=log_config.get("level", "INFO"),
        log_dir=log_config.get("directory", "logs"),
    )

    logger = get_logger("news.worker")
    logger.info("═══════════════════════════════════════════")
    logger.info("  📰 News Worker Starting")
    logger.info("═══════════════════════════════════════════")

    # Redis
    redis_client = None
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis
            redis_client = redis.Redis.from_url(redis_url)
            redis_client.ping()
            logger.info("✅ Redis connected")
        except Exception as e:
            logger.warning(f"Redis not available: {e}")

    # Discover per-profile news configs
    config_dir = os.environ.get("CONFIG_DIR", "config")
    profile_configs = _discover_profiles(config_dir)
    global_news_cfg = config.get("news", {})

    # Merge all unique sources across profiles + global settings
    all_subreddits: set[str] = set(global_news_cfg.get("reddit_subreddits", []))
    all_rss: set[str] = set(global_news_cfg.get("rss_feeds", []))
    for pcfg in profile_configs.values():
        all_subreddits.update(pcfg.get("reddit_subreddits", []))
        all_rss.update(pcfg.get("rss_feeds", []))

    merged_config = {
        **global_news_cfg,
        "reddit_subreddits": sorted(all_subreddits),
        "rss_feeds": sorted(all_rss),
    }

    logger.info(
        f"📰 Discovered profiles: {list(profile_configs.keys()) or ['(global only)']}"
    )
    logger.info(
        f"📰 Merged sources: {len(all_subreddits)} subreddits, {len(all_rss)} RSS feeds"
    )

    reddit_creds = {
        "reddit_client_id": os.environ.get("REDDIT_CLIENT_ID", ""),
        "reddit_client_secret": os.environ.get("REDDIT_CLIENT_SECRET", ""),
        "reddit_user_agent": os.environ.get("REDDIT_USER_AGENT", "auto-traitor-bot/0.1"),
    }

    # Try to create an IBKR client for fetching exchange-native news
    ibkr_client = None
    if "ibkr" in profile_configs:
        ibkr_news_cfg = profile_configs["ibkr"]
        if ibkr_news_cfg.get("ibkr_news_enabled", True):
            try:
                from src.core.ib_client import IBClient
                ib_host = os.environ.get("IBKR_HOST", "127.0.0.1")
                ib_port = int(os.environ.get("IBKR_PORT", "4002"))
                ib_client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))
                ibkr_client = IBClient(
                    paper_mode=False,
                    ib_host=ib_host,
                    ib_port=ib_port,
                    ib_client_id=ib_client_id + 20,  # Offset to avoid ID collision
                )
                logger.info("✅ IBKR client connected for news fetching")
            except Exception as e:
                logger.warning(f"⚠️ IBKR client not available for news: {e}")

    # Load IBKR trading config for pairs (needed by fetch_ibkr_news)
    ibkr_full_config = {}
    if "ibkr" in profile_configs:
        try:
            ibkr_cfg_path = Path(config_dir) / "ibkr.yaml"
            with open(ibkr_cfg_path) as f:
                ibkr_full_config = yaml.safe_load(f) or {}
        except Exception:
            pass

    # Inject IBKR trading pairs into merged config for news fetching
    if ibkr_full_config.get("trading", {}).get("pairs"):
        merged_config["trading"] = ibkr_full_config.get("trading", {})

    # Single aggregator with merged sources (avoids duplicate HTTP requests)
    aggregator = NewsAggregator(
        config=merged_config,
        redis_client=redis_client,
        exchange_client=ibkr_client,
        profile="ibkr" if ibkr_client else None,
        **reddit_creds,
    )

    _intervals = [merged_config.get("fetch_interval", 300)] + [
        pcfg.get("fetch_interval", 300) for pcfg in profile_configs.values()
    ]
    fetch_interval = min(_intervals)

    # Pre-compute per-profile matching sets for fast article routing
    profile_match: dict[str, dict] = {}
    for pname, pcfg in profile_configs.items():
        subs = {s.lower() for s in pcfg.get("reddit_subreddits", [])}
        rss_ids: set[str] = set()
        import re as _re
        for url in pcfg.get("rss_feeds", []):
            m = _re.search(r'//(?:www\.)?([^/]+)', url)
            if m:
                rss_ids.add(m.group(1).lower().replace(".", "_"))
        # For IBKR profile, also build a set of tracked ticker symbols
        ibkr_tickers: set[str] = set()
        if pname == "ibkr":
            try:
                ibkr_cfg_path = Path(config_dir) / "ibkr.yaml"
                with open(ibkr_cfg_path) as f:
                    ibkr_cfg = yaml.safe_load(f) or {}
                for pair in ibkr_cfg.get("trading", {}).get("pairs", []):
                    base = pair.split("-")[0].upper() if "-" in pair else pair.upper()
                    ibkr_tickers.add(base)
            except Exception:
                pass
        profile_match[pname] = {"subs": subs, "rss": rss_ids, "ibkr_tickers": ibkr_tickers}

    def _route_article(article: dict, subs: set[str], rss_ids: set[str], ibkr_tickers: set[str] | None = None) -> bool:
        """Return True if the article belongs to a profile's sources."""
        tags = {t.lower() for t in article.get("tags", [])}
        source = (article.get("source") or "").lower()
        if tags & subs:
            return True
        if tags & rss_ids:
            return True
        for sub in subs:
            if sub in source:
                return True
        for rid in rss_ids:
            if rid in source:
                return True
        # Match IBKR-sourced articles by source prefix or ticker tags
        if ibkr_tickers:
            if "ibkr" in source or "benzinga" in source or "ib-" in source:
                return True
            tags_upper = {t.upper() for t in article.get("tags", [])}
            if tags_upper & ibkr_tickers:
                return True
        return False

    logger.info(f"📰 News worker running | Fetch interval: {fetch_interval}s")

    # Main loop
    while True:
        try:
            articles = aggregator.fetch_all()
            logger.info(f"📰 Fetched {len(articles)} articles total")

            # Write per-profile keys
            if redis_client and profile_configs:
                from dataclasses import asdict
                all_dicts = [
                    asdict(a) if hasattr(a, "__dataclass_fields__") else a
                    for a in articles
                ]
                for pname, pm in profile_match.items():
                    matched = [
                        a for a in all_dicts
                        if _route_article(a, pm["subs"], pm["rss"], pm.get("ibkr_tickers"))
                    ]
                    max_arts = profile_configs[pname].get("max_articles", 50)
                    matched = matched[:max_arts]
                    redis_client.set(
                        f"news:{pname}:latest",
                        json.dumps(matched, default=str),
                        ex=900,
                    )
                    logger.info(f"  └─ {pname}: {len(matched)} articles")

            stats = aggregator.get_stats()
            if redis_client:
                redis_client.set("news:stats", json.dumps(stats, default=str), ex=600)

        except Exception as e:
            logger.error(f"News fetch failed: {e}")

        time.sleep(fetch_interval)


if __name__ == "__main__":
    main()
