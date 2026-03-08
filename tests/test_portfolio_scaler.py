"""
Tests for src/core/portfolio_scaler.py — Tier selection and PortfolioScaler.
"""
from __future__ import annotations

import pytest

from src.core.portfolio_scaler import PortfolioScaler, Tier, get_tier


class TestGetTier:
    def test_micro_tier(self):
        tier = get_tier(10)
        assert tier.name == "MICRO"
        assert tier.max_position_pct == 0.40

    def test_small_tier(self):
        tier = get_tier(200)
        assert tier.name == "SMALL"

    def test_medium_tier(self):
        tier = get_tier(2000)
        assert tier.name == "MEDIUM"

    def test_large_tier(self):
        tier = get_tier(10000)
        assert tier.name == "LARGE"

    def test_whale_tier(self):
        tier = get_tier(100000)
        assert tier.name == "WHALE"

    def test_boundary_micro_small(self):
        assert get_tier(49.99).name == "MICRO"
        assert get_tier(50).name == "SMALL"

    def test_boundary_small_medium(self):
        assert get_tier(499.99).name == "SMALL"
        assert get_tier(500).name == "MEDIUM"

    def test_risk_decreases_with_tier(self):
        """Higher tiers should have lower risk per position."""
        tiers = [get_tier(v) for v in [10, 200, 2000, 10000, 100000]]
        pcts = [t.max_position_pct for t in tiers]
        # Should be non-increasing
        for i in range(len(pcts) - 1):
            assert pcts[i] >= pcts[i + 1]


class TestPortfolioScaler:
    def test_default_tier_is_medium(self):
        scaler = PortfolioScaler({})
        assert scaler.tier.name == "MEDIUM"

    def test_update_changes_tier(self):
        scaler = PortfolioScaler({"trading": {"portfolio_scaling": True}})
        scaler.update(10)
        assert scaler.tier.name == "MICRO"
        scaler.update(10000)
        assert scaler.tier.name == "LARGE"

    def test_scaling_disabled(self):
        scaler = PortfolioScaler({"trading": {"portfolio_scaling": False}})
        scaler.update(10)
        assert scaler.tier.name == "MEDIUM"  # Stays at default

    def test_portfolio_value_property(self):
        scaler = PortfolioScaler({"trading": {"portfolio_scaling": True}})
        scaler.update(1234)
        assert scaler.portfolio_value == pytest.approx(1234)

    def test_accessor_methods(self):
        scaler = PortfolioScaler({"trading": {"portfolio_scaling": True}})
        scaler.update(200)  # SMALL tier
        assert scaler.get_max_position_pct() == get_tier(200).max_position_pct
        assert scaler.get_max_cash_per_trade_pct() == get_tier(200).max_cash_per_trade_pct
        assert scaler.get_max_open_positions() == get_tier(200).max_open_positions
        assert scaler.get_max_active_pairs() == get_tier(200).max_active_pairs
        assert scaler.get_stop_loss_pct() == get_tier(200).stop_loss_pct
        assert scaler.get_take_profit_pct() == get_tier(200).take_profit_pct
