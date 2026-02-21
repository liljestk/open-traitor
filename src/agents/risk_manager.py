"""
Risk Manager Agent — Validates and adjusts trade proposals.
Applies position sizing (Kelly Criterion), enforces rules,
manages stop-losses, and accounts for portfolio correlation.
"""

from __future__ import annotations

import math
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

    Position sizing hierarchy:
      1. Kelly Criterion (half-Kelly) based on historical win rate
      2. ATR volatility adjustment
      3. Correlation penalty for correlated positions
      4. Cap by max_position_pct config
    """

    def __init__(self, llm, state, config, rules: AbsoluteRules):
        super().__init__("risk_manager", llm, state, config)
        self.rules = rules
        self.risk_config = config.get("risk", {})
        self.trading_config = config.get("trading", {})
        self.stop_loss_pct = self.risk_config.get("stop_loss_pct", 0.03)
        self.take_profit_pct = self.risk_config.get("take_profit_pct", 0.06)
        self.use_kelly = self.risk_config.get("use_kelly_criterion", True)
        self.kelly_fraction = self.risk_config.get("kelly_fraction", 0.5)  # Half-Kelly
        self.use_correlation_penalty = self.risk_config.get("use_correlation_penalty", True)
        self.correlation_threshold = self.risk_config.get("correlation_threshold", 0.7)

    def _compute_kelly_size(
        self,
        portfolio_value: float,
        win_rate: float = 0.0,
        avg_win: float = 0.0,
        avg_loss: float = 0.0,
    ) -> float:
        """
        Compute Half-Kelly position size.

        Kelly formula: f* = (p * b - q) / b
          where p = win probability, q = 1-p, b = avg_win / avg_loss

        We use half-Kelly (0.5 * f*) for safety — reduces variance significantly
        while retaining ~75% of full-Kelly growth rate.

        Returns maximum position size as a fraction of portfolio.
        """
        if win_rate <= 0 or avg_win <= 0 or avg_loss <= 0:
            # Insufficient data → fallback to config max
            return self.risk_config.get("max_position_pct", 0.05)

        p = min(max(win_rate, 0.01), 0.99)
        q = 1 - p
        b = avg_win / avg_loss  # Win/loss ratio

        kelly_f = (p * b - q) / b

        if kelly_f <= 0:
            # Kelly says don't bet (negative edge)
            self.logger.info(
                f"Kelly: negative edge (f*={kelly_f:.4f}, win_rate={p:.2f}, "
                f"W/L ratio={b:.2f}) — minimum position"
            )
            return 0.01  # Minimum 1% if we still proceed

        # Apply fraction (half-Kelly by default)
        position_frac = kelly_f * self.kelly_fraction

        # Cap at config max
        max_pct = self.risk_config.get("max_position_pct", 0.05)
        position_frac = min(position_frac, max_pct)

        self.logger.info(
            f"Kelly: f*={kelly_f:.4f}, half-Kelly={kelly_f * self.kelly_fraction:.4f}, "
            f"capped={position_frac:.4f} | "
            f"win_rate={p:.2f}, W/L={b:.2f}"
        )

        return position_frac

    def _compute_correlation_penalty(
        self,
        pair: str,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
    ) -> float:
        """
        Reduce position size if the new asset is highly correlated with
        existing open positions.

        Returns a multiplier between 0.5 and 1.0.
        """
        if not self.use_correlation_penalty or not correlation_matrix:
            return 1.0

        base_currency = pair.split("-")[0] if "-" in pair else pair
        open_bases = set()
        for trade in self.state.get_open_trades():
            if trade.action == TradeAction.BUY:
                open_base = trade.pair.split("-")[0] if "-" in trade.pair else trade.pair
                open_bases.add(open_base)

        if not open_bases:
            return 1.0

        max_corr = 0.0
        for open_base in open_bases:
            # Check correlation between new asset and each existing position
            corr = 0.0
            if base_currency in correlation_matrix:
                for existing_pair_key in correlation_matrix[base_currency]:
                    if open_base in existing_pair_key:
                        corr = abs(correlation_matrix[base_currency].get(existing_pair_key, 0))
                        break
            # Also check reverse direction
            for existing_pair_key in correlation_matrix:
                if open_base in existing_pair_key:
                    corr = max(corr, abs(
                        correlation_matrix.get(existing_pair_key, {}).get(base_currency, 0)
                    ))

            max_corr = max(max_corr, corr)

        if max_corr >= self.correlation_threshold:
            # High correlation: reduce position by up to 50%
            penalty = 1.0 - (max_corr - self.correlation_threshold) / (1.0 - self.correlation_threshold) * 0.5
            penalty = max(0.5, min(1.0, penalty))
            self.logger.info(
                f"Correlation penalty: {pair} has {max_corr:.2f} corr with open positions → "
                f"size multiplier={penalty:.2f}"
            )
            return penalty

        return 1.0

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
            - win_rate: float (optional, from StatsDB for Kelly sizing)
            - avg_win: float (optional, average winning trade %)
            - avg_loss: float (optional, average losing trade %)
            - correlation_matrix: dict (optional, from PairsCorrelationMonitor)
        """
        proposal = context.get("proposal", {})
        portfolio_value = context.get("portfolio_value", 0)
        cash_balance = context.get("cash_balance", 0)
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        win_rate = context.get("win_rate", 0)
        avg_win = context.get("avg_win", 0)
        avg_loss = context.get("avg_loss", 0)
        correlation_matrix = context.get("correlation_matrix")

        action = proposal.get("action", "hold")

        # Hold = no risk check needed
        if action == "hold":
            return {"approved": True, "action": "hold", "reason": "No trade proposed"}

        pair = proposal.get("pair", "BTC-EUR")
        quote_amount = float(proposal.get("quote_amount", proposal.get("usd_amount", 0)) or 0)
        price = float(proposal.get("current_price", 0) or self.state.current_prices.get(pair, 0))
        stop_loss = proposal.get("stop_loss_price")
        take_profit = proposal.get("take_profit_price")

        # Get ATR from pipeline context (computed by TechnicalAnalyzer)
        atr = context.get("atr")

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

        # ===== POSITION SIZING =====
        # Step 1: Start with Kelly Criterion or config max
        if self.use_kelly and action == "buy" and portfolio_value > 0:
            kelly_frac = self._compute_kelly_size(
                portfolio_value, win_rate, avg_win, avg_loss
            )
            max_position = portfolio_value * kelly_frac
        else:
            max_position = portfolio_value * self.risk_config.get("max_position_pct", 0.05)

        # Step 2: ATR volatility adjustment
        if atr and price > 0 and action == "buy":
            float_atr = float(atr)
            atr_pct = float_atr / price
            # If ATR is > 5% of price, reduce position size (high volatility)
            if atr_pct > 0.05:
                volatility_reduction = min(0.5, (0.05 / atr_pct))  # Cap reduction at 50%
                max_position = max_position * volatility_reduction
                self.logger.info(f"High volatility detected (ATR {atr_pct:.1%}). Reduced max position size by {1-volatility_reduction:.1%}.")

        # Step 3: Correlation penalty
        if action == "buy":
            corr_mult = self._compute_correlation_penalty(pair, correlation_matrix)
            if corr_mult < 1.0:
                max_position *= corr_mult

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
        # H26 fix: stop_loss/take_profit are None for sell orders
        sl_str = f"{stop_loss:,.2f}" if stop_loss is not None else "N/A"
        tp_str = f"{take_profit:,.2f}" if take_profit is not None else "N/A"
        self.logger.info(
            f"{status}: {action.upper()} {quote_amount:,.2f} of {pair} | "
            f"SL: {sl_str} | TP: {tp_str}"
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
