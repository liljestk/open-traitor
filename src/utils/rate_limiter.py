"""
Thread-safe rate limiter for API calls.
Respects Coinbase, Telegram, and other service rate limits.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("utils.rate_limiter")


class RateLimiter:
    """
    Token-bucket rate limiter with per-service tracking.

    Rate limits (free tiers):
      - Coinbase REST API: 10 requests/second
      - Coinbase WebSocket: 750 messages/second
      - Telegram Bot API: 30 messages/second (to same chat: 1/s)
      - Reddit API: 60 requests/minute (with auth), 10/min (without)
      - CoinGecko: 10-30 requests/minute (free tier)
    """

    # Default rate limits per service
    DEFAULT_LIMITS = {
        "coinbase_rest": {"calls": 8, "period": 1.0},        # 8/s (buffer from 10)
        "coinbase_ws": {"calls": 500, "period": 1.0},        # 500/s (buffer from 750)
        "telegram": {"calls": 25, "period": 1.0},            # 25/s (buffer from 30)
        "telegram_chat": {"calls": 1, "period": 1.1},        # 1/s per chat (strict)
        "reddit": {"calls": 50, "period": 60.0},             # 50/min (buffer from 60)
        "reddit_noauth": {"calls": 8, "period": 60.0},       # 8/min (buffer from 10)
        "coingecko": {"calls": 8, "period": 60.0},           # 8/min
        "ollama": {"calls": 5, "period": 1.0},               # Self-imposed for GPU
    }

    def __init__(self, custom_limits: Optional[dict] = None):
        self._limits = {**self.DEFAULT_LIMITS}
        if custom_limits:
            self._limits.update(custom_limits)

        # Track calls per service: {service: [timestamps]}
        self._calls: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

        logger.info("⏱️ Rate limiter initialized")

    def acquire(self, service: str, block: bool = True, timeout: float = 30.0) -> bool:
        """
        Acquire a rate limit token for the given service.
        If block=True, waits until a token is available.
        Returns True if acquired, False if timed out.
        """
        if service not in self._limits:
            return True  # Unknown service = no limit

        limit = self._limits[service]
        max_calls = limit["calls"]
        period = limit["period"]
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                now = time.monotonic()

                # Remove old entries outside the window
                self._calls[service] = [
                    t for t in self._calls[service]
                    if now - t < period
                ]

                if len(self._calls[service]) < max_calls:
                    self._calls[service].append(now)
                    return True

            if not block:
                return False

            if time.monotonic() >= deadline:
                logger.warning(f"Rate limit timeout for {service}")
                return False

            # Wait a bit before retrying
            wait = period / max_calls
            time.sleep(min(wait, 0.1))

    async def async_acquire(self, service: str, timeout: float = 30.0) -> bool:
        """
        Acquire a token asynchronously without blocking the event loop.
        """
        import asyncio
        if service not in self._limits:
            return True

        limit = self._limits[service]
        max_calls = limit["calls"]
        period = limit["period"]
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                now = time.monotonic()

                # Remove old entries outside the window
                self._calls[service] = [
                    t for t in self._calls[service]
                    if now - t < period
                ]

                if len(self._calls[service]) < max_calls:
                    self._calls[service].append(now)
                    return True

            if time.monotonic() >= deadline:
                logger.warning(f"Rate limit timeout for {service}")
                return False

            # Wait a bit before retrying
            wait = period / max_calls
            await asyncio.sleep(min(wait, 0.1))

    def wait(self, service: str, timeout: float = 30.0) -> None:
        """Blocking acquire — raises if timed out."""
        if not self.acquire(service, block=True, timeout=timeout):
            raise RuntimeError(f"Rate limit timeout for {service} after {timeout}s")

    async def async_wait(self, service: str, timeout: float = 30.0) -> None:
        """Non-blocking wait for rate limit token."""
        if not await self.async_acquire(service, timeout=timeout):
            raise RuntimeError(f"Rate limit timeout for {service} after {timeout}s")

    def get_status(self) -> dict:
        """Get current rate limit status for all services."""
        with self._lock:
            now = time.monotonic()
            status = {}
            for service, limit in self._limits.items():
                recent = [
                    t for t in self._calls.get(service, [])
                    if now - t < limit["period"]
                ]
                status[service] = {
                    "used": len(recent),
                    "limit": limit["calls"],
                    "period_seconds": limit["period"],
                    "available": limit["calls"] - len(recent),
                }
            return status


# Global rate limiter instance
_global_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter."""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = RateLimiter()
    return _global_limiter
