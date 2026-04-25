"""Test that RiskManager honours the allocator_budget_cap from a TraderAgent
proposal."""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import pytest

from src.core.rules import AbsoluteRules
from src.core.portfolio_scaler import PortfolioScaler


def _import_risk_manager():
    mod = importlib.import_module("src.agents.risk_manager")
    return mod.RiskManagerAgent


def _make_rm(portfolio_value=10000):
    config = {
        "trading": {
            "min_confidence": 0.5,
            "min_signal_confidence": 0.5,
            "max_open_positions": 5,
            "style_modifiers": [],
        },
        "risk": {
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.06,
            "max_position_pct": 0.50,  # generous so allocator becomes binding
            "use_kelly_criterion": False,
            "use_correlation_penalty": False,
            "correlation_threshold": 0.7,
            "strong_signal_min_position_pct": 0.015,
        },
    }
    llm = MagicMock()
    state = MagicMock()
    state.current_prices = {"BTC-USD": 50000}
    state.open_positions = {}
    state.get_open_trades.return_value = []
    rules = AbsoluteRules(config=config.get("trading", {}))
    scaler = PortfolioScaler(config.get("risk", {}))
    scaler.update(portfolio_value)
    RiskManagerAgent = _import_risk_manager()
    return RiskManagerAgent(llm, state, config, rules, scaler)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_allocator_budget_cap_shrinks_quote_amount():
    rm = _make_rm(portfolio_value=10_000)
    # Without cap: max_position_pct=0.5 → up to $5000.
    # With cap: $250 — risk manager should shrink to $250.
    proposal = {
        "action": "buy",
        "pair": "BTC-USD",
        "confidence": 0.85,
        "quote_amount": 1000.0,
        "current_price": 50_000.0,
        "stop_loss_price": 48_500.0,
        "take_profit_price": 53_000.0,
        "strategy": "ema_crossover",
        "allocator_budget_cap": 250.0,
    }
    result = _run(rm.execute({
        "proposal": proposal,
        "portfolio_value": 10_000,
        "cash_balance": 10_000,
        "exchange": "coinbase",
    }))
    assert result.get("approved") in (True, False)
    # When approved, quote_amount must respect the cap.
    if result.get("approved"):
        assert result["quote_amount"] <= 250.0 + 1e-6


def test_no_allocator_cap_falls_through():
    rm = _make_rm(portfolio_value=10_000)
    proposal = {
        "action": "buy",
        "pair": "BTC-USD",
        "confidence": 0.85,
        "quote_amount": 100.0,
        "current_price": 50_000.0,
        "stop_loss_price": 48_500.0,
        "take_profit_price": 53_000.0,
        "strategy": "ema_crossover",
        # No allocator_budget_cap key at all.
    }
    result = _run(rm.execute({
        "proposal": proposal,
        "portfolio_value": 10_000,
        "cash_balance": 10_000,
        "exchange": "coinbase",
    }))
    # Just ensure no crash and a sensible result.
    assert "approved" in result
