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
            self.logger.info(
                f"📋 Trader: HOLD {ctx.pair} (pre-screen: "
                f"signal={signal_type}/{confidence:.2f}, ensemble inactive)"
            )
            return self._hold_result(
                ctx.pair,
                f"signal {signal_type}/{confidence:.2f} below threshold and ensemble inactive",
            )

        hard_skip_reason = self._hard_veto_skip_reason(ctx, tool_payload)
        if hard_skip_reason:
            self.logger.info(f"📋 Trader: HOLD {ctx.pair} (pre-veto: {hard_skip_reason})")
            return self._hold_result(ctx.pair, hard_skip_reason)

        # First LLM pass.
        proposal = await self._llm_propose(tool_payload, context)
        proposal_metrics = _pop_llm_metrics(proposal)
        llm_metrics = [proposal_metrics] if proposal_metrics else []
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
            proposal2 = await self._llm_propose(retry_payload, context)
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

        # Persist reasoning trace if a stats_db is available.
        cycle_id = context.get("cycle_id")
        stats_db = context.get("stats_db")
        if cycle_id and stats_db:
            try:
                token_metrics = _sum_llm_metrics(llm_metrics)
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair=ctx.pair,
                    agent_name="trader",
                    reasoning_json={
                        **proposal,
                        "verdict": verdict,
                        "llm_attempts": len(llm_metrics),
                        "llm_metrics": llm_metrics,
                    },
                    signal_type=signal_type,
                    confidence=float(proposal.get("confidence", 0.0) or 0.0),
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

        # Pipeline expects a strategist-shaped dict.
        result = dict(verdict["proposal"])
        # Keep backward-compatible fields the pipeline reads.
        result.setdefault("pair", ctx.pair)
        result.setdefault("action", verdict["action"])
        result.setdefault("confidence", float(proposal.get("confidence", 0.0) or 0.0))
        result["decision_engine_verdict"] = verdict

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
            if (
                (quote_amount_value is None or quote_amount_value <= 0)
                and (quantity_value is None or quantity_value <= 0)
            ):
                result_pair = str(result.get("pair") or ctx.pair)
                held_quantity = _position_quantity(ctx.open_positions, result_pair)
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
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _llm_propose(
        self,
        tool_payload: dict,
        context: dict,
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
            )
        except Exception as exc:
            self.logger.warning(f"trader LLM call failed: {exc} — falling back")
            fallback = self._deterministic_fallback(tool_payload)
            fallback["_llm_metrics"] = _build_llm_metrics(
                span, system_prompt, user_msg, {"error": str(exc)}, payload_text
            )
            return fallback

        if not isinstance(llm_response, dict) or "error" in llm_response:
            self.logger.warning(f"trader LLM error: {llm_response}")
            fallback = self._deterministic_fallback(tool_payload)
            fallback["_llm_metrics"] = _build_llm_metrics(
                span, system_prompt, user_msg, llm_response, payload_text
            )
            return fallback

        sanitized, schema_err = validate_strategist(llm_response)
        if schema_err:
            self.logger.warning(f"trader LLM schema invalid: {schema_err}")
            fallback = self._deterministic_fallback(tool_payload)
            fallback["_llm_metrics"] = _build_llm_metrics(
                span, system_prompt, user_msg, llm_response, payload_text
            )
            return fallback
        # `extra="ignore"` strips `strategy` — preserve it from the raw response.
        if "strategy" in llm_response and "strategy" not in sanitized:
            sanitized["strategy"] = str(llm_response.get("strategy") or "llm_strategist")
        sanitized.setdefault("strategy", "llm_strategist")
        sanitized["_llm_metrics"] = _build_llm_metrics(
            span, system_prompt, user_msg, llm_response, payload_text
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

        return {
            "action": action,
            "pair": pair,
            "confidence": confidence,
            "quote_amount": None,
            "quantity": None,
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
        held_qty = _optional_float(ctx.open_positions.get(ctx.pair)) if ctx.open_positions else None
        has_position = held_qty is not None and held_qty != 0.0
        actionable_signal = signal_type in {"buy", "strong_buy", "sell", "strong_sell"} and confidence >= 0.65
        pattern = tool_payload.get("pattern") or {}
        pattern_direction = str(pattern.get("direction", "neutral") or "neutral").lower()
        pattern_actionable = bool(pattern.get("available")) and pattern_direction in {"bullish", "bearish"}
        ensemble = (tool_payload.get("strategy_signals") or {}).get("ensemble")
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

    @staticmethod
    def _snapshot_for_prompt(toolkit: TradingToolkit) -> dict:
        return _strip_empty_payload({
            "market": toolkit.get_market_snapshot(),
            "strategy_signals": toolkit.get_strategy_signals(),
            "pattern": toolkit.get_pattern_signal(),
            "edges": toolkit.get_edge_stats(),
            "allocator": toolkit.get_allocator_weights(),
            "portfolio": toolkit.get_portfolio_state(),
        })


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
    raw_position = positions.get(pair)
    if isinstance(raw_position, dict):
        raw_position = raw_position.get("quantity", raw_position.get("amount"))
    elif hasattr(raw_position, "quantity"):
        raw_position = getattr(raw_position, "quantity")
    return _optional_float(raw_position)


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
        "raw_prompt": json.dumps({
            "system_chars": len(system_prompt),
            "payload_chars": len(payload_text),
            "user": user_msg,
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
