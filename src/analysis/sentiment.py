"""
Sentiment Scoring Module — Pre-LLM numerical sentiment analysis.

Converts raw news/social text into a normalized sentiment score [-1.0, 1.0]
using keyword-based scoring (no external NLP dependencies required).

This provides a fast, deterministic baseline sentiment signal that the
market analyst agent can use alongside LLM-based reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("analysis.sentiment")


# ─── Crypto-specific sentiment lexicon ───────────────────────────────────

_BULLISH_KEYWORDS: dict[str, float] = {
    # Strong bullish (0.6-1.0)
    "breakout": 0.8,
    "all-time high": 0.9,
    "ath": 0.9,
    "moon": 0.7,
    "parabolic": 0.8,
    "accumulation": 0.7,
    "institutional buying": 0.9,
    "etf approved": 1.0,
    "etf approval": 1.0,
    "adoption": 0.7,
    "partnership": 0.6,
    "bullish": 0.7,
    "rally": 0.7,
    "surge": 0.7,
    "soaring": 0.8,
    "skyrocket": 0.9,
    "pump": 0.6,
    "long": 0.5,
    "recovery": 0.6,
    "rebound": 0.6,
    "upgrade": 0.6,
    "buying pressure": 0.7,
    "support held": 0.6,
    "golden cross": 0.8,
    "halving": 0.7,
    # Moderate bullish (0.3-0.5)
    "growth": 0.4,
    "positive": 0.4,
    "gain": 0.4,
    "upside": 0.5,
    "buy": 0.4,
    "optimistic": 0.5,
    "bullish divergence": 0.6,
    "strong": 0.3,
    "confidence": 0.4,
}

_BEARISH_KEYWORDS: dict[str, float] = {
    # Strong bearish (0.6-1.0)
    "crash": 0.9,
    "collapse": 0.9,
    "plunge": 0.8,
    "capitulation": 0.9,
    "liquidation": 0.8,
    "hack": 0.9,
    "exploit": 0.8,
    "rug pull": 1.0,
    "rugpull": 1.0,
    "scam": 0.9,
    "fraud": 0.9,
    "sec lawsuit": 0.8,
    "sec charges": 0.8,
    "delisting": 0.9,
    "ban": 0.8,
    "regulation crackdown": 0.8,
    "death cross": 0.8,
    "bear market": 0.7,
    "bearish": 0.7,
    "dump": 0.7,
    "sell-off": 0.8,
    "selloff": 0.8,
    "panic": 0.8,
    "fear": 0.6,
    "fud": 0.6,
    "short": 0.5,
    # Moderate bearish (0.3-0.5)
    "decline": 0.5,
    "drop": 0.4,
    "loss": 0.4,
    "downside": 0.5,
    "sell": 0.4,
    "pessimistic": 0.5,
    "bearish divergence": 0.6,
    "weak": 0.3,
    "resistance rejected": 0.5,
    "overvalued": 0.4,
    "bubble": 0.6,
    "warning": 0.4,
}


@dataclass
class SentimentResult:
    """Result of sentiment analysis on a piece of text."""
    score: float              # -1.0 (max bearish) to 1.0 (max bullish)
    label: str                # "very_bullish", "bullish", "neutral", "bearish", "very_bearish"
    confidence: float         # 0.0-1.0, how many keywords matched
    bullish_matches: list[str] = field(default_factory=list)
    bearish_matches: list[str] = field(default_factory=list)
    source: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "bullish_matches": self.bullish_matches,
            "bearish_matches": self.bearish_matches,
            "source": self.source,
            "timestamp": self.timestamp,
        }


@dataclass
class AggregateSentiment:
    """Aggregated sentiment across multiple sources."""
    mean_score: float
    median_score: float
    weighted_score: float  # Recency-weighted
    bullish_count: int
    bearish_count: int
    neutral_count: int
    total_sources: int
    label: str
    top_bullish: list[str] = field(default_factory=list)
    top_bearish: list[str] = field(default_factory=list)


class SentimentAnalyzer:
    """
    Keyword-based crypto sentiment analyzer.

    Produces numerical scores from text without requiring external NLP
    libraries. Designed for speed and determinism in the trading pipeline.
    """

    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("sentiment", {})
        self.bullish_keywords = _BULLISH_KEYWORDS.copy()
        self.bearish_keywords = _BEARISH_KEYWORDS.copy()

        # Allow config to add custom keywords
        for kw, weight in cfg.get("custom_bullish", {}).items():
            self.bullish_keywords[kw.lower()] = weight
        for kw, weight in cfg.get("custom_bearish", {}).items():
            self.bearish_keywords[kw.lower()] = weight

    def _score_label(self, score: float) -> str:
        """Convert numeric score to label."""
        if score >= 0.5:
            return "very_bullish"
        elif score >= 0.15:
            return "bullish"
        elif score <= -0.5:
            return "very_bearish"
        elif score <= -0.15:
            return "bearish"
        return "neutral"

    def analyze_text(self, text: str, source: str = "") -> SentimentResult:
        """
        Analyze a single text for crypto sentiment.

        Returns SentimentResult with score from -1.0 (bearish) to 1.0 (bullish).
        """
        if not text:
            return SentimentResult(
                score=0.0,
                label="neutral",
                confidence=0.0,
                source=source,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        text_lower = text.lower()

        bullish_score = 0.0
        bearish_score = 0.0
        bull_matches = []
        bear_matches = []

        # Check multi-word phrases first (longer matches take priority)
        for keyword, weight in sorted(
            self.bullish_keywords.items(), key=lambda x: -len(x[0])
        ):
            if keyword in text_lower:
                bullish_score += weight
                bull_matches.append(keyword)

        for keyword, weight in sorted(
            self.bearish_keywords.items(), key=lambda x: -len(x[0])
        ):
            if keyword in text_lower:
                bearish_score += weight
                bear_matches.append(keyword)

        total_matches = len(bull_matches) + len(bear_matches)

        if total_matches == 0:
            return SentimentResult(
                score=0.0,
                label="neutral",
                confidence=0.0,
                source=source,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Net score normalized to [-1, 1]
        raw_score = bullish_score - bearish_score
        max_possible = max(bullish_score + bearish_score, 1.0)
        score = max(-1.0, min(1.0, raw_score / max_possible))

        # Confidence based on number of keyword matches (more = more confident)
        confidence = min(1.0, total_matches / 5.0)

        return SentimentResult(
            score=score,
            label=self._score_label(score),
            confidence=confidence,
            bullish_matches=bull_matches[:5],
            bearish_matches=bear_matches[:5],
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def analyze_batch(
        self,
        items: list[dict],
        text_key: str = "title",
        source_key: str = "source",
    ) -> AggregateSentiment:
        """
        Analyze a batch of news/social items.

        Args:
            items: list of dicts with text content
            text_key: key containing the text to analyze
            source_key: key containing the source name

        Returns:
            AggregateSentiment with combined scores
        """
        if not items:
            return AggregateSentiment(
                mean_score=0.0,
                median_score=0.0,
                weighted_score=0.0,
                bullish_count=0,
                bearish_count=0,
                neutral_count=0,
                total_sources=0,
                label="neutral",
            )

        results = []
        for item in items:
            text = item.get(text_key, "") or ""
            # Also check body/content/description
            for extra_key in ["body", "content", "description", "summary"]:
                extra = item.get(extra_key, "")
                if extra:
                    text = f"{text} {extra}"
            source = item.get(source_key, "")
            results.append(self.analyze_text(text, source))

        scores = [r.score for r in results]
        scores.sort()

        # Median
        n = len(scores)
        median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2

        # Recency-weighted: more recent items get higher weight
        # (assumes items are ordered newest-first)
        weights = [1.0 / (i + 1) for i in range(len(scores))]
        weight_sum = sum(weights)
        weighted_score = sum(s * w for s, w in zip(scores, weights)) / weight_sum if weight_sum > 0 else 0.0

        # Counts
        bullish = sum(1 for r in results if r.label in ("bullish", "very_bullish"))
        bearish = sum(1 for r in results if r.label in ("bearish", "very_bearish"))
        neutral = sum(1 for r in results if r.label == "neutral")

        mean_score = sum(scores) / len(scores) if scores else 0.0

        # Top keywords
        all_bull = []
        all_bear = []
        for r in results:
            all_bull.extend(r.bullish_matches)
            all_bear.extend(r.bearish_matches)

        # Count frequency of each keyword
        from collections import Counter
        top_bull = [kw for kw, _ in Counter(all_bull).most_common(5)]
        top_bear = [kw for kw, _ in Counter(all_bear).most_common(5)]

        return AggregateSentiment(
            mean_score=mean_score,
            median_score=median,
            weighted_score=weighted_score,
            bullish_count=bullish,
            bearish_count=bearish,
            neutral_count=neutral,
            total_sources=len(results),
            label=self._score_label(weighted_score),
            top_bullish=top_bull,
            top_bearish=top_bear,
        )

    def score_for_pair(
        self,
        pair: str,
        news_items: list[dict],
    ) -> dict:
        """
        Convenience: filter news by pair, analyze, return dict for pipeline.

        Returns dict suitable for inclusion in analysis context:
          {
            "sentiment_score": float,
            "sentiment_label": str,
            "bullish_count": int,
            "bearish_count": int,
            "top_keywords": list[str],
          }
        """
        base = pair.split("-")[0].lower() if "-" in pair else pair.lower()

        # Map common symbols to search terms
        name_map = {
            "btc": ["bitcoin", "btc"],
            "eth": ["ethereum", "eth", "ether"],
            "sol": ["solana", "sol"],
            "doge": ["dogecoin", "doge"],
            "xrp": ["ripple", "xrp"],
            "ada": ["cardano", "ada"],
            "avax": ["avalanche", "avax"],
            "link": ["chainlink", "link"],
            "dot": ["polkadot", "dot"],
            "matic": ["polygon", "matic"],
            "atom": ["cosmos", "atom"],
        }

        search_terms = name_map.get(base, [base])

        # Filter items that mention this pair
        relevant = []
        for item in news_items:
            text = (
                (item.get("title", "") or "") + " " +
                (item.get("body", "") or "") + " " +
                (item.get("description", "") or "")
            ).lower()
            if any(term in text for term in search_terms):
                relevant.append(item)

        if not relevant:
            return {
                "sentiment_score": 0.0,
                "sentiment_label": "neutral",
                "bullish_count": 0,
                "bearish_count": 0,
                "neutral_count": 0,
                "total_articles": 0,
                "top_keywords": [],
            }

        agg = self.analyze_batch(relevant)

        return {
            "sentiment_score": round(agg.weighted_score, 4),
            "sentiment_label": agg.label,
            "bullish_count": agg.bullish_count,
            "bearish_count": agg.bearish_count,
            "neutral_count": agg.neutral_count,
            "total_articles": agg.total_sources,
            "top_keywords": agg.top_bullish + agg.top_bearish,
        }
