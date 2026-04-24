"""
News Reflex Agent (Phase 14).

Reads aggregated news from Redis (key ``news:{profile}:latest``) once per
cycle, scores it for net sentiment + impact, and writes a ``news_bias``
multiplier to ``data/<profile>/news_bias.json`` that the orchestrator can
read at risk-sizing time.

Strict bounds: the bias is in [0.5, 1.5] — never zero, never unbounded.
This is *advisory* input, not a hard rule. AbsoluteRules still gates all
trades.

Output schema:
    {
      "ts": ISO,
      "profile": "coinbase",
      "bias": 1.05,           # 1.0 = neutral, >1.0 risk-on, <1.0 risk-off
      "sentiment_mean": 0.21, # -1..+1
      "high_impact_count": 3,
      "sample_size": 47,
      "rationale": "bullish-leaning recent news"
    }
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from src.utils.logger import get_logger

logger = get_logger("news.reflex")

_BIAS_FLOOR = 0.5
_BIAS_CEIL  = 1.5
_HIGH_IMPACT_RELEVANCE = 0.7


def _sentiment_to_float(label: str) -> float:
    s = (label or "").lower().strip()
    if s in {"bullish", "positive"}:
        return 1.0
    if s in {"bearish", "negative"}:
        return -1.0
    return 0.0


def score_articles(articles: Iterable[dict]) -> dict:
    """Pure scoring function — does NOT touch Redis or the filesystem."""
    arts = list(articles or [])
    if not arts:
        return {
            "bias": 1.0,
            "sentiment_mean": 0.0,
            "high_impact_count": 0,
            "sample_size": 0,
            "rationale": "no articles",
        }
    scores: list[float] = []
    weights: list[float] = []
    high_impact = 0
    for a in arts:
        sent = _sentiment_to_float(a.get("sentiment"))
        rel = float(a.get("relevance", a.get("relevance_score", 0.5)) or 0.0)
        rel = max(0.0, min(1.0, rel))
        if rel >= _HIGH_IMPACT_RELEVANCE and sent != 0.0:
            high_impact += 1
        scores.append(sent)
        weights.append(rel + 0.05)  # floor weight so neutral counts a bit
    total_w = sum(weights) or 1.0
    mean = sum(s * w for s, w in zip(scores, weights)) / total_w
    # Map sentiment in [-1, 1] → bias in [_BIAS_FLOOR, _BIAS_CEIL] linearly,
    # then clamp.
    bias = 1.0 + 0.4 * mean
    bias = max(_BIAS_FLOOR, min(_BIAS_CEIL, bias))
    if mean > 0.15:
        rationale = "bullish-leaning recent news"
    elif mean < -0.15:
        rationale = "bearish-leaning recent news"
    else:
        rationale = "balanced sentiment"
    return {
        "bias": round(bias, 4),
        "sentiment_mean": round(mean, 4),
        "high_impact_count": int(high_impact),
        "sample_size": len(arts),
        "rationale": rationale,
    }


class NewsReflex:
    """Per-profile reflex. Cheap to run every cycle."""

    def __init__(self, profile: str, redis_client=None) -> None:
        self.profile = (profile or "default").lower()
        self.redis = redis_client
        self.out_path = Path("data") / self.profile / "news_bias.json"
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_articles(self) -> list[dict]:
        # Prefer Redis if available, else fall back to data/news/<profile>/...
        if self.redis is not None:
            try:
                key = f"news:{self.profile}:latest"
                raw = self.redis.get(key)
                if raw:
                    data = json.loads(raw)
                    return data if isinstance(data, list) else data.get("articles", [])
            except Exception as e:
                logger.warning(f"news_reflex.redis_read err: {e}")
        # Filesystem fallback
        path = Path("data") / "news" / f"{self.profile}_latest.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return data if isinstance(data, list) else data.get("articles", [])
            except Exception as e:
                logger.warning(f"news_reflex.file_read err: {e}")
        return []

    def evaluate_and_persist(self) -> dict:
        articles = self._read_articles()
        scored = score_articles(articles)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "profile": self.profile,
            **scored,
        }
        try:
            tmp = self.out_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, self.out_path)
        except Exception as e:
            logger.warning(f"news_reflex.persist err: {e}")
        return record

    def current_bias(self) -> float:
        if self.out_path.exists():
            try:
                rec = json.loads(self.out_path.read_text())
                return float(rec.get("bias", 1.0))
            except Exception:
                pass
        return 1.0


__all__ = ["NewsReflex", "score_articles"]
