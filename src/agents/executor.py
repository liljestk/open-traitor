"""
Executor Agent — Executes approved trades on Coinbase.
Handles order placement, tracking, and position management.
Supports both market and limit orders based on urgency and confidence.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.agents.base_agent import BaseAgent
from src.core.exchange_client import ExchangeClient
from src.core.rules import AbsoluteRules
from src.models.trade import Trade, TradeAction, TradeStatus
from src.utils.logger import get_logger

logger = get_logger("agent.executor")


class ExecutorAgent(BaseAgent):
    """Executes approved trade proposals on Coinbase."""

    def __init__(
        self,
        llm,
        state,
        config,
        exchange: ExchangeClient,
        rules: AbsoluteRules,
    ):
        super().__init__("executor", llm, state, config)
        self.exchange = exchange
        self.rules = rules
        exec_cfg = config.get("execution", {})
        self.use_limit_orders = exec_cfg.get("use_limit_orders", True)
        self.limit_price_offset_pct = exec_cfg.get("limit_price_offset_pct", 0.001)
        self.urgency_threshold = exec_cfg.get("urgency_confidence_threshold", 0.8)

    def _should_use_limit(self, trade_info: dict) -> bool:
        """
        Decide whether to use a limit order vs. market order.

        Use MARKET when:
          - Stop-loss triggered (urgent)
          - Confidence >= urgency threshold (strong immediate signal)
          - Sell orders from stop-loss/take-profit (need immediate fill)
          - Explicitly requested via order_type

        Use LIMIT when:
          - Normal buy entries (non-urgent, saves on fees)
          - Lower confidence entries (patient accumulation)
        """
        if not self.use_limit_orders:
            return False

        order_type = trade_info.get("order_type", "auto")
        if order_type == "market":
            return False
        if order_type == "limit":
            return True

        # Auto-decide
        action = trade_info.get("action", "hold")
        reason = trade_info.get("reasoning", "").lower()
        confidence = trade_info.get("confidence", 0)

        # Urgent situations → market
        if any(kw in reason for kw in ["stop_loss", "stop loss", "trailing stop", "take_profit", "take profit"]):
            return False
        if action == "sell":
            return False  # Sells should be immediate
        if confidence >= self.urgency_threshold:
            return False  # High confidence → get in NOW

        # Normal buy → limit order to save on fees
        return action == "buy"

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Execute an approved trade.

        Context expected:
            - approved_trade: dict (from RiskManagerAgent)
        """
        trade_info = context.get("approved_trade", {})

        if not trade_info.get("approved", False):
            return {"executed": False, "reason": "Trade not approved"}

        if trade_info.get("needs_approval"):
            return {
                "executed": False,
                "reason": "Waiting for Telegram approval",
                "pending_approval": True,
                "trade_info": trade_info,
            }

        action = trade_info.get("action", "hold")
        if action == "hold":
            return {"executed": False, "reason": "Hold — no trade"}

        pair = trade_info["pair"]
        quote_amount = trade_info.get("quote_amount", trade_info.get("usd_amount", 0))
        quantity = trade_info.get("quantity", 0)
        price = trade_info.get("price", 0)
        stop_loss = trade_info.get("stop_loss")
        take_profit = trade_info.get("take_profit")
        confidence = trade_info.get("confidence", 0)
        reasoning = trade_info.get("reasoning", "")

        use_limit = self._should_use_limit(trade_info)

        # Create Trade record
        trade = Trade(
            pair=pair,
            action=TradeAction.BUY if action == "buy" else TradeAction.SELL,
            quantity=quantity,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            reasoning=reasoning,
        )

        # Execute on Coinbase
        try:
            expected_price = price  # For slippage measurement

            if use_limit and action == "buy":
                # Limit buy: place slightly below current price to ensure maker fee
                limit_price = price * (1 - self.limit_price_offset_pct)
                base_size = str(round(quote_amount / limit_price, 8))
                result = self.exchange.place_limit_order(
                    pair=pair,
                    side="BUY",
                    size=float(base_size),
                    price=limit_price,
                )
                self.logger.info(
                    f"📋 Limit BUY placed for {pair} @ {limit_price:,.2f} "
                    f"(market: {price:,.2f}, offset: {self.limit_price_offset_pct:.2%})"
                )
            elif action == "buy":
                result = self.exchange.place_market_order(
                    pair=pair,
                    side="BUY",
                    amount=quote_amount,
                    amount_is_base=False,
                )
            else:
                result = self.exchange.place_market_order(
                    pair=pair,
                    side="SELL",
                    amount=quantity,
                    amount_is_base=True,
                )

            if result.get("success", True) and "error" not in result:
                order = result.get("order", result)
                order_id = order.get("order_id", "")

                # For limit orders, check if it's resting (OPEN) vs filled
                order_status = order.get("status", "FILLED")
                if order_status == "OPEN":
                    # Limit order resting — record as PENDING
                    trade.status = TradeStatus.PENDING
                    trade.coinbase_order_id = order_id
                    self.state.add_trade(trade)
                    self.logger.info(
                        f"📋 Limit order resting: {trade.to_summary()} — "
                        "will check fill on next cycle"
                    )
                    return {
                        "executed": True,
                        "trade": trade.model_dump(mode="json"),
                        "order": order,
                        "order_type": "limit",
                        "resting": True,
                    }

                # Verify fill status for live orders
                if order_id and not getattr(self.exchange, "paper_mode", False):
                    order = await self._verify_fill(order_id, order)

                fill_status = order.get("status", "FILLED")
                if fill_status == "FILLED" or getattr(self.exchange, "paper_mode", False):
                    trade.status = TradeStatus.FILLED
                elif fill_status in ("CANCELLED", "FAILED", "EXPIRED"):
                    trade.status = TradeStatus.FAILED
                    self.logger.error(
                        f"❌ Order {order_id} ended with status {fill_status!r} — not recording as filled"
                    )
                    return {"executed": False, "error": f"Order {fill_status}", "trade_id": trade.id}
                else:
                    trade.status = TradeStatus.PENDING
                    self.logger.warning(
                        f"⚠️ Order {order_id} fill unconfirmed (status={fill_status!r}) — "
                        "recording as PENDING; verify on next reconciliation cycle."
                    )
                trade.coinbase_order_id = order_id
                trade.filled_price = float(order.get("average_filled_price", price))
                trade.filled_quantity = float(order.get("filled_size", quantity))
                trade.fees = float(order.get("fee", 0))

                # Slippage measurement
                slippage_pct = 0.0
                if expected_price > 0 and trade.filled_price > 0:
                    slippage_pct = (trade.filled_price - expected_price) / expected_price * 100
                    if action == "sell":
                        slippage_pct = -slippage_pct  # For sells, lower fill = negative slippage
                    trade_type = "limit" if use_limit else "market"
                    self.logger.info(
                        f"📊 Slippage: {slippage_pct:+.4f}% "
                        f"(expected={expected_price:,.2f}, filled={trade.filled_price:,.2f}, "
                        f"type={trade_type})"
                    )

                # Record in state and rules
                # H27 fix: if add_trade raises ValueError (insufficient balance/position),
                # the exchange order is already filled — attempt to cancel if resting,
                # otherwise log the orphaned order for manual reconciliation.
                try:
                    self.state.add_trade(trade)
                except ValueError as ve:
                    self.logger.error(
                        f"\u274c add_trade rejected filled order for {pair}: {ve} \u2014 "
                        f"order_id={trade.coinbase_order_id} is orphaned on exchange. "
                        "Manual reconciliation may be needed."
                    )
                    trade.status = TradeStatus.FAILED
                    return {
                        "executed": False,
                        "error": f"State rejected trade: {ve}",
                        "trade_id": trade.id,
                        "orphaned_order_id": trade.coinbase_order_id,
                    }
                self.rules.record_trade(quote_amount, action=action)

                self.logger.info(f"✅ Trade executed: {trade.to_summary()}")

                return {
                    "executed": True,
                    "trade": trade.model_dump(mode="json"),
                    "order": order,
                    "order_type": "limit" if use_limit else "market",
                    "slippage_pct": slippage_pct if expected_price > 0 else 0,
                }
            else:
                trade.status = TradeStatus.FAILED
                error = result.get("error", "Unknown error")
                self.logger.error(f"❌ Trade failed: {error}")
                return {"executed": False, "error": error, "trade_id": trade.id}

        except Exception as e:
            trade.status = TradeStatus.FAILED
            self.logger.error(f"❌ Execution error: {e}", exc_info=True)
            return {"executed": False, "error": str(e), "trade_id": trade.id}

    async def _verify_fill(self, order_id: str, initial_order: dict, max_attempts: int = 8) -> dict:
        """Poll order status to verify fill (for live orders).

        Uses a short initial poll interval (200 ms) with exponential back-off
        so fast fills (the common case for market orders) are confirmed quickly.
        """
        delay = 0.2  # Start at 200 ms
        for attempt in range(max_attempts):
            try:
                order = self.exchange.get_order(order_id)
                status = order.get("status", "")
                if status in ("FILLED", "CANCELLED", "FAILED", "EXPIRED"):
                    self.logger.info(f"Order {order_id} status: {status}")
                    return order
            except Exception as e:
                self.logger.debug(f"Fill check attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)  # Back-off up to 2 s
        self.logger.warning(
            f"⚠️ Order {order_id} fill not confirmed after {max_attempts} attempts — "
            "returning last-known state; position marked PENDING."
        )
        return initial_order

    def close_position_by_pair(self, pair: str, price: float, reason: str, quantity: float = 0) -> dict | None:
        """Find the open BUY trade for *pair* and close it at *price*.

        Called by the orchestrator when a trailing stop fires so that the
        actual sell order is placed — returns the close result dict or None
        if no matching open trade was found.

        If *quantity* > 0, perform a partial sell of that amount instead of
        closing the full position.
        """
        for trade in self.state.get_open_trades():
            if trade.pair == pair and trade.action == TradeAction.BUY:
                if quantity > 0:
                    return self._partial_sell(trade, quantity, price, reason)
                return self._close_position(trade, price, reason)
        self.logger.warning(f"close_position_by_pair: no open BUY trade found for {pair}")
        return None

    def _partial_sell(self, trade: Trade, quantity: float, price: float, reason: str) -> dict:
        """Sell a portion of a position (used for tiered exits)."""
        result = self.exchange.place_market_order(
            pair=trade.pair,
            side="SELL",
            amount=quantity,
            amount_is_base=True,
        )

        success = result.get("success", True) and "error" not in result
        close_price = price
        fees = 0.0

        if success:
            order = result.get("order", result)
            close_price = float(order.get("average_filled_price", price))
            fees = float(order.get("fee", 0))

            # Update trade state via public method (M12 fix: don't access state._lock directly)
            remaining = max(0.0, (trade.filled_quantity or trade.quantity) - quantity)
            if remaining > 0:
                # H5 fix: pass sold_quantity so positions are deducted
                self.state.update_partial_fill(trade.id, remaining, sold_quantity=quantity)
            else:
                # Position fully exited by tiers
                pass

            if remaining <= 0:
                self.state.close_trade(trade.id, close_price, fees)

            pnl = (close_price - (trade.filled_price or trade.price)) * quantity - fees
            self.logger.info(
                f"Partial sell ({reason}): {trade.pair} — "
                f"sold {quantity:.6f} at ${close_price:,.2f}, "
                f"PnL: ${pnl:,.2f}, remaining: {remaining:.6f}"
            )
        else:
            pnl = 0
            self.logger.error(f"❌ Partial sell FAILED for {trade.pair}: {result}")

        return {
            "pair": trade.pair,
            "reason": reason,
            "quantity_sold": quantity,
            "close_price": close_price,
            "pnl": pnl,
            "success": success,
        }

    def check_stop_losses(self) -> list[dict]:
        """Check all open positions against their stop-losses."""
        closed_trades = []

        for trade in self.state.get_open_trades():
            if trade.action != TradeAction.BUY:
                continue

            current_price = self.state.current_prices.get(trade.pair, 0)
            if current_price <= 0:
                continue

            # Check stop-loss
            if trade.stop_loss and current_price <= trade.stop_loss:
                self.logger.warning(
                    f"⚠️ STOP-LOSS triggered for {trade.pair} | "
                    f"Price: ${current_price:,.2f} <= SL: ${trade.stop_loss:,.2f}"
                )
                close_result = self._close_position(trade, current_price, "stop_loss")
                closed_trades.append(close_result)

            # Check take-profit
            elif trade.take_profit and current_price >= trade.take_profit:
                self.logger.info(
                    f"🎯 TAKE-PROFIT hit for {trade.pair} | "
                    f"Price: ${current_price:,.2f} >= TP: ${trade.take_profit:,.2f}"
                )
                close_result = self._close_position(trade, current_price, "take_profit")
                closed_trades.append(close_result)

        return closed_trades

    def _close_position(self, trade: Trade, price: float, reason: str) -> dict:
        """Close a position by selling.

        IMPORTANT: state.close_trade is only called when the sell order succeeds.
        Calling it unconditionally would corrupt the internal position/cash state
        if the exchange rejects the order.
        """
        qty = trade.filled_quantity or trade.quantity

        result = self.exchange.place_market_order(
            pair=trade.pair,
            side="SELL",
            amount=qty,
            amount_is_base=True,
        )

        success = result.get("success", True) and "error" not in result
        close_price = price
        fees = 0.0

        if success:
            order = result.get("order", result)
            close_price = float(order.get("average_filled_price", price))
            fees = float(order.get("fee", 0))

            # C2 fix: check return value — exchange sell already placed
            closed = self.state.close_trade(trade.id, close_price, fees)
            if not closed:
                self.logger.error(
                    f"❌ close_trade returned None for {trade.id} ({trade.pair}) — "
                    "exchange sold but state not updated; potential divergence"
                )
            elif closed.pnl and closed.pnl < 0:
                self.rules.record_loss(abs(closed.pnl))

            self.logger.info(
                f"Position closed ({reason}): {trade.to_summary()}"
            )
        else:
            error = result.get("error", "Unknown error")
            self.logger.error(
                f"❌ Failed to close position for {trade.pair} ({reason}): {error} — "
                f"position state NOT updated; will retry on next cycle."
            )

        return {
            "trade_id": trade.id,
            "pair": trade.pair,
            "close_price": close_price,
            "pnl": trade.pnl if success else None,
            "reason": reason,
            "success": success,
        }

    # =========================================================================
    # Limit Order Lifecycle Management
    # =========================================================================

    # Default TTL for resting limit orders before they are cancelled (seconds)
    _LIMIT_ORDER_TTL: float = 900.0  # 15 minutes

    def check_pending_orders(self) -> list[dict]:
        """Check all PENDING trades and manage their lifecycle.

        For each PENDING trade with a ``coinbase_order_id``:
          - FILLED  → update trade status, record in state + rules
          - CANCELLED / EXPIRED / FAILED → mark trade accordingly, clean up state
          - OPEN and older than ``_LIMIT_ORDER_TTL`` → cancel the order

        Called once per orchestrator cycle.

        Returns a list of dicts describing what happened to each pending order.
        """
        results: list[dict] = []
        pending_trades = [
            t for t in self.state.get_open_trades()
            if t.status == TradeStatus.PENDING and t.coinbase_order_id
        ]

        if not pending_trades:
            return results

        self.logger.info(f"📋 Checking {len(pending_trades)} pending limit order(s)...")

        for trade in pending_trades:
            try:
                order = self.exchange.get_order(trade.coinbase_order_id)
                if not order:
                    self.logger.warning(
                        f"⚠️ Order {trade.coinbase_order_id} for {trade.pair} "
                        "not found — marking as FAILED"
                    )
                    # H4 fix: use state method instead of direct mutation
                    self.state.mark_trade_status(trade.id, TradeStatus.FAILED)
                    results.append({
                        "trade_id": trade.id,
                        "pair": trade.pair,
                        "order_id": trade.coinbase_order_id,
                        "action": "marked_failed",
                        "reason": "order_not_found",
                    })
                    continue

                status = order.get("status", "")

                if status == "FILLED":
                    # H4 fix: update trade atomically under state lock
                    filled_price = float(order.get("average_filled_price", trade.price))
                    filled_quantity = float(order.get("filled_size", trade.quantity))
                    fees = float(order.get("fee", 0))
                    self.state.update_trade_fill(
                        trade.id, filled_price, filled_quantity, fees,
                        status=TradeStatus.FILLED,
                    )

                    # Record spend in rules (for daily limit tracking)
                    quote_amount = filled_price * filled_quantity
                    self.rules.record_trade(quote_amount, action=trade.action.value)

                    self.logger.info(
                        f"✅ Limit order FILLED: {trade.pair} @ "
                        f"{filled_price:,.2f} (qty: {filled_quantity:.6f})"
                    )
                    results.append({
                        "trade_id": trade.id,
                        "pair": trade.pair,
                        "order_id": trade.coinbase_order_id,
                        "action": "filled",
                        "filled_price": filled_price,
                        "filled_quantity": filled_quantity,
                    })

                elif status in ("CANCELLED", "FAILED", "EXPIRED"):
                    # H4 fix: set status under lock
                    new_status = TradeStatus.CANCELLED if status == "CANCELLED" else TradeStatus.FAILED
                    self.state.mark_trade_status(trade.id, new_status)
                    # Reverse the position/cash booking from add_trade
                    self.state.reverse_trade_booking(trade)
                    self.logger.info(
                        f"📋 Pending order {status}: {trade.pair} — "
                        "state booking reversed"
                    )
                    results.append({
                        "trade_id": trade.id,
                        "pair": trade.pair,
                        "order_id": trade.coinbase_order_id,
                        "action": f"marked_{status.lower()}",
                    })

                elif status == "OPEN":
                    # Check if the order has been resting too long
                    age_seconds = (
                        time.time()
                        - trade.timestamp.timestamp()
                    )
                    if age_seconds > self._LIMIT_ORDER_TTL:
                        cancel_result = self._cancel_stale_order(trade)
                        results.append(cancel_result)
                    else:
                        remaining = self._LIMIT_ORDER_TTL - age_seconds
                        self.logger.debug(
                            f"📋 Limit order still resting: {trade.pair} — "
                            f"{remaining:.0f}s until auto-cancel"
                        )

                # Partially filled — let it ride but log
                elif status == "PENDING" or "PARTIAL" in status.upper():
                    self.logger.debug(
                        f"📋 Order {trade.coinbase_order_id} for {trade.pair} "
                        f"still {status} — will recheck next cycle"
                    )

            except Exception as e:
                self.logger.warning(
                    f"⚠️ Failed to check pending order {trade.coinbase_order_id} "
                    f"for {trade.pair}: {e}"
                )

        return results

    def _cancel_stale_order(self, trade: Trade) -> dict:
        """Cancel a resting limit order that has exceeded its TTL."""
        self.logger.info(
            f"⏰ Cancelling stale limit order for {trade.pair} "
            f"(age: {time.time() - trade.timestamp.timestamp():.0f}s > "
            f"TTL: {self._LIMIT_ORDER_TTL:.0f}s)"
        )
        cancel_result = self.exchange.cancel_order(trade.coinbase_order_id)

        if cancel_result.get("success"):
            self.state.mark_trade_status(trade.id, TradeStatus.CANCELLED)
            # Reverse the position/cash booking via public API
            self.state.reverse_trade_booking(trade)
            self.logger.info(
                f"✅ Stale limit order cancelled: {trade.pair} — state reversed"
            )
        else:
            self.logger.warning(
                f"⚠️ Failed to cancel stale order for {trade.pair}: "
                f"{cancel_result.get('error', 'unknown')}"
            )

        return {
            "trade_id": trade.id,
            "pair": trade.pair,
            "order_id": trade.coinbase_order_id,
            "action": "cancelled_stale" if cancel_result.get("success") else "cancel_failed",
        }
