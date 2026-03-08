"""
Tests for src/strategies/ — EMA Crossover and Bollinger Reversion strategies.
"""
from __future__ import annotations

import pytest

from src.strategies.base import BaseStrategy, StrategySignal, StrategyType
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.bollinger_reversion import BollingerReversionStrategy


def _make_config(strategy_overrides=None):
    cfg = {
        "strategies": {
            "ema_crossover": {},
            "bollinger_reversion": {},
        },
        "analysis": {"technical": {}},
    }
    if strategy_overrides:
        cfg["strategies"].update(strategy_overrides)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
# StrategySignal
# ═══════════════════════════════════════════════════════════════════════════

class TestStrategySignal:
    def test_to_dict(self):
        sig = StrategySignal(
            strategy_name="test",
            strategy_type=StrategyType.TREND_FOLLOWING,
            action="buy",
            pair="BTC-EUR",
            confidence=0.75,
        )
        d = sig.to_dict()
        assert d["strategy_name"] == "test"
        assert d["action"] == "buy"
        assert d["confidence"] == 0.75

    def test_is_actionable(self):
        sig = StrategySignal(
            strategy_name="test",
            strategy_type=StrategyType.TREND_FOLLOWING,
            action="buy", pair="BTC-EUR", confidence=0.5,
        )
        assert sig.is_actionable is True

    def test_hold_not_actionable(self):
        sig = StrategySignal(
            strategy_name="test",
            strategy_type=StrategyType.TREND_FOLLOWING,
            action="hold", pair="BTC-EUR", confidence=0.5,
        )
        assert sig.is_actionable is False

    def test_zero_confidence_not_actionable(self):
        sig = StrategySignal(
            strategy_name="test",
            strategy_type=StrategyType.TREND_FOLLOWING,
            action="buy", pair="BTC-EUR", confidence=0,
        )
        assert sig.is_actionable is False


# ═══════════════════════════════════════════════════════════════════════════
# BaseStrategy regime detection
# ═══════════════════════════════════════════════════════════════════════════

class TestRegimeDetection:
    def _make_strat(self):
        return EMACrossoverStrategy(_make_config())

    def test_strong_trend(self):
        s = self._make_strat()
        regime, conf = s.detect_regime({
            "indicators": {"adx": 35, "atr": 1000},
            "current_price": 50000,
        })
        assert regime == "strong_trend"
        assert conf > 0.5

    def test_ranging_market(self):
        s = self._make_strat()
        regime, conf = s.detect_regime({
            "indicators": {"adx": 15, "atr": 500},
            "current_price": 50000,
        })
        assert regime == "ranging"

    def test_volatile_market(self):
        s = self._make_strat()
        regime, conf = s.detect_regime({
            "indicators": {"adx": 30, "atr": 3000},
            "current_price": 50000,
        })
        assert regime == "volatile"

    def test_unknown_without_adx(self):
        s = self._make_strat()
        regime, conf = s.detect_regime({"indicators": {}})
        assert regime == "unknown"
        assert conf == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# EMA Crossover
# ═══════════════════════════════════════════════════════════════════════════

class TestEMACrossover:
    def test_preferred_regime(self):
        s = EMACrossoverStrategy(_make_config())
        assert s.preferred_regime() == "trending"

    def test_insufficient_data_holds(self):
        s = EMACrossoverStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {"indicators": {}, "current_price": 0})
        assert sig.action == "hold"
        assert sig.confidence == 0

    def test_insufficient_candles_holds(self):
        s = EMACrossoverStrategy(_make_config())
        # Provide EMAs but not enough candles
        candles = [{"close": 100, "open": 100, "high": 101, "low": 99,
                     "volume": 1000, "time": "2025-01-01"}] * 10
        analysis = {
            "indicators": {"ema_50": 100, "ema_200": 100, "adx": 30},
            "current_price": 100,
        }
        sig = s.generate_signal("BTC-EUR", candles, analysis)
        assert sig.action == "hold"

    def test_name_and_type(self):
        s = EMACrossoverStrategy(_make_config())
        assert s.name == "ema_crossover"
        assert s.strategy_type == StrategyType.TREND_FOLLOWING


# ═══════════════════════════════════════════════════════════════════════════
# Bollinger Reversion
# ═══════════════════════════════════════════════════════════════════════════

class TestBollingerReversion:
    def test_preferred_regime(self):
        s = BollingerReversionStrategy(_make_config())
        assert s.preferred_regime() == "ranging"

    def test_trending_market_holds(self):
        s = BollingerReversionStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {
            "indicators": {"adx": 30},  # > 25 threshold
            "current_price": 50000,
        })
        assert sig.action == "hold"
        assert "trending" in sig.reasoning.lower() or "ADX" in sig.reasoning

    def test_insufficient_data_holds(self):
        s = BollingerReversionStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {
            "indicators": {"adx": 15},
            "current_price": 0,
        })
        assert sig.action == "hold"

    def test_oversold_generates_buy(self):
        s = BollingerReversionStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {
            "indicators": {
                "adx": 15,
                "rsi": 25,
                "bb_upper": 52000,
                "bb_lower": 48000,
                "bb_middle": 50000,
                "bb_signal": "oversold",
                "atr": 500,
            },
            "current_price": 47500,
        })
        assert sig.action == "buy"
        assert sig.confidence > 0.5

    def test_overbought_generates_sell(self):
        s = BollingerReversionStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {
            "indicators": {
                "adx": 15,
                "rsi": 75,
                "bb_upper": 52000,
                "bb_lower": 48000,
                "bb_middle": 50000,
                "bb_signal": "overbought",
                "atr": 500,
            },
            "current_price": 52500,
        })
        assert sig.action == "sell"
        assert sig.confidence > 0.4

    def test_within_bands_holds(self):
        s = BollingerReversionStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {
            "indicators": {
                "adx": 15,
                "rsi": 50,
                "bb_upper": 52000,
                "bb_lower": 48000,
                "bb_middle": 50000,
                "bb_signal": "neutral",
                "atr": 500,
            },
            "current_price": 50000,
        })
        assert sig.action == "hold"

    def test_stoch_rsi_boosts_confidence(self):
        s = BollingerReversionStrategy(_make_config())
        sig = s.generate_signal("BTC-EUR", [], {
            "indicators": {
                "adx": 15,
                "rsi": 25,
                "bb_upper": 52000,
                "bb_lower": 48000,
                "bb_middle": 50000,
                "bb_signal": "oversold",
                "atr": 500,
                "stoch_rsi_k": 10,
            },
            "current_price": 47500,
        })
        assert sig.confidence > 0.7  # RSI + StochRSI boost

    def test_name_and_type(self):
        s = BollingerReversionStrategy(_make_config())
        assert s.name == "bollinger_reversion"
        assert s.strategy_type == StrategyType.MEAN_REVERSION
