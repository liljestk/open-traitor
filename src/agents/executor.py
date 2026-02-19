"""
Executor Agent — Executes approved trades on Coinbase.
Handles order placement, tracking, and position management.
"""

from __future__ import annotations

import time
from typing import Any

from src.agents.base_agent import BaseAgent
from src.core.coinbase_client import CoinbaseClient
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
        coinbase: CoinbaseClient,
        rules: AbsoluteRules,
    ):
        super().__init__("executor", llm, state, config)
        self.coinbase = coinbase
        self.rules = rules

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
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
        usd_amount = trade_info["usd_amount"]
        quantity = trade_info.get("quantity", 0)
        price = trade_info.get("price", 0)
        stop_loss = trade_info.get("stop_loss")
        take_profit = trade_info.get("take_profit")
        confidence = trade_info.get("confidence", 0)
        reasoning = trade_info.get("reasoning", "")

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
            if action == "buy":
                result = self.coinbase.market_order_buy(
                    product_id=pair,
                    quote_size=str(round(usd_amount, 2)),
                )
            else:
                result = self.coinbase.market_order_sell(
                    product_id=pair,
                    base_size=str(quantity),
                )

            if result.get("success", True) and "error" not in result:
                order = result.get("order", result)
                order_id = order.get("order_id", "")

                # Verify fill status for live orders
                if order_id and not self.coinbase.paper_mode:
                    order = self._verify_fill(order_id, order)

                trade.status = TradeStatus.FILLED
                trade.coinbase_order_id = order_id
                trade.filled_price = float(order.get("average_filled_price", price))
                trade.filled_quantity = float(order.get("filled_size", quantity))
                trade.fees = float(order.get("fee", 0))

                # Record in state and rules
                self.state.add_trade(trade)
                self.rules.record_trade(usd_amount)

                self.logger.info(f"✅ Trade executed: {trade.to_summary()}")

                return {
                    "executed": True,
                    "trade": trade.model_dump(mode="json"),
                    "order": order,
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

    def _verify_fill(self, order_id: str, initial_order: dict, max_attempts: int = 8) -> dict:
        """Poll order status to verify fill (for live orders).

        Uses a short initial poll interval (200 ms) with exponential back-off
        so fast fills (the common case for market orders) are confirmed quickly.
        """
        delay = 0.2  # Start at 200 ms
        for attempt in range(max_attempts):
            try:
                order = self.coinbase.get_order(order_id)
                status = order.get("status", "")
                if status in ("FILLED", "CANCELLED", "FAILED", "EXPIRED"):
                    self.logger.info(f"Order {order_id} status: {status}")
                    return order
            except Exception as e:
                self.logger.debug(f"Fill check attempt {attempt + 1} failed: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 2.0)  # Back-off up to 2 s
        self.logger.warning(f"Order {order_id} fill not confirmed after {max_attempts} attempts")
        return initial_order

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

        result = self.coinbase.market_order_sell(
            product_id=trade.pair,
            base_size=str(qty),
        )

        success = result.get("success", True) and "error" not in result
        close_price = price
        fees = 0.0

        if success:
            order = result.get("order", result)
            close_price = float(order.get("average_filled_price", price))
            fees = float(order.get("fee", 0))

            self.state.close_trade(trade.id, close_price, fees)

            if trade.pnl and trade.pnl < 0:
                self.rules.record_loss(abs(trade.pnl))

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
