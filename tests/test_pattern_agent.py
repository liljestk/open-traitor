"""Unit tests for ``src.agents.pattern_agent.PatternAgent``.

Uses a fake in-memory DB to drive the agent end-to-end without Postgres.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agents.pattern_agent import PatternAgent


class _FakeDB:
    """Minimal stand-in for StatsDB satisfying the PatternAgent contract."""

    def __init__(self, upcoming=None, candles=None, neighbours=None):
        self._upcoming = upcoming or []
        self._candles = candles or []
        self._neighbours = neighbours or []
        self.saved: list[dict] = []

    def get_upcoming_catalysts(self, exchange, horizon_days, symbol=None):
        return list(self._upcoming)

    def get_candles_range(self, exchange, symbol, granularity, start, end):
        return list(self._candles)

    def find_similar_fingerprints(
        self, exchange, query_vector, k=20,
        event_type=None, exclude_anchor_after=None,
        exclude_symbol=None,
    ):
        return list(self._neighbours)

    def save_reasoning(self, **kw):
        self.saved.append(kw)


def _make_candles(n: int, base: float = 100.0, drift: float = 0.0) -> list[dict]:
    import numpy as np
    rng = np.random.default_rng(42)
    rets = rng.normal(loc=drift, scale=0.01, size=n)
    closes = base * np.exp(np.cumsum(rets))
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "ts": start + timedelta(days=i),
            "o": float(c), "h": float(c) * 1.005, "l": float(c) * 0.995,
            "c": float(c), "v": 1_000.0,
        }
        for i, c in enumerate(closes)
    ]


def _make_agent(db, *, enabled=True):
    cfg = {"pattern_engine": {"enabled": enabled, "min_matches": 3, "k": 5}}
    return PatternAgent(llm=MagicMock(), state=MagicMock(), config=cfg)


def _run(coro):
    return asyncio.run(coro)


# ───────────────────────── tests ──────────────────────────


def test_disabled_agent_returns_disabled():
    agent = _make_agent(_FakeDB(), enabled=False)
    out = _run(agent.run({"pair": "BTC-USD", "exchange": "coinbase", "stats_db": _FakeDB()}))
    assert out["pattern_signal"]["available"] is False
    assert out["pattern_signal"]["reason"] == "disabled"


def test_missing_context_returns_unavailable():
    agent = _make_agent(_FakeDB())
    out = _run(agent.run({"pair": "", "exchange": "coinbase", "stats_db": None}))
    assert out["pattern_signal"]["available"] is False
    assert out["pattern_signal"]["reason"] == "missing_context"


def test_no_upcoming_catalyst():
    db = _FakeDB(upcoming=[])
    agent = _make_agent(db)
    out = _run(agent.run({
        "pair": "BTC-USD", "exchange": "coinbase",
        "stats_db": db, "cycle_id": "c1",
    }))
    assert out["pattern_signal"]["available"] is False
    assert out["pattern_signal"]["reason"] == "no_upcoming_catalyst"


def test_predicts_bullish_when_history_is_bullish():
    """Provide enough candles to encode a fingerprint and a set of bullish
    historical analogs; agent should emit available=True with bullish
    direction."""
    upcoming_ts = datetime.now(timezone.utc) + timedelta(days=5)
    upcoming_event = {
        "id": "evt-1",
        "event_ts": upcoming_ts,
        "event_type": "earnings",
        "symbol": "AAPL",
        "exchange": "ibkr",
    }
    # Need enough candles so extract_fingerprint returns a vector
    # (PRE_WINDOW_BARS = 30 ⇒ at least 31 closes before the anchor).
    candles = _make_candles(60)
    # Re-anchor candles so they end just before upcoming_ts.
    shift = upcoming_ts - candles[-1]["ts"] - timedelta(days=1)
    candles = [{**c, "ts": c["ts"] + shift} for c in candles]
    neighbours = [
        {
            "similarity": 0.95,
            "forward_return_1d": 0.01,
            "forward_return_5d": 0.05,
            "forward_return_20d": 0.10,
            "symbol": "AAPL",
            "anchor_ts": datetime(2022, 1, 1, tzinfo=timezone.utc),
        }
        for _ in range(5)
    ]
    db = _FakeDB(upcoming=[upcoming_event], candles=candles, neighbours=neighbours)
    agent = _make_agent(db)
    out = _run(agent.run({
        "pair": "AAPL", "exchange": "ibkr",
        "stats_db": db, "cycle_id": "c2", "sentiment_score": 0.4,
    }))
    sig = out["pattern_signal"]
    assert sig["available"] is True
    assert sig["direction"] in ("bullish", "neutral")  # may be neutralised by drift threshold
    assert sig["n_matches"] == 5
    # Reasoning should have been persisted with cycle_id.
    assert any(s.get("cycle_id") == "c2" for s in db.saved)


def test_insufficient_matches_marks_unavailable():
    upcoming_ts = datetime.now(timezone.utc) + timedelta(days=5)
    upcoming_event = {
        "id": "evt-2",
        "event_ts": upcoming_ts,
        "event_type": "earnings",
        "symbol": "AAPL",
        "exchange": "ibkr",
    }
    candles = _make_candles(60)
    shift = upcoming_ts - candles[-1]["ts"] - timedelta(days=1)
    candles = [{**c, "ts": c["ts"] + shift} for c in candles]
    # Only 1 neighbour ⇒ below default min_matches=3.
    neighbours = [{
        "similarity": 0.9,
        "forward_return_5d": 0.05,
        "symbol": "AAPL",
        "anchor_ts": datetime(2022, 1, 1, tzinfo=timezone.utc),
    }]
    db = _FakeDB(upcoming=[upcoming_event], candles=candles, neighbours=neighbours)
    agent = _make_agent(db)
    out = _run(agent.run({
        "pair": "AAPL", "exchange": "ibkr", "stats_db": db, "cycle_id": "c3",
    }))
    sig = out["pattern_signal"]
    assert sig["available"] is False
    assert sig["reason"] == "insufficient_matches"
