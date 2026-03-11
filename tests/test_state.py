"""Tests for TradingState — core portfolio and position tracking."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.core.state import TradingState, PortfolioSnapshot
from src.models.signal import Signal, SignalType
from src.models.trade import Trade, TradeAction, TradeStatus


@pytest.fixture
def state(tmp_path):
    """Fresh TradingState with no warm-start file."""
    fake_state = tmp_path / "trading_state.json"
    with patch("src.core.state._get_state_file", return_value=str(fake_state)):
        return TradingState(initial_balance=10_000.0)


# ═══════════════════════════════════════════════════════════════════════
# Initialization
# ═══════════════════════════════════════════════════════════════════════

class TestInit:
    def test_initial_balance(self, state):
        assert state.cash_balance == 10_000.0
        assert state.initial_balance == 10_000.0

    def test_no_positions(self, state):
        assert state.positions == {}
        assert state.open_positions == {}

    def test_no_trades(self, state):
        assert state.total_trades == 0
        assert state.trades == []

    def test_performance_defaults(self, state):
        assert state.total_pnl == 0.0
        assert state.win_rate == 0.0
        assert state.max_drawdown == 0.0

    def test_status_defaults(self, state):
        assert state.is_running is False
        assert state.is_paused is False
        assert state.circuit_breaker_triggered is False


# ═══════════════════════════════════════════════════════════════════════
# Price Updates
# ═══════════════════════════════════════════════════════════════════════

class TestPriceUpdates:
    def test_update_price(self, state):
        state.update_price("BTC-EUR", 50_000.0)
        assert state.current_prices["BTC-EUR"] == 50_000.0

    def test_update_price_overwrites(self, state):
        state.update_price("BTC-EUR", 50_000.0)
        state.update_price("BTC-EUR", 51_000.0)
        assert state.current_prices["BTC-EUR"] == 51_000.0


# ═══════════════════════════════════════════════════════════════════════
# Adding Trades
# ═══════════════════════════════════════════════════════════════════════

class TestAddTrade:
    def _make_buy(self, pair="BTC-EUR", price=50_000.0, qty=0.1, quote=5_000.0):
        return Trade(
            pair=pair, action=TradeAction.BUY,
            quantity=qty, price=price, confidence=0.8,
        )

    def _make_sell(self, pair="BTC-EUR", price=55_000.0, qty=0.1, quote=5_500.0):
        return Trade(
            pair=pair, action=TradeAction.SELL,
            quantity=qty, price=price, confidence=0.8,
        )

    def test_buy_updates_positions_and_cash(self, state):
        trade = self._make_buy()
        state.add_trade(trade)
        assert state.positions["BTC-EUR"] == pytest.approx(0.1)
        assert state.cash_balance == pytest.approx(5_000.0)
        assert state.total_trades == 1

    def test_sell_updates_positions_and_cash(self, state):
        # First buy
        state.add_trade(self._make_buy())
        # Then sell
        state.add_trade(self._make_sell())
        assert state.positions["BTC-EUR"] == pytest.approx(0.0)
        assert state.cash_balance == pytest.approx(10_500.0)

    def test_buy_insufficient_cash_raises(self, state):
        trade = self._make_buy(quote=20_000.0, qty=0.4, price=50_000.0)
        with pytest.raises(ValueError, match="Insufficient cash"):
            state.add_trade(trade)

    def test_sell_insufficient_position_raises(self, state):
        trade = self._make_sell(qty=1.0, quote=55_000.0)
        with pytest.raises(ValueError, match="Insufficient position"):
            state.add_trade(trade)

    def test_force_bypasses_validation(self, state):
        trade = self._make_buy(quote=20_000.0, qty=0.4, price=50_000.0)
        state.add_trade(trade, force=True)
        assert state.total_trades == 1

    def test_multiple_buys_accumulate(self, state):
        t1 = self._make_buy(qty=0.1, quote=5_000.0)
        t2 = self._make_buy(qty=0.05, quote=2_500.0)
        state.add_trade(t1)
        state.add_trade(t2)
        assert state.positions["BTC-EUR"] == pytest.approx(0.15)
        assert state.cash_balance == pytest.approx(2_500.0)


# ═══════════════════════════════════════════════════════════════════════
# Close Trades
# ═══════════════════════════════════════════════════════════════════════

class TestCloseTrade:
    def test_close_with_profit(self, state):
        trade = Trade(
            pair="BTC-EUR", action=TradeAction.BUY,
            quantity=0.1, price=50_000.0, confidence=0.8,
        )
        state.add_trade(trade)
        result = state.close_trade(trade.id, close_price=55_000.0)
        assert result is not None
        assert result.pnl > 0
        assert state.winning_trades == 1
        assert state.total_pnl > 0

    def test_close_with_loss(self, state):
        trade = Trade(
            pair="BTC-EUR", action=TradeAction.BUY,
            quantity=0.1, price=50_000.0, confidence=0.8,
        )
        state.add_trade(trade)
        result = state.close_trade(trade.id, close_price=45_000.0)
        assert result is not None
        assert result.pnl < 0
        assert state.losing_trades == 1

    def test_close_nonexistent_returns_none(self, state):
        result = state.close_trade("nonexistent-id", close_price=50_000.0)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Signals
# ═══════════════════════════════════════════════════════════════════════

class TestSignals:
    def test_add_signal(self, state):
        sig = Signal(
            pair="BTC-EUR", signal_type=SignalType.BUY, confidence=0.8,
            current_price=50_000.0, reasoning="test signal",
        )
        state.add_signal(sig)
        assert len(state.signals) == 1
        assert state.last_analysis_time is not None

    def test_signals_bounded(self, state):
        for i in range(1100):
            sig = Signal(pair="BTC-EUR", signal_type=SignalType.BUY, confidence=0.5, current_price=50_000.0)
            state.add_signal(sig)
        assert len(state.signals) == 1000

    def test_recent_signals_capped(self, state):
        for i in range(20):
            state.add_signal(Signal(pair="BTC-EUR", signal_type=SignalType.BUY, confidence=0.5, current_price=50_000.0))
        assert len(state.recent_signals) == 10


# ═══════════════════════════════════════════════════════════════════════
# Portfolio Snapshots & Valuation
# ═══════════════════════════════════════════════════════════════════════

class TestPortfolio:
    def test_portfolio_value_cash_only(self, state):
        assert state.portfolio_value == pytest.approx(10_000.0)

    def test_portfolio_value_with_positions(self, state):
        state.positions["BTC-EUR"] = 0.1
        state.current_prices["BTC-EUR"] = 50_000.0
        # cash + position value
        assert state.portfolio_value == pytest.approx(15_000.0)

    def test_portfolio_prefers_live_value(self, state):
        state.live_portfolio_value = 12_345.0
        state._live_snapshot_ts = time.time()
        assert state.portfolio_value == pytest.approx(12_345.0)

    def test_take_snapshot_tracks_drawdown(self, state):
        state.take_portfolio_snapshot()
        assert state.peak_portfolio_value == 10_000.0

        # Simulate loss
        state.cash_balance = 8_000.0
        snap = state.take_portfolio_snapshot()
        assert state.max_drawdown == pytest.approx(0.2)

    def test_return_pct(self, state):
        state.cash_balance = 11_000.0
        assert state.return_pct == pytest.approx(0.1)


# ═══════════════════════════════════════════════════════════════════════
# Live Holdings Sync
# ═══════════════════════════════════════════════════════════════════════

class TestLiveHoldingsSync:
    def _make_snapshot(self, holdings=None, total=5000.0, native="EUR"):
        return {
            "total_portfolio": total,
            "native_currency": native,
            "currency_symbol": "€",
            "holdings": holdings or [],
        }

    def test_basic_sync(self, state):
        snapshot = self._make_snapshot(
            holdings=[
                {"currency": "EUR", "amount": 100.0, "native_value": 100.0, "is_fiat": True},
                {
                    "currency": "BTC", "amount": 0.05, "native_value": 2500.0,
                    "is_fiat": False, "pair": "BTC-EUR", "price": 50_000.0,
                },
            ],
            total=2600.0,
        )
        new = state.sync_live_holdings(snapshot)
        assert state.native_currency == "EUR"
        assert state.currency_symbol == "€"
        assert state.live_portfolio_value == 2600.0
        assert "BTC-EUR" in state.positions
        assert state.current_prices["BTC-EUR"] == 50_000.0

    def test_additive_merge_does_not_overwrite(self, state):
        # Bot already has a position
        state.positions["BTC-EUR"] = 0.2
        snapshot = self._make_snapshot(
            holdings=[
                {
                    "currency": "BTC", "amount": 0.1, "native_value": 5000.0,
                    "is_fiat": False, "pair": "BTC-EUR", "price": 50_000.0,
                },
            ],
            total=5000.0,
        )
        state.sync_live_holdings(snapshot)
        # Should NOT overwrite existing position
        assert state.positions["BTC-EUR"] == 0.2

    def test_dust_filtered(self, state):
        snapshot = self._make_snapshot(
            holdings=[
                {
                    "currency": "SHIB", "amount": 1.0, "native_value": 0.001,
                    "is_fiat": False, "pair": "SHIB-EUR", "price": 0.001,
                },
            ],
        )
        state.sync_live_holdings(snapshot)
        assert "SHIB-EUR" not in state.positions

    def test_initial_balance_synced_once(self, state):
        snapshot = self._make_snapshot(total=15_000.0)
        state.sync_live_holdings(snapshot)
        assert state.initial_balance == 15_000.0
        assert state._initial_balance_synced is True
        # Second sync should not change initial_balance
        snapshot2 = self._make_snapshot(total=16_000.0)
        state.sync_live_holdings(snapshot2)
        assert state.initial_balance == 15_000.0

    def test_new_externals_returned(self, state):
        snapshot = self._make_snapshot(
            holdings=[
                {
                    "currency": "ETH", "amount": 1.0, "native_value": 3000.0,
                    "is_fiat": False, "pair": "ETH-EUR", "price": 3000.0,
                },
            ],
            total=3000.0,
        )
        new = state.sync_live_holdings(snapshot)
        assert "ETH-EUR" in new
        assert new["ETH-EUR"] == 3000.0
        assert state.positions_meta["ETH-EUR"]["origin"] == "external"


# ═══════════════════════════════════════════════════════════════════════
# Position Reconciliation
# ═══════════════════════════════════════════════════════════════════════

class TestReconcilePosition:
    def test_reconcile_adds_position(self, state):
        result = state.reconcile_position("ETH-EUR", actual_qty=2.0, current_price=3000.0)
        assert result["new_qty"] == 2.0
        assert result["delta"] == 2.0
        assert state.positions["ETH-EUR"] == 2.0

    def test_reconcile_removes_dust(self, state):
        state.positions["ETH-EUR"] = 0.001
        result = state.reconcile_position("ETH-EUR", actual_qty=0.0, current_price=3000.0)
        assert "ETH-EUR" not in state.positions

    def test_reconcile_adjusts_cash(self, state):
        initial_cash = state.cash_balance
        state.reconcile_position("ETH-EUR", actual_qty=1.0, current_price=3000.0)
        # positive delta = spent cash
        assert state.cash_balance == pytest.approx(initial_cash - 3000.0)


# ═══════════════════════════════════════════════════════════════════════
# Trade Booking Reversal
# ═══════════════════════════════════════════════════════════════════════

class TestReverseTradeBooking:
    def test_reverse_buy(self, state):
        trade = Trade(
            pair="BTC-EUR", action=TradeAction.BUY,
            quantity=0.1, price=50_000.0, confidence=0.8,
        )
        state.add_trade(trade, force=True)
        state.reverse_trade_booking(trade)
        assert state.positions.get("BTC-EUR", 0) == pytest.approx(0.0)
        assert state.cash_balance == pytest.approx(10_000.0)


# ═══════════════════════════════════════════════════════════════════════
# Summary / Serialization
# ═══════════════════════════════════════════════════════════════════════

class TestSummary:
    def test_to_summary_has_keys(self, state):
        s = state.to_summary()
        required = {
            "portfolio_value", "cash_balance", "return_pct", "total_pnl",
            "total_trades", "win_rate", "max_drawdown", "open_positions",
            "current_prices", "is_running", "is_paused", "circuit_breaker",
        }
        assert required.issubset(s.keys())

    def test_save_and_reload(self, state, tmp_path):
        trade = Trade(
            pair="BTC-EUR", action=TradeAction.BUY,
            quantity=0.1, price=50_000.0, confidence=0.8,
        )
        state.add_trade(trade, force=True)
        fp = str(tmp_path / "save_test.json")
        state.save_state(fp)
        # Verify file exists and is valid JSON
        import json
        with open(fp) as f:
            data = json.load(f)
        assert "trades" in data
        assert len(data["trades"]) >= 1


# ═══════════════════════════════════════════════════════════════════════
# Holdings Summary (LLM prompt)
# ═══════════════════════════════════════════════════════════════════════

class TestHoldingsSummary:
    def test_empty_holdings(self, state):
        assert "No live holdings" in state.holdings_summary

    def test_with_holdings(self, state):
        state.live_holdings = [
            {"currency": "EUR", "amount": 500.0, "native_value": 500.0, "is_fiat": True},
            {"currency": "BTC", "amount": 0.05, "native_value": 2500.0, "is_fiat": False, "price": 50_000.0},
        ]
        state.live_cash_balances = {"EUR": 500.0}
        state.currency_symbol = "€"
        summary = state.holdings_summary
        assert "€500.00 EUR" in summary
        assert "BTC" in summary


# ═══════════════════════════════════════════════════════════════════════
# Open Trades / Positions
# ═══════════════════════════════════════════════════════════════════════

class TestOpenPositions:
    def test_filters_dust(self, state):
        state.positions["BTC-EUR"] = 1e-10
        state.positions["ETH-EUR"] = 1.0
        op = state.open_positions
        assert "ETH-EUR" in op
        assert "BTC-EUR" not in op

    def test_get_open_trades(self, state):
        trade = Trade(
            pair="BTC-EUR", action=TradeAction.BUY,
            quantity=0.1, price=50_000.0, confidence=0.8,
        )
        state.add_trade(trade, force=True)
        opens = state.get_open_trades()
        assert len(opens) == 1
        assert opens[0].pair == "BTC-EUR"

    def test_recent_trades_capped(self, state):
        for i in range(30):
            t = Trade(
                pair="BTC-EUR", action=TradeAction.BUY,
                quantity=0.0001, price=50_000.0, confidence=0.5,
            )
            state.add_trade(t, force=True)
        assert len(state.recent_trades) == 20
