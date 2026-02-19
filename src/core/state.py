"""
Shared trading state for coordinating between agents.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.models.signal import Signal
from src.models.trade import Trade, TradeStatus
from src.utils.logger import get_logger

logger = get_logger("core.state")

_STATE_FILE = "data/trading_state.json"


class PortfolioSnapshot(BaseModel):
    """Snapshot of the portfolio at a point in time."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_value: float = 0.0
    cash_balance: float = 0.0
    positions: dict[str, float] = Field(default_factory=dict)
    unrealized_pnl: float = 0.0


class TradingState:
    """
    Thread-safe shared state for the trading system.
    Maintains portfolio, signals, trades, and performance metrics.
    """

    def __init__(self, initial_balance: float = 10000.0):
        self._lock = threading.RLock()
        self.initial_balance = initial_balance
        self.start_time = datetime.now(timezone.utc)

        # Current state
        self.current_prices: dict[str, float] = {}
        self.positions: dict[str, float] = {}  # pair -> quantity
        self.cash_balance: float = initial_balance

        # History
        self.signals: list[Signal] = []
        self.trades: list[Trade] = []
        self.portfolio_history: list[PortfolioSnapshot] = []

        # Performance metrics
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_pnl: float = 0.0
        self.max_drawdown: float = 0.0
        self.peak_portfolio_value: float = initial_balance

        # Status
        self.is_running: bool = False
        self.is_paused: bool = False
        self.circuit_breaker_triggered: bool = False
        self.last_analysis_time: Optional[datetime] = None
        self.last_trade_time: Optional[datetime] = None

        # Agent states
        self.agent_states: dict[str, dict] = {}

        # Warm-start from persisted snapshot (trades + signals only; balance is live)
        self._warm_start(_STATE_FILE)

        logger.info(f"TradingState initialized | Balance: ${initial_balance:,.2f}")

    def _warm_start(self, filepath: str) -> None:
        """Load recent trades and signals from the last saved snapshot."""
        path = Path(filepath)
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for t in data.get("trades", []):
                try:
                    self.trades.append(Trade(**t))
                except Exception:
                    pass
            for s in data.get("signals", []):
                try:
                    self.signals.append(Signal(**s))
                except Exception:
                    pass
            # Restore performance counters from persisted summary
            summary = data.get("summary", {})
            self.total_trades = summary.get("total_trades", 0)
            closed = [t for t in self.trades if t.pnl is not None]
            self.winning_trades = sum(1 for t in closed if t.pnl and t.pnl > 0)
            self.losing_trades = sum(1 for t in closed if t.pnl and t.pnl <= 0)
            self.total_pnl = sum(t.pnl for t in closed if t.pnl is not None)
            logger.info(
                f"🔄 Warm-start: loaded {len(self.trades)} trades, "
                f"{len(self.signals)} signals from snapshot"
            )
        except Exception as e:
            logger.warning(f"Warm-start failed (non-fatal): {e}")

    def update_price(self, pair: str, price: float) -> None:
        """Update the current price for a pair."""
        with self._lock:
            self.current_prices[pair] = price

    def add_signal(self, signal: Signal) -> None:
        """Add a new signal to history."""
        with self._lock:
            self.signals.append(signal)
            # Keep last 1000 signals
            if len(self.signals) > 1000:
                self.signals = self.signals[-500:]
            self.last_analysis_time = signal.timestamp

    def add_trade(self, trade: Trade) -> None:
        """Record a new trade."""
        with self._lock:
            self.trades.append(trade)
            self.total_trades += 1
            self.last_trade_time = trade.timestamp

            if trade.action.value == "buy":
                self.positions[trade.pair] = (
                    self.positions.get(trade.pair, 0) + trade.quantity
                )
                self.cash_balance -= trade.value
            elif trade.action.value == "sell":
                self.positions[trade.pair] = (
                    self.positions.get(trade.pair, 0) - trade.quantity
                )
                self.cash_balance += trade.value

            logger.info(f"Trade recorded: {trade.to_summary()}")

    def close_trade(self, trade_id: str, close_price: float, fees: float = 0.0) -> Optional[Trade]:
        """Close an existing trade and update PnL."""
        with self._lock:
            for trade in self.trades:
                if trade.id == trade_id and trade.is_open:
                    trade.close(close_price, fees)

                    if trade.pnl is not None:
                        self.total_pnl += trade.pnl
                        if trade.pnl > 0:
                            self.winning_trades += 1
                        else:
                            self.losing_trades += 1

                    logger.info(f"Trade closed: {trade.to_summary()}")
                    return trade
            return None

    def take_portfolio_snapshot(self) -> PortfolioSnapshot:
        """Take a snapshot of the current portfolio state."""
        with self._lock:
            total_value = self.cash_balance
            unrealized_pnl = 0.0

            for pair, qty in self.positions.items():
                if qty > 0:
                    price = self.current_prices.get(pair, 0)
                    total_value += qty * price

            snapshot = PortfolioSnapshot(
                total_value=total_value,
                cash_balance=self.cash_balance,
                positions=self.positions.copy(),
                unrealized_pnl=unrealized_pnl,
            )
            self.portfolio_history.append(snapshot)

            # Update peak and drawdown
            if total_value > self.peak_portfolio_value:
                self.peak_portfolio_value = total_value

            if self.peak_portfolio_value > 0:
                current_drawdown = (
                    (self.peak_portfolio_value - total_value) / self.peak_portfolio_value
                )
                self.max_drawdown = max(self.max_drawdown, current_drawdown)

            # Keep last 10000 snapshots
            if len(self.portfolio_history) > 10000:
                self.portfolio_history = self.portfolio_history[-5000:]

            return snapshot

    @property
    def portfolio_value(self) -> float:
        """Get current total portfolio value."""
        with self._lock:
            total = self.cash_balance
            for pair, qty in self.positions.items():
                if qty > 0:
                    price = self.current_prices.get(pair, 0)
                    total += qty * price
            return total

    @property
    def win_rate(self) -> float:
        """Get the win rate."""
        with self._lock:
            completed = self.winning_trades + self.losing_trades
            if completed == 0:
                return 0.0
            return self.winning_trades / completed

    @property
    def return_pct(self) -> float:
        """Get the total return percentage."""
        with self._lock:
            if self.initial_balance == 0:
                return 0.0
            pv = self.cash_balance
            for pair, qty in self.positions.items():
                if qty > 0:
                    price = self.current_prices.get(pair, 0)
                    pv += qty * price
            return (pv - self.initial_balance) / self.initial_balance

    @property
    def open_positions(self) -> dict[str, float]:
        """Get all open positions with non-zero quantities."""
        with self._lock:
            return {pair: qty for pair, qty in self.positions.items() if abs(qty) > 1e-8}

    @property
    def recent_signals(self) -> list[Signal]:
        """Get the 10 most recent signals."""
        with self._lock:
            return list(self.signals[-10:])

    @property
    def recent_trades(self) -> list[Trade]:
        """Get the 20 most recent trades."""
        with self._lock:
            return list(self.trades[-20:])

    def get_open_trades(self) -> list[Trade]:
        """Get all open trades."""
        with self._lock:
            return [t for t in self.trades if t.is_open]

    def get_trades_for_pair(self, pair: str) -> list[Trade]:
        """Get all trades for a specific pair."""
        with self._lock:
            return [t for t in self.trades if t.pair == pair]

    def update_agent_state(self, agent_name: str, state: dict) -> None:
        """Update the state for a specific agent."""
        with self._lock:
            self.agent_states[agent_name] = {
                **state,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

    def to_summary(self) -> dict:
        """Get a summary of the current state."""
        return {
            "portfolio_value": self.portfolio_value,
            "cash_balance": self.cash_balance,
            "return_pct": self.return_pct,
            "total_pnl": self.total_pnl,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "open_positions": self.open_positions,
            "current_prices": self.current_prices,
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "circuit_breaker": self.circuit_breaker_triggered,
        }

    def save_state(self, filepath: str = _STATE_FILE) -> None:
        """Save the current state to a JSON file (atomic write)."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            state_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "summary": self.to_summary(),
                "trades": [t.model_dump(mode="json") for t in self.trades[-100:]],
                "signals": [s.model_dump(mode="json") for s in self.signals[-50:]],
            }

        # Write to temp file then rename for atomicity
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(state_data, f, indent=2, default=str)
            tmp_path.replace(path)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            return

        logger.debug(f"State saved to {filepath}")
