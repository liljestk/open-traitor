"""
Trailing Stop-Loss Manager — Dynamic stops that lock in profits.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("core.trailing_stop")


class TrailingStop:
    """
    Trailing stop-loss for a single position.

    Instead of a fixed stop price, the stop "trails" the price as it moves
    in your favor. If you bought at $100 with a 3% trail:
      - Price goes to $110 → stop at $106.70
      - Price goes to $120 → stop at $116.40
      - Price drops to $116.40 → TRIGGERED (sell)

    You captured $16.40 profit instead of the fixed stop at $97.
    """

    def __init__(
        self,
        pair: str,
        entry_price: float,
        trail_pct: float = 0.03,
        initial_stop: Optional[float] = None,
        side: str = "long",
    ):
        self.pair = pair
        self.entry_price = entry_price
        self.trail_pct = trail_pct
        self.side = side  # "long" or "short"
        self.created_at = datetime.now(timezone.utc)

        # Tracking
        if side == "long":
            self.highest_price = entry_price
            self.stop_price = initial_stop or (entry_price * (1 - trail_pct))
        else:
            self.lowest_price = entry_price
            self.stop_price = initial_stop or (entry_price * (1 + trail_pct))

        self.triggered = False
        self.trigger_price: Optional[float] = None
        self.trigger_time: Optional[datetime] = None
        self.updates = 0

    def update(self, current_price: float) -> bool:
        """
        Update the trailing stop with the current price.
        Returns True if the stop was triggered.
        """
        if self.triggered:
            return True

        if self.side == "long":
            return self._update_long(current_price)
        else:
            return self._update_short(current_price)

    def _update_long(self, current_price: float) -> bool:
        """Update trailing stop for a long position."""
        if current_price > self.highest_price:
            self.highest_price = current_price
            new_stop = current_price * (1 - self.trail_pct)

            if new_stop > self.stop_price:
                old_stop = self.stop_price
                self.stop_price = new_stop
                self.updates += 1
                logger.debug(
                    f"📈 {self.pair} trail raised: "
                    f"${old_stop:,.2f} → ${new_stop:,.2f} "
                    f"(high: ${current_price:,.2f})"
                )

        # Check if triggered
        if current_price <= self.stop_price:
            self.triggered = True
            self.trigger_price = current_price
            self.trigger_time = datetime.now(timezone.utc)

            pnl_pct = (current_price - self.entry_price) / self.entry_price * 100
            logger.info(
                f"🛑 {self.pair} trailing stop TRIGGERED @ ${current_price:,.2f} "
                f"(entry: ${self.entry_price:,.2f}, PnL: {pnl_pct:+.1f}%)"
            )
            return True

        return False

    def _update_short(self, current_price: float) -> bool:
        """Update trailing stop for a short position."""
        if current_price < self.lowest_price:
            self.lowest_price = current_price
            new_stop = current_price * (1 + self.trail_pct)

            if new_stop < self.stop_price:
                self.stop_price = new_stop
                self.updates += 1

        if current_price >= self.stop_price:
            self.triggered = True
            self.trigger_price = current_price
            self.trigger_time = datetime.now(timezone.utc)
            return True

        return False

    def to_dict(self) -> dict:
        """Serialize for state persistence."""
        result = {
            "pair": self.pair,
            "entry_price": self.entry_price,
            "trail_pct": self.trail_pct,
            "side": self.side,
            "stop_price": self.stop_price,
            "triggered": self.triggered,
            "updates": self.updates,
            "created_at": self.created_at.isoformat(),
        }
        if self.side == "long":
            result["highest_price"] = self.highest_price
        else:
            result["lowest_price"] = self.lowest_price
        if self.triggered:
            result["trigger_price"] = self.trigger_price
            result["trigger_time"] = self.trigger_time.isoformat() if self.trigger_time else None
        return result


class TrailingStopManager:
    """
    Manages trailing stops across all open positions.
    Thread-safe for use with WebSocket price updates.
    """

    def __init__(self, default_trail_pct: float = 0.03):
        self.default_trail_pct = default_trail_pct
        self.stops: dict[str, TrailingStop] = {}
        self._lock = threading.Lock()

    def add_stop(
        self,
        pair: str,
        entry_price: float,
        trail_pct: Optional[float] = None,
        initial_stop: Optional[float] = None,
        side: str = "long",
    ) -> TrailingStop:
        """Create a trailing stop for a position."""
        with self._lock:
            stop = TrailingStop(
                pair=pair,
                entry_price=entry_price,
                trail_pct=trail_pct or self.default_trail_pct,
                initial_stop=initial_stop,
                side=side,
            )
            self.stops[pair] = stop
            logger.info(
                f"📌 Trailing stop set for {pair}: "
                f"entry ${entry_price:,.2f}, "
                f"trail {(trail_pct or self.default_trail_pct)*100:.1f}%, "
                f"initial stop ${stop.stop_price:,.2f}"
            )
            return stop

    def remove_stop(self, pair: str) -> None:
        """Remove a trailing stop."""
        with self._lock:
            self.stops.pop(pair, None)

    def update_prices(self, prices: dict[str, float]) -> list[dict]:
        """
        Update all trailing stops with new prices.
        Returns list of triggered stops.
        """
        triggered = []
        with self._lock:
            for pair, stop in list(self.stops.items()):
                price = prices.get(pair, 0)
                if price > 0 and stop.update(price):
                    triggered.append(stop.to_dict())
                    # Don't remove yet — executor handles the sale
        return triggered

    def get_stop(self, pair: str) -> Optional[TrailingStop]:
        """Get the trailing stop for a pair."""
        with self._lock:
            return self.stops.get(pair)

    def get_all_stops(self) -> dict:
        """Get all trailing stops as a dict."""
        with self._lock:
            return {pair: stop.to_dict() for pair, stop in self.stops.items()}

    def get_active_count(self) -> int:
        """Get number of active (non-triggered) trailing stops."""
        with self._lock:
            return sum(1 for s in self.stops.values() if not s.triggered)
