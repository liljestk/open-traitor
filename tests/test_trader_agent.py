"""Tests for src/agents/trader.py — TraderAgent uses TradingToolkit and
respects DecisionEngine vetoes."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.agents.trader import TraderAgent
from src.core.capital_allocator import CapitalAllocator
from src.core.decision_engine import DecisionEngine
from src.analysis.signal_edge_library import (
    InMemorySignalEdgeStore,
    SignalEdgeLibrary,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_engine_kit():
    edges = SignalEdgeLibrary(store=InMemorySignalEdgeStore(), exchange="test")
    allocator = CapitalAllocator()
    allocator.register(["ema_crossover", "bollinger_reversion", "pattern_engine"])
    eng = DecisionEngine(edges=edges, allocator=allocator, exchange="test",
                         min_edge_samples=30, min_edge_sharpe=0.10,
                         min_confidence=0.5)
    return eng, edges, allocator


def _ctx(action="buy", confidence=0.8):
    signal_type = "neutral" if action == "hold" else action
    return {
        "pair": "BTC-USD",
        "exchange": "coinbase",
        "regime": "trending",
        "current_price": 50_000.0,
        "portfolio_value": 10_000.0,
        "cash_balance": 10_000.0,
        "open_positions": {},
        "market_signal": {"action": action, "signal_type": signal_type,
                  "confidence": confidence, "market_regime": "trending"},
        "strategy_signals": {
            "ema_crossover": {"action": action, "confidence": confidence,
                              "market_regime": "trending"},
            "bollinger_reversion": {"action": action, "confidence": confidence,
                                    "market_regime": "trending"},
        },
        "pattern_signal": {"available": False, "direction": "neutral",
                           "confidence": 0.0, "patterns": [], "summary": "n/a"},
    }


def test_trader_falls_back_when_no_llm():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _err(*a, **kw):
        return {"error": "no_provider"}
    llm.chat_json = _err

    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}

    agent = TraderAgent(llm, state, config, decision_engine=eng)
    result = _run(agent.execute(_ctx(action="buy", confidence=0.8)))
    # Deterministic fallback should still produce a structured result.
    assert "action" in result
    assert "confidence" in result


def test_trader_holds_when_ensemble_neutral():
    """Pre-screen short-circuits LLM call when there's nothing actionable."""
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="hold", confidence=0.3)
    ctx["strategy_signals"] = {}
    result = _run(agent.execute(ctx))
    assert result.get("action") == "hold"


def test_trader_proposal_routed_through_engine_records_verdict():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _ok(*a, **kw):
        return {
            "action": "buy",
            "confidence": 0.85,
            "strategy": "ema_crossover",
            "reasoning": "test",
            "stop_loss_price": 48000.0,
            "take_profit_price": 53000.0,
            "quote_amount": 500.0,
        }
    llm.chat_json = _ok
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    result = _run(agent.execute(_ctx(action="buy", confidence=0.85)))
    assert "decision_engine_verdict" in result


def test_trader_honors_strategy_policy_veto():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _ok(*a, **kw):
        return {
            "action": "buy",
            "confidence": 0.95,
            "strategy": "ema_crossover",
            "reasoning": "test",
            "stop_loss_price": 48000.0,
            "take_profit_price": 56000.0,
            "quote_amount": 500.0,
        }

    llm.chat_json = _ok
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="buy", confidence=0.95)
    ctx["strategy_policy"] = {
        "posture": "block",
        "evidence_score": -0.4,
        "thesis": "negative expectancy",
    }

    result = _run(agent.execute(ctx))

    assert result["action"] == "hold"
    assert result["decision_engine_verdict"]["veto"] == "strategy_policy"


def test_trader_autosizes_sell_from_open_position_when_llm_omits_amount():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _ok(*a, **kw):
        return {
            "action": "sell",
            "pair": "BTC-USD",
            "confidence": 0.75,
            "strategy": "llm_strategist",
            "reasoning": "test sell",
            "quote_amount": None,
            "quantity": None,
        }

    llm.chat_json = _ok
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="sell", confidence=0.75)
    ctx["open_positions"] = {"BTC-USD": 0.01}

    result = _run(agent.execute(ctx))

    assert result["action"] == "sell"
    assert result["quantity"] == pytest.approx(0.01)
    assert result["quote_amount"] == pytest.approx(500.0)


def test_trader_holds_sell_when_no_position_available():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _ok(*a, **kw):
        return {
            "action": "sell",
            "pair": "BTC-USD",
            "confidence": 0.75,
            "strategy": "llm_strategist",
            "reasoning": "test sell",
            "quote_amount": None,
            "quantity": None,
        }

    llm.chat_json = _ok
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="buy", confidence=0.75)

    result = _run(agent.execute(ctx))

    assert result["action"] == "hold"
    assert "no held position" in result["reason"]


def test_trader_autosizes_sell_from_alias_position_key():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _ok(*a, **kw):
        return {
            "action": "sell",
            "pair": "SAN.MC-EUR",
            "confidence": 0.75,
            "strategy": "llm_strategist",
            "reasoning": "test sell",
            "quote_amount": None,
            "quantity": None,
        }

    llm.chat_json = _ok
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="buy", confidence=0.75)
    ctx["pair"] = "SAN.MC-EUR"
    ctx["current_price"] = 10.0
    ctx["open_positions"] = {"SAN.MC": 3.0}

    result = _run(agent.execute(ctx))

    assert result["action"] == "sell"
    assert result["quantity"] == pytest.approx(3.0)
    assert result["quote_amount"] == pytest.approx(30.0)


def test_trader_persists_early_hold_reasoning():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()
    llm.chat_json = MagicMock()
    state = MagicMock()
    state.save_state = MagicMock()
    stats_db = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="buy", confidence=0.6)
    ctx["strategy_signals"] = {}
    ctx.update({"cycle_id": "cycle-early-hold", "stats_db": stats_db})

    result = _run(agent.execute(ctx))

    assert result["action"] == "hold"
    llm.chat_json.assert_not_called()
    kwargs = stats_db.save_reasoning.call_args.kwargs
    assert kwargs["agent_name"] == "trader"
    assert kwargs["reasoning_json"]["action"] == "hold"
    assert "no actionable signal" in kwargs["reasoning_json"]["reason"]


def test_trader_persists_llm_token_metrics():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()

    async def _ok(*a, **kw):
        span = kw.get("span")
        span.prompt_tokens = 123
        span.completion_tokens = 45
        span.latency_ms = 67.8
        return {
            "action": "buy",
            "confidence": 0.85,
            "strategy": "ema_crossover",
            "reasoning": "test",
            "stop_loss_price": 48000.0,
            "take_profit_price": 53000.0,
            "quote_amount": 500.0,
        }

    class _Span:
        trace_id = "trace-1"
        span_id = "span-1"
        prompt_tokens = 0
        completion_tokens = 0
        latency_ms = 0.0

    class _Trace:
        def start_span(self, *args, **kwargs):
            return _Span()

    llm.chat_json = _ok
    state = MagicMock()
    state.save_state = MagicMock()
    stats_db = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="buy", confidence=0.85)
    ctx.update({"cycle_id": "cycle-1", "stats_db": stats_db, "trace_ctx": _Trace()})

    _run(agent.execute(ctx))

    kwargs = stats_db.save_reasoning.call_args.kwargs
    assert kwargs["prompt_tokens"] == 123
    assert kwargs["completion_tokens"] == 45
    assert kwargs["latency_ms"] == 67.8
    assert kwargs["langfuse_trace_id"] == "trace-1"
    assert kwargs["langfuse_span_id"] == "span-1"
    assert kwargs["reasoning_json"]["llm_attempts"] == 1


def test_trader_skips_llm_when_cashless_without_position():
    eng, _, _ = _make_engine_kit()
    llm = MagicMock()
    llm.chat_json = MagicMock()
    state = MagicMock()
    state.save_state = MagicMock()
    config = {"trading": {"min_confidence": 0.5}}
    agent = TraderAgent(llm, state, config, decision_engine=eng)
    ctx = _ctx(action="buy", confidence=0.85)
    ctx["cash_balance"] = 0.0

    result = _run(agent.execute(ctx))

    assert result["action"] == "hold"
    assert "no available cash" in result["reason"]
    llm.chat_json.assert_not_called()
