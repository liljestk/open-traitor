"""
Tests for RiskManagerAgent — position sizing, Kelly, correlation, rule checks.

Uses importlib to avoid circular imports (same pattern as test_executor.py).
"""

import asyncio
import importlib
import math
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from src.core.rules import AbsoluteRules
from src.core.portfolio_scaler import PortfolioScaler
from src.models.trade import TradeAction


def _import_risk_manager():
    mod = importlib.import_module("src.agents.risk_manager")
    return mod.RiskManagerAgent


def _make_rm(
    config=None,
    portfolio_value=10000,
    min_confidence=0.7,
    stop_loss_pct=0.03,
    take_profit_pct=0.06,
    max_position_pct=0.05,
    max_open=3,
    kelly=True,
    kelly_frac=0.5,
    style_modifiers=None,
    open_positions=None,
):
    if config is None:
        config = {
            "trading": {
                "min_confidence": min_confidence,
                "min_signal_confidence": 0.65,
                "max_open_positions": max_open,
                "style_modifiers": style_modifiers or [],
            },
            "risk": {
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "max_position_pct": max_position_pct,
                "use_kelly_criterion": kelly,
                "kelly_fraction": kelly_frac,
                "use_correlation_penalty": True,
                "correlation_threshold": 0.7,
                "strong_signal_min_position_pct": 0.015,
            },
        }

    llm = MagicMock()
    state = MagicMock()
    state.current_prices = {"BTC-EUR": 50000}
    state.open_positions = open_positions or {}
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


# ═══════════════════════════════════════════════════════════════════════════
# Kelly Criterion
# ═══════════════════════════════════════════════════════════════════════════

class TestKellyCriterion:
    def test_insufficient_data_returns_config_max(self):
        rm = _make_rm(max_position_pct=0.05)
        assert rm._compute_kelly_size(10000, 0, 0, 0) == 0.05

    def test_negative_edge_returns_minimum(self):
        # win_rate=0.3, avg_win=1.0, avg_loss=2.0 → f* < 0
        rm = _make_rm()
        result = rm._compute_kelly_size(10000, 0.3, 1.0, 2.0)
        assert result == 0.01

    def test_positive_edge_gives_fraction(self):
        # win_rate=0.6, avg_win=2.0, avg_loss=1.0 → f* = (0.6*2 - 0.4)/2 = 0.4
        rm = _make_rm(max_position_pct=0.5)
        result = rm._compute_kelly_size(10000, 0.6, 2.0, 1.0)
        assert 0.0 < result <= 0.5

    def test_capped_by_max_position(self):
        rm = _make_rm(max_position_pct=0.03)
        result = rm._compute_kelly_size(10000, 0.8, 3.0, 1.0)
        assert result <= 0.03


# ═══════════════════════════════════════════════════════════════════════════
# Correlation penalty
# ═══════════════════════════════════════════════════════════════════════════

class TestCorrelationPenalty:
    def test_no_matrix_returns_one(self):
        rm = _make_rm()
        assert rm._compute_correlation_penalty("SOL-EUR") == 1.0

    def test_no_open_positions_returns_one(self):
        rm = _make_rm()
        rm.state.get_open_trades.return_value = []
        matrix = {"SOL": {"BTC-EUR": 0.9}}
        assert rm._compute_correlation_penalty("SOL-EUR", matrix) == 1.0

    def test_high_correlation_penalizes(self):
        rm = _make_rm()
        trade = MagicMock()
        trade.pair = "BTC-EUR"
        trade.action = TradeAction.BUY
        rm.state.get_open_trades.return_value = [trade]
        matrix = {"ETH": {"BTC-EUR": 0.95}}
        penalty = rm._compute_correlation_penalty("ETH-EUR", matrix)
        assert penalty < 1.0
        assert penalty >= 0.5

    def test_low_correlation_no_penalty(self):
        rm = _make_rm()
        trade = MagicMock()
        trade.pair = "BTC-EUR"
        trade.action = TradeAction.BUY
        rm.state.get_open_trades.return_value = [trade]
        matrix = {"SOL": {"BTC-EUR": 0.3}}
        assert rm._compute_correlation_penalty("SOL-EUR", matrix) == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Risk manager run() — trade validation
# ═══════════════════════════════════════════════════════════════════════════

class TestRiskManagerRun:
    def test_hold_auto_approved(self):
        rm = _make_rm()
        result = _run(rm.run({"proposal": {"action": "hold"}}))
        assert result["approved"] is True
        assert result["action"] == "hold"

    def test_low_confidence_buy_rejected(self):
        rm = _make_rm()
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.3,
                "quote_amount": 100,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
        }))
        assert result["approved"] is False
        assert "confidence" in result["reason"].lower()

    def test_buy_with_sufficient_confidence(self):
        rm = _make_rm()
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.85,
                "quote_amount": 100,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
            "signal_type": "buy",
        }))
        assert result["approved"] is True
        assert result["stop_loss"] is not None
        assert result["take_profit"] is not None

    def test_no_amount_rejected(self):
        rm = _make_rm()
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.9,
                "quote_amount": 0,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
        }))
        assert result["approved"] is False
        assert "amount" in result["reason"].lower()

    def test_max_positions_enforced(self):
        rm = _make_rm(max_open=2, portfolio_value=30)
        rm.state.open_positions = {"BTC-EUR": 0.1, "ETH-EUR": 0.5}
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "SOL-EUR",
                "confidence": 0.9,
                "quote_amount": 5,
                "current_price": 50,
            },
            "portfolio_value": 30,
            "cash_balance": 20,
            "signal_type": "buy",
        }))
        assert result["approved"] is False
        assert "position" in result["reason"].lower()

    def test_sell_always_passes_position_check(self):
        rm = _make_rm(max_open=1)
        rm.state.open_positions = {"BTC-EUR": 0.1}
        result = _run(rm.run({
            "proposal": {
                "action": "sell",
                "pair": "BTC-EUR",
                "confidence": 0.8,
                "quantity": 0.1,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
        }))
        assert result["approved"] is True

    def test_atr_stop_loss(self):
        rm = _make_rm()
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.85,
                "quote_amount": 100,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
            "atr": 1500.0,
            "signal_type": "buy",
        }))
        assert result["approved"] is True
        # ATR-based SL: price - 2*ATR = 50000 - 3000 = 47000
        assert result["stop_loss"] == pytest.approx(47000.0, rel=0.01)
        # ATR-based TP: price + 3*ATR = 50000 + 4500 = 54500
        assert result["take_profit"] == pytest.approx(54500.0, rel=0.01)

    def test_position_size_capped(self):
        rm = _make_rm(max_position_pct=0.05)
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.9,
                "quote_amount": 400,  # Will exceed max_position_pct * portfolio
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 8000,
            "signal_type": "strong_buy",
        }))
        assert result["approved"] is True
        # Should be capped by Kelly/max_position_pct
        assert result["quote_amount"] <= 500


# ═══════════════════════════════════════════════════════════════════════════
# Style modifiers
# ═══════════════════════════════════════════════════════════════════════════

class TestStyleModifiers:
    def test_high_conviction_only_rejects_buy(self):
        rm = _make_rm(style_modifiers=["high_conviction_only"])
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.9,
                "quote_amount": 100,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
            "signal_type": "buy",  # not strong_buy
        }))
        assert result["approved"] is False
        assert "high_conviction_only" in result["reason"]

    def test_high_conviction_allows_strong_buy(self):
        rm = _make_rm(style_modifiers=["high_conviction_only"])
        result = _run(rm.run({
            "proposal": {
                "action": "buy",
                "pair": "BTC-EUR",
                "confidence": 0.9,
                "quote_amount": 100,
                "current_price": 50000,
            },
            "portfolio_value": 10000,
            "cash_balance": 5000,
            "signal_type": "strong_buy",
        }))
        assert result["approved"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Signal strength multiplier
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalStrength:
    def test_strong_buy_full_allocation(self):
        RiskManagerAgent = _import_risk_manager()
        assert RiskManagerAgent._SIGNAL_STRENGTH_MULT["strong_buy"] == 1.0

    def test_buy_eighty_percent(self):
        RiskManagerAgent = _import_risk_manager()
        assert RiskManagerAgent._SIGNAL_STRENGTH_MULT["buy"] == 0.8

    def test_weak_buy_sixty_percent(self):
        RiskManagerAgent = _import_risk_manager()
        assert RiskManagerAgent._SIGNAL_STRENGTH_MULT["weak_buy"] == 0.6
