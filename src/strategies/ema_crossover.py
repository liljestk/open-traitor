"""
EMA Crossover Trend-Following Strategy

Entry signals:
  - Golden Cross: EMA-50 crosses above EMA-200 (bullish)
  - Confirmed by: ADX > 25 (trending market) + volume above average
  - Trailing stop exit or reverse crossover (Death Cross)

This is a classic trend-following strategy. It suffers in sideways markets
(hence the ADX filter) but captures large directional moves.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.strategies.base import BaseStrategy, StrategySignal, StrategyType
from src.analysis.technical import TechnicalAnalyzer
from src.utils.logger import get_logger

logger = get_logger("strategy.ema_crossover")


class EMACrossoverStrategy(BaseStrategy):
    """
    EMA Crossover with ADX trend filter.

    Parameters (via config):
      fast_ema: 50 (default)
      slow_ema: 200 (default)
      adx_threshold: 25 (minimum ADX to confirm trend)
      volume_threshold: 1.2 (minimum volume ratio to confirm)
      atr_stop_multiplier: 2.0 (stop-loss = entry - ATR * multiplier)
      atr_target_multiplier: 3.0 (take-profit = entry + ATR * multiplier)
    """

    def __init__(self, config: dict):
        super().__init__(
            name="ema_crossover",
            strategy_type=StrategyType.TREND_FOLLOWING,
            config=config,
        )
        strat_cfg = config.get("strategies", {}).get("ema_crossover", {})
        self.fast_ema = strat_cfg.get("fast_ema", 50)
        self.slow_ema = strat_cfg.get("slow_ema", 200)
        self.adx_threshold = strat_cfg.get("adx_threshold", 25)
        self.volume_threshold = strat_cfg.get("volume_threshold", 1.2)
        self.atr_stop_mult = strat_cfg.get("atr_stop_multiplier", 2.0)
        self.atr_target_mult = strat_cfg.get("atr_target_multiplier", 3.0)
        self.min_confidence = strat_cfg.get("min_confidence", 0.5)

    def preferred_regime(self) -> str:
        return "trending"

    def generate_signal(
        self,
        pair: str,
        candles: list[dict],
        analysis: dict,
        context: Optional[dict] = None,
    ) -> StrategySignal:
        """Generate signal based on EMA crossover with ADX confirmation."""
        indicators = analysis.get("indicators", {})
        current_price = analysis.get("current_price", 0)

        # Get EMAs
        fast_ema_val = indicators.get(f"ema_{self.fast_ema}")
        slow_ema_val = indicators.get(f"ema_{self.slow_ema}")
        adx = indicators.get("adx")
        plus_di = indicators.get("plus_di")
        minus_di = indicators.get("minus_di")
        volume_ratio = indicators.get("volume_ratio", 1.0)
        atr = indicators.get("atr")

        # Detect regime
        regime, regime_strength = self.detect_regime(analysis)

        # Default: hold
        if not fast_ema_val or not slow_ema_val or not current_price:
            return StrategySignal(
                strategy_name=self.name,
                strategy_type=self.strategy_type,
                action="hold",
                pair=pair,
                confidence=0,
                reasoning="Insufficient EMA data",
                market_regime=regime,
                regime_strength=regime_strength,
            )

        # Need historical EMAs from candle data to detect crossover
        analyzer = TechnicalAnalyzer(self.config.get("analysis", {}).get("technical", {}))
        df = analyzer.candles_to_dataframe(candles)
        if len(df) < self.slow_ema + 2:
            return StrategySignal(
                strategy_name=self.name,
                strategy_type=self.strategy_type,
                action="hold",
                pair=pair,
                confidence=0,
                reasoning=f"Need at least {self.slow_ema + 2} candles",
                market_regime=regime,
                regime_strength=regime_strength,
            )

        fast = df["close"].ewm(span=self.fast_ema, adjust=False).mean()
        slow = df["close"].ewm(span=self.slow_ema, adjust=False).mean()

        # Crossover detection (current vs previous candle)
        fast_now = float(fast.iloc[-1])
        fast_prev = float(fast.iloc[-2])
        slow_now = float(slow.iloc[-1])
        slow_prev = float(slow.iloc[-2])

        golden_cross = fast_prev <= slow_prev and fast_now > slow_now
        death_cross = fast_prev >= slow_prev and fast_now < slow_now
        fast_above = fast_now > slow_now

        # Confidence scoring
        confidence = 0.0
        reasons = []
        indicators_used = [f"EMA-{self.fast_ema}", f"EMA-{self.slow_ema}"]

        # ADX filter
        adx_ok = adx is not None and adx >= self.adx_threshold
        if adx_ok:
            indicators_used.append("ADX")

        # Volume confirmation
        vol_ok = volume_ratio >= self.volume_threshold

        if golden_cross:
            action = "buy"
            confidence = 0.6
            reasons.append(f"Golden Cross: EMA-{self.fast_ema} crossed above EMA-{self.slow_ema}")

            if adx_ok:
                confidence += 0.15
                reasons.append(f"ADX={adx:.1f} confirms trend strength")
            else:
                confidence -= 0.1
                reasons.append(f"ADX={adx:.1f} — weak trend, lower confidence" if adx else "ADX unavailable")

            if vol_ok:
                confidence += 0.1
                reasons.append(f"Volume ratio {volume_ratio:.1f}x confirms buying pressure")

            # DI direction alignment
            if plus_di and minus_di and plus_di > minus_di:
                confidence += 0.1
                reasons.append("+DI > -DI, directional agreement")

        elif death_cross:
            action = "sell"
            confidence = 0.55
            reasons.append(f"Death Cross: EMA-{self.fast_ema} crossed below EMA-{self.slow_ema}")

            if adx_ok:
                confidence += 0.15
                reasons.append(f"ADX={adx:.1f} confirms trend strength")

            if minus_di and plus_di and minus_di > plus_di:
                confidence += 0.1
                reasons.append("-DI > +DI, bearish direction confirmed")

        elif fast_above and adx_ok:
            # No crossover but trend is intact — could be continuation
            action = "hold"
            confidence = 0.3
            reasons.append(f"Uptrend intact (EMA-{self.fast_ema} > EMA-{self.slow_ema}) but no fresh crossover")
        else:
            action = "hold"
            confidence = 0.0
            reasons.append("No crossover detected")

        # Clamp confidence
        confidence = max(0.0, min(1.0, confidence))

        # Stop-loss and take-profit from ATR
        stop_loss = 0.0
        take_profit = 0.0
        if atr and current_price > 0 and action == "buy":
            stop_loss = current_price - (atr * self.atr_stop_mult)
            take_profit = current_price + (atr * self.atr_target_mult)
            indicators_used.append("ATR")

        return StrategySignal(
            strategy_name=self.name,
            strategy_type=self.strategy_type,
            action=action,
            pair=pair,
            confidence=confidence,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reasoning=" | ".join(reasons),
            indicators_used=indicators_used,
            market_regime=regime,
            regime_strength=regime_strength,
        )
