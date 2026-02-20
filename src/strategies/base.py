"""
Base Strategy — Abstract interface for deterministic trading strategies.

Every strategy:
  1. Takes candle data + indicators → produces a StrategySignal
  2. Is independently backtestable (no LLM dependency)
  3. Reports its preferred market regime (for the orchestrator to route)
  4. Has a unique name for attribution/logging
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class StrategyType(str, Enum):
    """Strategy classification for regime routing."""
    TREND_FOLLOWING = "trend_following"
    MEAN_REVERSION = "mean_reversion"
    MOMENTUM = "momentum"
    STATISTICAL_ARB = "statistical_arb"
    HYBRID = "hybrid"


@dataclass
class StrategySignal:
    """Output from a deterministic strategy."""
    strategy_name: str
    strategy_type: StrategyType
    action: str  # "buy", "sell", "hold"
    pair: str
    confidence: float  # 0.0 – 1.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reasoning: str = ""
    indicators_used: list[str] = field(default_factory=list)
    # Regime context
    market_regime: str = ""  # "trending", "ranging", "volatile"
    regime_strength: float = 0.0  # 0.0–1.0 how sure we are about the regime

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "strategy_type": self.strategy_type.value,
            "action": self.action,
            "pair": self.pair,
            "confidence": round(self.confidence, 3),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "reasoning": self.reasoning,
            "indicators_used": self.indicators_used,
            "market_regime": self.market_regime,
            "regime_strength": round(self.regime_strength, 3),
        }

    @property
    def is_actionable(self) -> bool:
        return self.action in ("buy", "sell") and self.confidence > 0


class BaseStrategy(ABC):
    """Abstract base for all deterministic strategies."""

    def __init__(self, name: str, strategy_type: StrategyType, config: dict):
        self.name = name
        self.strategy_type = strategy_type
        self.config = config

    @abstractmethod
    def generate_signal(
        self,
        pair: str,
        candles: list[dict],
        analysis: dict,
        context: Optional[dict] = None,
    ) -> StrategySignal:
        """
        Generate a trading signal from market data.

        Args:
            pair: Trading pair (e.g., "BTC-EUR")
            candles: Raw OHLCV candle data
            analysis: Output from TechnicalAnalyzer.analyze()
            context: Optional extra context (portfolio state, etc.)

        Returns:
            StrategySignal with action, confidence, and reasoning
        """
        ...

    @abstractmethod
    def preferred_regime(self) -> str:
        """
        Return the market regime where this strategy performs best.
        Used by the orchestrator to activate/deactivate strategies.
        """
        ...

    def detect_regime(self, analysis: dict) -> tuple[str, float]:
        """
        Detect the current market regime from indicators.
        Returns (regime_name, confidence).
        """
        indicators = analysis.get("indicators", {})
        adx = indicators.get("adx")
        atr = indicators.get("atr")
        current_price = analysis.get("current_price", 0)

        if adx is None:
            return "unknown", 0.0

        # ATR-based volatility
        atr_pct = (atr / current_price * 100) if atr and current_price > 0 else 0

        if adx >= 30:
            regime = "strong_trend"
            confidence = min(1.0, adx / 50)
        elif adx >= 25:
            regime = "trending"
            confidence = 0.6 + (adx - 25) / 25
        elif adx < 20:
            regime = "ranging"
            confidence = 0.6 + (20 - adx) / 20
        else:
            regime = "weak_trend"
            confidence = 0.4

        # Override to volatile if ATR is extreme
        if atr_pct > 5:
            regime = "volatile"
            confidence = min(1.0, atr_pct / 8)

        return regime, round(confidence, 3)
