"""
Portfolio Rotator — Autonomous crypto-to-crypto swaps based on relative strength.

Core logic:
  1. Rank all tracked assets by predicted performance (technical + sentiment)
  2. If holding a weak asset and a strong asset exists → propose swap
  3. Only swap if expected gain > fees * safety_margin
  4. Limit swap frequency and size to prevent churn

The rotator uses a % of portfolio for autonomous swaps.
High-impact or high-probability trades can be escalated to the owner
for approval via Telegram.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.fee_manager import FeeManager, FeeEstimate
from src.core.high_stakes import HighStakesManager
from src.analysis.fear_greed import FearGreedIndex
from src.analysis.multi_timeframe import MultiTimeframeAnalyzer
from src.utils.logger import get_logger
from src.utils.audit import AuditLog
from src.utils.journal import TradeJournal

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
        usd_amount: float,
        sell_score: float,
        buy_score: float,
        expected_gain_pct: float,
        fee_estimate: FeeEstimate,
        net_gain_pct: float,
        confidence: float,
        priority: str,          # "autonomous", "high_impact", "critical"
        reasoning: str,
    ):
        self.sell_pair = sell_pair
        self.buy_pair = buy_pair
        self.usd_amount = usd_amount
        self.sell_score = sell_score
        self.buy_score = buy_score
        self.expected_gain_pct = expected_gain_pct
        self.fee_estimate = fee_estimate
        self.net_gain_pct = net_gain_pct
        self.confidence = confidence
        self.priority = priority
        self.reasoning = reasoning
        self.approved: Optional[bool] = None
        self.executed: bool = False
        self.created_at = datetime.now(timezone.utc)


class PortfolioRotator:
    """
    Autonomous portfolio rotation engine.

    Continuously evaluates relative strength of held assets vs alternatives
    and proposes swaps when profitable after fees.

    Behaviour modes:
      - Autonomous: Low-impact swaps within allocated % — no approval needed
      - High-impact: Significant swaps sent to Telegram for approval
      - High-stakes: Owner-enabled mode with elevated limits (time-bounded)
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

        # Above this USD amount, always ask owner
        self.approval_threshold_usd = rotation_cfg.get("approval_threshold_usd", 200.0)

        # Track last swap times to prevent churn
        self._last_swap_times: dict[str, float] = {}

        # Pending swap proposals awaiting approval
        self.pending_swaps: dict[str, SwapProposal] = {}

        logger.info(
            f"🔄 Portfolio Rotator initialized: "
            f"autonomous={self.autonomous_allocation_pct*100:.0f}% allocation, "
            f"min delta={self.min_score_delta}, "
            f"min confidence={self.min_confidence}"
        )

    def evaluate_rotation(
        self,
        held_pairs: list[str],
        all_pairs: list[str],
        current_prices: dict[str, float],
        portfolio_value: float,
    ) -> list[SwapProposal]:
        """
        Evaluate whether any portfolio rotations are beneficial.

        Args:
            held_pairs: Pairs we currently hold positions in
            all_pairs: All tracked/tradeable pairs
            current_prices: Current prices for all pairs
            portfolio_value: Total portfolio value in USD

        Returns:
            List of SwapProposals (ranked by expected net gain)
        """
        if not self.enabled:
            return []

        logger.info(f"🔄 Evaluating rotation: holding {held_pairs}, tracking {all_pairs}")

        # Step 1: Rank all assets
        rankings = self._rank_assets(all_pairs, current_prices)
        if len(rankings) < 2:
            logger.debug("Not enough assets ranked for rotation")
            return []

        # Sort by score (best first)
        rankings.sort(key=lambda r: r.score, reverse=True)

        logger.info(
            "📊 Asset rankings: " +
            " | ".join(f"{r.pair}: {r.score:+.2f}" for r in rankings)
        )

        # Step 2: Find swap opportunities
        proposals = []

        # Get effective allocation (may be elevated in high-stakes)
        allocation_pct = self.autonomous_allocation_pct
        if self.high_stakes.is_active:
            limits = self.high_stakes.get_effective_limits({
                "swap_allocation_pct": self.autonomous_allocation_pct,
            })
            allocation_pct = limits.get("swap_allocation_pct", self.autonomous_allocation_pct)

        max_swap_usd = portfolio_value * allocation_pct

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

            # Find best candidate to swap INTO
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
                swap_usd = min(
                    max_swap_usd,
                    current_prices.get(held_pair, 0) * 1.0,  # Position value
                )

                if swap_usd < self.fee_manager.min_trade_usd:
                    continue

                worthwhile, fee_estimate = self.fee_manager.is_trade_worthwhile(
                    usd_amount=swap_usd,
                    expected_gain_pct=expected_gain_pct / 100,  # Convert to decimal
                    is_swap=True,
                )

                if not worthwhile:
                    logger.info(
                        f"🔄 Swap {held_pair} → {candidate.pair}: "
                        f"NOT worthwhile (gain {expected_gain_pct:.2f}% < fees)"
                    )
                    continue

                net_gain_pct = (expected_gain_pct / 100) - fee_estimate.total_fee_pct

                # Determine priority
                avg_confidence = (held_ranking.confidence + candidate.confidence) / 2
                priority = self._determine_priority(
                    avg_confidence, swap_usd, score_delta
                )

                proposal = SwapProposal(
                    sell_pair=held_pair,
                    buy_pair=candidate.pair,
                    usd_amount=swap_usd,
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
                        f"net: {net_gain_pct*100:.2f}%"
                    ),
                )

                proposals.append(proposal)

                logger.info(
                    f"🔄 Swap proposal: {held_pair} → {candidate.pair} | "
                    f"${swap_usd:.0f} | gain: {expected_gain_pct:.2f}% | "
                    f"net: {net_gain_pct*100:.2f}% | priority: {priority}"
                )

                break  # One swap per held pair per cycle

        # Sort by net gain (best first)
        proposals.sort(key=lambda p: p.net_gain_pct, reverse=True)
        return proposals

    def execute_swap(
        self,
        proposal: SwapProposal,
    ) -> dict:
        """
        Execute a crypto-to-crypto swap (sell A → buy B).
        Returns execution result dict.
        """
        logger.info(
            f"🔄 Executing swap: {proposal.sell_pair} → {proposal.buy_pair} "
            f"(${proposal.usd_amount:.2f})"
        )

        result = {
            "executed": False,
            "sell_pair": proposal.sell_pair,
            "buy_pair": proposal.buy_pair,
            "sell_result": None,
            "buy_result": None,
        }

        try:
            # Step 1: Sell the weak asset
            # Calculate base_size from USD amount and current price
            sell_price = self.coinbase.get_current_price(proposal.sell_pair)
            if sell_price <= 0:
                result["error"] = f"Cannot get price for {proposal.sell_pair}"
                return result
            sell_quantity = proposal.usd_amount / sell_price

            sell_result = self.coinbase.market_order_sell(
                product_id=proposal.sell_pair,
                base_size=str(round(sell_quantity, 8)),
            )

            if not sell_result or sell_result.get("error"):
                logger.error(f"Swap sell failed: {sell_result}")
                result["error"] = f"Sell failed: {sell_result}"
                return result

            result["sell_result"] = sell_result

            # Extract actual proceeds from the sell order
            order = sell_result.get("order", sell_result)
            actual_proceeds = float(order.get("filled_value", proposal.usd_amount))
            sell_fee = float(order.get("fee", 0))
            actual_proceeds -= sell_fee

            # Step 2: Buy the strong asset with proceeds
            buy_result = self.coinbase.market_order_buy(
                product_id=proposal.buy_pair,
                quote_size=str(round(actual_proceeds, 2)),
            )

            if not buy_result or buy_result.get("error"):
                logger.error(
                    f"⚠️ Swap buy failed after sell! "
                    f"Sold {proposal.sell_pair} but couldn't buy {proposal.buy_pair}. "
                    f"Proceeds: ${actual_proceeds:.2f}"
                )
                result["error"] = f"Buy failed (sell succeeded): {buy_result}"
                result["partial"] = True
                return result

            result["buy_result"] = buy_result
            result["executed"] = True

            # Update cooldown
            self._last_swap_times[proposal.sell_pair] = time.time()
            self._last_swap_times[proposal.buy_pair] = time.time()

            # Log to journal and audit
            if self.journal:
                self.journal.log_trade(
                    pair=f"{proposal.sell_pair}→{proposal.buy_pair}",
                    action="swap",
                    quantity=0,
                    price=0,
                    usd_amount=proposal.usd_amount,
                    fee=proposal.fee_estimate.total_fee_usd,
                    confidence=proposal.confidence,
                    signal_type="rotation",
                    reasoning=proposal.reasoning,
                )

            if self.audit:
                self.audit.log("swap_execution", {
                    "sell_pair": proposal.sell_pair,
                    "buy_pair": proposal.buy_pair,
                    "usd_amount": proposal.usd_amount,
                    "expected_gain_pct": proposal.expected_gain_pct,
                    "fee_pct": proposal.fee_estimate.total_fee_pct,
                    "net_gain_pct": proposal.net_gain_pct,
                    "priority": proposal.priority,
                    "high_stakes_active": self.high_stakes.is_active,
                })

            logger.info(
                f"✅ Swap completed: {proposal.sell_pair} → {proposal.buy_pair} | "
                f"${proposal.usd_amount:.2f} | "
                f"fees: ${proposal.fee_estimate.total_fee_usd:.2f}"
            )

            proposal.executed = True

        except Exception as e:
            logger.error(f"Swap execution error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

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
        usd_amount: float,
        score_delta: float,
    ) -> str:
        """
        Determine swap priority level.

        Returns:
            "autonomous" — Execute without asking
            "high_impact" — Send to Telegram for approval
            "critical" — Critical trade, owner must approve
        """
        # Check if high-stakes mode lowers the bar for autonomous
        if self.high_stakes.is_active:
            hs_limits = self.high_stakes.get_effective_limits({
                "require_approval_above_usd": self.approval_threshold_usd,
                "min_confidence": self.min_confidence,
            })
            approval_threshold = hs_limits.get("require_approval_above_usd", 500)
        else:
            approval_threshold = self.approval_threshold_usd

        # Critical: very large trades
        if usd_amount > approval_threshold * 2:
            return "critical"

        # High-impact: above approval threshold OR very high confidence
        if usd_amount > approval_threshold:
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
            lines.append(
                f"{emoji} {i}. {p.sell_pair} → {p.buy_pair}\n"
                f"   Amount: ${p.usd_amount:.0f}\n"
                f"   Expected: +{p.expected_gain_pct:.2f}%\n"
                f"   Fees: {p.fee_estimate.total_fee_pct*100:.2f}%\n"
                f"   Net gain: {p.net_gain_pct*100:.2f}%\n"
                f"   Confidence: {p.confidence:.0%}\n"
                f"   Priority: {p.priority}\n"
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
            f"Amount: ${proposal.usd_amount:.2f}\n"
            f"Expected gain: +{proposal.expected_gain_pct:.2f}%\n"
            f"Trading fees: {proposal.fee_estimate.total_fee_pct*100:.2f}% "
            f"(${proposal.fee_estimate.total_fee_usd:.2f})\n"
            f"Net gain: {proposal.net_gain_pct*100:.2f}%\n"
            f"Confidence: {proposal.confidence:.0%}\n\n"
            f"💡 {proposal.reasoning}\n\n"
            f"Reply /approve or /reject"
        )
