"""Tests for ExecutorAgent — order decision logic and trade execution."""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.rules import AbsoluteRules
from src.models.trade import TradeAction, TradeStatus

# Avoid circular import by importing the module directly
_executor_mod = importlib.import_module("src.agents.executor")
ExecutorAgent = _executor_mod.ExecutorAgent


def _make_executor(style_modifiers=None, use_limit=True, urgency_threshold=0.8):
    llm = MagicMock()
    state = MagicMock()
    state.add_trade = MagicMock(return_value=True)
    state.get_open_trades = MagicMock(return_value=[])

    rules = AbsoluteRules({
        "max_single_trade": 500,
        "max_daily_spend": 2000,
        "max_daily_loss": 300,
        "max_portfolio_risk_pct": 0.20,
        "require_approval_above": 200,
        "min_trade_interval_seconds": 0,
        "max_trades_per_day": 20,
        "max_cash_per_trade_pct": 0.25,
        "emergency_stop_portfolio": 0,
        "always_use_stop_loss": False,
    })

    exchange = MagicMock()
    exchange.paper_mode = True
    exchange.asset_class = "crypto"
    exchange.place_market_order = MagicMock(return_value={
        "success": True,
        "order": {
            "order_id": "test-order-1",
            "status": "FILLED",
            "average_filled_price": "100.0",
            "filled_size": "1.0",
            "fee": "0.6",
        },
    })
    exchange.place_limit_order = MagicMock(return_value={
        "success": True,
        "order": {
            "order_id": "test-limit-1",
            "status": "FILLED",
            "average_filled_price": "99.9",
            "filled_size": "1.0",
            "fee": "0.3",
        },
    })

    config = {
        "execution": {
            "use_limit_orders": use_limit,
            "limit_price_offset_pct": 0.001,
            "urgency_confidence_threshold": urgency_threshold,
        },
        "trading": {
            "style_modifiers": style_modifiers or [],
        },
    }

    executor = ExecutorAgent(llm, state, config, exchange, rules)
    return executor, exchange, state


# ═══════════════════════════════════════════════════════════════════════
# _should_use_limit decision logic
# ═══════════════════════════════════════════════════════════════════════

class TestShouldUseLimit:
    def test_normal_buy_uses_limit(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "buy", "confidence": 0.5}) is True

    def test_sell_uses_market(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "sell", "confidence": 0.5}) is False

    def test_high_confidence_uses_market(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "buy", "confidence": 0.95}) is False

    def test_stop_loss_reason_uses_market(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "sell", "reasoning": "stop_loss hit"}) is False

    def test_trailing_stop_uses_market(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "sell", "reasoning": "trailing stop triggered"}) is False

    def test_take_profit_uses_market(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "sell", "reasoning": "take_profit reached"}) is False

    def test_limit_disabled_globally(self):
        ex, _, _ = _make_executor(use_limit=False)
        assert ex._should_use_limit({"action": "buy", "confidence": 0.3}) is False

    def test_explicit_market_order_type(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "buy", "order_type": "market"}) is False

    def test_explicit_limit_order_type(self):
        ex, _, _ = _make_executor()
        assert ex._should_use_limit({"action": "buy", "order_type": "limit"}) is True

    def test_prefer_maker_forces_limit_for_buy(self):
        ex, _, _ = _make_executor(style_modifiers=["prefer_maker"])
        assert ex._should_use_limit({"action": "buy", "confidence": 0.5}) is True

    def test_prefer_maker_still_market_for_stop_loss(self):
        ex, _, _ = _make_executor(style_modifiers=["prefer_maker"])
        assert ex._should_use_limit({"action": "buy", "reasoning": "stop_loss exit"}) is False


# ═══════════════════════════════════════════════════════════════════════
# Trade Execution
# ═══════════════════════════════════════════════════════════════════════

class TestExecution:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_not_approved_returns_early(self):
        ex, _, _ = _make_executor()
        result = self._run(ex.run({"approved_trade": {"approved": False}}))
        assert result["executed"] is False

    def test_needs_approval_returns_pending(self):
        ex, _, _ = _make_executor()
        result = self._run(ex.run({"approved_trade": {"approved": True, "needs_approval": True}}))
        assert result["executed"] is False
        assert result.get("pending_approval") is True

    def test_hold_action(self):
        ex, _, _ = _make_executor()
        result = self._run(ex.run({"approved_trade": {"approved": True, "action": "hold"}}))
        assert result["executed"] is False

    def test_invalid_price_rejected(self):
        ex, _, _ = _make_executor()
        result = self._run(ex.run({"approved_trade": {
            "approved": True, "action": "buy", "pair": "BTC-EUR",
            "quote_amount": 100, "price": 0, "confidence": 0.7,
        }}))
        assert result["executed"] is False

    def test_market_buy_executed(self):
        ex, exchange, state = _make_executor()
        result = self._run(ex.run({"approved_trade": {
            "approved": True, "action": "buy", "pair": "BTC-EUR",
            "quote_amount": 100, "quantity": 0.002, "price": 50000,
            "confidence": 0.9, "reasoning": "strong signal",
        }}))
        assert result["executed"] is True
        exchange.place_market_order.assert_called_once()

    def test_market_sell_executed(self):
        ex, exchange, state = _make_executor()
        result = self._run(ex.run({"approved_trade": {
            "approved": True, "action": "sell", "pair": "BTC-EUR",
            "quote_amount": 100, "quantity": 0.002, "price": 50000,
            "confidence": 0.7, "reasoning": "exit",
        }}))
        assert result["executed"] is True
        args = exchange.place_market_order.call_args
        assert args[1]["side"] == "SELL"

    def test_limit_buy_used_for_low_confidence(self):
        ex, exchange, state = _make_executor()
        result = self._run(ex.run({"approved_trade": {
            "approved": True, "action": "buy", "pair": "ETH-EUR",
            "quote_amount": 50, "quantity": 0.01, "price": 3000,
            "confidence": 0.6, "reasoning": "moderate signal",
        }}))
        assert result["executed"] is True
        exchange.place_limit_order.assert_called_once()

    def test_failed_order_not_recorded(self):
        ex, exchange, state = _make_executor()
        exchange.place_market_order.return_value = {"success": False, "error": "API down"}
        result = self._run(ex.run({"approved_trade": {
            "approved": True, "action": "buy", "pair": "BTC-EUR",
            "quote_amount": 100, "quantity": 0.002, "price": 50000,
            "confidence": 0.9, "reasoning": "strong signal",
        }}))
        assert result["executed"] is False


# ═══════════════════════════════════════════════════════════════════════
# Duplicate Close Prevention (HIGH-2)
# ═══════════════════════════════════════════════════════════════════════

class TestDuplicateClosePrevention:
    def test_closing_trades_lock(self):
        ex, _, _ = _make_executor()
        # Verify the lock and set exist
        assert hasattr(ex, "_closing_trades")
        assert hasattr(ex, "_closing_trades_lock")
        assert isinstance(ex._closing_trades, set)

    def test_close_failure_tracking(self):
        ex, _, _ = _make_executor()
        assert ex._close_failure_limit == 3
        ex._close_failures["test-id"] = 2
        assert ex._close_failures["test-id"] == 2
