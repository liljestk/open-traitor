"""
Background news worker — runs as a separate process in Docker.
Continuously fetches and processes crypto news.
"""

from __future__ import annotations

import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml
from dotenv import load_dotenv

from src.news.aggregator import NewsAggregator
from src.utils.logger import setup_logger, get_logger


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

    # Initialize aggregator
    news_config = config.get("news", {})
    aggregator = NewsAggregator(
        config=news_config,
        redis_client=redis_client,
        reddit_client_id=os.environ.get("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.environ.get("REDDIT_USER_AGENT", "auto-traitor-bot/0.1"),
    )

    fetch_interval = news_config.get("fetch_interval", 300)

    logger.info(f"📰 News worker running | Fetch interval: {fetch_interval}s")

    # Main loop
    while True:
        try:
            articles = aggregator.fetch_all()
            logger.info(f"📰 Fetched {len(articles)} articles")

            stats = aggregator.get_stats()
            if redis_client:
                import json
                redis_client.set("news:stats", json.dumps(stats, default=str), ex=600)

        except Exception as e:
            logger.error(f"News fetch failed: {e}")

        time.sleep(fetch_interval)


if __name__ == "__main__":
    main()
