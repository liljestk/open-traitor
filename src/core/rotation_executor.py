"""
Rotation Executor — Swap execution, routing, rules checks, and failure recovery.

Extracted from PortfolioRotator to keep files under 1000 lines.
Used as a mixin: PortfolioRotator inherits from RotationExecutorMixin.
"""

from __future__ import annotations

import time
from typing import Any, Optional, TYPE_CHECKING

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.core.route_finder import RouteLeg, SwapRoute
    from src.core.rules import AbsoluteRules
    from src.models.trade import TradeAction

logger = get_logger("core.rotation_executor")


class RotationExecutorMixin:
    """Mixin providing swap execution, rules checking, and failure recovery.

    Expects the host class to provide:
      - self.coinbase         (ExchangeClient)
      - self.rules            (AbsoluteRules | None)
      - self.high_stakes      (HighStakesManager)
      - self.journal          (TradeJournal | None)
      - self.audit            (AuditLog | None)
      - self._set_last_swap_times(*pairs)
      - self._record_rotation_leg(quote_value, action, leg_name)
    """

    def execute_swap(
        self,
        proposal,  # SwapProposal
        portfolio_value: float = 0.0,
        cash_balance: float = 0.0,
    ) -> dict:
        """
        Execute a crypto-to-crypto swap (sell A → buy B).
        Dispatches to the optimal route (direct / bridged / fiat-routed).

        Includes AbsoluteRules gate on each leg and partial-failure recovery.
        """
        logger.info(
            f"🔄 Executing swap: {proposal.sell_pair} → {proposal.buy_pair} "
            f"({proposal.quote_amount:.2f})"
            f"{f' via {proposal.route.route_type}' if proposal.route else ' (legacy)'}"
        )

        result = {
            "executed": False,
            "sell_pair": proposal.sell_pair,
            "buy_pair": proposal.buy_pair,
            "sell_result": None,
            "buy_result": None,
            "route_type": proposal.route.route_type if proposal.route else "legacy",
            "bridge_currency": (
                proposal.route.bridge_currency if proposal.route else None
            ),
            "n_legs": proposal.route.n_legs if proposal.route else 2,
        }

        try:
            if proposal.route:
                if proposal.route.route_type == "direct":
                    return self._execute_direct(
                        proposal, result, portfolio_value, cash_balance
                    )
                elif proposal.route.route_type == "bridged":
                    return self._execute_bridged(
                        proposal, result, portfolio_value, cash_balance
                    )
                else:
                    return self._execute_fiat_routed(
                        proposal, result, portfolio_value, cash_balance
                    )
            else:
                return self._execute_legacy(
                    proposal, result, portfolio_value, cash_balance
                )
        except Exception as e:
            logger.error(f"Swap execution error: {e}", exc_info=True)
            result["error"] = str(e)
            return result

    # ─── Rules gate ──────────────────────────────────────────────────────

    def _check_leg_rules(
        self,
        pair: str,
        action_str: str,
        quote_value: float,
        portfolio_value: float,
        cash_balance: float,
    ) -> tuple[bool, str]:
        """
        Validate a single trade leg against AbsoluteRules.
        Returns (allowed, reason).
        """
        if not self.rules:
            return True, ""

        from src.models.trade import TradeAction

        action = TradeAction.SELL if action_str == "sell" else TradeAction.BUY

        allowed, violations, _ = self.rules.check_trade(
            pair=pair,
            action=action,
            quote_value=quote_value,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
            has_stop_loss=False,
        )

        if not allowed:
            reasons = "; ".join(str(v) for v in violations)
            logger.warning(
                f"🚫 AbsoluteRules blocked swap leg {pair} {action_str}: {reasons}"
            )
            return False, reasons

        return True, ""

    # ─── Direct (1-leg) swap ─────────────────────────────────────────────

    def _execute_direct(
        self,
        proposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Execute a direct 1-leg swap via a direct trading pair."""
        route = proposal.route
        leg = route.legs[0]
        logger.info(
            f"🔄 Direct swap: {leg.product_id} ({leg.side}) — 1-leg swap"
        )

        allowed, reason = self._check_leg_rules(
            leg.product_id,
            leg.side,
            proposal.quote_amount,
            portfolio_value,
            cash_balance,
        )
        if not allowed:
            result["error"] = f"AbsoluteRules blocked: {reason}"
            return result

        try:
            leg_quote_value = proposal.quote_amount
            if leg.side == "sell":
                sell_price = self.coinbase.get_current_price(leg.product_id)
                if sell_price <= 0:
                    result["error"] = f"Cannot get price for {leg.product_id}"
                    return result
                sell_pair_price = self.coinbase.get_current_price(
                    proposal.sell_pair
                )
                if sell_pair_price <= 0:
                    sell_pair_price = sell_price
                sell_qty = proposal.quote_amount / sell_pair_price
                direct_result = self.coinbase.market_order_sell(
                    product_id=leg.product_id,
                    base_size=str(round(sell_qty, 8)),
                )
            else:
                buy_price = self.coinbase.get_current_price(leg.product_id)
                if buy_price <= 0:
                    result["error"] = f"Cannot get price for {leg.product_id}"
                    return result
                sell_pair_price = self.coinbase.get_current_price(
                    proposal.sell_pair
                )
                if sell_pair_price <= 0:
                    sell_pair_price = buy_price
                quote_for_buy = (
                    proposal.quote_amount / sell_pair_price * buy_price
                )
                leg_quote_value = quote_for_buy
                direct_result = self.coinbase.market_order_buy(
                    product_id=leg.product_id,
                    quote_size=str(round(quote_for_buy, 2)),
                )

            if direct_result and not direct_result.get("error"):
                result["sell_result"] = direct_result
                result["buy_result"] = direct_result
                result["executed"] = True
                result["direct_pair"] = leg.product_id
                self._record_rotation_leg(
                    leg_quote_value, leg.side, "direct"
                )
                self._set_last_swap_times(
                    proposal.sell_pair, proposal.buy_pair
                )
                self._log_swap(
                    proposal, result, "swap_direct", "rotation_direct"
                )
                logger.info(
                    f"✅ Direct swap completed: {proposal.sell_pair} → "
                    f"{proposal.buy_pair} via {leg.product_id}"
                )
                proposal.executed = True
            else:
                error = (
                    direct_result.get("error", "Unknown")
                    if direct_result
                    else "No result"
                )
                logger.warning(
                    f"Direct swap failed: {error} — "
                    f"falling back to fiat routing"
                )
                return self._execute_fiat_routed(
                    proposal, result, portfolio_value, cash_balance
                )
        except Exception as de:
            logger.warning(
                f"Direct swap failed, falling back to fiat routing: {de}"
            )
            return self._execute_fiat_routed(
                proposal, result, portfolio_value, cash_balance
            )

        return result

    # ─── Bridged (2-leg) swap ────────────────────────────────────────────

    def _execute_bridged(
        self,
        proposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Execute a 2-leg bridged swap (sell → bridge → buy)."""
        route = proposal.route
        leg1, leg2 = route.legs[0], route.legs[1]
        bridge = route.bridge_currency

        logger.info(
            f"🔄 Bridged swap: {proposal.sell_pair} → {bridge} → "
            f"{proposal.buy_pair} | Leg1: {leg1.product_id}({leg1.side}) | "
            f"Leg2: {leg2.product_id}({leg2.side})"
        )

        # AbsoluteRules gate — check BOTH legs before executing either
        for leg_info in [(leg1, "Leg1"), (leg2, "Leg2")]:
            leg, label = leg_info
            allowed, reason = self._check_leg_rules(
                leg.product_id,
                leg.side,
                proposal.quote_amount,
                portfolio_value,
                cash_balance,
            )
            if not allowed:
                result["error"] = f"AbsoluteRules blocked {label}: {reason}"
                return result

        # ── Leg 1: sell_asset → bridge_currency ──
        try:
            sell_price = self.coinbase.get_current_price(proposal.sell_pair)
            if sell_price <= 0:
                result["error"] = f"Cannot get price for {proposal.sell_pair}"
                return result

            if leg1.side == "sell":
                sell_qty = proposal.quote_amount / sell_price
                leg1_result = self.coinbase.market_order_sell(
                    product_id=leg1.product_id,
                    base_size=str(round(sell_qty, 8)),
                )
            else:
                leg1_result = self.coinbase.market_order_buy(
                    product_id=leg1.product_id,
                    quote_size=str(round(proposal.quote_amount, 2)),
                )

            if not leg1_result or leg1_result.get("error"):
                logger.error(f"Bridged swap leg1 failed: {leg1_result}")
                result["error"] = f"Leg1 failed: {leg1_result}"
                return result

            result["sell_result"] = leg1_result

            order = leg1_result.get("order", leg1_result)
            bridge_amount = float(
                order.get("filled_value", proposal.quote_amount)
            )
            leg1_fee = float(order.get("fee", 0))
            bridge_amount -= leg1_fee
            self._record_rotation_leg(
                proposal.quote_amount, leg1.side, "bridged_leg1"
            )
        except Exception as e:
            logger.error(f"Bridged swap leg1 exception: {e}")
            result["error"] = f"Leg1 exception: {e}"
            return result

        # ── Leg 2: bridge_currency → buy_asset ──
        try:
            if leg2.side == "buy":
                leg2_result = self.coinbase.market_order_buy(
                    product_id=leg2.product_id,
                    quote_size=str(round(bridge_amount, 2)),
                )
            else:
                bridge_price = self.coinbase.get_current_price(
                    leg2.product_id
                )
                if bridge_price > 0:
                    sell_qty = bridge_amount / bridge_price
                    leg2_result = self.coinbase.market_order_sell(
                        product_id=leg2.product_id,
                        base_size=str(round(sell_qty, 8)),
                    )
                else:
                    leg2_result = None

            if not leg2_result or leg2_result.get("error"):
                logger.error(
                    f"⚠️ Bridged swap leg2 FAILED after leg1 succeeded! "
                    f"Holding {bridge_amount:.2f} of bridge currency {bridge}. "
                    f"Attempting reversal..."
                )
                result["error"] = (
                    f"Leg2 failed (leg1 succeeded): {leg2_result}"
                )
                result["partial"] = True
                result["bridge_stuck_amount"] = bridge_amount
                result["bridge_stuck_currency"] = bridge

                self._attempt_bridge_reversal(
                    proposal, leg1, bridge, bridge_amount, result
                )
                return result

            result["buy_result"] = leg2_result
            result["executed"] = True

            self._record_rotation_leg(
                bridge_amount, leg2.side, "bridged_leg2"
            )
            self._set_last_swap_times(
                proposal.sell_pair, proposal.buy_pair
            )
            self._log_swap(
                proposal, result, "swap_bridged", "rotation_bridged"
            )
            logger.info(
                f"✅ Bridged swap completed: {proposal.sell_pair} → "
                f"{bridge} → {proposal.buy_pair} | "
                f"fees: {proposal.fee_estimate.total_fee_quote:.2f}"
            )
            proposal.executed = True
        except Exception as e:
            logger.error(
                f"⚠️ Bridged swap leg2 exception after leg1 succeeded: {e}. "
                f"Attempting reversal..."
            )
            result["error"] = f"Leg2 exception (leg1 succeeded): {e}"
            result["partial"] = True
            result["bridge_stuck_amount"] = bridge_amount
            result["bridge_stuck_currency"] = bridge
            self._attempt_bridge_reversal(
                proposal, leg1, bridge, bridge_amount, result
            )

        return result

    # ─── Failure recovery ────────────────────────────────────────────────

    def _attempt_bridge_reversal(
        self,
        proposal,
        leg1,
        bridge: str,
        bridge_amount: float,
        result: dict,
    ) -> None:
        """
        Attempt to reverse a failed bridged swap by selling the bridge
        back to the original asset.
        """
        try:
            sell_base = proposal.sell_pair.split("-")[0]
            reverse_pair = (
                self.coinbase.find_direct_pair(bridge, sell_base)
                if hasattr(self.coinbase, "find_direct_pair")
                else None
            )
            if reverse_pair:
                rev_pair_id, rev_direction = reverse_pair
                logger.info(
                    f"🔄 Attempting bridge reversal via {rev_pair_id} "
                    f"({rev_direction})"
                )
                if rev_direction == "sell":
                    bridge_price = self.coinbase.get_current_price(
                        rev_pair_id
                    )
                    if bridge_price > 0:
                        rev_qty = bridge_amount / bridge_price
                        rev_result = self.coinbase.market_order_sell(
                            product_id=rev_pair_id,
                            base_size=str(round(rev_qty, 8)),
                        )
                    else:
                        rev_result = None
                else:
                    rev_result = self.coinbase.market_order_buy(
                        product_id=rev_pair_id,
                        quote_size=str(round(bridge_amount, 2)),
                    )

                if rev_result and not rev_result.get("error"):
                    logger.info(
                        f"✅ Bridge reversal succeeded: recovered to {sell_base}"
                    )
                    result["reversal"] = "success"
                    result["reversal_result"] = rev_result
                    self._record_rotation_leg(
                        bridge_amount, "sell", "bridge_reversal"
                    )
                    return

            logger.warning(
                f"⚠️ Bridge reversal FAILED. Stuck holding "
                f"{bridge_amount:.4f} {bridge}. "
                f"Will be treated as new holding in next rotation cycle."
            )
            result["reversal"] = "failed"
        except Exception as re:
            logger.error(f"Bridge reversal exception: {re}")
            result["reversal"] = "error"
            result["reversal_error"] = str(re)

        if self.audit:
            self.audit.log(
                "bridge_stuck_position",
                {
                    "original_sell": proposal.sell_pair,
                    "intended_buy": proposal.buy_pair,
                    "bridge_currency": bridge,
                    "bridge_amount": bridge_amount,
                    "reversal_attempted": True,
                    "reversal_result": result.get("reversal", "unknown"),
                },
            )

    def _attempt_fiat_reversal(
        self,
        proposal,
        fiat_amount: float,
        result: dict,
    ) -> None:
        """Attempt to reverse failed fiat-routed swap by buying back."""
        try:
            reversal = self.coinbase.market_order_buy(
                product_id=proposal.sell_pair,
                quote_size=str(round(fiat_amount, 2)),
            )
            if reversal and not reversal.get("error"):
                result["reversal"] = "success"
                result["reversal_result"] = reversal
                logger.info(
                    f"✅ Fiat reversal succeeded: bought back "
                    f"{proposal.sell_pair} for {fiat_amount:.2f}"
                )
                return

            result["reversal"] = "failed"
            result["reversal_error"] = str(reversal)
            logger.warning(
                f"⚠️ Fiat reversal failed for {proposal.sell_pair}: "
                f"{reversal}"
            )
        except Exception as re:
            result["reversal"] = "error"
            result["reversal_error"] = str(re)
            logger.error(f"Fiat reversal exception: {re}")

        if self.audit:
            self.audit.log(
                "fiat_swap_partial_failure",
                {
                    "original_sell": proposal.sell_pair,
                    "intended_buy": proposal.buy_pair,
                    "fiat_amount": fiat_amount,
                    "reversal_attempted": True,
                    "reversal_result": result.get("reversal", "unknown"),
                    "reversal_error": result.get("reversal_error"),
                },
            )

    # ─── Fiat-routed (2-leg) swap ────────────────────────────────────────

    def _execute_fiat_routed(
        self,
        proposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Execute a fiat-routed swap (sell A → fiat → buy B)."""
        result["route_type"] = "fiat"

        for pair, action in [
            (proposal.sell_pair, "sell"),
            (proposal.buy_pair, "buy"),
        ]:
            allowed, reason = self._check_leg_rules(
                pair, action, proposal.quote_amount,
                portfolio_value, cash_balance,
            )
            if not allowed:
                result["error"] = (
                    f"AbsoluteRules blocked {pair} {action}: {reason}"
                )
                return result

        # Step 1: Sell the weak asset
        sell_price = self.coinbase.get_current_price(proposal.sell_pair)
        if sell_price <= 0:
            result["error"] = f"Cannot get price for {proposal.sell_pair}"
            return result
        sell_quantity = proposal.quote_amount / sell_price

        sell_result = self.coinbase.market_order_sell(
            product_id=proposal.sell_pair,
            base_size=str(round(sell_quantity, 8)),
        )

        if not sell_result or sell_result.get("error"):
            logger.error(f"Swap sell failed: {sell_result}")
            result["error"] = f"Sell failed: {sell_result}"
            return result

        result["sell_result"] = sell_result

        order = sell_result.get("order", sell_result)
        actual_proceeds = float(
            order.get("filled_value", proposal.quote_amount)
        )
        sell_fee = float(order.get("fee", 0))
        actual_proceeds -= sell_fee
        self._record_rotation_leg(actual_proceeds, "sell", "fiat_leg1")

        # Step 2: Buy the strong asset
        buy_result = self.coinbase.market_order_buy(
            product_id=proposal.buy_pair,
            quote_size=str(round(actual_proceeds, 2)),
        )

        if not buy_result or buy_result.get("error"):
            logger.error(
                f"⚠️ Swap buy failed after sell! "
                f"Sold {proposal.sell_pair} but couldn't buy "
                f"{proposal.buy_pair}. Proceeds: {actual_proceeds:.2f}"
            )
            result["error"] = (
                f"Buy failed (sell succeeded): {buy_result}"
            )
            result["partial"] = True
            result["partial_failure_type"] = "fiat_routed"
            result["fiat_stuck_amount"] = actual_proceeds
            result["alert_message"] = (
                f"⚠️ Rotation partial failure "
                f"({proposal.sell_pair}→{proposal.buy_pair}): "
                f"sell leg succeeded, buy leg failed. "
                f"Attempting auto-reversal."
            )
            self._attempt_fiat_reversal(proposal, actual_proceeds, result)
            return result

        result["buy_result"] = buy_result
        result["executed"] = True
        self._record_rotation_leg(actual_proceeds, "buy", "fiat_leg2")

        self._set_last_swap_times(proposal.sell_pair, proposal.buy_pair)
        self._log_swap(proposal, result, "swap", "rotation")
        logger.info(
            f"✅ Fiat-routed swap completed: {proposal.sell_pair} → "
            f"{proposal.buy_pair} | {proposal.quote_amount:.2f} | "
            f"fees: {proposal.fee_estimate.total_fee_quote:.2f}"
        )
        proposal.executed = True
        return result

    # ─── Legacy fallback ─────────────────────────────────────────────────

    def _execute_legacy(
        self,
        proposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Legacy execution path — tries direct pair, then fiat routing."""
        result["route_type"] = "legacy"

        direct = None
        if hasattr(self.coinbase, "find_direct_pair"):
            sell_base = proposal.sell_pair.split("-")[0]
            buy_base = proposal.buy_pair.split("-")[0]
            direct = self.coinbase.find_direct_pair(sell_base, buy_base)

        if direct:
            from src.core.route_finder import RouteLeg, SwapRoute

            direct_pair, direction = direct
            leg = RouteLeg(
                product_id=direct_pair,
                side=direction,
                base_currency=sell_base,
                quote_currency=buy_base,
            )
            proposal.route = SwapRoute(
                sell_asset=sell_base,
                buy_asset=buy_base,
                route_type="direct",
                legs=[leg],
                n_legs=1,
            )
            result["route_type"] = "direct"
            return self._execute_direct(
                proposal, result, portfolio_value, cash_balance
            )

        return self._execute_fiat_routed(
            proposal, result, portfolio_value, cash_balance
        )

    # ─── Swap logging ────────────────────────────────────────────────────

    def _log_swap(
        self,
        proposal,
        result: dict,
        action: str,
        signal_type: str,
    ) -> None:
        """Centralized swap logging to journal and audit."""
        if self.journal:
            self.journal.log_trade(
                pair=f"{proposal.sell_pair}→{proposal.buy_pair}",
                action=action,
                quantity=0,
                price=0,
                quote_amount=proposal.quote_amount,
                fee=proposal.fee_estimate.total_fee_quote,
                confidence=proposal.confidence,
                signal_type=signal_type,
                reasoning=proposal.reasoning,
            )

        if self.audit:
            self.audit.log(
                "swap_execution",
                {
                    "sell_pair": proposal.sell_pair,
                    "buy_pair": proposal.buy_pair,
                    "quote_amount": proposal.quote_amount,
                    "expected_gain_pct": proposal.expected_gain_pct,
                    "fee_pct": proposal.fee_estimate.total_fee_pct,
                    "net_gain_pct": proposal.net_gain_pct,
                    "priority": proposal.priority,
                    "high_stakes_active": self.high_stakes.is_active,
                    "route_type": result.get("route_type", "unknown"),
                    "bridge_currency": result.get("bridge_currency"),
                    "n_legs": result.get("n_legs", 2),
                },
            )
