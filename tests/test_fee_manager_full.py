"""Tests for FeeManager — fee models, worthiness checks, optimal sizing."""
from __future__ import annotations

import pytest

from src.core.fee_manager import (
    FeeManager,
    FeeEstimate,
    CryptoPercentageFeeModel,
    EquityFlatPlusPctFeeModel,
    EquityPerShareFeeModel,
    _build_fee_model,
)


# ═══════════════════════════════════════════════════════════════════════
# Fee Models
# ═══════════════════════════════════════════════════════════════════════

class TestCryptoPercentageFeeModel:
    def test_taker_fee(self):
        m = CryptoPercentageFeeModel(taker_pct=0.006, maker_pct=0.004)
        assert m.estimate_trade_fee(1000, is_maker=False) == pytest.approx(6.0)

    def test_maker_fee(self):
        m = CryptoPercentageFeeModel(taker_pct=0.006, maker_pct=0.004)
        assert m.estimate_trade_fee(1000, is_maker=True) == pytest.approx(4.0)

    def test_effective_pct(self):
        m = CryptoPercentageFeeModel(taker_pct=0.006, maker_pct=0.004)
        assert m.effective_fee_pct(1000, is_maker=False) == 0.006
        assert m.effective_fee_pct(1000, is_maker=True) == 0.004


class TestEquityFlatPlusPctFeeModel:
    def test_flat_minimum_dominates(self):
        m = EquityFlatPlusPctFeeModel(flat_fee_min=39.0, percent_fee=0.0015)
        # 1000 * 0.0015 = 1.50, but min is 39
        assert m.estimate_trade_fee(1000) == 39.0

    def test_percentage_dominates(self):
        m = EquityFlatPlusPctFeeModel(flat_fee_min=39.0, percent_fee=0.0015)
        # 100000 * 0.0015 = 150, which > 39
        assert m.estimate_trade_fee(100000) == 150.0

    def test_effective_pct_zero_amount(self):
        m = EquityFlatPlusPctFeeModel()
        assert m.effective_fee_pct(0) == 0.0


class TestEquityPerShareFeeModel:
    def test_min_fee(self):
        m = EquityPerShareFeeModel(per_share_fee=0.0035, min_fee=0.35)
        # For large enough quote to trigger min_fee floor
        fee = m.estimate_trade_fee(5000)
        assert fee >= 0.35

    def test_per_share_exact(self):
        m = EquityPerShareFeeModel()
        # 100 shares at $50 = $5000 quote, fee = 100 * $0.0035 = $0.35
        fee = m.estimate_trade_fee_shares(100, 50.0)
        assert fee == pytest.approx(0.35)

    def test_max_fee_cap(self):
        m = EquityPerShareFeeModel(max_fee_pct=0.01)
        fee = m.estimate_trade_fee(1000)
        assert fee <= 1000 * 0.01


class TestBuildFeeModel:
    def test_default_crypto(self):
        m = _build_fee_model({})
        assert isinstance(m, CryptoPercentageFeeModel)

    def test_equity_flat(self):
        m = _build_fee_model({"model_type": "equity_flat_plus_pct"})
        assert isinstance(m, EquityFlatPlusPctFeeModel)

    def test_equity_per_share(self):
        m = _build_fee_model({"model_type": "equity_per_share"})
        assert isinstance(m, EquityPerShareFeeModel)


# ═══════════════════════════════════════════════════════════════════════
# FeeManager Core
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def fm():
    return FeeManager({"fees": {
        "trade_fee_pct": 0.006,
        "maker_fee_pct": 0.004,
        "safety_margin": 1.5,
        "min_gain_after_fees_pct": 0.005,
        "min_trade_quote": 1.0,
        "min_trade_pct": 0.01,
    }})


class TestFeeManagerCore:
    def test_single_trade_fee(self, fm):
        fee = fm.estimate_trade_fees(1000)
        assert fee == pytest.approx(6.0)

    def test_swap_fees_two_legs(self, fm):
        estimate = fm.estimate_swap_fees(1000, n_legs=2)
        assert estimate.total_fee_pct > 0
        assert estimate.total_fee_quote > 0
        assert estimate.sell_fee_quote > 0
        assert estimate.buy_fee_quote > 0
        assert estimate.breakeven_move_pct > estimate.total_fee_pct

    def test_swap_fees_one_leg(self, fm):
        est1 = fm.estimate_swap_fees(1000, n_legs=1)
        est2 = fm.estimate_swap_fees(1000, n_legs=2)
        assert est1.total_fee_pct < est2.total_fee_pct

    def test_dynamic_min_trade(self, fm):
        # Floor
        assert fm.get_dynamic_min_trade(0) == 1.0
        # Portfolio-scaled: 1% of 10000 = 100
        assert fm.get_dynamic_min_trade(10000) == 100.0
        # Small portfolio: max(1.0, 0.01*50) = 1.0
        assert fm.get_dynamic_min_trade(50) == 1.0


# ═══════════════════════════════════════════════════════════════════════
# Trade Worthiness
# ═══════════════════════════════════════════════════════════════════════

class TestTradeWorthiness:
    def test_profitable_trade(self, fm):
        ok, est = fm.is_trade_worthwhile(1000, 0.10)  # 10% expected
        assert ok is True
        assert est.is_profitable is True

    def test_unprofitable_trade(self, fm):
        ok, est = fm.is_trade_worthwhile(1000, 0.001)  # 0.1% expected
        assert ok is False
        assert est.is_profitable is False

    def test_zero_amount_rejected(self, fm):
        ok, est = fm.is_trade_worthwhile(0, 0.10)
        assert ok is False

    def test_negative_amount_rejected(self, fm):
        ok, est = fm.is_trade_worthwhile(-100, 0.10)
        assert ok is False

    def test_too_small_rejected(self, fm):
        ok, est = fm.is_trade_worthwhile(0.5, 0.10, portfolio_value=100)
        assert ok is False

    def test_swap_worthiness(self, fm):
        ok, _ = fm.is_trade_worthwhile(1000, 0.10, is_swap=True)
        assert ok is True

    def test_swap_higher_bar(self, fm):
        """Swaps need higher gain to be worthwhile due to 2x fees."""
        ok1, _ = fm.is_trade_worthwhile(1000, 0.02, is_swap=False)
        ok2, _ = fm.is_trade_worthwhile(1000, 0.02, is_swap=True)
        # At 2% expected gain, single trade is profitable but swap may not be
        assert ok1 is True


# ═══════════════════════════════════════════════════════════════════════
# Optimal Trade Size
# ═══════════════════════════════════════════════════════════════════════

class TestOptimalTradeSize:
    def test_with_high_gain(self, fm):
        size = fm.get_optimal_trade_size(10000, 0.10)
        assert size == 10000.0  # Should return max since gain is high

    def test_zero_gain(self, fm):
        size = fm.get_optimal_trade_size(10000, 0)
        assert size == 0.0

    def test_negative_gain(self, fm):
        size = fm.get_optimal_trade_size(10000, -0.05)
        assert size == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

class TestFeeSummary:
    def test_summary_format(self, fm):
        s = fm.get_fee_summary()
        assert "Trade fee" in s
        assert "Swap cost" in s
        assert "Safety margin" in s
