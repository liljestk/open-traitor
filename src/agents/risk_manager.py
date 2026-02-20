"""
Risk Manager Agent — Validates and adjusts trade proposals.
Applies position sizing, enforces rules, and manages stop-losses.
"""

from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.core.rules import AbsoluteRules
from src.models.trade import TradeAction
from src.utils.logger import get_logger

logger = get_logger("agent.risk_manager")


class RiskManagerAgent(BaseAgent):
    """
    Validates trade proposals against absolute rules and risk parameters.
    This agent has the final say before a trade reaches the executor.
    """

    def __init__(self, llm, state, config, rules: AbsoluteRules):
        super().__init__("risk_manager", llm, state, config)
        self.rules = rules
        self.risk_config = config.get("risk", {})
        self.trading_config = config.get("trading", {})
        self.stop_loss_pct = self.risk_config.get("stop_loss_pct", 0.03)
        self.take_profit_pct = self.risk_config.get("take_profit_pct", 0.06)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Validate and potentially adjust a trade proposal.

        Context expected:
            - proposal: dict (from StrategistAgent)
            - portfolio_value: float
            - cash_balance: float
            - cycle_id: str (optional, for reasoning persistence)
            - stats_db: StatsDB instance (optional)
            - trace_ctx: TraceContext (optional, for Langfuse tracing)
        """
        proposal = context.get("proposal", {})
        portfolio_value = context.get("portfolio_value", 0)
        cash_balance = context.get("cash_balance", 0)
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")

        action = proposal.get("action", "hold")

        # Hold = no risk check needed
        if action == "hold":
            return {"approved": True, "action": "hold", "reason": "No trade proposed"}

        pair = proposal.get("pair", "BTC-EUR")
        quote_amount = float(proposal.get("quote_amount", proposal.get("usd_amount", 0)) or 0)
        price = float(proposal.get("current_price", 0) or self.state.current_prices.get(pair, 0))
        stop_loss = proposal.get("stop_loss_price")
        take_profit = proposal.get("take_profit_price")

        # Get ATR if available from the latest signal
        latest_signal = self.state.latest_signals.get(pair)
        atr = None
        if latest_signal and hasattr(latest_signal, "technical") and latest_signal.technical:
            atr = getattr(latest_signal.technical, "atr", None)

        # If no amount specified, use quantity * price
        quantity = float(proposal.get("quantity", 0) or 0)
        if quote_amount <= 0 and quantity > 0 and price > 0:
            quote_amount = quantity * price

        # If still no amount, reject
        if quote_amount <= 0:
            return {
                "approved": False,
                "action": action,
                "reason": "No valid trade amount specified",
            }

        # Enforce max_open_positions for buy orders
        if action == "buy":
            max_positions = self.trading_config.get("max_open_positions", 3)
            current_positions = len(self.state.open_positions)
            if current_positions >= max_positions:
                self.logger.info(
                    f"🚫 Trade rejected: {current_positions} open positions "
                    f"(max {max_positions})"
                )
                return {
                    "approved": False,
                    "action": action,
                    "pair": pair,
                    "quote_amount": quote_amount,
                    "reason": (
                        f"Max open positions reached ({current_positions}/{max_positions}). "
                        f"Close an existing position before opening a new one."
                    ),
                }

        # Ensure stop-loss
        has_stop_loss = stop_loss is not None
        if not has_stop_loss and action == "buy" and price > 0:
            if atr:
                # Use 2x ATR for stop loss
                float_atr = float(atr)
                stop_loss = price - (2 * float_atr)
                self.logger.info(f"Added ATR-based stop-loss (2x ATR={float_atr:.2f}): {stop_loss:,.2f}")
            else:
                stop_loss = price * (1 - self.stop_loss_pct)
                self.logger.info(f"Added default percentage stop-loss: {stop_loss:,.2f}")
            has_stop_loss = True

        # Ensure take-profit
        if take_profit is None and action == "buy" and price > 0:
            if atr:
                # Use 3x ATR for take profit (1.5 risk/reward)
                float_atr = float(atr)
                take_profit = price + (3 * float_atr)
                self.logger.info(f"Added ATR-based take-profit (3x ATR={float_atr:.2f}): {take_profit:,.2f}")
            else:
                take_profit = price * (1 + self.take_profit_pct)
                self.logger.info(f"Added default percentage take-profit: {take_profit:,.2f}")

        # ===== CHECK ABSOLUTE RULES =====
        trade_action = TradeAction.BUY if action == "buy" else TradeAction.SELL
        is_allowed, violations, needs_approval = self.rules.check_trade(
            pair=pair,
            action=trade_action,
            quote_value=quote_amount,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
            has_stop_loss=has_stop_loss,
        )

        if not is_allowed:
            violation_text = "; ".join(str(v) for v in violations)
            self.logger.warning(f"🚫 Trade REJECTED by absolute rules: {violation_text}")
            return {
                "approved": False,
                "action": action,
                "pair": pair,
                "quote_amount": quote_amount,
                "reason": f"Absolute rule violation: {violation_text}",
                "violations": [str(v) for v in violations],
            }

        # Adjust position size if too large relative to portfolio
        max_position = portfolio_value * self.risk_config.get("max_position_pct", 0.05)
        
        # Volatility-adjusted position sizing
        if atr and price > 0 and action == "buy":
            float_atr = float(atr)
            atr_pct = float_atr / price
            # If ATR is > 5% of price, reduce position size (high volatility)
            if atr_pct > 0.05:
                volatility_reduction = min(0.5, (0.05 / atr_pct))  # Cap reduction at 50%
                max_position = max_position * volatility_reduction
                self.logger.info(f"High volatility detected (ATR {atr_pct:.1%}). Reduced max position size by {1-volatility_reduction:.1%}.")
                
        if quote_amount > max_position:
            original = quote_amount
            quote_amount = max_position
            self.logger.info(
                f"Adjusted position size: {original:,.2f} → {quote_amount:,.2f} "
                f"(max allowed: {max_position:,.2f})"
            )

        # Calculate final quantity
        # For sells with a specific quantity from the strategist (e.g. pre-existing
        # holdings), preserve the original quantity rather than recalculating.
        if action == "sell" and quantity > 0:
            # Strategist specified exact quantity from ACTUAL COINBASE HOLDINGS
            pass  # keep quantity as-is
        elif price > 0:
            quantity = quote_amount / price

        result = {
            "approved": True,
            "needs_approval": needs_approval,
            "action": action,
            "pair": pair,
            "quote_amount": quote_amount,
            "quantity": quantity,
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": proposal.get("confidence", 0),
            "reasoning": proposal.get("reasoning", ""),
        }

        status = "✅ APPROVED" if not needs_approval else "⚠️ NEEDS TELEGRAM APPROVAL"
        self.logger.info(
            f"{status}: {action.upper()} {quote_amount:,.2f} of {pair} | "
            f"SL: {stop_loss:,.2f} | TP: {take_profit:,.2f}"
        )

        # Persist risk decision trace (no LLM call — rule-based, so tokens=0)
        if stats_db and cycle_id:
            try:
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair=pair,
                    agent_name="risk_manager",
                    reasoning_json=result,
                    signal_type=action,
                    confidence=float(proposal.get("confidence", 0)),
                )
            except Exception as e:
                self.logger.debug(f"Failed to save risk_manager trace: {e}")

        return result
