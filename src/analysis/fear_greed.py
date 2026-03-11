"""
Fear & Greed Index — Free sentiment indicator.
Source: alternative.me (no API key required)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.utils.logger import get_logger
from src.utils.rate_limiter import get_rate_limiter

logger = get_logger("analysis.fear_greed")

# Cache results to avoid hammering the free API
_cache: dict = {"value": None, "timestamp": 0}
CACHE_TTL = 600  # 10 minutes


class FearGreedIndex:
    """
    Crypto Fear & Greed Index from alternative.me.
    Free, no API key required. Updated daily.

    Scale:
        0-25   = Extreme Fear  (potential buy opportunity)
        26-46  = Fear
        47-54  = Neutral
        55-75  = Greed
        76-100 = Extreme Greed (potential sell signal)
    """

    API_URL = "https://api.alternative.me/fng/?limit=7"

    def __init__(self):
        self.rate_limiter = get_rate_limiter()
        self.last_value: Optional[int] = None
        self.last_classification: Optional[str] = None
        self.last_update: Optional[datetime] = None
        self.history: list[dict] = []

    def fetch(self) -> dict:
        """
        Fetch the current Fear & Greed Index.
        Returns dict with value, classification, and timestamp.
        Uses caching to avoid excessive API calls.
        """
        global _cache

        # Check cache
        now = time.time()
        if _cache["value"] and (now - _cache["timestamp"]) < CACHE_TTL:
            return _cache["value"]

        self.rate_limiter.wait("coingecko")  # Reuse same tier rate limit

        try:
            response = requests.get(self.API_URL, timeout=10)
            response.raise_for_status()
            data = response.json()

            entries = data.get("data", [])
            if not entries:
                logger.warning("Fear & Greed API returned no data")
                return self._fallback()

            current = entries[0]
            value = int(current.get("value", 50))
            classification = current.get("value_classification", "Neutral")
            timestamp = int(current.get("timestamp", time.time()))

            self.last_value = value
            self.last_classification = classification
            self.last_update = datetime.fromtimestamp(timestamp, tz=timezone.utc)

            # Store history (last 7 days)
            self.history = [
                {
                    "value": int(e.get("value", 50)),
                    "classification": e.get("value_classification", "Neutral"),
                    "date": datetime.fromtimestamp(
                        int(e.get("timestamp", 0)), tz=timezone.utc
                    ).strftime("%Y-%m-%d"),
                }
                for e in entries
            ]

            result = {
                "value": value,
                "classification": classification,
                "signal": self._to_signal(value),
                "trend": self._calculate_trend(),
                "history": self.history,
            }

            # Update cache
            _cache = {"value": result, "timestamp": now}

            logger.info(
                f"😱 Fear & Greed Index: {value} ({classification}) — "
                f"Signal: {result['signal']}"
            )
            return result

        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")
            return self._fallback()

    def _to_signal(self, value: int) -> str:
        """Convert F&G value to a trading signal interpretation."""
        if value <= 15:
            return "extreme_fear_buy_opportunity"
        elif value <= 25:
            return "fear_potential_buy"
        elif value <= 46:
            return "mild_fear_cautious"
        elif value <= 54:
            return "neutral"
        elif value <= 75:
            return "greed_caution"
        elif value <= 85:
            return "high_greed_consider_selling"
        else:
            return "extreme_greed_sell_signal"

    def _calculate_trend(self) -> str:
        """Calculate if fear/greed is trending up or down over last week."""
        if len(self.history) < 3:
            return "insufficient_data"

        recent = self.history[0]["value"]
        week_ago = self.history[-1]["value"]
        diff = recent - week_ago

        if diff > 15:
            return "rapidly_increasing_greed"
        elif diff > 5:
            return "increasing_greed"
        elif diff > -5:
            return "stable"
        elif diff > -15:
            return "increasing_fear"
        else:
            return "rapidly_increasing_fear"

    def _fallback(self) -> dict:
        """Return cached or default value."""
        if self.last_value is not None:
            return {
                "value": self.last_value,
                "classification": self.last_classification,
                "signal": self._to_signal(self.last_value),
                "trend": "cached",
                "history": self.history,
            }
        return {
            "value": 50,
            "classification": "Neutral",
            "signal": "neutral",
            "trend": "unavailable",
            "history": [],
        }

    def get_current(self) -> dict:
        """Get the current Fear & Greed Index data (alias for fetch)."""
        return self.fetch()

    def get_for_prompt(self) -> str:
        """Get a formatted string suitable for LLM prompt injection."""
        data = self.fetch()
        trend_emoji = {
            "rapidly_increasing_greed": "📈📈",
            "increasing_greed": "📈",
            "stable": "➡️",
            "increasing_fear": "📉",
            "rapidly_increasing_fear": "📉📉",
        }
        emoji = trend_emoji.get(data["trend"], "❓")

        return (
            f"Fear & Greed Index: {data['value']}/100 "
            f"({data['classification']}) {emoji}\n"
            f"Signal: {data['signal'].replace('_', ' ').title()}\n"
            f"7-Day Trend: {data['trend'].replace('_', ' ').title()}"
        )
