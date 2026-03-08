"""
Tests for style modifiers — orthogonal add-ons to presets.

Modifiers:
  prefer_maker         — Force limit orders for crypto buys (no-op on equity)
  high_conviction_only — Only trade strong_buy/strong_sell signals
  wider_targets        — TP ×2.0, SL ×1.33
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.risk_manager import RiskManagerAgent
from src.agents.executor import ExecutorAgent
from src.core.rules import AbsoluteRules


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_config(modifiers: list[str] | None = None, **overrides) -> dict:
    cfg = {
        "trading": {
            "min_confidence": 0.55,
            "min_signal_confidence": 0.65,
            "max_open_positions": 8,
            "style_modifiers": modifiers or [],
        },
        "risk": {
            "stop_loss_pct": 0.04,
            "take_profit_pct": 0.06,
            "max_position_pct": 0.15,
            "use_kelly_criterion": False,
        },
    }
    cfg.update(overrides)
    return cfg


def _make_rules() -> AbsoluteRules:
    return AbsoluteRules({
        "max_single_trade": 500,
        "max_daily_spend": 2000,
        "max_daily_loss": 300,
        "max_portfolio_risk_pct": 0.20,
        "require_approval_above": 200,
        "min_trade_interval_seconds": 0,
        "max_trades_per_day": 20,
        "max_cash_per_trade_pct": 0.50,
        "always_use_stop_loss": True,
        "max_stop_loss_pct": 0.10,
        "emergency_stop_portfolio": 0,
    })


def _make_risk_manager(modifiers: list[str] | None = None) -> RiskManagerAgent:
    config = _make_config(modifiers)
    llm = MagicMock()
    state = MagicMock()
    state.open_positions = {}
    state.current_prices = {"BTC-EUR": 50000}
    rules = _make_rules()
    return RiskManagerAgent(llm, state, config, rules, portfolio_scaler=None)


def _make_executor(modifiers: list[str] | None = None, asset_class: str = "crypto") -> ExecutorAgent:
    config = _make_config(modifiers)
    llm = MagicMock()
    state = MagicMock()
    state.open_positions = {}
    exchange = MagicMock()
    exchange.asset_class = asset_class
    rules = _make_rules()
    return ExecutorAgent(llm, state, config, exchange, rules)


# ═══════════════════════════════════════════════════════════════════════════
# wider_targets modifier
# ═══════════════════════════════════════════════════════════════════════════

class TestWiderTargets:

    @pytest.mark.asyncio
    async def test_wider_targets_doubles_tp_and_widens_sl(self):
        """wider_targets should multiply TP by 2.0 and SL by 1.33."""
        rm = _make_risk_manager(["wider_targets"])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "strong_buy",
        })
        assert result["approved"] is True
        # Default: TP=6%, SL=4% → with modifier: TP=12%, SL=5.32%
        expected_tp = 50000 * (1 + 0.06 * 2.0)
        expected_sl = 50000 * (1 - 0.04 * 1.33)
        assert abs(result["take_profit"] - expected_tp) < 1
        assert abs(result["stop_loss"] - expected_sl) < 1

    @pytest.mark.asyncio
    async def test_no_modifier_uses_default_tp_sl(self):
        """Without wider_targets, TP/SL should use tier defaults."""
        rm = _make_risk_manager([])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "strong_buy",
        })
        assert result["approved"] is True
        expected_tp = 50000 * (1 + 0.06)
        expected_sl = 50000 * (1 - 0.04)
        assert abs(result["take_profit"] - expected_tp) < 1
        assert abs(result["stop_loss"] - expected_sl) < 1


# ═══════════════════════════════════════════════════════════════════════════
# high_conviction_only modifier
# ═══════════════════════════════════════════════════════════════════════════

class TestHighConvictionOnly:

    @pytest.mark.asyncio
    async def test_rejects_buy_signal(self):
        """high_conviction_only should reject 'buy' signals."""
        rm = _make_risk_manager(["high_conviction_only"])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "buy",
        })
        assert result["approved"] is False
        assert "high_conviction_only" in result["reason"]

    @pytest.mark.asyncio
    async def test_rejects_weak_buy_signal(self):
        """high_conviction_only should reject 'weak_buy' signals."""
        rm = _make_risk_manager(["high_conviction_only"])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "weak_buy",
        })
        assert result["approved"] is False

    @pytest.mark.asyncio
    async def test_allows_strong_buy_signal(self):
        """high_conviction_only should allow 'strong_buy' signals."""
        rm = _make_risk_manager(["high_conviction_only"])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "strong_buy",
        })
        assert result["approved"] is True

    @pytest.mark.asyncio
    async def test_sell_always_allowed(self):
        """Sell orders should never be blocked by modifiers."""
        rm = _make_risk_manager(["high_conviction_only"])
        proposal = {
            "action": "sell",
            "pair": "BTC-EUR",
            "quantity": 0.001,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "sell",
        })
        assert result["approved"] is True


# ═══════════════════════════════════════════════════════════════════════════
# prefer_maker modifier
# ═══════════════════════════════════════════════════════════════════════════

class TestPreferMaker:

    def test_forces_limit_on_crypto_buy(self):
        """prefer_maker should force limit orders for crypto buys."""
        executor = _make_executor(["prefer_maker"], asset_class="crypto")
        trade_info = {
            "action": "buy",
            "confidence": 0.95,  # normally → market (above urgency threshold)
            "reasoning": "strong momentum entry",
        }
        assert executor._should_use_limit(trade_info) is True

    def test_no_effect_on_equity(self):
        """prefer_maker should NOT force limit orders on equity (flat fees)."""
        executor = _make_executor(["prefer_maker"], asset_class="equity")
        trade_info = {
            "action": "buy",
            "confidence": 0.95,
            "reasoning": "strong momentum entry",
        }
        # Equity with high confidence → should fall through to normal logic → market
        assert executor._should_use_limit(trade_info) is False

    def test_no_limit_on_sell(self):
        """Sells should remain market orders even with prefer_maker."""
        executor = _make_executor(["prefer_maker"], asset_class="crypto")
        trade_info = {
            "action": "sell",
            "confidence": 0.50,
            "reasoning": "take profit exit",
        }
        assert executor._should_use_limit(trade_info) is False

    def test_no_limit_on_stop_loss(self):
        """Stop-loss buys should still use market orders."""
        executor = _make_executor(["prefer_maker"], asset_class="crypto")
        trade_info = {
            "action": "buy",
            "confidence": 0.60,
            "reasoning": "stop_loss re-entry after flush",
        }
        # Stop-loss keyword in reasoning → should not use limit
        assert executor._should_use_limit(trade_info) is False

    def test_without_modifier_high_confidence_uses_market(self):
        """Without prefer_maker, high-confidence buys should use market orders."""
        executor = _make_executor([], asset_class="crypto")
        trade_info = {
            "action": "buy",
            "confidence": 0.95,
            "reasoning": "strong momentum entry",
        }
        assert executor._should_use_limit(trade_info) is False


# ═══════════════════════════════════════════════════════════════════════════
# Multiple modifiers stacked
# ═══════════════════════════════════════════════════════════════════════════

class TestStackedModifiers:

    @pytest.mark.asyncio
    async def test_wider_targets_and_high_conviction(self):
        """Both modifiers active: strong_buy allowed with wider TP/SL."""
        rm = _make_risk_manager(["wider_targets", "high_conviction_only"])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "strong_buy",
        })
        assert result["approved"] is True
        # TP should be widened
        expected_tp = 50000 * (1 + 0.06 * 2.0)
        assert abs(result["take_profit"] - expected_tp) < 1

    @pytest.mark.asyncio
    async def test_high_conviction_blocks_buy_even_with_wider(self):
        """Stacked modifiers: buy signal rejected despite wider_targets."""
        rm = _make_risk_manager(["wider_targets", "high_conviction_only"])
        proposal = {
            "action": "buy",
            "pair": "BTC-EUR",
            "quote_amount": 50,
            "current_price": 50000,
            "confidence": 0.80,
        }
        result = await rm.run({
            "proposal": proposal,
            "portfolio_value": 1000,
            "cash_balance": 500,
            "signal_type": "buy",
        })
        assert result["approved"] is False

    def test_all_three_modifiers_crypto(self):
        """All three modifiers active on crypto: limit forced for strong buy."""
        executor = _make_executor(
            ["prefer_maker", "high_conviction_only", "wider_targets"],
            asset_class="crypto",
        )
        trade_info = {
            "action": "buy",
            "confidence": 0.95,
            "reasoning": "strong momentum entry",
        }
        assert executor._should_use_limit(trade_info) is True


# ═══════════════════════════════════════════════════════════════════════════
# Settings manager metadata
# ═══════════════════════════════════════════════════════════════════════════

class TestModifierMetadata:

    def test_valid_modifiers_defined(self):
        from src.utils.settings_manager import VALID_STYLE_MODIFIERS, STYLE_MODIFIER_META
        assert "prefer_maker" in VALID_STYLE_MODIFIERS
        assert "high_conviction_only" in VALID_STYLE_MODIFIERS
        assert "wider_targets" in VALID_STYLE_MODIFIERS
        # Every valid modifier has metadata
        for mod in VALID_STYLE_MODIFIERS:
            assert mod in STYLE_MODIFIER_META
            meta = STYLE_MODIFIER_META[mod]
            assert "label" in meta
            assert "desc" in meta
            assert "exchanges" in meta

    def test_prefer_maker_crypto_only(self):
        from src.utils.settings_manager import STYLE_MODIFIER_META
        assert STYLE_MODIFIER_META["prefer_maker"]["exchanges"] == ["crypto"]

    def test_universal_modifiers(self):
        from src.utils.settings_manager import STYLE_MODIFIER_META
        for mod in ["high_conviction_only", "wider_targets"]:
            exchanges = STYLE_MODIFIER_META[mod]["exchanges"]
            assert "crypto" in exchanges
            assert "equity" in exchanges

    def test_modifiers_summary_empty(self):
        from src.utils.settings_manager import get_style_modifiers_summary
        assert get_style_modifiers_summary([]) == ""

    def test_modifiers_summary_with_active(self):
        from src.utils.settings_manager import get_style_modifiers_summary
        summary = get_style_modifiers_summary(["wider_targets", "prefer_maker"])
        assert "Wider Targets" in summary
        assert "Prefer Limit Orders" in summary
