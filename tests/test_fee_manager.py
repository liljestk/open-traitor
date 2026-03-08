"""
Tests for src/core/fee_manager.py — Fee calculations and trade profitability.
"""
from __future__ import annotations

import pytest

from src.core.fee_manager import (
    CryptoPercentageFeeModel,
    EquityFlatPlusPctFeeModel,
    EquityPerShareFeeModel,
    FeeEstimate,
    FeeManager,
    _build_fee_model,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fee models
# ═══════════════════════════════════════════════════════════════════════════

class TestCryptoPercentageFeeModel:
    def test_taker_fee(self):
        m = CryptoPercentageFeeModel(taker_pct=0.006, maker_pct=0.004)
        fee = m.estimate_trade_fee(1000, is_maker=False)
        assert fee == pytest.approx(6.0)

    def test_maker_fee(self):
        m = CryptoPercentageFeeModel(taker_pct=0.006, maker_pct=0.004)
        fee = m.estimate_trade_fee(1000, is_maker=True)
        assert fee == pytest.approx(4.0)

    def test_effective_fee_pct(self):
        m = CryptoPercentageFeeModel(taker_pct=0.006, maker_pct=0.004)
        assert m.effective_fee_pct(500, is_maker=False) == pytest.approx(0.006)
        assert m.effective_fee_pct(500, is_maker=True) == pytest.approx(0.004)


class TestEquityFlatPlusPctFeeModel:
    def test_flat_minimum_applies(self):
        m = EquityFlatPlusPctFeeModel(flat_fee_min=39.0, percent_fee=0.0015)
        # trade of 100 → 0.15% = 0.15, but min is 39
        fee = m.estimate_trade_fee(100)
        assert fee == pytest.approx(39.0)

    def test_percentage_applies_above_breakpoint(self):
        m = EquityFlatPlusPctFeeModel(flat_fee_min=39.0, percent_fee=0.0015)
        # trade of 100000 → 0.15% = 150, > 39 min
        fee = m.estimate_trade_fee(100000)
        assert fee == pytest.approx(150.0)

    def test_effective_fee_pct_zero_amount(self):
        m = EquityFlatPlusPctFeeModel()
        assert m.effective_fee_pct(0) == 0.0


class TestEquityPerShareFeeModel:
    def test_min_fee(self):
        m = EquityPerShareFeeModel(min_fee=0.35)
        # Trade large enough that max_fee_pct cap doesn't override min_fee
        fee = m.estimate_trade_fee(100.0)
        assert fee >= 0.35

    def test_fee_shares(self):
        m = EquityPerShareFeeModel(per_share_fee=0.0035, min_fee=0.35)
        fee = m.estimate_trade_fee_shares(100, 50.0)
        assert fee == pytest.approx(max(0.35, 100 * 0.0035))

    def test_effective_fee_pct_zero_amount(self):
        m = EquityPerShareFeeModel()
        assert m.effective_fee_pct(0) == 0.0


class TestBuildFeeModel:
    def test_default_is_crypto(self):
        model = _build_fee_model({})
        assert isinstance(model, CryptoPercentageFeeModel)

    def test_equity_flat(self):
        model = _build_fee_model({"model_type": "equity_flat_plus_pct"})
        assert isinstance(model, EquityFlatPlusPctFeeModel)

    def test_equity_per_share(self):
        model = _build_fee_model({"model_type": "equity_per_share"})
        assert isinstance(model, EquityPerShareFeeModel)


# ═══════════════════════════════════════════════════════════════════════════
# FeeManager
# ═══════════════════════════════════════════════════════════════════════════

class TestFeeManager:
    def _make_fm(self, overrides=None):
        cfg = {
            "fees": {
                "model_type": "crypto_percentage",
                "trade_fee_pct": 0.006,
                "maker_fee_pct": 0.004,
                "safety_margin": 1.5,
                "min_gain_after_fees_pct": 0.005,
                "min_trade_usd": 1.0,
                "min_trade_pct": 0.01,
            }
        }
        if overrides:
            cfg["fees"].update(overrides)
        return FeeManager(cfg)

    def test_estimate_trade_fees(self):
        fm = self._make_fm()
        fee = fm.estimate_trade_fees(1000)
        assert fee == pytest.approx(6.0)

    def test_estimate_swap_fees_two_legs(self):
        fm = self._make_fm()
        est = fm.estimate_swap_fees(1000, n_legs=2)
        assert isinstance(est, FeeEstimate)
        assert est.total_fee_quote > est.sell_fee_quote
        assert est.total_fee_pct > 0

    def test_trade_too_small(self):
        fm = self._make_fm({"min_trade_usd": 10.0})
        ok, est = fm.is_trade_worthwhile(5.0, expected_gain_pct=0.10)
        assert ok is False

    def test_trade_worthwhile_high_gain(self):
        fm = self._make_fm()
        ok, est = fm.is_trade_worthwhile(1000, expected_gain_pct=0.10)
        assert ok is True
        assert est.is_profitable is True

    def test_trade_not_worthwhile_low_gain(self):
        fm = self._make_fm({"safety_margin": 5.0, "min_gain_after_fees_pct": 0.10})
        ok, est = fm.is_trade_worthwhile(1000, expected_gain_pct=0.001)
        assert ok is False

    def test_dynamic_min_trade(self):
        fm = self._make_fm({"min_trade_usd": 1.0, "min_trade_pct": 0.01})
        # For a 10000 portfolio, 1% = 100, which > floor of 1.0
        assert fm.get_dynamic_min_trade(10000) == pytest.approx(100.0)
        # For a 50 portfolio, 1% = 0.50, which < floor of 1.0
        assert fm.get_dynamic_min_trade(50) == pytest.approx(1.0)
        # Zero portfolio
        assert fm.get_dynamic_min_trade(0) == pytest.approx(1.0)

    def test_swap_worthwhile(self):
        fm = self._make_fm()
        ok, est = fm.is_trade_worthwhile(
            1000, expected_gain_pct=0.10, is_swap=True, n_legs=2,
        )
        assert ok is True
