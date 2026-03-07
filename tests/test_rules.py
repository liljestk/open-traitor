"""
Tests for src/core/rules.py — AbsoluteRules enforcement.

The rules engine is the CRITICAL safety gatekeeper. Every rule path
must be covered to guarantee the system never executes a dangerous trade.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.core.rules import AbsoluteRules, RuleViolation
from src.models.trade import TradeAction


def _make_rules(overrides: dict | None = None) -> AbsoluteRules:
    """Build an AbsoluteRules instance with sane test defaults."""
    cfg = {
        "max_single_trade": 500,
        "max_daily_spend": 2000,
        "max_daily_loss": 300,
        "max_portfolio_risk_pct": 0.20,
        "require_approval_above": 200,
        "min_trade_interval_seconds": 60,
        "max_trades_per_day": 20,
        "max_cash_per_trade_pct": 0.25,
        "emergency_stop_portfolio": 5000,
        "always_use_stop_loss": True,
        "max_stop_loss_pct": 0.05,
    }
    if overrides:
        cfg.update(overrides)
    return AbsoluteRules(cfg)


# ═══════════════════════════════════════════════════════════════════════════
# SELL orders are NEVER blocked
# ═══════════════════════════════════════════════════════════════════════════

class TestSellAlwaysAllowed:
    """Critical invariant: sells must always be allowed."""

    def test_sell_allowed_even_at_daily_loss_limit(self):
        r = _make_rules()
        r._daily_loss = 999999
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.SELL, 1000, 10000, 5000, has_stop_loss=False,
        )
        assert ok is True
        assert violations == []

    def test_sell_allowed_at_max_trades(self):
        r = _make_rules()
        r._daily_trade_count = 999
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.SELL, 1000, 10000, 5000,
        )
        assert ok is True

    def test_sell_allowed_below_emergency_stop(self):
        r = _make_rules()
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.SELL, 1000, 100, 100,  # portfolio far below 5000
        )
        assert ok is True

    def test_sell_allowed_for_blacklisted_pair(self):
        r = _make_rules({"never_trade_pairs": ["BTC-EUR"]})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.SELL, 1000, 10000, 5000,
        )
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# BUY rules enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestBuyRules:
    def test_valid_buy_passes(self):
        r = _make_rules()
        ok, violations, needs_approval = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 100, 10000, 5000, has_stop_loss=True,
        )
        assert ok is True
        assert violations == []
        assert needs_approval is False

    def test_max_single_trade_violation(self):
        r = _make_rules({"max_single_trade": 100})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 150, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "max_single_trade" for v in violations)

    def test_max_daily_spend_violation(self):
        r = _make_rules({"max_daily_spend": 100})
        r._last_reset_date = datetime.now(timezone.utc)
        r._daily_spend = 80
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 30, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "max_daily_spend" for v in violations)

    def test_max_daily_loss_violation(self):
        r = _make_rules({"max_daily_loss": 100})
        r._last_reset_date = datetime.now(timezone.utc)
        r._daily_loss = 100
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "max_daily_loss" for v in violations)

    def test_max_trades_per_day_violation(self):
        r = _make_rules({"max_trades_per_day": 5})
        r._last_reset_date = datetime.now(timezone.utc)
        r._daily_trade_count = 5
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "max_trades_per_day" for v in violations)

    def test_min_trade_interval_violation(self):
        r = _make_rules({"min_trade_interval_seconds": 120})
        r._last_trade_time = datetime.now(timezone.utc) - timedelta(seconds=30)
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "min_trade_interval" for v in violations)

    def test_min_trade_interval_ok_after_wait(self):
        r = _make_rules({"min_trade_interval_seconds": 5})
        r._last_trade_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert not any(v.rule_name == "min_trade_interval" for v in violations)

    def test_max_cash_per_trade_violation(self):
        r = _make_rules({"max_cash_per_trade_pct": 0.10})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 200, 10000, 1000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "max_cash_per_trade" for v in violations)

    def test_emergency_stop_violation(self):
        r = _make_rules({"emergency_stop_portfolio": 5000})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 4000, 4000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "emergency_stop" for v in violations)

    def test_portfolio_risk_violation(self):
        r = _make_rules({"max_portfolio_risk_pct": 0.05})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 600, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "max_portfolio_risk" for v in violations)

    def test_stop_loss_required(self):
        r = _make_rules({"always_use_stop_loss": True})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=False,
        )
        assert ok is False
        assert any(v.rule_name == "always_use_stop_loss" for v in violations)

    def test_stop_loss_not_required_when_disabled(self):
        r = _make_rules({"always_use_stop_loss": False})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=False,
        )
        assert not any(v.rule_name == "always_use_stop_loss" for v in violations)

    def test_blacklisted_pair(self):
        r = _make_rules({"never_trade_pairs": ["SCAM-EUR"]})
        ok, violations, _ = r.check_trade(
            "SCAM-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "never_trade_pair" for v in violations)

    def test_whitelist_violation(self):
        r = _make_rules({"only_trade_pairs": ["BTC-EUR", "ETH-EUR"]})
        ok, violations, _ = r.check_trade(
            "DOGE-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert ok is False
        assert any(v.rule_name == "only_trade_pairs" for v in violations)

    def test_whitelist_allows_listed_pair(self):
        r = _make_rules({"only_trade_pairs": ["BTC-EUR"]})
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert not any(v.rule_name == "only_trade_pairs" for v in violations)


# ═══════════════════════════════════════════════════════════════════════════
# Approval threshold
# ═══════════════════════════════════════════════════════════════════════════

class TestApproval:
    def test_needs_approval_above_threshold(self):
        r = _make_rules({"require_approval_above": 100})
        ok, violations, needs_approval = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 150, 10000, 5000, has_stop_loss=True,
        )
        assert ok is True
        assert needs_approval is True

    def test_no_approval_below_threshold(self):
        r = _make_rules({"require_approval_above": 200})
        ok, violations, needs_approval = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 50, 10000, 5000, has_stop_loss=True,
        )
        assert ok is True
        assert needs_approval is False

    def test_no_approval_for_sell(self):
        r = _make_rules({"require_approval_above": 10})
        ok, _, needs_approval = r.check_trade(
            "BTC-EUR", TradeAction.SELL, 1000, 10000, 5000,
        )
        assert ok is True
        assert needs_approval is False


# ═══════════════════════════════════════════════════════════════════════════
# Daily counter tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestDailyTracking:
    def test_record_buy_updates_spend_and_count(self):
        r = _make_rules()
        r.record_trade(100, action="buy")
        assert r._daily_spend == pytest.approx(100)
        assert r._daily_trade_count == 1

    def test_record_sell_only_updates_count(self):
        r = _make_rules()
        r.record_trade(500, action="sell")
        assert r._daily_spend == pytest.approx(0)
        assert r._daily_trade_count == 1

    def test_record_loss(self):
        r = _make_rules()
        r.record_loss(50)
        assert r.daily_loss == pytest.approx(50)
        r.record_loss(-30)  # Should use abs
        assert r.daily_loss == pytest.approx(80)

    def test_daily_reset(self):
        r = _make_rules()
        r._daily_spend = 1000
        r._daily_loss = 200
        r._daily_trade_count = 15
        r._last_reset_date = datetime.now(timezone.utc) - timedelta(days=2)
        # Trigger reset
        r._reset_daily_if_needed()
        assert r._daily_spend == 0
        assert r._daily_loss == 0
        assert r._daily_trade_count == 0

    def test_legacy_usd_value_kwarg(self):
        r = _make_rules()
        r.record_trade(0, action="buy", usd_value=250)
        assert r._daily_spend == pytest.approx(250)

    def test_get_status(self):
        r = _make_rules()
        r.record_trade(100, action="buy")
        status = r.get_status()
        assert status["daily_spend"] == pytest.approx(100)
        assert status["trades_today"] == 1
        assert status["daily_spend_remaining"] == pytest.approx(1900)


# ═══════════════════════════════════════════════════════════════════════════
# Tier-scaled limits (with PortfolioScaler)
# ═══════════════════════════════════════════════════════════════════════════

class TestTierScaling:
    def test_effective_limits_without_scaler(self):
        r = _make_rules()
        cash_pct, risk_pct, emerg, max_trade = r._get_effective_limits(10000)
        assert cash_pct == 0.25
        assert risk_pct == 0.20
        assert emerg == 5000
        assert max_trade == 500

    def test_effective_limits_with_micro_tier(self):
        from src.core.portfolio_scaler import PortfolioScaler
        r = _make_rules()
        scaler = PortfolioScaler({"trading": {"portfolio_scaling": True}})
        scaler.update(30)
        r.set_portfolio_scaler(scaler)
        cash_pct, risk_pct, emerg, max_trade = r._get_effective_limits(30)
        assert cash_pct == 0.50  # MICRO tier
        assert risk_pct == 0.50
        # Emergency should be HWM-based for MICRO
        assert emerg > 0

    def test_dynamic_max_trade_scales(self):
        from src.core.portfolio_scaler import PortfolioScaler
        r = _make_rules({"max_single_trade": 500})
        scaler = PortfolioScaler({"trading": {"portfolio_scaling": True}})
        scaler.update(10000)
        r.set_portfolio_scaler(scaler)
        _, _, _, max_trade = r._get_effective_limits(10000)
        # Should be min(500, 10000 * tier_cash_pct)
        assert max_trade <= 500


# ═══════════════════════════════════════════════════════════════════════════
# RuleViolation
# ═══════════════════════════════════════════════════════════════════════════

class TestRuleViolation:
    def test_str(self):
        v = RuleViolation("test_rule", "Some violation", "details here")
        s = str(v)
        assert "test_rule" in s
        assert "Some violation" in s

    def test_timestamp(self):
        v = RuleViolation("test", "desc")
        assert v.timestamp is not None


# ═══════════════════════════════════════════════════════════════════════════
# Runtime parameter updates
# ═══════════════════════════════════════════════════════════════════════════

class TestRuntimeUpdates:
    def test_get_all_rules(self):
        r = _make_rules()
        all_rules = r.get_all_rules()
        assert "max_single_trade" in all_rules
        assert "max_daily_loss" in all_rules
        assert all_rules["max_single_trade"] == 500

    def test_get_rules_text(self):
        r = _make_rules()
        text = r.get_rules_text()
        assert "Max single trade" in text
        assert "Max daily spend" in text
