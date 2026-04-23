"""P1 maker-only executor — config plumbing, buy/sell gating, drift replace, stats."""
from __future__ import annotations

import importlib
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.core.rules import AbsoluteRules
from src.models.trade import Trade, TradeAction, TradeStatus

_executor_mod = importlib.import_module("src.agents.executor")
ExecutorAgent = _executor_mod.ExecutorAgent


def _make(cfg_execution: dict | None = None):
    llm = MagicMock()
    state = MagicMock()
    state.get_open_trades = MagicMock(return_value=[])
    exchange = MagicMock()
    exchange.paper_mode = True
    exchange.asset_class = "crypto"
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
    cfg = {
        "execution": cfg_execution or {},
        "trading": {"style_modifiers": []},
    }
    return ExecutorAgent(llm, state, cfg, exchange, rules), exchange, state


class TestMakerOnlyConfig:
    def test_default_off(self):
        ex, _, _ = _make()
        assert ex.maker_only is False
        assert ex.replace_on_drift_pct == 0.0

    def test_maker_only_flag(self):
        ex, _, _ = _make({"maker_only": True})
        assert ex.maker_only is True

    def test_drift_pct_parsed(self):
        ex, _, _ = _make({"replace_on_drift_pct": 0.005})
        assert ex.replace_on_drift_pct == 0.005

    def test_ttl_override(self):
        ex, _, _ = _make({"limit_order_ttl_seconds": 60})
        assert ex._LIMIT_ORDER_TTL == 60.0

    def test_bad_ttl_ignored(self):
        ex, _, _ = _make({"limit_order_ttl_seconds": "bogus"})
        # default unchanged
        assert ex._LIMIT_ORDER_TTL == 900.0


class TestMakerOnlyGating:
    def test_maker_only_forces_limit_on_buy(self):
        ex, _, _ = _make({"maker_only": True})
        assert ex._should_use_limit({"action": "buy", "confidence": 0.9}) is True

    def test_maker_only_keeps_sell_as_market(self):
        ex, _, _ = _make({"maker_only": True})
        assert ex._should_use_limit({"action": "sell", "confidence": 0.6}) is False

    def test_maker_only_bypassed_on_stop_loss(self):
        ex, _, _ = _make({"maker_only": True})
        assert ex._should_use_limit({
            "action": "buy", "confidence": 0.6, "reasoning": "stop_loss recovery",
        }) is False

    def test_maker_only_bypassed_on_trailing_stop(self):
        ex, _, _ = _make({"maker_only": True})
        assert ex._should_use_limit({
            "action": "buy", "confidence": 0.6, "reasoning": "trailing stop trigger",
        }) is False

    def test_maker_only_off_uses_normal_logic(self):
        ex, _, _ = _make({"maker_only": False})
        # high confidence buy → market
        assert ex._should_use_limit({"action": "buy", "confidence": 0.95}) is False
        # low confidence buy → limit
        assert ex._should_use_limit({"action": "buy", "confidence": 0.5}) is True


class TestPriceDrift:
    def _trade(self, price=100.0):
        return Trade(
            pair="BTC-USD",
            action=TradeAction.BUY,
            quantity=1.0,
            price=price,
            confidence=0.7,
            reasoning="x",
        )

    def test_drift_below_threshold_false(self):
        ex, exchange, _ = _make({"replace_on_drift_pct": 0.01})
        exchange.get_current_price = MagicMock(return_value=100.5)  # 0.5% drift
        assert ex._price_drift_exceeds_threshold(self._trade(100.0)) is False

    def test_drift_above_threshold_true(self):
        ex, exchange, _ = _make({"replace_on_drift_pct": 0.01})
        exchange.get_current_price = MagicMock(return_value=102.0)  # 2% drift
        assert ex._price_drift_exceeds_threshold(self._trade(100.0)) is True

    def test_drift_exchange_error_returns_false(self):
        ex, exchange, _ = _make({"replace_on_drift_pct": 0.01})
        exchange.get_current_price = MagicMock(side_effect=RuntimeError("boom"))
        assert ex._price_drift_exceeds_threshold(self._trade(100.0)) is False

    def test_drift_invalid_price_returns_false(self):
        ex, exchange, _ = _make({"replace_on_drift_pct": 0.01})
        exchange.get_current_price = MagicMock(return_value=0)
        assert ex._price_drift_exceeds_threshold(self._trade(100.0)) is False


class TestMakerStats:
    def test_initial_stats_zero(self):
        ex, _, _ = _make({"maker_only": True})
        assert ex.get_maker_stats() == {"filled": 0, "cancelled": 0, "replaced": 0}


class TestCheckPendingDriftReplace:
    def _pending_trade(self):
        t = Trade(
            pair="BTC-USD",
            action=TradeAction.BUY,
            quantity=1.0,
            price=100.0,
            confidence=0.7,
            reasoning="x",
        )
        t.status = TradeStatus.PENDING
        t.coinbase_order_id = "abc"
        t.timestamp = datetime.now(timezone.utc)
        return t

    def test_drift_triggers_cancel_replace(self):
        ex, exchange, state = _make({
            "maker_only": True,
            "replace_on_drift_pct": 0.01,
            "limit_order_ttl_seconds": 9999,  # no TTL trip
        })
        trade = self._pending_trade()
        state.get_open_trades = MagicMock(return_value=[trade])
        exchange.get_order = MagicMock(return_value={"status": "OPEN"})
        exchange.get_current_price = MagicMock(return_value=105.0)  # 5% drift
        exchange.cancel_order = MagicMock(return_value={"success": True})
        state.mark_trade_status = MagicMock()
        state.reverse_trade_booking = MagicMock()

        results = ex.check_pending_orders()

        assert len(results) == 1
        assert results[0]["action"] == "cancelled_drift"
        assert ex.get_maker_stats()["replaced"] == 1
        exchange.cancel_order.assert_called_once_with("abc")

    def test_no_drift_no_cancel(self):
        ex, exchange, state = _make({
            "maker_only": True,
            "replace_on_drift_pct": 0.01,
            "limit_order_ttl_seconds": 9999,
        })
        trade = self._pending_trade()
        state.get_open_trades = MagicMock(return_value=[trade])
        exchange.get_order = MagicMock(return_value={"status": "OPEN"})
        exchange.get_current_price = MagicMock(return_value=100.2)  # 0.2%
        exchange.cancel_order = MagicMock()

        results = ex.check_pending_orders()

        assert results == []
        exchange.cancel_order.assert_not_called()
        assert ex.get_maker_stats()["replaced"] == 0

    def test_drift_disabled_when_threshold_zero(self):
        ex, exchange, state = _make({
            "maker_only": True,
            "replace_on_drift_pct": 0.0,
            "limit_order_ttl_seconds": 9999,
        })
        trade = self._pending_trade()
        state.get_open_trades = MagicMock(return_value=[trade])
        exchange.get_order = MagicMock(return_value={"status": "OPEN"})
        exchange.get_current_price = MagicMock(return_value=200.0)  # huge drift
        exchange.cancel_order = MagicMock()

        results = ex.check_pending_orders()
        assert results == []
        exchange.cancel_order.assert_not_called()
