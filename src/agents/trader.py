"""
TraderAgent — autonomous LLM operator over the deterministic toolkit.

This agent replaces the StrategistAgent in the live decision path while
*preserving the LLM's autonomy* over what to trade. The LLM:

  1.  Receives a structured snapshot from ``TradingToolkit`` tools
      (market, strategy signals, edge stats, allocator weights,
      portfolio state, pattern engine output, fee context).
  2.  Proposes a trade as JSON: action / confidence / pair / quote_amount
      / stop / take_profit / reasoning.
  3.  The proposal is *immediately* run through the deterministic
      ``DecisionEngine``. Vetoes are surfaced back to the agent caller
      (and, optionally, the LLM gets a single retry with the rejection
      reason — autonomy within a guard rail).

When the LLM transport is unavailable (or the proposal is malformed),
the agent falls back to a deterministic synthesis of the ensemble +
pattern signal so the trading loop never stalls.

Output schema is intentionally *identical* to ``StrategistAgent`` so the
existing ``RiskManagerAgent`` / executor consume it unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.core.decision_engine import DecisionEngine
from src.core.trading_toolkit import ToolkitContext, TradingToolkit
from src.models.llm_responses import validate_strategist
from src.utils import llm_optimizer
from src.utils.logger import get_logger

logger = get_logger("agent.trader")


TRADER_SYSTEM_PROMPT = """You are an autonomous trading agent. You operate a deterministic toolkit of guardrails (edge library, capital allocator, AbsoluteRules). Your goal is to grow the portfolio while respecting those guardrails.

You have full autonomy over: which pair to trade (within the analyzed pair), buy/sell/hold, confidence, position sizing (within allocator budget), stop-loss, and take-profit.

You do NOT have authority over: enabling/disabling trading, changing the allocator weights, or bypassing AbsoluteRules.

Your toolkit has already gathered the following deterministic intel:
{tool_payload}

DECISION RULES:
- Combine ensemble verdict, pattern engine direction, edge stats, and your own market reading.
- If edge stats show no edge in the current regime (sharpe < 0.10 with >=30 samples), do not buy unless pattern engine strongly confirms.
- If allocator assigns ~0% to your strategy, do not propose a buy.
- Always set a stop-loss for buys.
- For sells, use the exact quantity from portfolio.open_positions for the pair.
- Honour fee context: do not propose a trade that cannot exceed the breakeven fee with reasonable probability.
- Honour strategy governance: new buys require posture=trade, or posture=watch with very high confidence and a clear fee-clearing target.
- Sizes must be positive and within ``allocator_budget_cap``.

Respond with EXACTLY this JSON shape (no prose, no markdown fences):
{{
  "action": "buy" | "sell" | "hold",
  "pair": "<pair from intel>",
  "confidence": 0.0-1.0,
  "quote_amount": <number or null>,
  "quantity": <number or null>,
  "stop_loss_price": <number or null>,
  "take_profit_price": <number or null>,
  "strategy": "llm_strategist",
  "reasoning": "<brief>"
}}
"""


class TraderAgent(BaseAgent):
    """LLM-driven autonomous trader operating the deterministic toolkit."""

    def __init__(self, llm, state, config, decision_engine: DecisionEngine):
        super().__init__("trader", llm, state, config)
        self.decision_engine = decision_engine
        self.min_confidence = (
            config.get("trading", {}).get("min_confidence", 0.65)
        )
        self.max_retries = 1  # one retry on veto

    # ------------------------------------------------------------------ #

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Args (context):
            pair, exchange, regime, current_price, market_signal,
            strategy_signals, pattern_signal, sentiment, news_headlines,
            fee_context, kelly_stats, recent_outcomes, strategic_context,
            portfolio_value, cash_balance, open_positions,
            edges, allocator,         # optional collaborators
            cycle_id, stats_db, trace_ctx, span (optional).
        """
        ctx = ToolkitContext(
            pair=context.get("pair", ""),
            exchange=context.get("exchange", "default"),
            regime=context.get("regime", "unknown"),
            current_price=float(context.get("current_price", 0.0) or 0.0),
            portfolio_value=float(context.get("portfolio_value", 0.0) or 0.0),
            cash_balance=float(context.get("cash_balance", 0.0) or 0.0),
            open_positions=dict(context.get("open_positions") or {}),
            market_signal=dict(context.get("market_signal") or context.get("signal") or {}),
            strategy_signals=dict(context.get("strategy_signals") or {}),
            pattern_signal=dict(context.get("pattern_signal") or {"available": False}),
            sentiment=dict(context.get("sentiment") or {}),
            news_headlines=str(context.get("news_headlines") or ""),
            fee_context=dict(context.get("fee_context") or {}),
            kelly_stats=dict(context.get("kelly_stats") or {}),
            recent_outcomes=str(context.get("recent_outcomes") or ""),
            strategic_context=str(context.get("strategic_context") or ""),
            strategy_policy=dict(context.get("strategy_policy") or {}),
        )
        toolkit = TradingToolkit(
            ctx=ctx,
            decision_engine=self.decision_engine,
            edges=context.get("edges"),
            allocator=context.get("allocator"),
        )

        tool_payload = self._snapshot_for_prompt(toolkit)

        # Cheap pre-screen: if there's an unambiguous "no actionable signal"
        # context, skip the LLM entirely. This mirrors strategist behaviour.
        signal_type = ctx.market_signal.get("signal_type", "neutral")
        confidence = float(ctx.market_signal.get("confidence", 0.0) or 0.0)
        if (
            signal_type in {"neutral", "weak_buy", "weak_sell"}
            and confidence < self.min_confidence
            and not _ensemble_actionable(ctx.strategy_signals.get("_ensemble"))
        ):
            reason = (
                f"signal {signal_type}/{confidence:.2f} below threshold and ensemble inactive"
            )
            self.logger.info(
                f"📋 Trader: HOLD {ctx.pair} (pre-screen: "
                f"signal={signal_type}/{confidence:.2f}, ensemble inactive)"
            )
            result = self._hold_result(ctx.pair, reason)
            self._persist_reasoning(
                context=context,
                ctx=ctx,
                result=result,
                verdict=result["decision_engine_verdict"],
                llm_metrics=[],
                signal_type=signal_type,
            )
            return result

        hard_skip_reason = self._hard_veto_skip_reason(ctx, tool_payload)
        if hard_skip_reason:
            self.logger.info(f"📋 Trader: HOLD {ctx.pair} (pre-veto: {hard_skip_reason})")
            result = self._hold_result(ctx.pair, hard_skip_reason)
            self._persist_reasoning(
                context=context,
                ctx=ctx,
                result=result,
                verdict=result["decision_engine_verdict"],
                llm_metrics=[],
                signal_type=signal_type,
            )
            return result

        # First LLM pass. Normal live-cycle reasoning uses Tier 2 unless the
        # deterministic context already marks the decision as high risk.
        route_tier, route_reasons = self._route_tier_for_decision(
            tool_payload,
            context,
        )
        route_attempts: list[dict[str, Any]] = [{
            "phase": "initial",
            "route_tier": route_tier,
            "reasons": list(route_reasons),
        }]
        proposal = await self._llm_propose(
            tool_payload,
            context,
            route_tier=route_tier,
            route_reasons=route_reasons,
        )
        proposal_metrics = _pop_llm_metrics(proposal)
        llm_metrics = [proposal_metrics] if proposal_metrics else []

        large_notional_reasons = self._large_notional_triggers(proposal, ctx)
        if route_tier < 3 and large_notional_reasons:
            route_tier = 3
            route_reasons = large_notional_reasons
            escalation_payload = dict(tool_payload)
            escalation_payload["tier3_escalation"] = {
                "reasons": route_reasons,
                "previous_proposal": _proposal_audit_view(proposal),
            }
            route_attempts.append({
                "phase": "large_notional_escalation",
                "route_tier": route_tier,
                "reasons": list(route_reasons),
            })
            proposal = await self._llm_propose(
                escalation_payload,
                context,
                route_tier=route_tier,
                route_reasons=route_reasons,
            )
            proposal_metrics = _pop_llm_metrics(proposal)
            if proposal_metrics:
                llm_metrics.append(proposal_metrics)

        verdict = toolkit.propose_trade(
            pair=proposal.get("pair", ctx.pair),
            action=proposal.get("action", "hold"),
            confidence=float(proposal.get("confidence", 0.0) or 0.0),
            strategy=proposal.get("strategy", "llm_strategist"),
            quote_amount=_optional_float(proposal.get("quote_amount")),
            quantity=_optional_float(proposal.get("quantity")),
            stop_loss_price=_optional_float(proposal.get("stop_loss_price")),
            take_profit_price=_optional_float(proposal.get("take_profit_price")),
            reasoning=str(proposal.get("reasoning", "")),
        )

        # One bounded retry on veto, with the rejection reason fed back.
        retry_enabled = bool(llm_optimizer.get("trader_retry_on_veto", True))
        if (
            retry_enabled
            and not verdict["approved"]
            and verdict.get("veto")
            and self.max_retries > 0
            and _veto_is_adjustable(str(verdict.get("veto") or ""))
        ):
            retry_payload = dict(tool_payload)
            retry_payload["last_proposal_rejected"] = {
                "veto": verdict["veto"],
                "reasons": verdict["reasons"],
                "hint": (
                    "Previous proposal was vetoed. Either submit a smaller / "
                    "differently-shaped proposal that respects the veto, or "
                    "decide to hold."
                ),
            }
            retry_reasons = ["retry_after_veto"]
            route_attempts.append({
                "phase": "retry_after_veto",
                "route_tier": 3,
                "reasons": retry_reasons,
            })
            proposal2 = await self._llm_propose(
                retry_payload,
                context,
                route_tier=3,
                route_reasons=retry_reasons,
            )
            proposal2_metrics = _pop_llm_metrics(proposal2)
            if proposal2_metrics:
                llm_metrics.append(proposal2_metrics)
            if proposal2.get("action") != proposal.get("action") or _optional_float(
                proposal2.get("quote_amount")
            ) != _optional_float(proposal.get("quote_amount")):
                verdict = toolkit.propose_trade(
                    pair=proposal2.get("pair", ctx.pair),
                    action=proposal2.get("action", "hold"),
                    confidence=float(proposal2.get("confidence", 0.0) or 0.0),
                    strategy=proposal2.get("strategy", "llm_strategist"),
                    quote_amount=_optional_float(proposal2.get("quote_amount")),
                    quantity=_optional_float(proposal2.get("quantity")),
                    stop_loss_price=_optional_float(proposal2.get("stop_loss_price")),
                    take_profit_price=_optional_float(proposal2.get("take_profit_price")),
                    reasoning=str(proposal2.get("reasoning", "")),
                )
                proposal = proposal2

        # Pipeline expects a strategist-shaped dict.
        result = dict(verdict["proposal"])
        # Keep backward-compatible fields the pipeline reads.
        result.setdefault("pair", ctx.pair)
        result.setdefault("action", verdict["action"])
        result.setdefault("confidence", float(proposal.get("confidence", 0.0) or 0.0))
        result["decision_engine_verdict"] = verdict
        result["llm_route_tier"] = int(route_attempts[-1]["route_tier"])
        result["llm_route_reasons"] = list(route_attempts[-1]["reasons"])
        result["llm_route_attempts"] = route_attempts

        # Sizing fallback: an LLM proposal that omits quote_amount/quantity
        # would otherwise be silently rejected downstream by RiskManager
        # ("No valid trade amount specified"). The DecisionEngine has already
        # computed the allocator-bounded ceiling for buys; sells can use the
        # live position quantity from the toolkit snapshot.
        quote_amount_value = None
        quantity_value = None
        if verdict["approved"]:
            quote_amount_value = _optional_float(result.get("quote_amount"))
            quantity_value = _optional_float(result.get("quantity"))

        if verdict["approved"] and result.get("action") == "buy":
            if (
                (quote_amount_value is None or quote_amount_value <= 0)
                and (quantity_value is None or quantity_value <= 0)
            ):
                cap = _optional_float(verdict.get("quote_amount_max"))
                if cap is not None and cap > 0:
                    result["quote_amount"] = cap
                    self.logger.info(
                        f"Trader sizing fallback: quote_amount=null → "
                        f"allocator_cap {cap:.2f} for {ctx.pair}"
                    )
        elif verdict["approved"] and result.get("action") == "sell":
            result_pair = str(result.get("pair") or ctx.pair)
            held_quantity = _position_quantity(ctx.open_positions, result_pair)
            if held_quantity is None or held_quantity <= 0:
                reason = f"Sell signal for {result_pair} but no held position is available to size."
                result["action"] = "hold"
                result["reason"] = reason
                result["reasoning"] = _append_reason(result.get("reasoning"), reason)
            elif (
                (quote_amount_value is None or quote_amount_value <= 0)
                and (quantity_value is None or quantity_value <= 0)
            ):
                price = _optional_float(result.get("current_price")) or ctx.current_price
                if held_quantity is not None and held_quantity > 0 and price > 0:
                    result["quantity"] = held_quantity
                    result["quote_amount"] = held_quantity * price
                    self.logger.info(
                        f"Trader sell sizing fallback: quantity=null → "
                        f"held_quantity {held_quantity:.8f} for {result_pair}"
                    )

        if not verdict["approved"]:
            result["action"] = "hold"
            result["reason"] = (
                f"DecisionEngine veto[{verdict.get('veto')}]: "
                + "; ".join(verdict.get("reasons", []))
            )
        self.logger.info(
            f"📋 Trader: {result['action'].upper()} {ctx.pair} "
            f"conf={result.get('confidence', 0):.2f} "
            f"approved={verdict['approved']} veto={verdict.get('veto')}"
        )
        self._persist_reasoning(
            context=context,
            ctx=ctx,
            result=result,
            verdict=verdict,
            llm_metrics=llm_metrics,
            signal_type=signal_type,
        )
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _llm_propose(
        self,
        tool_payload: dict,
        context: dict,
        *,
        route_tier: int = 2,
        route_reasons: Optional[list[str]] = None,
    ) -> dict:
        """Run the LLM proposal call. Falls back to a deterministic synthesis."""
        trace_ctx = context.get("trace_ctx")
        span = None
        payload_text = json.dumps(tool_payload, default=str, separators=(",", ":"))
        payload_cap = int(llm_optimizer.get("trader_tool_payload_max_chars", 2400) or 2400)
        if len(payload_text) > payload_cap:
            payload_text = payload_text[:payload_cap].rstrip() + " [...]"
        system_prompt = TRADER_SYSTEM_PROMPT.format(
            tool_payload=payload_text
        )
        user_msg = (
            f"Decide an action for {tool_payload.get('market', {}).get('pair', '?')}. "
            "Respond with the JSON shape from the system prompt only."
        )
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                self.name,
                input_data={"system": system_prompt[:500], "user": user_msg[:200]},
                model=getattr(self.llm, "model", ""),
            )
        try:
            llm_response = await self.llm.chat_json(
                system_prompt=system_prompt,
                user_message=user_msg,
                max_tokens=600,
                span=span,
                agent_name=self.name,
                priority="high" if route_tier >= 3 else None,
                route_tier=route_tier,
            )
        except Exception as exc:
            self.logger.warning(f"trader LLM call failed: {exc} — falling back")
            fallback = self._deterministic_fallback(tool_payload)
            fallback["_llm_metrics"] = _build_llm_metrics(
                span, system_prompt, user_msg, {"error": str(exc)}, payload_text,
                route_tier=route_tier,
                route_reasons=route_reasons,
            )
            return fallback

        if not isinstance(llm_response, dict) or "error" in llm_response:
            self.logger.warning(f"trader LLM error: {llm_response}")
            fallback = self._deterministic_fallback(tool_payload)
            fallback["_llm_metrics"] = _build_llm_metrics(
                span, system_prompt, user_msg, llm_response, payload_text,
                route_tier=route_tier,
                route_reasons=route_reasons,
            )
            return fallback

        sanitized, schema_err = validate_strategist(llm_response)
        if schema_err:
            self.logger.warning(f"trader LLM schema invalid: {schema_err}")
            fallback = self._deterministic_fallback(tool_payload)
            fallback["_llm_metrics"] = _build_llm_metrics(
                span, system_prompt, user_msg, llm_response, payload_text,
                route_tier=route_tier,
                route_reasons=route_reasons,
            )
            return fallback
        # `extra="ignore"` strips `strategy` — preserve it from the raw response.
        if "strategy" in llm_response and "strategy" not in sanitized:
            sanitized["strategy"] = str(llm_response.get("strategy") or "llm_strategist")
        sanitized.setdefault("strategy", "llm_strategist")
        sanitized["_llm_metrics"] = _build_llm_metrics(
            span, system_prompt, user_msg, llm_response, payload_text,
            route_tier=route_tier,
            route_reasons=route_reasons,
        )
        return sanitized

    @staticmethod
    def _deterministic_fallback(tool_payload: dict) -> dict:
        """If the LLM is unavailable, synthesize a conservative proposal from
        ensemble + market analyst + pattern. Never larger than the allocator
        cap. Promotes a high-conviction analyst signal when the ensemble is
        inactive (the FIL-EUR regression case), unless the pattern engine
        actively contradicts it.
        """
        ensemble = (tool_payload.get("strategy_signals") or {}).get("ensemble") or {}
        pattern = tool_payload.get("pattern") or {}
        market = tool_payload.get("market") or {}
        portfolio = tool_payload.get("portfolio") or {}
        pair = market.get("pair", "")

        ens_action = (ensemble.get("action") or "hold").lower()
        ens_conf = float(ensemble.get("confidence", 0.0) or 0.0)
        analyst_action = (market.get("signal_type") or "neutral").lower()
        analyst_conf = float(market.get("signal_confidence", 0.0) or 0.0)
        pattern_avail = bool(pattern.get("available"))
        pattern_dir = (pattern.get("direction") or "").lower()
        current_price = _optional_float(market.get("current_price"))
        stop_loss = _optional_float(market.get("suggested_stop_loss"))
        take_profit = _optional_float(market.get("suggested_take_profit"))
        quote_amount = None
        quantity = None

        reason_bits = [
            f"deterministic fallback: ensemble={ens_action}@{ens_conf:.2f}"
        ]
        if pattern_avail:
            reason_bits.append(f"pattern={pattern_dir or 'unknown'}")

        # Map signal types to trade action.
        analyst_action_map = {"buy": "buy", "sell": "sell", "strong_buy": "buy",
                              "strong_sell": "sell"}
        analyst_trade = analyst_action_map.get(analyst_action)

        # Detect pattern contradiction with the analyst direction.
        contradicts = False
        if pattern_avail and analyst_trade:
            if analyst_trade == "buy" and pattern_dir in ("bearish", "down"):
                contradicts = True
            elif analyst_trade == "sell" and pattern_dir in ("bullish", "up"):
                contradicts = True

        # Default: mirror the ensemble.
        action = ens_action
        confidence = ens_conf
        ensemble_actionable = ens_action in ("buy", "sell") and ens_conf >= 0.55

        if ensemble_actionable:
            reason_bits.append(f"ensemble={ens_action}@{ens_conf:.2f} actionable")
        elif analyst_trade and analyst_conf >= 0.70:
            if contradicts:
                action = "hold"
                confidence = 0.0
                reason_bits.append(
                    f"analyst={analyst_action}@{analyst_conf:.2f} contradicted by pattern"
                )
                stop_loss = None
                take_profit = None
            else:
                action = analyst_trade
                confidence = analyst_conf
                reason_bits.append(
                    f"analyst={analyst_action}@{analyst_conf:.2f} promoted (ensemble inactive)"
                )
                # Synthesize a conservative ±3% stop if the analyst did not
                # supply one and we know the current price.
                if action == "buy" and stop_loss is None and current_price:
                    stop_loss = round(current_price * 0.97, 8)
                    reason_bits.append("synthesized 3% stop")
                elif action == "sell" and stop_loss is None and current_price:
                    stop_loss = round(current_price * 1.03, 8)
                    reason_bits.append("synthesized 3% stop")
        else:
            action = "hold"
            confidence = 0.0
            stop_loss = None
            take_profit = None

        # Final safety net for an inherited buy below floor.
        if action == "buy" and confidence < 0.65:
            action = "hold"
            confidence = 0.0
            stop_loss = None
            take_profit = None
            reason_bits.append("confidence below buy floor")

        if action == "sell":
            held_quantity = _position_quantity(portfolio.get("open_positions") or {}, pair)
            if held_quantity is None or held_quantity <= 0:
                action = "hold"
                confidence = 0.0
                stop_loss = None
                take_profit = None
                reason_bits.append("sell signal ignored because no held position is available")
            else:
                quantity = held_quantity
                if current_price and current_price > 0:
                    quote_amount = held_quantity * current_price
                reason_bits.append("sized sell from held position")

        return {
            "action": action,
            "pair": pair,
            "confidence": confidence,
            "quote_amount": quote_amount,
            "quantity": quantity,
            "stop_loss_price": stop_loss,
            "take_profit_price": take_profit,
            "strategy": "llm_strategist",
            "reasoning": "; ".join(reason_bits),
        }

    @staticmethod
    def _hold_result(pair: str, reason: str) -> dict:
        return {
            "action": "hold",
            "pair": pair,
            "confidence": 0.0,
            "reasoning": reason,
            "reason": reason,
            "decision_engine_verdict": {
                "approved": True, "action": "hold", "veto": None,
                "reasons": [reason],
                "proposal": {"action": "hold", "pair": pair},
                "quote_amount_max": 0.0,
            },
        }

    @staticmethod
    def _hard_veto_skip_reason(ctx: ToolkitContext, tool_payload: dict) -> str:
        if not bool(llm_optimizer.get("trader_hard_veto_skip_enabled", True)):
            return ""
        signal_type = str(ctx.market_signal.get("signal_type", "neutral") or "neutral").lower()
        confidence = float(ctx.market_signal.get("confidence", 0.0) or 0.0)
        held_qty = _position_quantity(ctx.open_positions, ctx.pair)
        has_position = held_qty is not None and held_qty != 0.0
        actionable_signal = signal_type in {"buy", "strong_buy", "sell", "strong_sell"} and confidence >= 0.65
        pattern = tool_payload.get("pattern") or {}
        pattern_direction = str(pattern.get("direction", "neutral") or "neutral").lower()
        pattern_actionable = bool(pattern.get("available")) and pattern_direction in {"bullish", "bearish"}
        ensemble = (tool_payload.get("strategy_signals") or {}).get("ensemble")
        ensemble_action = str((ensemble or {}).get("action", "") or "").lower()
        if not has_position and (signal_type in {"sell", "strong_sell", "weak_sell"} or ensemble_action == "sell"):
            return "sell signal but no held position"
        if ctx.cash_balance <= 0 and not has_position and signal_type not in {"sell", "strong_sell"}:
            return "no available cash and no held position"
        allocator = tool_payload.get("allocator") or {}
        weights = allocator.get("weights") if isinstance(allocator, dict) else None
        if isinstance(weights, dict) and weights:
            max_weight = max((_optional_float(v) or 0.0) for v in weights.values())
            if max_weight <= 0:
                return "allocator has no positive strategy budget"
        if not actionable_signal and not pattern_actionable and not _ensemble_actionable(ensemble):
            return f"no actionable signal after deterministic checks ({signal_type}/{confidence:.2f})"
        return ""

    def _route_tier_for_decision(
        self,
        tool_payload: dict,
        context: dict[str, Any],
    ) -> tuple[int, list[str]]:
        reasons: list[str] = []
        if bool(context.get("high_stakes_active")):
            reasons.append("high_stakes_mode")
        reasons.extend(self._signal_uncertainty_triggers(tool_payload))
        if reasons:
            return 3, _dedupe(reasons)
        return 2, ["normal_cycle"]

    def _signal_uncertainty_triggers(self, tool_payload: dict) -> list[str]:
        market = tool_payload.get("market") or {}
        strategy_signals = tool_payload.get("strategy_signals") or {}
        ensemble = strategy_signals.get("ensemble") or {}
        pattern = tool_payload.get("pattern") or {}

        market_action = _market_signal_action(market.get("signal_type"))
        market_confidence = _optional_float(market.get("signal_confidence")) or 0.0
        ensemble_action = _trade_action(ensemble.get("action"))
        pattern_action = _pattern_direction_action(pattern.get("direction"))
        directional_actions = {
            action for action in (market_action, ensemble_action, pattern_action)
            if action in {"buy", "sell"}
        }

        reasons: list[str] = []
        if len(directional_actions) > 1:
            reasons.append("conflicting_signals")

        ambiguous_floor = float(llm_optimizer.get("trader_tier3_ambiguous_confidence", 0.70) or 0.70)
        if (
            market_action in {"buy", "sell"}
            and market_confidence >= 0.55
            and market_confidence < max(self.min_confidence, ambiguous_floor)
        ):
            reasons.append("ambiguous_market_signal")

        ensemble_agreement = _optional_float(ensemble.get("agreement"))
        ensemble_count = int(_optional_float(ensemble.get("n_strategies")) or 0)
        if (
            ensemble_action in {"buy", "sell"}
            and ensemble_count >= 2
            and ensemble_agreement is not None
            and ensemble_agreement < 0.50
        ):
            reasons.append("low_ensemble_agreement")

        return reasons

    @staticmethod
    def _large_notional_triggers(proposal: dict, ctx: ToolkitContext) -> list[str]:
        notional = _proposal_notional(proposal, ctx.current_price)
        if notional is None or notional <= 0:
            return []

        reasons: list[str] = []
        notional_threshold = float(
            llm_optimizer.get("trader_tier3_notional_threshold", 750.0) or 0.0
        )
        portfolio_pct_threshold = float(
            llm_optimizer.get("trader_tier3_portfolio_pct", 0.10) or 0.0
        )

        if notional_threshold > 0 and notional >= notional_threshold:
            reasons.append(f"large_notional:{notional:.2f}")
        if ctx.portfolio_value > 0 and portfolio_pct_threshold > 0:
            portfolio_pct = notional / ctx.portfolio_value
            if portfolio_pct >= portfolio_pct_threshold:
                reasons.append(f"large_portfolio_pct:{portfolio_pct:.2%}")
        return reasons

    @staticmethod
    def _snapshot_for_prompt(toolkit: TradingToolkit) -> dict:
        return _strip_empty_payload({
            "market": toolkit.get_market_snapshot(),
            "strategy_signals": toolkit.get_strategy_signals(),
            "pattern": toolkit.get_pattern_signal(),
            "edges": toolkit.get_edge_stats(),
            "allocator": toolkit.get_allocator_weights(),
            "strategy_policy": toolkit.get_strategy_policy(),
            "portfolio": toolkit.get_portfolio_state(),
        })

    def _persist_reasoning(
        self,
        *,
        context: dict[str, Any],
        ctx: ToolkitContext,
        result: dict[str, Any],
        verdict: dict[str, Any],
        llm_metrics: list[dict[str, Any]],
        signal_type: str,
    ) -> None:
        cycle_id = context.get("cycle_id")
        stats_db = context.get("stats_db")
        if not cycle_id or not stats_db:
            return
        try:
            token_metrics = _sum_llm_metrics(llm_metrics)
            stats_db.save_reasoning(
                cycle_id=cycle_id,
                pair=ctx.pair,
                agent_name="trader",
                reasoning_json={
                    **dict(result or {}),
                    "verdict": verdict,
                    "llm_attempts": len(llm_metrics),
                    "llm_metrics": llm_metrics,
                },
                signal_type=signal_type,
                confidence=float((result or {}).get("confidence", 0.0) or 0.0),
                langfuse_trace_id=token_metrics.get("langfuse_trace_id") or None,
                langfuse_span_id=token_metrics.get("langfuse_span_id") or None,
                prompt_tokens=int(token_metrics.get("prompt_tokens", 0) or 0),
                completion_tokens=int(token_metrics.get("completion_tokens", 0) or 0),
                latency_ms=float(token_metrics.get("latency_ms", 0.0) or 0.0),
                raw_prompt=str(token_metrics.get("raw_prompt", ""))[:1000],
                exchange=ctx.exchange,
            )
        except Exception as exc:
            self.logger.debug(f"trader reasoning persist failed: {exc}")


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _optional_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _position_quantity(positions: dict, pair: str) -> Optional[float]:
    if not isinstance(positions, dict) or not pair:
        return None
    aliases = _pair_aliases(pair)
    raw_position = None
    found = False
    for pos_pair, candidate in positions.items():
        if aliases.intersection(_pair_aliases(str(pos_pair))):
            raw_position = candidate
            found = True
            break
    if not found:
        return None
    if isinstance(raw_position, dict):
        raw_position = raw_position.get("quantity", raw_position.get("amount"))
    elif hasattr(raw_position, "quantity"):
        raw_position = getattr(raw_position, "quantity")
    return _optional_float(raw_position)


def _pair_aliases(pair: str) -> set[str]:
    value = str(pair or "").strip().upper()
    aliases = {value} if value else set()
    for separator in ("-", "/"):
        if separator in value:
            aliases.add(value.rsplit(separator, 1)[0])
    return aliases


def _append_reason(existing: Any, reason: str) -> str:
    text = str(existing or "").strip()
    if not text:
        return reason
    return f"{text}; {reason}"


def _trade_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    return action if action in {"buy", "sell", "hold"} else ""


def _market_signal_action(value: Any) -> str:
    signal = str(value or "").strip().lower()
    if signal in {"buy", "strong_buy", "weak_buy"}:
        return "buy"
    if signal in {"sell", "strong_sell", "weak_sell"}:
        return "sell"
    if signal == "neutral":
        return "hold"
    return ""


def _pattern_direction_action(value: Any) -> str:
    direction = str(value or "").strip().lower()
    if direction in {"bullish", "up", "buy"}:
        return "buy"
    if direction in {"bearish", "down", "sell"}:
        return "sell"
    if direction == "neutral":
        return "hold"
    return ""


def _proposal_notional(proposal: dict, current_price: float) -> Optional[float]:
    if not isinstance(proposal, dict):
        return None
    quote_amount = _optional_float(proposal.get("quote_amount"))
    if quote_amount is not None and quote_amount > 0:
        return abs(quote_amount)
    quantity = _optional_float(proposal.get("quantity"))
    price = _optional_float(proposal.get("current_price")) or current_price
    if quantity is not None and quantity > 0 and price > 0:
        return abs(quantity * price)
    return None


def _proposal_audit_view(proposal: dict) -> dict:
    if not isinstance(proposal, dict):
        return {}
    return {
        key: proposal.get(key)
        for key in (
            "action", "pair", "confidence", "quote_amount", "quantity",
            "stop_loss_price", "take_profit_price", "reasoning",
        )
        if key in proposal
    }


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _ensemble_actionable(ensemble: Optional[dict]) -> bool:
    if not isinstance(ensemble, dict):
        return False
    if (ensemble.get("action") or "hold").lower() == "hold":
        return False
    try:
        return float(ensemble.get("confidence", 0.0) or 0.0) >= 0.55
    except (TypeError, ValueError):
        return False


def _estimate_tokens(*parts: Any) -> int:
    chars = sum(len(str(part or "")) for part in parts)
    return max(0, int(chars / 4))


def _build_llm_metrics(
    span: Any,
    system_prompt: str,
    user_msg: str,
    output: Any,
    payload_text: str,
    *,
    route_tier: int = 2,
    route_reasons: Optional[list[str]] = None,
) -> dict:
    prompt_tokens = int(getattr(span, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(span, "completion_tokens", 0) or 0)
    if prompt_tokens <= 0:
        prompt_tokens = _estimate_tokens(system_prompt, user_msg)
    if completion_tokens <= 0 and output is not None:
        completion_tokens = _estimate_tokens(json.dumps(output, default=str))
    return {
        "langfuse_trace_id": getattr(span, "trace_id", "") if span else "",
        "langfuse_span_id": getattr(span, "span_id", "") if span else "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": float(getattr(span, "latency_ms", 0.0) or 0.0),
        "route_tier": int(route_tier),
        "route_reasons": list(route_reasons or []),
        "raw_prompt": json.dumps({
            "system_chars": len(system_prompt),
            "payload_chars": len(payload_text),
            "user": user_msg,
            "route_tier": int(route_tier),
            "route_reasons": list(route_reasons or []),
            "payload_preview": payload_text[:500],
        }, default=str),
    }


def _pop_llm_metrics(proposal: dict) -> dict:
    if not isinstance(proposal, dict):
        return {}
    metrics = proposal.pop("_llm_metrics", {})
    return metrics if isinstance(metrics, dict) else {}


def _sum_llm_metrics(metrics: list[dict]) -> dict:
    if not metrics:
        return {}
    return {
        "langfuse_trace_id": next((m.get("langfuse_trace_id") for m in metrics if m.get("langfuse_trace_id")), ""),
        "langfuse_span_id": next((m.get("langfuse_span_id") for m in reversed(metrics) if m.get("langfuse_span_id")), ""),
        "prompt_tokens": sum(int(m.get("prompt_tokens", 0) or 0) for m in metrics),
        "completion_tokens": sum(int(m.get("completion_tokens", 0) or 0) for m in metrics),
        "latency_ms": sum(float(m.get("latency_ms", 0.0) or 0.0) for m in metrics),
        "raw_prompt": metrics[-1].get("raw_prompt", ""),
    }


def _veto_is_adjustable(veto: str) -> bool:
    return veto in {"absolute_rules"}


def _strip_empty_payload(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("available") is False:
            return {}
        out = {}
        for key, item in value.items():
            cleaned = _strip_empty_payload(item)
            if cleaned in ({}, [], "", None):
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [item for item in (_strip_empty_payload(item) for item in value) if item not in ({}, [], "", None)]
    return value


__all__ = ["TraderAgent"]
