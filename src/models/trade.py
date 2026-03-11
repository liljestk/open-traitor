"""
Trade data models for the OpenTraitor trading agent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TradeAction(str, Enum):
    """Possible trade actions."""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class TradeStatus(str, Enum):
    """Possible trade statuses."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


class Trade(BaseModel):
    """Represents a single trade."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pair: str
    exchange: str = "coinbase"  # Exchange identifier ("coinbase", "ibkr", …)
    action: TradeAction
    status: TradeStatus = TradeStatus.PENDING
    quantity: float
    price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    filled_price: Optional[float] = None
    filled_quantity: Optional[float] = None
    coinbase_order_id: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""
    pnl: Optional[float] = None
    fees: float = 0.0
    closed_at: Optional[datetime] = None

    @property
    def is_open(self) -> bool:
        return self.status in (
            TradeStatus.PENDING,
            TradeStatus.SUBMITTED,
            TradeStatus.FILLED,
            TradeStatus.PARTIALLY_FILLED,
        )

    @property
    def value(self) -> float:
        price = self.filled_price or self.price
        qty = self.filled_quantity or self.quantity
        return price * qty

    def close(self, close_price: float, fees: float = 0.0) -> None:
        """Close the trade and calculate PnL."""
        qty = self.filled_quantity or self.quantity
        entry_price = self.filled_price or self.price

        if self.action == TradeAction.BUY:
            self.pnl = (close_price - entry_price) * qty - self.fees - fees
        else:
            self.pnl = (entry_price - close_price) * qty - self.fees - fees

        self.fees += fees
        self.status = TradeStatus.CLOSED
        self.closed_at = datetime.now(timezone.utc)

    def to_summary(self) -> str:
        """Human-readable trade summary."""
        status_emoji = {
            TradeStatus.PENDING: "⏳",
            TradeStatus.SUBMITTED: "📤",
            TradeStatus.FILLED: "✅",
            TradeStatus.PARTIALLY_FILLED: "🔄",
            TradeStatus.CANCELLED: "❌",
            TradeStatus.FAILED: "💥",
            TradeStatus.CLOSED: "🔒",
        }
        emoji = status_emoji.get(self.status, "❓")
        pnl_str = f" | PnL: ${self.pnl:+.2f}" if self.pnl is not None else ""
        return (
            f"{emoji} {self.action.value.upper()} {self.quantity:.6f} {self.pair} "
            f"@ ${self.price:,.2f} | Confidence: {self.confidence:.0%} | "
            f"Status: {self.status.value}{pnl_str}"
        )
