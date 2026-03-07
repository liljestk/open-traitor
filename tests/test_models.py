"""
Tests for src/models — Trade and Signal data models.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.trade import Trade, TradeAction, TradeStatus
from src.models.signal import (
    Signal,
    SignalType,
    MarketCondition,
    TechnicalSignals,
    SentimentSignals,
)


# ═══════════════════════════════════════════════════════════════════════════
# Trade model
# ═══════════════════════════════════════════════════════════════════════════

class TestTradeModel:
    def test_default_fields(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.BUY, quantity=0.01, price=50000)
        assert t.pair == "BTC-EUR"
        assert t.action == TradeAction.BUY
        assert t.status == TradeStatus.PENDING
        assert t.id  # auto-generated UUID
        assert t.exchange == "coinbase"
        assert t.confidence == 0.0
        assert t.pnl is None

    def test_is_open_for_active_statuses(self):
        for status in (TradeStatus.PENDING, TradeStatus.SUBMITTED,
                       TradeStatus.FILLED, TradeStatus.PARTIALLY_FILLED):
            t = Trade(pair="ETH-EUR", action=TradeAction.BUY,
                      quantity=1, price=3000, status=status)
            assert t.is_open is True

    def test_is_open_false_for_terminal_statuses(self):
        for status in (TradeStatus.CANCELLED, TradeStatus.FAILED, TradeStatus.CLOSED):
            t = Trade(pair="ETH-EUR", action=TradeAction.BUY,
                      quantity=1, price=3000, status=status)
            assert t.is_open is False

    def test_value_uses_filled_if_available(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.BUY,
                  quantity=0.1, price=50000,
                  filled_price=50100, filled_quantity=0.09)
        assert t.value == pytest.approx(50100 * 0.09)

    def test_value_fallback_to_order(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.BUY,
                  quantity=0.1, price=50000)
        assert t.value == pytest.approx(5000.0)

    def test_close_buy_trade_calculates_pnl(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.BUY,
                  quantity=0.1, price=50000, fees=5.0)
        t.close(close_price=51000, fees=5.0)
        # PnL = (51000 - 50000) * 0.1 - 5.0 (entry) - 5.0 (exit) = 100 - 10 = 90
        assert t.pnl == pytest.approx(90.0)
        assert t.status == TradeStatus.CLOSED
        assert t.closed_at is not None
        assert t.fees == pytest.approx(10.0)

    def test_close_sell_trade_calculates_pnl(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.SELL,
                  quantity=0.1, price=50000)
        t.close(close_price=49000)
        # PnL = (50000 - 49000) * 0.1 = 100
        assert t.pnl == pytest.approx(100.0)

    def test_close_losing_trade(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.BUY,
                  quantity=0.1, price=50000)
        t.close(close_price=48000)
        assert t.pnl == pytest.approx(-200.0)

    def test_to_summary_contains_key_info(self):
        t = Trade(pair="BTC-EUR", action=TradeAction.BUY,
                  quantity=0.001, price=50000, confidence=0.85)
        summary = t.to_summary()
        assert "BUY" in summary
        assert "BTC-EUR" in summary
        assert "85%" in summary

    def test_to_summary_with_pnl(self):
        t = Trade(pair="ETH-EUR", action=TradeAction.BUY,
                  quantity=1, price=3000)
        t.close(close_price=3100)
        summary = t.to_summary()
        assert "PnL" in summary

    def test_unique_ids(self):
        t1 = Trade(pair="BTC-EUR", action=TradeAction.BUY, quantity=1, price=100)
        t2 = Trade(pair="BTC-EUR", action=TradeAction.BUY, quantity=1, price=100)
        assert t1.id != t2.id


class TestTradeEnums:
    def test_trade_action_values(self):
        assert TradeAction.BUY.value == "buy"
        assert TradeAction.SELL.value == "sell"
        assert TradeAction.HOLD.value == "hold"

    def test_trade_status_values(self):
        assert TradeStatus.PENDING.value == "pending"
        assert TradeStatus.CLOSED.value == "closed"
        assert TradeStatus.FAILED.value == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# Signal model
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalModel:
    def test_default_signal(self):
        s = Signal(pair="BTC-EUR", current_price=50000)
        assert s.signal_type == SignalType.NEUTRAL
        assert s.confidence == 0.0
        assert s.market_condition == MarketCondition.UNKNOWN
        assert s.is_actionable is False
        assert s.is_buy_signal is False
        assert s.is_sell_signal is False

    def test_buy_signals(self):
        for st in (SignalType.STRONG_BUY, SignalType.BUY, SignalType.WEAK_BUY):
            s = Signal(pair="BTC-EUR", current_price=50000, signal_type=st)
            assert s.is_buy_signal is True
            assert s.is_sell_signal is False
            assert s.is_actionable is True

    def test_sell_signals(self):
        for st in (SignalType.STRONG_SELL, SignalType.SELL, SignalType.WEAK_SELL):
            s = Signal(pair="BTC-EUR", current_price=50000, signal_type=st)
            assert s.is_sell_signal is True
            assert s.is_buy_signal is False
            assert s.is_actionable is True

    def test_neutral_not_actionable(self):
        s = Signal(pair="BTC-EUR", current_price=50000, signal_type=SignalType.NEUTRAL)
        assert s.is_actionable is False

    def test_technical_signals_defaults(self):
        ts = TechnicalSignals()
        assert ts.rsi is None
        assert ts.macd is None
        assert ts.bb_upper is None

    def test_sentiment_signals_defaults(self):
        ss = SentimentSignals()
        assert ss.sentiment_score == 0.0
        assert ss.key_factors == []

    def test_signal_to_summary(self):
        s = Signal(pair="BTC-EUR", current_price=50000,
                   signal_type=SignalType.STRONG_BUY, confidence=0.9)
        summary = s.to_summary()
        assert "BTC-EUR" in summary
        assert "50,000" in summary


class TestMarketCondition:
    def test_all_conditions_exist(self):
        conditions = [
            "strongly_bullish", "bullish", "slightly_bullish", "neutral",
            "slightly_bearish", "bearish", "strongly_bearish", "volatile", "unknown",
        ]
        for c in conditions:
            assert MarketCondition(c)
