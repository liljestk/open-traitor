"""P3 portfolio exposure cap — aggregate open notional limit."""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.rules import AbsoluteRules


def _import_rm():
    return importlib.import_module("src.agents.risk_manager").RiskManagerAgent


def _make(max_total_exposure_pct: float = 0.8, open_positions: dict | None = None,
          current_prices: dict | None = None):
    config = {
        "trading": {
            "min_confidence": 0.5, "min_signal_confidence": 0.5,
            "max_open_positions": 20, "style_modifiers": [],
        },
        "risk": {
            "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
            "max_position_pct": 0.5,
            "max_total_exposure_pct": max_total_exposure_pct,
            "use_kelly_criterion": False, "use_correlation_penalty": False,
        },
    }
    llm = MagicMock()
    state = MagicMock()
    state.current_prices = current_prices or {}
    state.open_positions = open_positions or {}
    state.get_cash_balance = MagicMock(return_value=5000)
    state.get_portfolio_value = MagicMock(return_value=10000)
    state.get_performance_stats = MagicMock(return_value={"win_rate": 0.5, "avg_win": 100, "avg_loss": 50})
    state.compute_open_exposure = MagicMock(return_value=0.0)
    rules = AbsoluteRules({
        "max_single_trade": 100000, "max_daily_spend": 100000,
        "max_daily_loss": 100000, "max_portfolio_risk_pct": 0.9,
        "require_approval_above": 100000, "min_trade_interval_seconds": 0,
        "max_trades_per_day": 100, "max_cash_per_trade_pct": 0.9,
        "emergency_stop_portfolio": 0, "always_use_stop_loss": False,
    })
    return _import_rm()(llm, state, config, rules), state


def _run_buy(rm, *, price=100.0, quote_amount=500.0):
    ctx = {
        "proposal": {
            "action": "buy", "pair": "NEW-USD", "quote_amount": quote_amount,
            "current_price": price, "confidence": 0.8,
        },
        "atr": None, "portfolio_value": 10000, "cash_balance": 5000, "win_rate": 0.5,
    }
    return asyncio.run(rm.run(ctx))


class TestExposureCap:
    def test_no_open_positions_allows_buy(self):
        rm, _ = _make(max_total_exposure_pct=0.5)
        res = _run_buy(rm, quote_amount=1000)
        assert res["approved"] is True

    def test_existing_exposure_trims_order(self):
        # 4000 existing + 1000 new = 5000 cap → new trimmed to 1000.
        pos = SimpleNamespace(quantity=40.0)  # 40 * 100 = 4000
        rm, _ = _make(
            max_total_exposure_pct=0.5,  # 5000 cap
            open_positions={"BTC-USD": pos},
            current_prices={"BTC-USD": 100.0},
        )
        res = _run_buy(rm, price=100.0, quote_amount=2000)
        assert res["approved"] is True
        assert res["quote_amount"] <= 1000.0 + 1e-6

    def test_exposure_at_cap_rejects(self):
        pos = SimpleNamespace(quantity=50.0)  # 50 * 100 = 5000 exposure
        rm, _ = _make(
            max_total_exposure_pct=0.5,  # 5000 cap
            open_positions={"BTC-USD": pos},
            current_prices={"BTC-USD": 100.0},
        )
        res = _run_buy(rm, price=100.0, quote_amount=500)
        assert res["approved"] is False
        assert "exposure" in res["reason"].lower()

    def test_zero_cap_disabled(self):
        pos = SimpleNamespace(quantity=100.0)  # 100 * 100 = 10000 huge
        rm, _ = _make(
            max_total_exposure_pct=0.0,  # disabled
            open_positions={"BTC-USD": pos},
            current_prices={"BTC-USD": 100.0},
        )
        res = _run_buy(rm, price=100.0, quote_amount=500)
        assert res["approved"] is True

    def test_sell_not_affected_by_cap(self):
        pos = SimpleNamespace(quantity=100.0)
        rm, state = _make(
            max_total_exposure_pct=0.1,  # 1000 cap — far exceeded
            open_positions={"BTC-USD": pos},
            current_prices={"BTC-USD": 100.0},
        )
        ctx = {
            "proposal": {"action": "sell", "pair": "BTC-USD", "quote_amount": 200,
                         "current_price": 100.0, "confidence": 0.8, "quantity": 2.0},
            "atr": None, "portfolio_value": 10000, "cash_balance": 5000, "win_rate": 0.5,
        }
        res = asyncio.run(rm.run(ctx))
        assert res["approved"] is True
