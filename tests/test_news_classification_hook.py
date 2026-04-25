"""Test that EventManager._classify_latest_news pulls the latest articles
and stores classifications under a profile-prefixed Redis key."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.core.llm_advisor import LLMAdvisor, NewsClassification


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}
        self.expirations = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.expirations[key] = ttl


def _make_event_manager(redis, advisor):
    """Build a minimal EventManager pointing at a stub orchestrator."""
    from src.core.managers.event_manager import EventManager

    orch = MagicMock()
    orch.redis = redis
    orch.advisor = advisor
    orch.config = {"trading": {"exchange": "coinbase"}}
    em = EventManager.__new__(EventManager)
    em.orchestrator = orch
    return em


def test_classify_latest_news_writes_classified_key(monkeypatch):
    monkeypatch.delenv("AUTO_TRAITOR_PROFILE", raising=False)
    headlines = [
        {"title": "BTC ETF approved", "description": "..."},
        {"title": "Exchange hack disclosed", "description": "..."},
    ]
    redis = _FakeRedis({"news:coinbase:latest": json.dumps(headlines)})

    # Stub LLM client → always replies with a deterministic JSON.
    def _stub(prompt: str) -> str:
        return json.dumps({
            "sentiment": "bullish",
            "severity": 0.5,
            "affected_assets": ["BTC"],
            "reasoning": "stub",
            "confidence": 0.7,
        })
    advisor = LLMAdvisor(llm_client=_stub)

    em = _make_event_manager(redis, advisor)
    em._classify_latest_news()

    raw = redis.get("news:coinbase:classified")
    assert raw is not None
    out = json.loads(raw)
    assert isinstance(out, list)
    assert len(out) == 2
    for entry in out:
        assert entry["sentiment"] == "bullish"
        assert entry["confidence"] == 0.7


def test_classify_latest_news_noop_without_advisor():
    redis = _FakeRedis({"news:coinbase:latest": json.dumps([{"title": "x"}])})
    em = _make_event_manager(redis, None)
    # Should not raise, should not write the classified key.
    em._classify_latest_news()
    assert redis.get("news:coinbase:classified") is None
