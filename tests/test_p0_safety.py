"""
Tests for P0 safety-hardening changes:
  1. AbsoluteRules.portfolio_drawdown_halt_pct — portfolio-level kill switch
  2. TrailingStop.min_hold_minutes — suppress ping-pong exits
  3. src/models/llm_responses — Pydantic schema validation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.rules import AbsoluteRules
from src.core.trailing_stop import TrailingStop, TrailingStopManager
from src.models.llm_responses import (
    validate_market_analyst,
    validate_strategist,
)
from src.models.trade import TradeAction


def _make_rules(**over) -> AbsoluteRules:
    cfg = {
        "max_single_trade": 500,
        "max_daily_spend": 2000,
        "max_daily_loss": 300,
        "max_portfolio_risk_pct": 0.20,
        "require_approval_above": 9999,   # disable approval branch for tests
        "min_trade_interval_seconds": 0,
        "max_trades_per_day": 20,
        "max_cash_per_trade_pct": 1.0,
        "emergency_stop_portfolio": 0,
        "always_use_stop_loss": False,
        "max_stop_loss_pct": 0.05,
        "portfolio_drawdown_halt_pct": 0.15,
    }
    cfg.update(over)
    return AbsoluteRules(cfg)


# ────────────────────────────────────────────────────────────────────
# P0-1: Portfolio drawdown kill-switch
# ────────────────────────────────────────────────────────────────────

class TestDrawdownKillSwitch:
    def test_no_halt_when_portfolio_is_climbing(self):
        r = _make_rules()
        for pv in (1000, 1100, 1200, 1300):
            ok, violations, _ = r.check_trade(
                "BTC-EUR", TradeAction.BUY, 100, pv, pv,
            )
            assert ok is True, f"unexpected halt at pv={pv}: {violations}"
        assert r._portfolio_peak == 1300

    def test_halt_triggered_at_threshold(self):
        r = _make_rules(portfolio_drawdown_halt_pct=0.15)
        # Establish peak
        r.check_trade("BTC-EUR", TradeAction.BUY, 100, 1000, 1000)
        # Drop 15% → should halt
        ok, violations, _ = r.check_trade(
            "BTC-EUR", TradeAction.BUY, 100, 850, 850,
        )
        assert ok is False
        assert any(v.rule_name == "portfolio_drawdown_halt" for v in violations)

    def test_halt_does_not_block_sells(self):
        r = _make_rules()
        r.check_trade("BTC-EUR", TradeAction.BUY, 100, 1000, 1000)
        # Deep drawdown
        ok, _, _ = r.check_trade(
            "BTC-EUR", TradeAction.SELL, 100, 500, 500,
        )
        assert ok is True, "sells must never be blocked by drawdown halt"

    def test_halt_clears_on_new_peak(self):
        r = _make_rules(portfolio_drawdown_halt_pct=0.15)
        r.check_trade("BTC-EUR", TradeAction.BUY, 100, 1000, 1000)
        # Trigger halt
        ok, _, _ = r.check_trade("BTC-EUR", TradeAction.BUY, 100, 800, 800)
        assert ok is False
        assert r._drawdown_halted is True
        # Recover above previous peak
        ok, _, _ = r.check_trade("BTC-EUR", TradeAction.BUY, 100, 1100, 1100)
        assert ok is True
        assert r._drawdown_halted is False

    def test_halt_disabled_when_pct_is_zero(self):
        r = _make_rules(portfolio_drawdown_halt_pct=0.0)
        r.check_trade("BTC-EUR", TradeAction.BUY, 100, 1000, 1000)
        ok, _, _ = r.check_trade("BTC-EUR", TradeAction.BUY, 100, 500, 500)
        assert ok is True

    def test_status_exposes_drawdown_fields(self):
        r = _make_rules()
        r.check_trade("BTC-EUR", TradeAction.BUY, 100, 1000, 1000)
        s = r.get_status()
        assert s["portfolio_peak"] == 1000
        assert s["drawdown_halted"] is False
        assert "portfolio_drawdown_halt_pct" in s


# ────────────────────────────────────────────────────────────────────
# P0-2: Trailing-stop min-hold period
# ────────────────────────────────────────────────────────────────────

class TestMinHoldPeriod:
    def test_default_zero_preserves_legacy_behavior(self):
        ts = TrailingStop("BTC-EUR", 100.0, trail_pct=0.03)
        assert ts.min_hold_minutes == 0.0
        assert ts.update(96.0) is True  # immediate trigger

    def test_min_hold_suppresses_early_stop_out(self):
        ts = TrailingStop(
            "BTC-EUR", 100.0, trail_pct=0.03, min_hold_minutes=30.0,
        )
        # Fresh position: trigger price hit but min_hold not elapsed.
        assert ts.update(95.0) is False
        assert ts.triggered is False

    def test_min_hold_expires_then_trigger_fires(self):
        ts = TrailingStop(
            "BTC-EUR", 100.0, trail_pct=0.03, min_hold_minutes=30.0,
        )
        # Backdate created_at beyond the min_hold window.
        ts.created_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        assert ts.update(95.0) is True
        assert ts.triggered is True

    def test_manager_propagates_min_hold(self):
        mgr = TrailingStopManager(default_trail_pct=0.03, min_hold_minutes=15.0)
        stop = mgr.add_stop("BTC-EUR", entry_price=100.0)
        assert stop.min_hold_minutes == 15.0

    def test_tier_exits_are_not_suppressed_by_min_hold(self):
        # Tier exits should still fire immediately — they lock in profit.
        ts = TrailingStop(
            "BTC-EUR", 100.0, trail_pct=0.03,
            min_hold_minutes=30.0,
            tiers=[{"trigger_pct": 0.03, "exit_fraction": 0.5}],
            total_quantity=1.0,
        )
        ts.update(103.0)  # hit +3% tier
        exits = ts.get_pending_tier_exits()
        assert len(exits) == 1


# ────────────────────────────────────────────────────────────────────
# P0-3: Pydantic LLM response schemas
# ────────────────────────────────────────────────────────────────────

class TestMarketAnalystSchema:
    def test_valid_response_round_trips(self):
        raw = {
            "signal_type": "buy",
            "confidence": 0.72,
            "market_condition": "bullish",
            "sentiment_overall": "bullish",
            "sentiment_score": 0.4,
            "key_factors": ["macd_bullish", "rsi_ok"],
            "reasoning": "Clean breakout",
            "suggested_entry": 100.0,
            "suggested_stop_loss": 97.0,
            "suggested_take_profit": 106.0,
        }
        out, err = validate_market_analyst(raw)
        assert err is None
        assert out["signal_type"] == "buy"
        assert out["confidence"] == pytest.approx(0.72)

    def test_confidence_out_of_range_is_clamped(self):
        out, err = validate_market_analyst({"confidence": 1.9})
        assert err is None
        assert out["confidence"] == 1.0
        out2, _ = validate_market_analyst({"confidence": -0.5})
        assert out2["confidence"] == 0.0

    def test_invalid_signal_type_collapses_to_neutral(self):
        out, err = validate_market_analyst({"signal_type": "moon_soon"})
        assert err is None
        assert out["signal_type"] == "neutral"

    def test_negative_price_is_dropped(self):
        out, err = validate_market_analyst(
            {"suggested_entry": -1.0, "suggested_stop_loss": 0}
        )
        assert err is None
        assert out["suggested_entry"] is None
        assert out["suggested_stop_loss"] is None

    def test_nan_price_is_dropped(self):
        out, err = validate_market_analyst({"suggested_entry": float("nan")})
        assert err is None
        assert out["suggested_entry"] is None

    def test_non_dict_input_rejected(self):
        out, err = validate_market_analyst("not a dict")  # type: ignore[arg-type]
        assert err is not None

    def test_extra_fields_ignored(self):
        out, err = validate_market_analyst(
            {"signal_type": "buy", "hallucinated_field": "boom"}
        )
        assert err is None
        assert "hallucinated_field" not in out


class TestStrategistSchema:
    def test_valid_buy_passes(self):
        raw = {
            "action": "buy", "pair": "BTC-EUR", "confidence": 0.8,
            "quote_amount": 50.0, "stop_loss_price": 95.0,
            "take_profit_price": 110.0, "reasoning": "clean setup",
        }
        out, err = validate_strategist(raw)
        assert err is None
        assert out["action"] == "buy"

    def test_unknown_action_collapses_to_hold(self):
        out, err = validate_strategist({"action": "YOLO"})
        assert err is None
        assert out["action"] == "hold"

    def test_missing_action_defaults_to_hold(self):
        out, err = validate_strategist({})
        assert err is None
        assert out["action"] == "hold"

    def test_confidence_clamped_to_unit_interval(self):
        out, _ = validate_strategist({"action": "buy", "confidence": 5})
        assert out["confidence"] == 1.0
        out2, _ = validate_strategist({"action": "buy", "confidence": -2})
        assert out2["confidence"] == 0.0

    def test_negative_prices_dropped(self):
        out, _ = validate_strategist({
            "action": "buy", "stop_loss_price": -5, "quantity": 0,
        })
        assert out["stop_loss_price"] is None
        assert out["quantity"] is None

    def test_bool_confidence_rejected_as_number(self):
        # Bool is subclass of int; ensure we don't silently accept True→1.0
        out, _ = validate_strategist({"action": "buy", "confidence": True})
        assert out["confidence"] == 0.0

    def test_non_dict_input_rejected(self):
        out, err = validate_strategist(["buy", "BTC-EUR"])  # type: ignore[arg-type]
        assert err is not None
