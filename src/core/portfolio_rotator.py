"""
Portfolio Rotator — Autonomous crypto-to-crypto swaps based on relative strength.

Core logic:
  1. Rank all tracked assets by predicted performance (technical + sentiment)
  2. If holding a weak asset and a strong asset exists → propose swap
  3. Route-aware fee estimation (direct / bridged / fiat-routed)
  4. LLM validation of proposals (approve / veto)
  5. AbsoluteRules gate on every trade leg
  6. Partial-failure recovery for multi-leg swaps
  7. Limit swap frequency and size to prevent churn

The rotator operates fully autonomously — no human approval required
unless full_autonomy is disabled and trade size exceeds limits.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from src.core.fee_manager import FeeManager, FeeEstimate
from src.core.high_stakes import HighStakesManager
from src.analysis.fear_greed import FearGreedIndex
from src.analysis.multi_timeframe import MultiTimeframeAnalyzer
from src.utils.logger import get_logger
from src.utils.audit import AuditLog
from src.utils.journal import TradeJournal

if TYPE_CHECKING:
    from src.core.route_finder import RouteFinder, SwapRoute
    from src.core.rules import AbsoluteRules
    from src.models.trade import TradeAction

logger = get_logger("core.portfolio_rotator")


class AssetRanking:
    """Ranked assessment of a single asset."""

    def __init__(
        self,
        pair: str,
        score: float,           # -1.0 to 1.0 composite strength
        confidence: float,      # 0.0 to 1.0
        predicted_move_pct: float,  # Expected % move in next period
        signals: dict,          # Raw signal details
        reasoning: str = "",
    ):
        self.pair = pair
        self.score = score
        self.confidence = confidence
        self.predicted_move_pct = predicted_move_pct
        self.signals = signals
        self.reasoning = reasoning

    def __repr__(self) -> str:
        return (f"AssetRanking({self.pair}, score={self.score:+.2f}, "
                f"conf={self.confidence:.2f}, pred={self.predicted_move_pct:+.2f}%)")


class SwapProposal:
    """A proposed swap from one asset to another."""

    def __init__(
        self,
        sell_pair: str,
        buy_pair: str,
        usd_amount: float,  # kept as param name for backwards compat
        sell_score: float,
        buy_score: float,
        expected_gain_pct: float,
        fee_estimate: FeeEstimate,
        net_gain_pct: float,
        confidence: float,
        priority: str,          # "autonomous", "high_impact", "critical"
        reasoning: str,
        route: Optional["SwapRoute"] = None,
    ):
        self.sell_pair = sell_pair
        self.buy_pair = buy_pair
        self.quote_amount = usd_amount
        self.sell_score = sell_score
        self.buy_score = buy_score
        self.expected_gain_pct = expected_gain_pct
        self.fee_estimate = fee_estimate
        self.net_gain_pct = net_gain_pct
        self.confidence = confidence
        self.priority = priority
        self.reasoning = reasoning
        self.route = route
        self.approved: Optional[bool] = None
        self.executed: bool = False
        self.created_at = datetime.now(timezone.utc)


class PortfolioRotator:
    """
    Autonomous portfolio rotation engine.

    Continuously evaluates relative strength of held assets vs alternatives
    and proposes swaps when profitable after fees.

    Behaviour modes:
      - full_autonomy=True (default): All rule-passing proposals execute
        without approval. Telegram is notification-only.
      - full_autonomy=False: Large swaps escalated for Telegram approval.
      - High-stakes: Owner-enabled mode with elevated limits (time-bounded).
    """

    def __init__(
        self,
        config: dict,
        coinbase_client,
        llm_client,
        fee_manager: FeeManager,
        high_stakes: HighStakesManager,
        multi_tf: Optional[MultiTimeframeAnalyzer] = None,
        fear_greed: Optional[FearGreedIndex] = None,
        journal: Optional[TradeJournal] = None,
        audit: Optional[AuditLog] = None,
        route_finder: Optional["RouteFinder"] = None,
        rules: Optional["AbsoluteRules"] = None,
    ):
        self.config = config
        self.coinbase = coinbase_client
        self.llm = llm_client
        self.fee_manager = fee_manager
        self.high_stakes = high_stakes
        self.multi_tf = multi_tf
        self.fear_greed = fear_greed
        self.journal = journal
        self.audit = audit
        self.route_finder = route_finder
        self.rules = rules

        # Rotation config
        rotation_cfg = config.get("rotation", {})
        self.enabled = rotation_cfg.get("enabled", True)

        # % of portfolio available for autonomous swaps
        self.autonomous_allocation_pct = rotation_cfg.get("autonomous_allocation_pct", 0.10)

        # Minimum score difference between assets to trigger a swap
        self.min_score_delta = rotation_cfg.get("min_score_delta", 0.3)

        # Minimum confidence to attempt autonomous swap
        self.min_confidence = rotation_cfg.get("min_confidence", 0.65)

        # Above this confidence, escalate as "high impact" to owner
        self.high_impact_confidence = rotation_cfg.get("high_impact_confidence", 0.80)

        # Above this amount, always ask owner (only when full_autonomy=False)
        self.approval_threshold = rotation_cfg.get("approval_threshold", rotation_cfg.get("approval_threshold_usd", 200.0))

        # Full autonomy: all rule-passing proposals are autonomous (no human approval)
        self.full_autonomy = rotation_cfg.get("full_autonomy", True)

        # LLM validation: approve/veto proposals via LLM reasoning (default ON)
        self.llm_validation = rotation_cfg.get("llm_validation", True)
        self.llm_validation_temperature = rotation_cfg.get("llm_validation_temperature", 0.3)

        # Track last swap times to prevent churn
        self._last_swap_times: dict[str, float] = {}

        # Pending swap proposals awaiting approval
        self.pending_swaps: dict[str, SwapProposal] = {}

        logger.info(
            f"🔄 Portfolio Rotator initialized: "
            f"autonomous={self.autonomous_allocation_pct*100:.0f}% allocation, "
            f"min delta={self.min_score_delta}, "
            f"min confidence={self.min_confidence}, "
            f"full_autonomy={self.full_autonomy}, "
            f"llm_validation={self.llm_validation}, "
            f"route_finder={'ON' if self.route_finder else 'OFF'}"
        )

    async def evaluate_rotation(
        self,
        held_pairs: list[str],
        all_pairs: list[str],
        current_prices: dict[str, float],
        portfolio_value: float,
        scan_results: dict[str, dict] | None = None,
    ) -> list[SwapProposal]:
        """
        Evaluate whether any portfolio rotations are beneficial.

        Args:
            held_pairs: Pairs we currently hold positions in
            all_pairs: All tracked/tradeable pairs
            current_prices: Current prices for all pairs
            portfolio_value: Total portfolio value in USD
            scan_results: Optional universe scan data (pair → score dict) for ranking boost

        Returns:
            List of SwapProposals (ranked by expected net gain)
        """
        if not self.enabled:
            return []

        logger.info(f"🔄 Evaluating rotation: holding {held_pairs}, tracking {all_pairs}")

        # Step 1: Rank all assets
        rankings = self._rank_assets(all_pairs, current_prices)

        # Boost rankings with universe scan data (if available)
        if scan_results:
            for ranking in rankings:
                scan = scan_results.get(ranking.pair)
                if scan:
                    composite = scan.get("composite_score", 0)
                    # Blend scan score into ranking (20% weight)
                    ranking.score = ranking.score * 0.8 + composite * 0.2
                    ranking.predicted_move_pct += composite * 1.5  # scan boost
        if len(rankings) < 2:
            logger.debug("Not enough assets ranked for rotation")
            return []

        # Sort by score (best first)
        rankings.sort(key=lambda r: r.score, reverse=True)

        logger.info(
            "📊 Asset rankings: " +
            " | ".join(f"{r.pair}: {r.score:+.2f}" for r in rankings)
        )

        # Step 2: Find swap opportunities (evaluate ALL candidates per held pair)
        proposals = []

        # Get effective allocation (may be elevated in high-stakes)
        allocation_pct = self.autonomous_allocation_pct
        if self.high_stakes.is_active:
            limits = self.high_stakes.get_effective_limits({
                "swap_allocation_pct": self.autonomous_allocation_pct,
            })
            allocation_pct = limits.get("swap_allocation_pct", self.autonomous_allocation_pct)

        max_swap_quote = portfolio_value * allocation_pct

        for held_pair in held_pairs:
            held_ranking = next((r for r in rankings if r.pair == held_pair), None)
            if not held_ranking:
                continue

            # Check cooldown
            last_swap = self._last_swap_times.get(held_pair, 0)
            cooldown = self.fee_manager.swap_cooldown_seconds
            if time.time() - last_swap < cooldown:
                logger.debug(
                    f"Swap cooldown active for {held_pair}: "
                    f"{int(cooldown - (time.time() - last_swap))}s remaining"
                )
                continue

            # Evaluate ALL candidates — pick the best one after route-aware scoring
            best_proposal: SwapProposal | None = None

            for candidate in rankings:
                if candidate.pair == held_pair:
                    continue
                if candidate.pair in held_pairs:
                    continue  # Don't swap into something we already hold

                score_delta = candidate.score - held_ranking.score
                if score_delta < self.min_score_delta:
                    continue  # Not enough improvement

                # Estimate expected gain from the swap
                expected_gain_pct = (
                    candidate.predicted_move_pct - held_ranking.predicted_move_pct
                )

                if expected_gain_pct <= 0:
                    continue

                # Check if gain exceeds fees
                swap_quote = min(
                    max_swap_quote,
                    current_prices.get(held_pair, 0) * 1.0,  # Position value
                )

                if swap_quote < self.fee_manager.min_trade_quote:
                    continue

                # ── Route-aware fee estimation ──
                best_route = None
                n_legs = 2  # default fiat-routed

                if self.route_finder:
                    sell_base = held_pair.split('-')[0]
                    buy_base = candidate.pair.split('-')[0]
                    routes = self.route_finder.find_routes(sell_base, buy_base, swap_quote)
                    if routes:
                        best_route = routes[0]  # cheapest route
                        n_legs = best_route.n_legs

                worthwhile, fee_estimate = self.fee_manager.is_trade_worthwhile(
                    quote_amount=swap_quote,
                    expected_gain_pct=expected_gain_pct / 100,  # Convert to decimal
                    is_swap=True,
                    n_legs=n_legs,
                )

                if not worthwhile:
                    route_info = f" ({best_route.route_type}, {n_legs} legs)" if best_route else ""
                    logger.info(
                        f"🔄 Swap {held_pair} → {candidate.pair}{route_info}: "
                        f"NOT worthwhile (gain {expected_gain_pct:.2f}% < fees)"
                    )
                    continue

                net_gain_pct = (expected_gain_pct / 100) - fee_estimate.total_fee_pct

                # Determine priority
                avg_confidence = (held_ranking.confidence + candidate.confidence) / 2
                priority = self._determine_priority(
                    avg_confidence, swap_quote, score_delta
                )

                route_desc = ""
                if best_route:
                    route_desc = f" Route: {best_route.route_type}"
                    if best_route.bridge_currency:
                        route_desc += f" via {best_route.bridge_currency}"
                    route_desc += f" ({n_legs} leg{'s' if n_legs > 1 else ''})"

                proposal = SwapProposal(
                    sell_pair=held_pair,
                    buy_pair=candidate.pair,
                    usd_amount=swap_quote,
                    sell_score=held_ranking.score,
                    buy_score=candidate.score,
                    expected_gain_pct=expected_gain_pct,
                    fee_estimate=fee_estimate,
                    net_gain_pct=net_gain_pct,
                    confidence=avg_confidence,
                    priority=priority,
                    reasoning=(
                        f"{held_pair} weakening (score {held_ranking.score:+.2f}), "
                        f"{candidate.pair} strengthening (score {candidate.score:+.2f}). "
                        f"Expected gain: {expected_gain_pct:.2f}%, "
                        f"fees: {fee_estimate.total_fee_pct*100:.2f}%, "
                        f"net: {net_gain_pct*100:.2f}%{route_desc}"
                    ),
                    route=best_route,
                )

                # Keep the best proposal per held pair (highest net gain)
                if best_proposal is None or net_gain_pct > best_proposal.net_gain_pct:
                    best_proposal = proposal

                logger.info(
                    f"🔄 Swap candidate: {held_pair} → {candidate.pair} | "
                    f"{swap_quote:.0f} | gain: {expected_gain_pct:.2f}% | "
                    f"net: {net_gain_pct*100:.2f}% | priority: {priority}"
                    f"{route_desc}"
                )

            if best_proposal:
                proposals.append(best_proposal)

        # Sort by net gain (best first)
        proposals.sort(key=lambda p: p.net_gain_pct, reverse=True)

        # ── LLM validation (approve/veto) ──
        if self.llm_validation and proposals:
            proposals = await self._llm_validate_proposals(proposals, rankings)

        return proposals

    def execute_swap(
        self,
        proposal: SwapProposal,
        portfolio_value: float = 0.0,
        cash_balance: float = 0.0,
    ) -> dict:
        """
        Execute a crypto-to-crypto swap (sell A → buy B).
        Dispatches to the optimal route (direct / bridged / fiat-routed).

        Includes AbsoluteRules gate on each leg and partial-failure recovery.

        Returns execution result dict.
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
            "bridge_currency": proposal.route.bridge_currency if proposal.route else None,
            "n_legs": proposal.route.n_legs if proposal.route else 2,
        }

        try:
            if proposal.route:
                # ── Route-aware execution ──
                if proposal.route.route_type == "direct":
                    return self._execute_direct(proposal, result, portfolio_value, cash_balance)
                elif proposal.route.route_type == "bridged":
                    return self._execute_bridged(proposal, result, portfolio_value, cash_balance)
                else:
                    return self._execute_fiat_routed(proposal, result, portfolio_value, cash_balance)
            else:
                # ── Legacy fallback (no route pre-selected) ──
                return self._execute_legacy(proposal, result, portfolio_value, cash_balance)

        except Exception as e:
            logger.error(f"Swap execution error: {e}", exc_info=True)
            result["error"] = str(e)
            return result

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
            logger.warning(f"🚫 AbsoluteRules blocked swap leg {pair} {action_str}: {reasons}")
            return False, reasons

        return True, ""

    def _execute_direct(
        self,
        proposal: SwapProposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Execute a direct 1-leg swap via a direct trading pair."""
        route = proposal.route
        leg = route.legs[0]
        logger.info(
            f"🔄 Direct swap: {leg.product_id} ({leg.side}) — "
            f"1-leg swap"
        )

        # AbsoluteRules gate
        allowed, reason = self._check_leg_rules(
            leg.product_id, leg.side, proposal.quote_amount,
            portfolio_value, cash_balance,
        )
        if not allowed:
            result["error"] = f"AbsoluteRules blocked: {reason}"
            return result

        try:
            if leg.side == "sell":
                sell_price = self.coinbase.get_current_price(leg.product_id)
                if sell_price <= 0:
                    result["error"] = f"Cannot get price for {leg.product_id}"
                    return result
                # Calculate base quantity from the sell pair price
                sell_pair_price = self.coinbase.get_current_price(proposal.sell_pair)
                if sell_pair_price <= 0:
                    sell_pair_price = sell_price  # fallback
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
                sell_pair_price = self.coinbase.get_current_price(proposal.sell_pair)
                if sell_pair_price <= 0:
                    sell_pair_price = buy_price
                quote_for_buy = proposal.quote_amount / sell_pair_price * buy_price
                direct_result = self.coinbase.market_order_buy(
                    product_id=leg.product_id,
                    quote_size=str(round(quote_for_buy, 2)),
                )

            if direct_result and not direct_result.get("error"):
                result["sell_result"] = direct_result
                result["buy_result"] = direct_result
                result["executed"] = True
                result["direct_pair"] = leg.product_id
                self._last_swap_times[proposal.sell_pair] = time.time()
                self._last_swap_times[proposal.buy_pair] = time.time()
                self._log_swap(proposal, result, "swap_direct", "rotation_direct")
                logger.info(
                    f"✅ Direct swap completed: {proposal.sell_pair} → {proposal.buy_pair} "
                    f"via {leg.product_id}"
                )
                proposal.executed = True
            else:
                error = direct_result.get("error", "Unknown") if direct_result else "No result"
                logger.warning(f"Direct swap failed: {error} — falling back to fiat routing")
                return self._execute_fiat_routed(proposal, result, portfolio_value, cash_balance)

        except Exception as de:
            logger.warning(f"Direct swap failed, falling back to fiat routing: {de}")
            return self._execute_fiat_routed(proposal, result, portfolio_value, cash_balance)

        return result

    def _execute_bridged(
        self,
        proposal: SwapProposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Execute a 2-leg bridged swap (sell → bridge → buy)."""
        route = proposal.route
        leg1, leg2 = route.legs[0], route.legs[1]
        bridge = route.bridge_currency

        logger.info(
            f"🔄 Bridged swap: {proposal.sell_pair} → {bridge} → {proposal.buy_pair} | "
            f"Leg1: {leg1.product_id}({leg1.side}) | Leg2: {leg2.product_id}({leg2.side})"
        )

        # AbsoluteRules gate — check BOTH legs before executing either
        for leg_info in [(leg1, "Leg1"), (leg2, "Leg2")]:
            leg, label = leg_info
            allowed, reason = self._check_leg_rules(
                leg.product_id, leg.side, proposal.quote_amount,
                portfolio_value, cash_balance,
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

            # Extract actual proceeds from leg 1
            order = leg1_result.get("order", leg1_result)
            bridge_amount = float(order.get("filled_value", proposal.quote_amount))
            leg1_fee = float(order.get("fee", 0))
            bridge_amount -= leg1_fee

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
                # Selling bridge for buy_asset
                bridge_price = self.coinbase.get_current_price(leg2.product_id)
                if bridge_price > 0:
                    sell_qty = bridge_amount / bridge_price
                    leg2_result = self.coinbase.market_order_sell(
                        product_id=leg2.product_id,
                        base_size=str(round(sell_qty, 8)),
                    )
                else:
                    leg2_result = None

            if not leg2_result or leg2_result.get("error"):
                # ── PARTIAL FAILURE RECOVERY ──
                # Leg 1 succeeded but leg 2 failed.
                # Attempt to reverse: sell the bridge back to the original asset.
                logger.error(
                    f"⚠️ Bridged swap leg2 FAILED after leg1 succeeded! "
                    f"Holding {bridge_amount:.2f} of bridge currency {bridge}. "
                    f"Attempting reversal..."
                )
                result["error"] = f"Leg2 failed (leg1 succeeded): {leg2_result}"
                result["partial"] = True
                result["bridge_stuck_amount"] = bridge_amount
                result["bridge_stuck_currency"] = bridge

                self._attempt_bridge_reversal(
                    proposal, leg1, bridge, bridge_amount, result
                )
                return result

            result["buy_result"] = leg2_result
            result["executed"] = True

            self._last_swap_times[proposal.sell_pair] = time.time()
            self._last_swap_times[proposal.buy_pair] = time.time()
            self._log_swap(proposal, result, "swap_bridged", "rotation_bridged")
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

    def _attempt_bridge_reversal(
        self,
        proposal: SwapProposal,
        leg1,
        bridge: str,
        bridge_amount: float,
        result: dict,
    ) -> None:
        """
        Attempt to reverse a failed bridged swap by selling the bridge
        back to the original asset. If reversal also fails, log the stuck
        position for the next rotation cycle to handle.
        """
        try:
            sell_base = proposal.sell_pair.split('-')[0]
            # Try to find a pair: sell_base-bridge or bridge-sell_base
            reverse_pair = self.coinbase.find_direct_pair(bridge, sell_base) if hasattr(self.coinbase, 'find_direct_pair') else None
            if reverse_pair:
                rev_pair_id, rev_direction = reverse_pair
                logger.info(f"🔄 Attempting bridge reversal via {rev_pair_id} ({rev_direction})")
                if rev_direction == "sell":
                    bridge_price = self.coinbase.get_current_price(rev_pair_id)
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
                    logger.info(f"✅ Bridge reversal succeeded: recovered to {sell_base}")
                    result["reversal"] = "success"
                    result["reversal_result"] = rev_result
                    return

            logger.warning(
                f"⚠️ Bridge reversal FAILED. Stuck holding {bridge_amount:.4f} {bridge}. "
                f"Will be treated as new holding in next rotation cycle."
            )
            result["reversal"] = "failed"

        except Exception as re:
            logger.error(f"Bridge reversal exception: {re}")
            result["reversal"] = "error"
            result["reversal_error"] = str(re)

        # Log stuck bridge position for audit
        if self.audit:
            self.audit.log("bridge_stuck_position", {
                "original_sell": proposal.sell_pair,
                "intended_buy": proposal.buy_pair,
                "bridge_currency": bridge,
                "bridge_amount": bridge_amount,
                "reversal_attempted": True,
                "reversal_result": result.get("reversal", "unknown"),
            })

    def _execute_fiat_routed(
        self,
        proposal: SwapProposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """Execute a fiat-routed swap (sell A → fiat → buy B)."""
        result["route_type"] = "fiat"

        # AbsoluteRules gate on both legs
        for pair, action in [(proposal.sell_pair, "sell"), (proposal.buy_pair, "buy")]:
            allowed, reason = self._check_leg_rules(
                pair, action, proposal.quote_amount,
                portfolio_value, cash_balance,
            )
            if not allowed:
                result["error"] = f"AbsoluteRules blocked {pair} {action}: {reason}"
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

        # Extract actual proceeds
        order = sell_result.get("order", sell_result)
        actual_proceeds = float(order.get("filled_value", proposal.quote_amount))
        sell_fee = float(order.get("fee", 0))
        actual_proceeds -= sell_fee

        # Step 2: Buy the strong asset
        buy_result = self.coinbase.market_order_buy(
            product_id=proposal.buy_pair,
            quote_size=str(round(actual_proceeds, 2)),
        )

        if not buy_result or buy_result.get("error"):
            logger.error(
                f"⚠️ Swap buy failed after sell! "
                f"Sold {proposal.sell_pair} but couldn't buy {proposal.buy_pair}. "
                f"Proceeds: {actual_proceeds:.2f}"
            )
            result["error"] = f"Buy failed (sell succeeded): {buy_result}"
            result["partial"] = True
            return result

        result["buy_result"] = buy_result
        result["executed"] = True

        self._last_swap_times[proposal.sell_pair] = time.time()
        self._last_swap_times[proposal.buy_pair] = time.time()
        self._log_swap(proposal, result, "swap", "rotation")
        logger.info(
            f"✅ Fiat-routed swap completed: {proposal.sell_pair} → {proposal.buy_pair} | "
            f"{proposal.quote_amount:.2f} | fees: {proposal.fee_estimate.total_fee_quote:.2f}"
        )
        proposal.executed = True
        return result

    def _execute_legacy(
        self,
        proposal: SwapProposal,
        result: dict,
        portfolio_value: float,
        cash_balance: float,
    ) -> dict:
        """
        Legacy execution path — no route pre-selected.
        Tries direct pair first, then falls back to fiat routing.
        """
        result["route_type"] = "legacy"

        # Try direct pair
        direct = None
        if hasattr(self.coinbase, 'find_direct_pair'):
            sell_base = proposal.sell_pair.split('-')[0]
            buy_base = proposal.buy_pair.split('-')[0]
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
            return self._execute_direct(proposal, result, portfolio_value, cash_balance)

        # Fall back to fiat routing
        return self._execute_fiat_routed(proposal, result, portfolio_value, cash_balance)

    def _log_swap(
        self,
        proposal: SwapProposal,
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
            self.audit.log("swap_execution", {
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
            })

    # ── LLM Validation ──────────────────────────────────────────────

    async def _llm_validate_proposals(
        self,
        proposals: list[SwapProposal],
        rankings: list[AssetRanking],
    ) -> list[SwapProposal]:
        """
        Use LLM to approve/veto each proposal. No adjustments —
        the LLM can only accept or reject to preserve the deterministic
        safety layer. On any LLM failure, falls back to all proposals
        (non-fatal, graceful degradation).
        """
        try:
            # Build compact ranking table
            ranking_table = "Asset Rankings:\n"
            ranking_table += "Pair | Score | Confidence | Predicted Move\n"
            ranking_table += "-" * 55 + "\n"
            for r in rankings[:20]:  # Top 20 for context
                ranking_table += (
                    f"{r.pair} | {r.score:+.2f} | {r.confidence:.2f} | "
                    f"{r.predicted_move_pct:+.2f}%\n"
                )

            # Build proposals summary
            proposals_text = "\nProposed Swaps:\n"
            for i, p in enumerate(proposals, 1):
                route_info = ""
                if p.route:
                    route_info = f" | Route: {p.route.route_type}"
                    if p.route.bridge_currency:
                        route_info += f" via {p.route.bridge_currency}"
                proposals_text += (
                    f"{i}. SELL {p.sell_pair} (score {p.sell_score:+.2f}) → "
                    f"BUY {p.buy_pair} (score {p.buy_score:+.2f}) | "
                    f"Amount: {p.quote_amount:.0f} | "
                    f"Expected: +{p.expected_gain_pct:.2f}% | "
                    f"Fees: {p.fee_estimate.total_fee_pct*100:.2f}% | "
                    f"Net: {p.net_gain_pct*100:.2f}%{route_info}\n"
                )

            system_prompt = (
                "You are a crypto portfolio rotation validator. Your job is to review "
                "proposed asset swaps and approve or veto each one based on the asset "
                "rankings and market context provided.\n\n"
                "Rules:\n"
                "- You can ONLY approve or veto. No adjustments or alternative suggestions.\n"
                "- Approve if the technical/quantitative case is sound.\n"
                "- Veto if there's a clear reason the swap is likely to lose money "
                "(e.g., the buy asset is in a clear downtrend despite a positive score, "
                "the sell asset is about to reverse, or the expected gain is marginal).\n"
                "- When in doubt, approve — the system has already passed fee and risk checks.\n"
                "- Be concise in reasoning (1-2 sentences max).\n\n"
                "Respond with JSON only."
            )

            user_message = (
                f"{ranking_table}\n{proposals_text}\n"
                f"For each proposal, respond with:\n"
                f'{{"decisions": [\n'
                f'  {{"sell_pair": "...", "buy_pair": "...", "action": "approve"|"veto", '
                f'"reasoning": "..."}}\n'
                f"]}}"
            )

            llm_response = await self.llm.chat_json(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=self.llm_validation_temperature,
                agent_name="portfolio_rotator",
            )

            return self._apply_llm_decisions(proposals, llm_response)

        except Exception as e:
            logger.warning(
                f"⚠️ LLM validation failed (non-fatal, using deterministic proposals): {e}"
            )
            return proposals

    def _apply_llm_decisions(
        self,
        proposals: list[SwapProposal],
        llm_response: dict,
    ) -> list[SwapProposal]:
        """Apply LLM approve/veto decisions. Invalid decisions are ignored."""
        decisions = llm_response.get("decisions", [])
        if not decisions:
            logger.warning("LLM returned no decisions, keeping all proposals")
            return proposals

        # Build lookup
        decision_map: dict[tuple[str, str], dict] = {}
        for d in decisions:
            key = (d.get("sell_pair", ""), d.get("buy_pair", ""))
            if key[0] and key[1]:
                decision_map[key] = d

        approved: list[SwapProposal] = []
        for p in proposals:
            key = (p.sell_pair, p.buy_pair)
            decision = decision_map.get(key)
            if not decision:
                # No decision from LLM — keep proposal (default approve)
                logger.debug(f"LLM: no decision for {p.sell_pair}→{p.buy_pair}, keeping")
                approved.append(p)
                continue

            action = decision.get("action", "approve").lower()
            reasoning = decision.get("reasoning", "")

            if action == "veto":
                logger.info(
                    f"🤖 LLM VETOED: {p.sell_pair} → {p.buy_pair} | {reasoning}"
                )
                if self.audit:
                    self.audit.log("llm_veto", {
                        "sell_pair": p.sell_pair,
                        "buy_pair": p.buy_pair,
                        "reasoning": reasoning,
                    })
            else:
                logger.info(
                    f"🤖 LLM APPROVED: {p.sell_pair} → {p.buy_pair} | {reasoning}"
                )
                # Append LLM reasoning to proposal
                p.reasoning += f" | LLM: {reasoning}"
                approved.append(p)

        logger.info(
            f"🤖 LLM validation: {len(approved)}/{len(proposals)} proposals approved"
        )
        return approved

    def _rank_assets(
        self,
        pairs: list[str],
        current_prices: dict[str, float],
    ) -> list[AssetRanking]:
        """
        Rank assets by composite strength score.
        Uses multi-timeframe analysis + Fear & Greed context.
        """
        rankings = []

        for pair in pairs:
            try:
                # Get multi-timeframe score
                mtf_result = {}
                if self.multi_tf:
                    mtf_result = self.multi_tf.analyze(pair)

                confluence_score = mtf_result.get("confluence_score", 0)
                aligned = mtf_result.get("aligned", False)

                # Adjust confidence based on alignment
                base_confidence = abs(confluence_score)
                confidence = base_confidence * (1.2 if aligned else 0.7)
                confidence = min(confidence, 0.95)

                # Predict expected move based on score strength
                # Conservative: even a perfect score only predicts ~3% move
                predicted_move = confluence_score * 3.0  # -3% to +3%

                # Factor in Fear & Greed
                signals = {"confluence": confluence_score, "aligned": aligned}
                if self.fear_greed:
                    fg_data = self.fear_greed.fetch()
                    fg_value = fg_data.get("value", 50)
                    signals["fear_greed"] = fg_value

                    # Extreme fear + bullish technical = stronger buy signal
                    if fg_value < 25 and confluence_score > 0:
                        predicted_move *= 1.3
                        confidence *= 1.1
                    # Extreme greed + bearish technical = stronger sell signal
                    elif fg_value > 75 and confluence_score < 0:
                        predicted_move *= 1.3
                        confidence *= 1.1

                rankings.append(AssetRanking(
                    pair=pair,
                    score=confluence_score,
                    confidence=min(confidence, 0.95),
                    predicted_move_pct=predicted_move,
                    signals=signals,
                    reasoning=mtf_result.get("summary", ""),
                ))

            except Exception as e:
                logger.warning(f"Failed to rank {pair}: {e}")

        return rankings

    def _determine_priority(
        self,
        confidence: float,
        quote_amount: float,
        score_delta: float,
    ) -> str:
        """
        Determine swap priority level.

        Returns:
            "autonomous" — Execute without asking
            "high_impact" — Send to Telegram for approval (only if full_autonomy=False)
            "critical" — Critical trade, owner must approve (only if full_autonomy=False)

        When full_autonomy=True, ALL proposals that pass AbsoluteRules
        and fee checks are "autonomous". Telegram gets notifications, not approval requests.
        """
        # Full autonomy: everything that passed rules/fees is autonomous
        if self.full_autonomy:
            return "autonomous"

        # Check if high-stakes mode lowers the bar for autonomous
        if self.high_stakes.is_active:
            hs_limits = self.high_stakes.get_effective_limits({
                "require_approval_above": self.approval_threshold,
                "min_confidence": self.min_confidence,
            })
            approval_threshold = hs_limits.get("require_approval_above", 500)
        else:
            approval_threshold = self.approval_threshold

        # Critical: very large trades
        if quote_amount > approval_threshold * 2:
            return "critical"

        # High-impact: above approval threshold OR very high confidence
        if quote_amount > approval_threshold:
            return "high_impact"

        if confidence >= self.high_impact_confidence and score_delta >= 0.5:
            return "high_impact"

        # Autonomous: within allocation, reasonable confidence
        if confidence >= self.min_confidence:
            return "autonomous"

        return "high_impact"  # Default: ask

    def get_rotation_summary(
        self,
        proposals: list[SwapProposal],
    ) -> str:
        """Format proposals for Telegram notification."""
        if not proposals:
            return "🔄 No profitable swaps identified this cycle."

        lines = ["🔄 *Portfolio Rotation Analysis*\n"]

        for i, p in enumerate(proposals, 1):
            emoji = {"autonomous": "🟢", "high_impact": "🟡", "critical": "🔴"}.get(
                p.priority, "⚪"
            )
            route_info = ""
            if p.route:
                route_info = f"   Route: {p.route.route_type}"
                if p.route.bridge_currency:
                    route_info += f" via {p.route.bridge_currency}"
                route_info += f" ({p.route.n_legs} leg{'s' if p.route.n_legs > 1 else ''})\n"
            lines.append(
                f"{emoji} {i}. {p.sell_pair} → {p.buy_pair}\n"
                f"   Amount: {p.quote_amount:.0f}\n"
                f"   Expected: +{p.expected_gain_pct:.2f}%\n"
                f"   Fees: {p.fee_estimate.total_fee_pct*100:.2f}%\n"
                f"   Net gain: {p.net_gain_pct*100:.2f}%\n"
                f"   Confidence: {p.confidence:.0%}\n"
                f"   Priority: {p.priority}\n"
                f"{route_info}"
            )

        lines.append(
            "\n🟢 = autonomous  🟡 = needs approval  🔴 = critical"
        )

        return "\n".join(lines)

    def format_swap_approval_request(self, proposal: SwapProposal) -> str:
        """Format a swap proposal for Telegram approval."""
        return (
            f"🔄 *Swap Approval Request*\n\n"
            f"Sell: {proposal.sell_pair} (score: {proposal.sell_score:+.2f})\n"
            f"Buy: {proposal.buy_pair} (score: {proposal.buy_score:+.2f})\n"
            f"Amount: {proposal.quote_amount:.2f}\n"
            f"Expected gain: +{proposal.expected_gain_pct:.2f}%\n"
            f"Trading fees: {proposal.fee_estimate.total_fee_pct*100:.2f}% "
            f"({proposal.fee_estimate.total_fee_quote:.2f})\n"
            f"Net gain: {proposal.net_gain_pct*100:.2f}%\n"
            f"Confidence: {proposal.confidence:.0%}\n\n"
            f"💡 {proposal.reasoning}\n\n"
            f"Reply /approve or /reject"
        )
