"""
Shared trading state for coordinating between agents.
"""

from __future__ import annotations

import json
import threading
import time
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.models.signal import Signal
from src.models.trade import Trade, TradeStatus
from src.utils.helpers import get_data_dir
from src.utils.logger import get_logger

logger = get_logger("core.state")


def _get_state_file() -> str:
    """Return the profile-scoped trading state file path."""
    return os.path.join(get_data_dir(), "trading_state.json")


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

        # ── Live Holdings (from Coinbase API) ──
        self.live_holdings: list[dict] = []           # all Coinbase account balances
        self.live_cash_balances: dict[str, float] = {}  # {"EUR": 0.55, "USDC": 0.0, ...}
        self.live_portfolio_value: float = 0.0         # total portfolio in native currency
        self.native_currency: str = "USD"              # detected account currency
        self.currency_symbol: str = "$"                # display symbol
        self._live_snapshot_ts: float = 0.0            # epoch of last refresh
        self._initial_balance_synced: bool = False     # whether initial_balance has been corrected
        self.positions_meta: dict[str, dict] = {}      # {pair: {"origin": "external"|"bot", ...}}

        # History
        self.signals: deque = deque(maxlen=1000)
        self.trades: list[Trade] = []
        self._max_trades_in_memory = 10000  # M5 fix: Bound trades list to prevent memory growth
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
        self._circuit_breaker_ts: float = 0.0  # epoch when circuit breaker fired
        self.last_analysis_time: Optional[datetime] = None
        self.last_trade_time: Optional[datetime] = None

        # Agent states
        self.agent_states: dict[str, dict] = {}

        # Warm-start from persisted snapshot (trades + signals only; balance is live)
        self._warm_start(_get_state_file())

        logger.info(f"TradingState initialized | Balance: {self.currency_symbol}{initial_balance:,.2f}")

    def _warm_start(self, filepath: str) -> None:
        """Load recent trades and signals from the last saved snapshot."""
        path = Path(filepath)
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for i, t in enumerate(data.get("trades", [])):
                try:
                    self.trades.append(Trade(**t))
                except Exception as exc:
                    # M21: Log instead of silently swallowing corrupt records
                    logger.warning(f"Warm-start: skipping corrupt trade record #{i}: {exc}")
            for i, s in enumerate(data.get("signals", [])):
                try:
                    self.signals.append(Signal(**s))
                except Exception as exc:
                    logger.warning(f"Warm-start: skipping corrupt signal record #{i}: {exc}")
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

    # =========================================================================
    # Live Holdings Sync (from Coinbase API)
    # =========================================================================

    def sync_live_holdings(self, snapshot: dict, dust_threshold: float = 0.01) -> dict[str, float]:
        """Merge live Coinbase snapshot into shared state.

        Uses additive-only merge for positions: only adds holdings that are NOT
        already tracked, preventing races with in-flight trades.

        Args:
            snapshot: Dict from Orchestrator._live_coinbase_snapshot().
            dust_threshold: Minimum native-currency value to include.

        Returns:
            Dict of {pair: discovery_price} for positions newly added in this
            sync (external holdings not previously tracked by the bot).
            Callers should use this to register synthetic FIFO lots and
            trailing stops so the bot has a cost-basis reference point.
        """
        with self._lock:
            self.live_holdings = snapshot.get("holdings", [])
            self.live_portfolio_value = snapshot.get("total_portfolio", 0.0)
            self.native_currency = snapshot.get("native_currency", "USD")
            self.currency_symbol = snapshot.get("currency_symbol", "$")
            self._live_snapshot_ts = time.time()

            # Populate live_cash_balances from all fiat/stablecoin holdings
            self.live_cash_balances = {}
            for h in self.live_holdings:
                if h.get("is_fiat"):
                    self.live_cash_balances[h["currency"]] = h.get("native_value", 0.0)

            # Update cash_balance to reflect real fiat totals.
            # M13 fix: Relaxed guard - update even with open trades if the live value
            # is significantly different. The delta check prevents executor races on
            # recent orders while still allowing drift correction.
            has_open_trades = any(t.is_open for t in self.trades)
            total_cash = sum(self.live_cash_balances.values())
            if total_cash > 0:
                # Calculate maximum expected in-flight cash from open trades
                in_flight_cash = sum(
                    t.value for t in self.trades 
                    if t.is_open and t.action.value == "buy"
                )
                # If delta exceeds in-flight buffer, update; otherwise skip to avoid race
                delta = abs(total_cash - self.cash_balance)
                if not has_open_trades or delta > in_flight_cash * 1.5:
                    self.cash_balance = total_cash

            # Correct initial_balance on first successful sync
            if not self._initial_balance_synced and self.live_portfolio_value > 0:
                self.initial_balance = self.live_portfolio_value
                self.peak_portfolio_value = self.live_portfolio_value
                self._initial_balance_synced = True
                # Clear any phantom drawdown accumulated before live data was available
                self.max_drawdown = 0.0
                self.circuit_breaker_triggered = False
                logger.info(
                    f"📊 Initial balance corrected to live portfolio: "
                    f"{self.currency_symbol}{self.live_portfolio_value:,.2f} "
                    f"(drawdown reset)"
                )

            # Additive merge: only add crypto holdings NOT already in positions
            # Filter out dust holdings (below dust_threshold in native value)
            synced_count = 0
            new_externals: dict[str, float] = {}  # {pair: discovery_price}
            for h in self.live_holdings:
                if h.get("is_fiat"):
                    continue
                pair = h.get("pair")
                amount = h.get("amount", 0)
                native_value = h.get("native_value", 0)
                if not pair or amount <= 0:
                    continue
                # Skip dust holdings — they count against position limits
                # but are too small to trade
                if native_value < dust_threshold:
                    continue
                # setdefault — does NOT overwrite existing bot-opened positions
                if pair not in self.positions:
                    self.positions[pair] = amount
                    ts_now = datetime.now(timezone.utc).isoformat()
                    price = h.get("price", 0)
                    self.positions_meta[pair] = {
                        "origin": "external",
                        "synced_at": ts_now,
                        "discovery_price": price,
                    }
                    if price > 0:
                        new_externals[pair] = price
                    synced_count += 1
                # Always refresh price from Coinbase — not just on first sync
                price = h.get("price", 0)
                if price > 0:
                    self.current_prices[pair] = price

            asset_count = len([h for h in self.live_holdings if not h.get("is_fiat")])
            logger.info(
                f"📡 Live holdings synced: {asset_count} assets, "
                f"total={self.currency_symbol}{self.live_portfolio_value:,.2f}"
                + (f" ({synced_count} new positions added)" if synced_count else "")
            )
            return new_externals

    @property
    def holdings_summary(self) -> str:
        """Formatted holdings text for the LLM strategist prompt.

        Groups cash and crypto separately, uses detected currency_symbol,
        and filters out dust holdings below threshold.
        """
        with self._lock:
            if not self.live_holdings:
                return "No live holdings data available."

            sym = self.currency_symbol
            lines = []

            # Cash section
            cash_parts = []
            for currency, val in sorted(self.live_cash_balances.items()):
                cash_parts.append(f"{sym}{val:,.2f} {currency}")
            if cash_parts:
                lines.append(f"- Cash: {', '.join(cash_parts)}")
            else:
                lines.append(f"- Cash: {sym}0.00")

            # Crypto section — sorted by value descending, dust filtered
            crypto_holdings = [
                h for h in self.live_holdings
                if not h.get("is_fiat") and h.get("native_value", 0) > 0
            ]
            crypto_holdings.sort(key=lambda h: h.get("native_value", 0), reverse=True)

            if crypto_holdings:
                lines.append("")
                lines.append("ACTUAL COINBASE HOLDINGS (live from Coinbase API):")
                shown = 0
                for h in crypto_holdings:
                    val = h.get("native_value", 0)
                    if val < 0.01:  # hard minimum to avoid noise
                        continue
                    price = h.get("price", 0)
                    amount = h.get("amount", 0)
                    currency = h.get("currency", "?")
                    price_str = f" @ {sym}{price:,.4f}" if price > 0 else ""
                    lines.append(
                        f"- {currency}: {amount:.6f}{price_str} (value: {sym}{val:,.2f})"
                    )
                    shown += 1
                    if shown >= 50:  # hard cap to avoid token budget blowout
                        remaining = len(crypto_holdings) - shown
                        if remaining > 0:
                            lines.append(f"  ... and {remaining} more small holdings")
                        break
            else:
                lines.append("\nNo crypto holdings.")

            lines.append("")
            lines.append(
                "NOTE: You can propose sells of ANY holding above, not just bot-opened positions."
            )
            lines.append(
                "      For sells, specify the exact quantity from this list."
            )

            return "\n".join(lines)

    def update_price(self, pair: str, price: float) -> None:
        """Update the current price for a pair."""
        with self._lock:
            self.current_prices[pair] = price

    def add_signal(self, signal: Signal) -> None:
        """Add a new signal to history."""
        with self._lock:
            self.signals.append(signal)
            self.last_analysis_time = signal.timestamp

    def add_trade(self, trade: Trade, force: bool = False) -> None:
        """Record a new trade.
        
        Args:
            trade: The trade to record
            force: M1 fix - If True, skip balance validation. Use when exchange
                   has already filled the order and we must record it regardless
                   of local state drift.
        """
        with self._lock:
            if not force:
                if trade.action.value == "buy":
                    if trade.value > self.cash_balance:
                        raise ValueError(
                            f"Insufficient cash for buy {trade.pair}: have={self.cash_balance:.2f}, need={trade.value:.2f}"
                        )
                elif trade.action.value == "sell":
                    position = self.positions.get(trade.pair, 0)
                    if trade.quantity > position:
                        raise ValueError(
                            f"Insufficient position for sell {trade.pair}: have={position:.8f}, need={trade.quantity:.8f}"
                        )

            # M5 fix: Trim old closed trades BEFORE appending to avoid peak memory spikes
            if len(self.trades) >= self._max_trades_in_memory:
                self._trim_closed_trades()

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

    def _trim_closed_trades(self) -> None:
        """M5 fix: Remove oldest closed trades to keep memory bounded.
        
        Keeps all open trades and the most recent closed trades.
        Should only be called while holding _lock.
        """
        # Separate open and closed trades
        open_trades = [t for t in self.trades if t.is_open]
        closed_trades = [t for t in self.trades if not t.is_open]
        
        # Keep only the most recent closed trades
        max_closed = self._max_trades_in_memory - len(open_trades) - 1000  # buffer for new trades
        if len(closed_trades) > max_closed:
            # Sort by timestamp and keep most recent
            closed_trades.sort(key=lambda t: t.close_time or t.timestamp, reverse=True)
            closed_trades = closed_trades[:max_closed]
            trimmed_count = len(self.trades) - len(open_trades) - len(closed_trades)
            logger.info(f"🗑️ Trimmed {trimmed_count} old closed trades from memory")
        
        # Rebuild trades list: closed (oldest first) + open
        self.trades = sorted(closed_trades, key=lambda t: t.close_time or t.timestamp) + open_trades

    def close_trade(self, trade_id: str, close_price: float, fees: float = 0.0) -> Optional[Trade]:
        """Close an existing trade and update PnL, positions, and cash."""
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

                    # Update positions and cash (close reverses the original booking)
                    qty = trade.filled_quantity or trade.quantity
                    if trade.action.value == "buy":
                        # Closing a buy = selling the position
                        self.positions[trade.pair] = max(0.0, self.positions.get(trade.pair, 0) - qty)
                        self.cash_balance += (close_price * qty) - fees
                    elif trade.action.value == "sell":
                        # Closing a sell = buying back
                        self.positions[trade.pair] = self.positions.get(trade.pair, 0) + qty
                        self.cash_balance -= (close_price * qty) + fees

                    logger.info(f"Trade closed: {trade.to_summary()}")
                    return trade
            return None

    def update_partial_fill(self, trade_id: str, remaining_quantity: float, sold_quantity: float = 0.0, sell_proceeds: float = 0.0) -> None:
        """Update trade quantity after a partial sell (M12 fix: public API instead of _lock).

        H5 fix: also deduct *sold_quantity* from self.positions so that position
        tracking stays accurate between partial sell and full close.
        Cycle-3 fix: credit *sell_proceeds* (quantity * price - fees) to cash_balance.
        """
        with self._lock:
            for t in self.trades:
                if t.id == trade_id:
                    t.filled_quantity = remaining_quantity
                    # H5: deduct sold portion from positions
                    if sold_quantity > 0:
                        current = self.positions.get(t.pair, 0)
                        self.positions[t.pair] = max(0.0, current - sold_quantity)
                    # Cycle-3: credit sell proceeds to cash
                    if sell_proceeds > 0:
                        self.cash_balance += sell_proceeds
                    return

    def update_trade_fill(self, trade_id: str, filled_price: float, filled_quantity: float, fees: float, status=None) -> bool:
        """Atomically update a trade's fill data under lock (H4 fix).

        Cycle-3 fix: also reconcile positions and cash_balance when the
        actual fill differs from the original add_trade booking.

        Returns True if the trade was found and updated.
        """
        from src.models.trade import TradeStatus
        with self._lock:
            for t in self.trades:
                if t.id == trade_id:
                    # Compute deltas vs original booking
                    old_qty = t.filled_quantity if t.filled_quantity is not None else t.quantity
                    old_value = (t.filled_price or t.price) * old_qty
                    new_value = filled_price * filled_quantity

                    if status is not None:
                        t.status = status
                    t.filled_price = filled_price
                    t.filled_quantity = filled_quantity
                    t.fees = fees

                    # Reconcile position quantity delta
                    qty_delta = filled_quantity - old_qty
                    if abs(qty_delta) > 1e-12:
                        if t.action.value == "buy":
                            self.positions[t.pair] = self.positions.get(t.pair, 0) + qty_delta
                        else:
                            self.positions[t.pair] = max(0.0, self.positions.get(t.pair, 0) - qty_delta)

                    # Reconcile cash delta (buy: more value = less cash; sell: inverse)
                    value_delta = new_value - old_value + fees
                    if abs(value_delta) > 1e-12:
                        if t.action.value == "buy":
                            self.cash_balance -= value_delta
                        else:
                            self.cash_balance += value_delta
                    return True
            return False

    def mark_trade_status(self, trade_id: str, status) -> bool:
        """Set a trade's status under lock (H4 fix for pending order lifecycle)."""
        with self._lock:
            for t in self.trades:
                if t.id == trade_id:
                    t.status = status
                    return True
            return False

    def reconcile_position(
        self,
        pair: str,
        actual_qty: float,
        current_price: float = 0.0,
        reason: str = "reconciliation",
    ) -> dict:
        """C2 fix: Atomically reconcile a position to match exchange reality.

        Updates both position quantity AND cash_balance to maintain accounting
        integrity. Returns delta info for audit logging.

        Args:
            pair: Trading pair (e.g. "BTC-USD")
            actual_qty: The actual quantity on the exchange
            current_price: Current market price (for cash adjustment estimation)
            reason: Reason string for logging

        Returns:
            {"old_qty": float, "new_qty": float, "delta": float, "cash_adj": float}
        """
        with self._lock:
            old_qty = self.positions.get(pair, 0.0)
            delta = actual_qty - old_qty

            if actual_qty > 1e-8:
                self.positions[pair] = actual_qty
            else:
                self.positions.pop(pair, None)
                actual_qty = 0.0

            # Cash adjustment: if we have MORE than expected, we spent cash to get it
            # (or it was deposited externally). If we have LESS, we sold it for cash.
            # This is an approximation — we don't know the actual prices of external
            # trades, but it keeps the accounting directionally correct.
            cash_adj = 0.0
            if abs(delta) > 1e-8 and current_price > 0:
                # Positive delta = we have more asset = spent cash
                # Negative delta = we have less asset = gained cash
                cash_adj = -delta * current_price
                self.cash_balance += cash_adj

            return {
                "old_qty": old_qty,
                "new_qty": actual_qty,
                "delta": delta,
                "cash_adj": cash_adj,
                "reason": reason,
            }

    def reverse_trade_booking(self, trade) -> None:
        """Undo the position/cash changes from add_trade for a cancelled/failed order.

        Cycle-3 fix: use filled_quantity when available (partially-filled
        cancel) so we only reverse the *unfilled* portion.  The filled
        portion has real exchange-side consequences and must stay booked.
        """
        with self._lock:
            filled_qty = getattr(trade, "filled_quantity", None) or 0.0
            unfilled_qty = max(0.0, trade.quantity - filled_qty)
            unfilled_value = unfilled_qty * (trade.filled_price or trade.price) if unfilled_qty else trade.value

            if trade.action.value == "buy":
                self.positions[trade.pair] = max(
                    0.0, self.positions.get(trade.pair, 0) - unfilled_qty
                )
                self.cash_balance += unfilled_value
            elif trade.action.value == "sell":
                self.positions[trade.pair] = (
                    self.positions.get(trade.pair, 0) + unfilled_qty
                )
                self.cash_balance -= unfilled_value

    # ── Live-snapshot staleness threshold (seconds) ──
    _LIVE_STALENESS_THRESHOLD: float = 300.0  # 5 minutes
    _STALE_WARNING_INTERVAL: float = 300.0    # Log stale warning at most once per 5 min
    _last_stale_warning_ts: float = 0.0

    def _get_portfolio_value_unlocked(self) -> float:
        """Return best-available portfolio value (caller must hold _lock).

        Prefers the authoritative live `live_portfolio_value` when it is
        fresh (updated within `_LIVE_STALENESS_THRESHOLD` seconds).

        When live data is stale, still prefers the last-known live value over
        a local positions-based computation.  The local computation is unreliable
        because ``positions`` uses additive-only merge and can drift from reality,
        producing grossly inflated values.  A stale-but-accurate live value is
        far better than a fresh-but-wrong local estimate.

        The local computation is ONLY used in paper mode (where there is no
        live exchange data at all).
        """
        # Always prefer live value when available (even if slightly stale)
        if self.live_portfolio_value > 0 and self._live_snapshot_ts > 0:
            stale_secs = time.time() - self._live_snapshot_ts
            if stale_secs >= self._LIVE_STALENESS_THRESHOLD:
                now = time.time()
                if now - self._last_stale_warning_ts >= self._STALE_WARNING_INTERVAL:
                    self._last_stale_warning_ts = now
                    logger.warning(
                        f"⚠️ Live portfolio data is {stale_secs:.0f}s stale "
                        f"(threshold {self._LIVE_STALENESS_THRESHOLD:.0f}s) — "
                        f"using last-known live value {self.currency_symbol}"
                        f"{self.live_portfolio_value:,.2f} instead of local computation"
                    )
            return self.live_portfolio_value

        # Fallback: local computation (paper mode only — no live data ever received)
        total = self.cash_balance
        for pair, qty in self.positions.items():
            if qty > 0:
                price = self.current_prices.get(pair, 0)
                total += qty * price
        return total

    def take_portfolio_snapshot(self) -> PortfolioSnapshot:
        """Take a snapshot of the current portfolio state."""
        with self._lock:
            total_value = self._get_portfolio_value_unlocked()
            unrealized_pnl = 0.0

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
    def current_drawdown(self) -> float:
        """Current drawdown from peak (can recover, unlike max_drawdown)."""
        with self._lock:
            total_value = self._get_portfolio_value_unlocked()
            if self.peak_portfolio_value <= 0:
                return 0.0
            return max(0.0, (self.peak_portfolio_value - total_value) / self.peak_portfolio_value)

    @property
    def portfolio_value(self) -> float:
        """Get current total portfolio value (prefers live Coinbase data)."""
        with self._lock:
            return self._get_portfolio_value_unlocked()

    def _get_win_rate_unlocked(self) -> float:
        """H2 fix: Get win rate without acquiring lock (caller must hold _lock)."""
        completed = self.winning_trades + self.losing_trades
        if completed == 0:
            return 0.0
        return self.winning_trades / completed

    @property
    def win_rate(self) -> float:
        """Get the win rate."""
        with self._lock:
            return self._get_win_rate_unlocked()

    def _get_return_pct_unlocked(self) -> float:
        """H2 fix: Get return percentage without acquiring lock (caller must hold _lock)."""
        if self.initial_balance == 0:
            return 0.0
        pv = self._get_portfolio_value_unlocked()
        return (pv - self.initial_balance) / self.initial_balance

    @property
    def return_pct(self) -> float:
        """Get the total return percentage."""
        with self._lock:
            return self._get_return_pct_unlocked()

    def _get_open_positions_unlocked(self) -> dict[str, float]:
        """H2 fix: Get open positions without acquiring lock (caller must hold _lock)."""
        return {pair: qty for pair, qty in self.positions.items() if abs(qty) > 1e-8}

    @property
    def open_positions(self) -> dict[str, float]:
        """Get all open positions with non-zero quantities."""
        with self._lock:
            return self._get_open_positions_unlocked()

    @property
    def recent_signals(self) -> list[Signal]:
        """Get the 10 most recent signals."""
        with self._lock:
            return list(self.signals)[-10:]

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
        """Get an atomic snapshot of the current state."""
        with self._lock:
            # H2 fix: Use unlocked versions to avoid RLock reentry (fragile pattern)
            return {
                "portfolio_value": self._get_portfolio_value_unlocked(),
                "cash_balance": self.cash_balance,
                "return_pct": self._get_return_pct_unlocked(),
                "total_pnl": self.total_pnl,
                "total_trades": self.total_trades,
                "win_rate": self._get_win_rate_unlocked(),
                "max_drawdown": self.max_drawdown,
                "open_positions": self._get_open_positions_unlocked(),
                "current_prices": dict(self.current_prices),
                "is_running": self.is_running,
                "is_paused": self.is_paused,
                "circuit_breaker": self.circuit_breaker_triggered,
            }

    def save_state(self, filepath: str = None) -> None:
        """Save the current state to a JSON file (atomic write)."""
        if filepath is None:
            filepath = _get_state_file()
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        # H4 fix: Build state_data AND write to temp file both inside the lock
        # to prevent stale snapshot on crash/restart
        with self._lock:
            # H2 fix: Use unlocked versions directly instead of calling to_summary()
            summary = {
                "portfolio_value": self._get_portfolio_value_unlocked(),
                "cash_balance": self.cash_balance,
                "return_pct": self._get_return_pct_unlocked(),
                "total_pnl": self.total_pnl,
                "total_trades": self.total_trades,
                "win_rate": self._get_win_rate_unlocked(),
                "max_drawdown": self.max_drawdown,
                "open_positions": self._get_open_positions_unlocked(),
                "current_prices": dict(self.current_prices),
                "is_running": self.is_running,
                "is_paused": self.is_paused,
                "circuit_breaker": self.circuit_breaker_triggered,
            }
            # HIGH-9: always include ALL open trades even if they fall
            # outside the last-100 window, so warm-restart never loses
            # knowledge of existing positions.
            _recent = self.trades[-100:]
            _open_outside_window = (
                [t for t in self.trades[:-100] if t.is_open]
                if len(self.trades) > 100 else []
            )
            state_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "summary": summary,
                "trades": [t.model_dump(mode="json") for t in _open_outside_window + _recent],
                "signals": [s.model_dump(mode="json") for s in list(self.signals)[-50:]],
            }
            
            # H4 fix: Write to temp file inside lock, then rename (atomic)
            # M20 fix: Use os.replace() instead of Path.replace() which fails
            # on Windows when the target already exists
            tmp_path = path.with_suffix(".tmp")
            try:
                with open(tmp_path, "w") as f:
                    json.dump(state_data, f, indent=2, default=str)
                import os as _os
                _os.replace(str(tmp_path), str(path))
            except Exception as e:
                logger.error(f"Failed to save state: {e}")
                if tmp_path.exists():
                    tmp_path.unlink()
                return

        logger.debug(f"State saved to {filepath}")
