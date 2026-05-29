from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI

from src.agents.trader import TraderAgent
from src.core.decision_engine import DecisionEngine
from src.core.llm_client import LLMClient
from src.core.llm_providers import LLMProvider, build_providers


def _provider(
    name: str,
    *,
    tier: str = "free",
    capability_tier: int | None = None,
    is_local: bool = False,
    reserve_for_priority: str = "",
) -> LLMProvider:
    kwargs = {}
    if capability_tier is not None:
        kwargs["capability_tier"] = capability_tier
    return LLMProvider(
        name=name,
        client=AsyncOpenAI(base_url="http://example.test/v1", api_key="test"),
        model=f"{name}-model",
        tier=tier,
        is_local=is_local,
        reserve_for_priority=reserve_for_priority,
        **kwargs,
    )


def test_build_providers_infers_capability_tiers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    providers = build_providers([
        {
            "name": "openrouter-free",
            "enabled": True,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "tier": "free",
        },
        {
            "name": "openrouter-paid",
            "enabled": True,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "model": "meta-llama/llama-3.3-70b-instruct",
            "tier": "paid",
        },
        {
            "name": "ollama",
            "enabled": True,
            "base_url": "http://localhost:11434",
            "model": "llama3.1:8b",
            "is_local": True,
        },
    ])

    assert {provider.name: provider.capability_tier for provider in providers} == {
        "openrouter-free": 2,
        "openrouter-paid": 3,
        "ollama": 1,
    }


def test_select_providers_by_route_tier():
    client = LLMClient(providers=[
        _provider("cheap-a", capability_tier=2),
        _provider("strong", tier="paid", capability_tier=3),
        _provider("local", capability_tier=1, is_local=True),
        _provider("cheap-b", capability_tier=2),
    ])

    assert [provider.name for provider in client._select_providers(route_tier=1)] == ["local"]
    assert [provider.name for provider in client._select_providers(route_tier=2)] == [
        "cheap-a", "cheap-b", "local",
    ]
    assert [provider.name for provider in client._select_providers(route_tier=3)] == [
        "strong", "cheap-a", "cheap-b", "local",
    ]


def test_route_tier_keeps_priority_reservations():
    client = LLMClient(providers=[
        _provider("reserved-cheap", capability_tier=2, reserve_for_priority="high"),
        _provider("local", capability_tier=1, is_local=True),
    ])

    assert [provider.name for provider in client._select_providers(route_tier=2)] == ["local"]
    assert [
        provider.name
        for provider in client._select_providers(route_tier=2, priority="high")
    ] == ["reserved-cheap", "local"]


class RecordingLLM:
    model = "fake-model"

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return dict(self.responses[index])


class FakeVerdict:
    def __init__(self, payload: dict):
        self.payload = payload

    def to_dict(self) -> dict:
        return dict(self.payload)


class VetoThenApproveEngine:
    def __init__(self):
        self.calls = 0

    def evaluate(self, proposal, *, portfolio_value: float = 0.0, cash_balance: float = 0.0):
        self.calls += 1
        if self.calls == 1:
            return FakeVerdict({
                "approved": False,
                "action": "hold",
                "proposal": {"action": "hold", "pair": proposal.pair},
                "quote_amount_max": 0.0,
                "reasons": ["max trade exceeded"],
                "veto": "absolute_rules",
            })
        return FakeVerdict({
            "approved": True,
            "action": proposal.action,
            "proposal": proposal.to_proposal_dict(),
            "quote_amount_max": float(proposal.quote_amount or 0.0),
            "reasons": ["approved after retry"],
            "veto": None,
        })


@pytest.fixture()
def trader_optimizer_defaults(monkeypatch):
    values = {
        "trader_tool_payload_max_chars": 2400,
        "trader_tier3_notional_threshold": 750.0,
        "trader_tier3_portfolio_pct": 0.05,
        "trader_tier3_ambiguous_confidence": 0.70,
        "trader_retry_on_veto": True,
        "trader_hard_veto_skip_enabled": True,
    }
    monkeypatch.setattr(
        "src.agents.trader.llm_optimizer.get",
        lambda key, default=None: values.get(key, default),
    )


def _proposal(quote_amount: float = 100.0) -> dict:
    return {
        "action": "buy",
        "pair": "BTC-USD",
        "confidence": 0.82,
        "quote_amount": quote_amount,
        "quantity": None,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "strategy": "llm_strategist",
        "reasoning": "test proposal",
    }


def _context(**overrides) -> dict:
    context = {
        "pair": "BTC-USD",
        "exchange": "coinbase",
        "regime": "bull",
        "current_price": 100.0,
        "portfolio_value": 10_000.0,
        "cash_balance": 5_000.0,
        "open_positions": {},
        "market_signal": {"signal_type": "buy", "confidence": 0.82},
        "strategy_signals": {
            "_ensemble": {
                "action": "buy",
                "confidence": 0.75,
                "agreement": 0.80,
                "n_strategies": 3,
            }
        },
        "pattern_signal": {"available": True, "direction": "bullish"},
        "fee_context": {},
        "strategy_policy": {"posture": "trade", "evidence_score": 0.85},
    }
    context.update(overrides)
    return context


@pytest.mark.asyncio
async def test_trader_uses_tier3_when_high_stakes_active(trader_optimizer_defaults):
    llm = RecordingLLM([_proposal(100.0)])
    agent = TraderAgent(llm, SimpleNamespace(), {"trading": {"min_confidence": 0.65}}, DecisionEngine())

    result = await agent.run(_context(high_stakes_active=True))

    assert [call["route_tier"] for call in llm.calls] == [3]
    assert result["llm_route_tier"] == 3
    assert "high_stakes_mode" in result["llm_route_reasons"]


@pytest.mark.asyncio
async def test_trader_escalates_large_notional_to_tier3(trader_optimizer_defaults):
    llm = RecordingLLM([_proposal(800.0), _proposal(250.0)])
    agent = TraderAgent(llm, SimpleNamespace(), {"trading": {"min_confidence": 0.65}}, DecisionEngine())

    result = await agent.run(_context())

    assert [call["route_tier"] for call in llm.calls] == [2, 3]
    assert result["quote_amount"] == 250.0
    assert result["llm_route_tier"] == 3
    assert result["llm_route_attempts"][1]["phase"] == "large_notional_escalation"


@pytest.mark.asyncio
async def test_trader_retry_after_veto_uses_tier3(trader_optimizer_defaults):
    llm = RecordingLLM([_proposal(100.0), _proposal(50.0)])
    agent = TraderAgent(
        llm,
        SimpleNamespace(),
        {"trading": {"min_confidence": 0.65}},
        VetoThenApproveEngine(),
    )

    result = await agent.run(_context())

    assert [call["route_tier"] for call in llm.calls] == [2, 3]
    assert result["quote_amount"] == 50.0
    assert result["llm_route_tier"] == 3
    assert result["llm_route_attempts"][-1]["phase"] == "retry_after_veto"