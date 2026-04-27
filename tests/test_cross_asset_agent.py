"""Unit tests for the cross-asset agent's redis-publish + LLM-narrate hooks.

We do not boot a real LLM, redis, or DB. Each test stubs out the
specific dependency and asserts the agent's contract:

* When ``narrate_with_llm`` is False (default), no LLM call is made and
  the payload has no ``narrative`` key.
* When ``narrate_with_llm`` is True and the LLM is available, the
  narrative text is attached to the payload.
* If LLM raises, the agent silently continues with ``narrative=None``.
* ``_publish`` writes a typed envelope to the configured Redis channel.
* ``_publish`` swallows redis failures.
* The redis envelope includes the cross-asset summary fields the
  dashboard subscriber needs (type, exchange, target, direction).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state():
    """Minimal stub satisfying BaseAgent's state attribute."""
    s = MagicMock()
    s.update_agent_state = MagicMock()
    return s


def _make_agent(*, redis_client=None, narrate=False, llm=None):
    from src.agents.cross_asset_agent import CrossAssetAgent
    cfg = {
        "cross_asset_engine": {
            "enabled": True,
            "narrate_with_llm": narrate,
        }
    }
    return CrossAssetAgent(
        llm or MagicMock(is_available=lambda: True),
        _make_state(),
        cfg,
        redis_client=redis_client,
    )


# ---------------------------------------------------------------------------
# _publish
# ---------------------------------------------------------------------------

def test_publish_writes_envelope_to_redis():
    redis = MagicMock()
    agent = _make_agent(redis_client=redis)
    payload = {
        "exchange": "coinbase",
        "target": "ETH-USD",
        "direction": "long",
        "expected_drift": 0.012,
        "reactive": [{"a": 1}, {"b": 2}],
        "proactive": {"upcoming_count": 3},
        "computed_at": "2025-01-01T00:00:00+00:00",
    }
    agent._publish(payload)
    redis.publish.assert_called_once()
    channel, body = redis.publish.call_args[0]
    assert channel == "cross_asset:signals"
    env = json.loads(body)
    assert env["type"] == "cross_asset_signal"
    assert env["exchange"] == "coinbase"
    assert env["target"] == "ETH-USD"
    assert env["direction"] == "long"
    assert env["reactive_n"] == 2
    assert env["proactive_n"] == 3


def test_publish_swallows_redis_failure():
    redis = MagicMock()
    redis.publish.side_effect = RuntimeError("redis down")
    agent = _make_agent(redis_client=redis)
    # Must not raise.
    agent._publish({"exchange": "coinbase", "target": "X-USD"})


def test_publish_noop_without_redis():
    agent = _make_agent(redis_client=None)
    # Must not raise.
    agent._publish({"exchange": "coinbase", "target": "X-USD"})


# ---------------------------------------------------------------------------
# _narrate
# ---------------------------------------------------------------------------

def test_narrate_returns_none_when_llm_unavailable():
    llm = MagicMock(is_available=lambda: False)
    agent = _make_agent(llm=llm, narrate=True)
    out = asyncio.run(agent._narrate({
        "target": "ETH-USD", "direction": "long", "expected_drift": 0.01,
        "reactive": [], "proactive": {"upcoming_count": 0, "events": []},
        "cluster_mates": [],
    }))
    assert out is None


def test_narrate_returns_text_on_success():
    llm = MagicMock(is_available=lambda: True)
    llm.chat = AsyncMock(return_value="ETH likely to react +1% to BTC halving in 2 days.")
    agent = _make_agent(llm=llm, narrate=True)
    out = asyncio.run(agent._narrate({
        "target": "ETH-USD", "direction": "long", "expected_drift": 0.01,
        "reactive": [{"driver_symbol": "BTC-USD", "driver_event_type": "halving",
                      "days_to_event": 2, "expected_drift": 0.01,
                      "r_squared": 0.4, "sample_count": 8}],
        "proactive": {"upcoming_count": 0, "events": []},
        "cluster_mates": ["BTC-USD"],
    }))
    assert out is not None
    assert "ETH" in out or "halving" in out.lower()
    llm.chat.assert_awaited_once()
    kwargs = llm.chat.await_args.kwargs
    assert kwargs.get("priority") == "low"
    assert kwargs.get("agent_name") == "cross_asset_agent"


def test_narrate_returns_none_on_llm_exception():
    llm = MagicMock(is_available=lambda: True)
    llm.chat = AsyncMock(side_effect=RuntimeError("provider down"))
    agent = _make_agent(llm=llm, narrate=True)
    out = asyncio.run(agent._narrate({
        "target": "X-USD", "direction": "neutral", "expected_drift": 0.0,
        "reactive": [], "proactive": {"upcoming_count": 0, "events": []},
        "cluster_mates": [],
    }))
    assert out is None


def test_narrate_returns_none_on_empty_string():
    llm = MagicMock(is_available=lambda: True)
    llm.chat = AsyncMock(return_value="   ")
    agent = _make_agent(llm=llm, narrate=True)
    out = asyncio.run(agent._narrate({
        "target": "X-USD", "direction": "neutral", "expected_drift": 0.0,
        "reactive": [], "proactive": {"upcoming_count": 0, "events": []},
        "cluster_mates": [],
    }))
    assert out is None


# ---------------------------------------------------------------------------
# Integration: run() wires both hooks
# ---------------------------------------------------------------------------

class _StubDB:
    """Minimal StatsDB stub that lets one reactive signal flow through."""
    def get_cross_event_regressions(self, *, exchange, target_symbol, **_):
        return [{
            "driver_symbol": "BTC-USD",
            "driver_event_type": "halving",
            "horizon_days": 5,
            "beta": 0.8,
            "r_squared": 0.4,
            "sample_count": 12,
        }]

    def get_catalyst_events(self, *, exchange, symbol, start, end, limit):
        if symbol == "BTC-USD":
            return [{
                "event_type": "halving",
                "event_ts": datetime.now(timezone.utc),
            }]
        return []

    def get_cluster_for_symbol(self, *, exchange, symbol):
        return ["ETH-USD", "BTC-USD"]


def test_run_publishes_to_redis_and_logs_signal():
    redis = MagicMock()
    agent = _make_agent(redis_client=redis, narrate=False)
    out = asyncio.run(agent.run({
        "pair": "ETH-USD",
        "exchange": "coinbase",
        "stats_db": _StubDB(),
    }))
    sig = out["cross_asset_signal"]
    assert sig["available"] is True
    assert sig["target"] == "ETH-USD"
    assert "narrative" not in sig  # narrate disabled
    redis.publish.assert_called_once()


def test_run_includes_narrative_when_enabled():
    redis = MagicMock()
    llm = MagicMock(is_available=lambda: True)
    llm.chat = AsyncMock(return_value="Cluster-mate halving will likely lift ETH ~3% over 5 days.")
    agent = _make_agent(redis_client=redis, narrate=True, llm=llm)
    out = asyncio.run(agent.run({
        "pair": "ETH-USD",
        "exchange": "coinbase",
        "stats_db": _StubDB(),
    }))
    sig = out["cross_asset_signal"]
    assert sig["available"] is True
    assert sig["narrative"] and "ETH" in sig["narrative"]
    # Redis publish payload also carries the narrative.
    body = json.loads(redis.publish.call_args[0][1])
    assert body["narrative"]
