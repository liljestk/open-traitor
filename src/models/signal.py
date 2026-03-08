"""
Signal data models for the Auto-Traitor trading agent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    """Types of trading signals."""
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    WEAK_BUY = "weak_buy"
    NEUTRAL = "neutral"
    WEAK_SELL = "weak_sell"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class MarketCondition(str, Enum):
    """Overall market condition assessment."""
    STRONGLY_BULLISH = "strongly_bullish"
    BULLISH = "bullish"
    SLIGHTLY_BULLISH = "slightly_bullish"
    NEUTRAL = "neutral"
    SLIGHTLY_BEARISH = "slightly_bearish"
    BEARISH = "bearish"
    STRONGLY_BEARISH = "strongly_bearish"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class TechnicalSignals(BaseModel):
    """Technical analysis signals."""
    rsi: Optional[float] = None
    rsi_signal: Optional[str] = None
    macd: Optional[float] = None
    macd_signal_line: Optional[float] = None
    macd_histogram: Optional[float] = None
    macd_signal: Optional[str] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_signal: Optional[str] = None
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    ema_signal: Optional[str] = None
    volume_trend: Optional[str] = None
    price_change_1h: Optional[float] = None
    price_change_24h: Optional[float] = None
    support_level: Optional[float] = None
    resistance_level: Optional[float] = None
    atr: Optional[float] = None


class SentimentSignals(BaseModel):
    """Sentiment analysis signals."""
    overall_sentiment: Optional[str] = None  # bullish, bearish, neutral
    sentiment_score: float = 0.0  # -1.0 to 1.0
    key_factors: list[str] = Field(default_factory=list)
    news_summary: Optional[str] = None


class Signal(BaseModel):
    """Combined trading signal from all analyses."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pair: str
    exchange: str = "coinbase"  # Exchange identifier ("coinbase", "ibkr", …)
    current_price: float
    signal_type: SignalType = SignalType.NEUTRAL
    confidence: float = 0.0  # 0.0 to 1.0
    market_condition: MarketCondition = MarketCondition.UNKNOWN
    technical: TechnicalSignals = Field(default_factory=TechnicalSignals)
    sentiment: SentimentSignals = Field(default_factory=SentimentSignals)
    suggested_entry: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    suggested_position_size: Optional[float] = None
    reasoning: str = ""
    risk_assessment: str = ""

    @property
    def is_actionable(self) -> bool:
        """Whether this signal suggests taking action (not neutral/hold)."""
        return self.signal_type not in (SignalType.NEUTRAL,)

    @property
    def is_buy_signal(self) -> bool:
        return self.signal_type in (
            SignalType.STRONG_BUY,
            SignalType.BUY,
            SignalType.WEAK_BUY,
        )

    @property
    def is_sell_signal(self) -> bool:
        return self.signal_type in (
            SignalType.STRONG_SELL,
            SignalType.SELL,
            SignalType.WEAK_SELL,
        )

    def to_summary(self, currency_symbol: str = "") -> str:
        """Human-readable signal summary."""
        signal_emoji = {
            SignalType.STRONG_BUY: "🟢🟢",
            SignalType.BUY: "🟢",
            SignalType.WEAK_BUY: "🟡🟢",
            SignalType.NEUTRAL: "⚪",
            SignalType.WEAK_SELL: "🟡🔴",
            SignalType.SELL: "🔴",
            SignalType.STRONG_SELL: "🔴🔴",
        }
        emoji = signal_emoji.get(self.signal_type, "❓")
        # Infer currency symbol from pair if not provided
        if not currency_symbol and self.pair and "-" in self.pair:
            _known_symbols = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF ", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
            quote = self.pair.rsplit("-", 1)[-1].upper()
            currency_symbol = _known_symbols.get(quote, "$")
        elif not currency_symbol:
            currency_symbol = "$"
        return (
            f"{emoji} {self.pair} | Signal: {self.signal_type.value} | "
            f"Confidence: {self.confidence:.0%} | "
            f"Price: {currency_symbol}{self.current_price:,.2f} | "
            f"Market: {self.market_condition.value}"
        )
