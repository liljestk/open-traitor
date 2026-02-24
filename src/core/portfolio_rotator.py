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
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from src.core.fee_manager import FeeManager, FeeEstimate
from src.core.high_stakes import HighStakesManager
from src.core.rotation_executor import RotationExecutorMixin
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


class PortfolioRotator(RotationExecutorMixin):
    """
    Autonomous portfolio rotation engine.

    Continuously evaluates relative strength of held assets vs alternatives
    and proposes swaps when profitable after fees.

    Execution methods (execute_swap, _execute_direct, _execute_bridged,
    _execute_fiat_routed, _execute_legacy, recovery helpers, and logging)
    live in RotationExecutorMixin (rotation_executor.py).

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
        self._state_lock = threading.Lock()

        logger.info(
            f"🔄 Portfolio Rotator initialized: "
            f"autonomous={self.autonomous_allocation_pct*100:.0f}% allocation, "
            f"min delta={self.min_score_delta}, "
            f"min confidence={self.min_confidence}, "
            f"full_autonomy={self.full_autonomy}, "
            f"llm_validation={self.llm_validation}, "
            f"route_finder={'ON' if self.route_finder else 'OFF'}"
        )

    def _get_last_swap_time(self, pair: str) -> float:
        with self._state_lock:
            return self._last_swap_times.get(pair, 0.0)

    def get_pending_swaps(self) -> dict[str, "SwapProposal"]:
        """Thread-safe copy of pending swaps (M27 fix)."""
        with self._state_lock:
            return dict(self.pending_swaps)

    def pop_pending_swap(self, swap_id: str) -> "SwapProposal | None":
        """Thread-safe removal of a pending swap (M27 fix)."""
        with self._state_lock:
            return self.pending_swaps.pop(swap_id, None)

    def add_pending_swap(self, swap_id: str, proposal: "SwapProposal") -> None:
        """Thread-safe addition of a pending swap (M27 fix)."""
        with self._state_lock:
            self.pending_swaps[swap_id] = proposal

    def _set_last_swap_times(self, *pairs: str) -> None:
        now_ts = time.time()
        with self._state_lock:
            for pair in pairs:
                self._last_swap_times[pair] = now_ts

    def _record_rotation_leg(self, quote_value: float, action: str, leg_name: str) -> None:
        if not self.rules:
            return
        try:
            safe_quote = max(0.0, float(quote_value))
            self.rules.record_trade(safe_quote, action=action)
        except Exception as e:
            logger.warning(f"Failed to record {leg_name} ({action}) leg in rules counters: {e}")

    async def evaluate_rotation(
        self,
        held_pairs: list[str],
        all_pairs: list[str],
        current_prices: dict[str, float],
        portfolio_value: float,
        scan_results: dict[str, dict] | None = None,
        open_positions: dict[str, float] | None = None,
    ) -> list[SwapProposal]:
        """
        Evaluate whether any portfolio rotations are beneficial.

        Args:
            held_pairs: Pairs we currently hold positions in
            all_pairs: All tracked/tradeable pairs
            current_prices: Current prices for all pairs
            portfolio_value: Total portfolio value in USD
            scan_results: Optional universe scan data (pair → score dict) for ranking boost
            open_positions: Pair → held quantity (from TradingState.open_positions)

        Returns:
            List of SwapProposals (ranked by expected net gain)
        """
        if open_positions is None:
            open_positions = {}
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
            last_swap = self._get_last_swap_time(held_pair)
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
                # Use actual held quantity × current price for position value
                held_qty = open_positions.get(held_pair, 0)
                position_value = current_prices.get(held_pair, 0) * held_qty
                swap_quote = min(
                    max_swap_quote,
                    position_value,
                )

                if swap_quote < self.fee_manager.get_dynamic_min_trade(
                    sum(current_prices.get(p, 0) * open_positions.get(p, 0) for p in open_positions)
                ):
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

        # Hoist Fear & Greed fetch outside loop to avoid N redundant API calls
        fg_value = 50
        if self.fear_greed:
            try:
                fg_data = self.fear_greed.fetch()
                fg_value = fg_data.get("value", 50)
            except Exception as e:
                logger.debug(f"Fear & Greed fetch failed: {e}")

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
