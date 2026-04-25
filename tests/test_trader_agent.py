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
    return {
        "pair": "BTC-USD",
        "exchange": "coinbase",
        "regime": "trending",
        "current_price": 50_000.0,
        "portfolio_value": 10_000.0,
        "cash_balance": 10_000.0,
        "open_positions": {},
        "market_signal": {"action": action, "confidence": confidence,
                          "market_regime": "trending"},
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
