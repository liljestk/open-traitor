"""
Bollinger Band Mean-Reversion Strategy

Entry signals:
  - Price touches lower BB + RSI < 30 → long (oversold bounce)
  - Price touches upper BB + RSI > 70 → sell signal (overbought)
  - ADX < 25 confirms ranging market (mean-reversion is appropriate)

Exit:
  - Price crosses middle band (mean) or approaches opposite band
  - Hard stop-loss below lower BB (for longs)

This strategy is disabled when ADX > 25 (trending market), since
mean-reversion fails in strong trends.
"""

from __future__ import annotations

from typing import Optional

from src.strategies.base import BaseStrategy, StrategySignal, StrategyType
from src.utils.logger import get_logger

logger = get_logger("strategy.bollinger_reversion")


class BollingerReversionStrategy(BaseStrategy):
    """
    Bollinger Band mean-reversion with RSI confirmation and ADX regime filter.

    Parameters (via config):
      rsi_oversold: 30
      rsi_overbought: 70
      adx_max: 25 (disable in trending markets)
      stoch_rsi_oversold: 20 (additional Stochastic RSI confirmation)
      stoch_rsi_overbought: 80
      atr_stop_multiplier: 1.5
    """

    def __init__(self, config: dict):
        super().__init__(
            name="bollinger_reversion",
            strategy_type=StrategyType.MEAN_REVERSION,
            config=config,
        )
        strat_cfg = config.get("strategies", {}).get("bollinger_reversion", {})
        self.rsi_oversold = strat_cfg.get("rsi_oversold", 30)
        self.rsi_overbought = strat_cfg.get("rsi_overbought", 70)
        self.adx_max = strat_cfg.get("adx_max", 25)
        self.stoch_oversold = strat_cfg.get("stoch_rsi_oversold", 20)
        self.stoch_overbought = strat_cfg.get("stoch_rsi_overbought", 80)
        self.atr_stop_mult = strat_cfg.get("atr_stop_multiplier", 1.5)
        self.atr_target_mult = strat_cfg.get("atr_target_multiplier", 2.0)

    def preferred_regime(self) -> str:
        return "ranging"

    def generate_signal(
        self,
        pair: str,
        candles: list[dict],
        analysis: dict,
        context: Optional[dict] = None,
    ) -> StrategySignal:
        """Generate signal based on Bollinger Band + RSI mean reversion."""
        indicators = analysis.get("indicators", {})
        current_price = analysis.get("current_price", 0)

        # Read indicators
        rsi = indicators.get("rsi")
        bb_upper = indicators.get("bb_upper")
        bb_lower = indicators.get("bb_lower")
        bb_middle = indicators.get("bb_middle")
        bb_signal = indicators.get("bb_signal")
        adx = indicators.get("adx")
        atr = indicators.get("atr")
        stoch_k = indicators.get("stoch_rsi_k")
        obv_signal = indicators.get("obv_signal", "")
        volume_ratio = indicators.get("volume_ratio", 1.0)

        # Detect regime
        regime, regime_strength = self.detect_regime(analysis)

        # Regime filter: DON'T mean-revert in trending markets
        if adx is not None and adx > self.adx_max:
            return StrategySignal(
                strategy_name=self.name,
                strategy_type=self.strategy_type,
                action="hold",
                pair=pair,
                confidence=0,
                reasoning=f"ADX={adx:.1f} > {self.adx_max} — trending market, mean-reversion disabled",
                indicators_used=["ADX"],
                market_regime=regime,
                regime_strength=regime_strength,
            )

        if not current_price or bb_upper is None or bb_lower is None:
            return StrategySignal(
                strategy_name=self.name,
                strategy_type=self.strategy_type,
                action="hold",
                pair=pair,
                confidence=0,
                reasoning="Insufficient Bollinger Band data",
                market_regime=regime,
                regime_strength=regime_strength,
            )

        confidence = 0.0
        action = "hold"
        reasons = []
        indicators_used = ["BB"]

        # ── BUY: Price at/below lower band + RSI oversold ──
        if bb_signal == "oversold" or current_price <= bb_lower:
            action = "buy"
            confidence = 0.5
            reasons.append(f"Price ${current_price:,.2f} at/below lower BB ${bb_lower:,.2f}")

            if rsi is not None and rsi <= self.rsi_oversold:
                confidence += 0.2
                reasons.append(f"RSI={rsi:.1f} confirms oversold")
                indicators_used.append("RSI")
            elif rsi is not None and rsi <= 40:
                confidence += 0.05
                reasons.append(f"RSI={rsi:.1f} bearish but not extreme")

            # Stochastic RSI double-confirmation
            if stoch_k is not None and stoch_k <= self.stoch_oversold:
                confidence += 0.1
                reasons.append(f"Stoch RSI %K={stoch_k:.1f} confirms oversold")
                indicators_used.append("Stoch RSI")

            # OBV divergence (bullish divergence = strong buy)
            if obv_signal == "bullish_divergence":
                confidence += 0.15
                reasons.append("OBV bullish divergence — volume accumulation on price drop")
                indicators_used.append("OBV")

            # Ranging market bonus
            if adx is not None and adx < 20:
                confidence += 0.05
                reasons.append(f"ADX={adx:.1f} confirms range-bound — ideal for reversion")

        # ── SELL: Price at/above upper band + RSI overbought ──
        elif bb_signal == "overbought" or current_price >= bb_upper:
            action = "sell"
            confidence = 0.45
            reasons.append(f"Price ${current_price:,.2f} at/above upper BB ${bb_upper:,.2f}")

            if rsi is not None and rsi >= self.rsi_overbought:
                confidence += 0.2
                reasons.append(f"RSI={rsi:.1f} confirms overbought")
                indicators_used.append("RSI")

            if stoch_k is not None and stoch_k >= self.stoch_overbought:
                confidence += 0.1
                reasons.append(f"Stoch RSI %K={stoch_k:.1f} confirms overbought")
                indicators_used.append("Stoch RSI")

            if obv_signal == "bearish_divergence":
                confidence += 0.15
                reasons.append("OBV bearish divergence — volume declining on price rise")
                indicators_used.append("OBV")

        else:
            reasons.append("Price within Bollinger Bands — no mean-reversion signal")

        # Clamp
        confidence = max(0.0, min(1.0, confidence))

        # Stop/target
        stop_loss = 0.0
        take_profit = 0.0
        if atr and current_price > 0 and action == "buy":
            stop_loss = current_price - (atr * self.atr_stop_mult)
            # Target: middle band (mean) or ATR-based
            if bb_middle:
                take_profit = bb_middle
            else:
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
