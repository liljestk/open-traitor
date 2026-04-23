"""P2 ATR-scaled stop/TP with configurable multiplier and clamp band."""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import pytest

from src.core.rules import AbsoluteRules


def _import_rm():
    return importlib.import_module("src.agents.risk_manager").RiskManagerAgent


def _make(risk_cfg: dict | None = None):
    config = {
        "trading": {
            "min_confidence": 0.55,
            "min_signal_confidence": 0.5,
            "max_open_positions": 10,
            "style_modifiers": [],
        },
        "risk": {
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.06,
            "max_position_pct": 0.1,
            "use_kelly_criterion": False,
            "use_correlation_penalty": False,
            **(risk_cfg or {}),
        },
    }
    llm = MagicMock()
    state = MagicMock()
    state.current_prices = {"BTC-EUR": 50000}
    state.open_positions = {}
    state.get_cash_balance = MagicMock(return_value=5000)
    state.get_portfolio_value = MagicMock(return_value=10000)
    state.get_performance_stats = MagicMock(return_value={"win_rate": 0.5, "avg_win": 100, "avg_loss": 50})
    state.compute_open_exposure = MagicMock(return_value=0.0)

    rules = AbsoluteRules({
        "max_single_trade": 100000,
        "max_daily_spend": 100000,
        "max_daily_loss": 100000,
        "max_portfolio_risk_pct": 0.9,
        "require_approval_above": 100000,
        "min_trade_interval_seconds": 0,
        "max_trades_per_day": 100,
        "max_cash_per_trade_pct": 0.9,
        "emergency_stop_portfolio": 0,
        "always_use_stop_loss": False,
    })

    RiskManagerAgent = _import_rm()
    agent = RiskManagerAgent(llm, state, config, rules)
    return agent, state


class TestAtrConfigPlumbing:
    def test_defaults(self):
        rm, _ = _make()
        assert rm.atr_stop_mult == 2.0
        assert rm.atr_tp_mult == 3.0
        assert rm.atr_stop_floor_pct == 0.01
        assert rm.atr_stop_ceiling_pct == 0.10

    def test_overrides(self):
        rm, _ = _make({
            "atr_stop_mult": 1.5,
            "atr_tp_mult": 4.0,
            "atr_stop_floor_pct": 0.005,
            "atr_stop_ceiling_pct": 0.06,
        })
        assert rm.atr_stop_mult == 1.5
        assert rm.atr_tp_mult == 4.0
        assert rm.atr_stop_floor_pct == 0.005
        assert rm.atr_stop_ceiling_pct == 0.06


def _run_atr(rm, state, *, price=50000, atr=500.0, confidence=0.8):
    state.current_prices["BTC-EUR"] = price
    proposal = {
        "action": "buy",
        "pair": "BTC-EUR",
        "quote_amount": 100,
        "current_price": price,
        "confidence": confidence,
    }
    context = {
        "proposal": proposal,
        "atr": atr,
        "portfolio_value": 10000,
        "cash_balance": 5000,
        "win_rate": 0.5,
    }
    res = asyncio.run(rm.run(context))
    return res


class TestAtrScaledStops:
    def test_normal_atr_in_band(self):
        rm, state = _make()
        res = _run_atr(rm, state, price=50000, atr=500.0)
        # 2 * 500 / 50000 = 0.02 (in band 0.01..0.10)
        assert res["approved"]
        assert abs(res["stop_loss"] - 50000 * (1 - 0.02)) < 1
        # 3 * 500 / 50000 = 0.03 (in band [0.015..0.15])
        assert abs(res["take_profit"] - 50000 * (1 + 0.03)) < 1

    def test_low_vol_hits_floor(self):
        rm, state = _make({"atr_stop_floor_pct": 0.02})
        res = _run_atr(rm, state, price=50000, atr=100.0)  # raw = 0.4%
        assert res["approved"]
        # clamped up to 2%
        assert abs(res["stop_loss"] - 50000 * (1 - 0.02)) < 1

    def test_high_vol_hits_ceiling(self):
        rm, state = _make({"atr_stop_ceiling_pct": 0.05})
        res = _run_atr(rm, state, price=50000, atr=5000.0)  # raw = 20%
        assert res["approved"]
        # clamped down to 5%
        assert abs(res["stop_loss"] - 50000 * (1 - 0.05)) < 1

    def test_custom_mult_reflected(self):
        rm, state = _make({"atr_stop_mult": 1.5, "atr_tp_mult": 4.5})
        res = _run_atr(rm, state, price=50000, atr=500.0)
        # 1.5 * 500 / 50000 = 0.015
        assert abs(res["stop_loss"] - 50000 * (1 - 0.015)) < 1
        # 4.5 * 500 / 50000 = 0.045 (in band scaled by rr=3)
        assert abs(res["take_profit"] - 50000 * (1 + 0.045)) < 1

    def test_missing_atr_falls_back_to_pct(self):
        rm, state = _make({"stop_loss_pct": 0.04})
        state.current_prices["BTC-EUR"] = 50000
        proposal = {"action": "buy", "pair": "BTC-EUR", "quote_amount": 100,
                    "current_price": 50000, "confidence": 0.8}
        context = {"proposal": proposal, "portfolio_value": 10000,
                   "cash_balance": 5000, "win_rate": 0.5}
        res = asyncio.run(rm.run(context))
        # no atr → falls back to effective_stop_loss_pct (0.04)
        assert res["approved"]
        assert abs(res["stop_loss"] - 50000 * (1 - 0.04)) < 1

    def test_stop_never_negative(self):
        rm, state = _make({"atr_stop_ceiling_pct": 2.0})  # absurd ceiling
        res = _run_atr(rm, state, price=50.0, atr=200.0)  # raw = 800%
        # clamped to 200% → max(price * (1-2), 0) = 0
        assert res["stop_loss"] == 0.0
