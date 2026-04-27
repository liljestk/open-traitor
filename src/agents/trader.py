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

        # First LLM pass.
        proposal = await self._llm_propose(tool_payload, context)
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
        if not verdict["approved"] and verdict.get("veto") and self.max_retries > 0:
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
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair=ctx.pair,
                    agent_name="trader",
                    reasoning_json={**proposal, "verdict": verdict},
                    signal_type=signal_type,
                    confidence=float(proposal.get("confidence", 0.0) or 0.0),
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
        # computed the allocator-bounded ceiling — use it as the size when
        # the LLM left sizing to the toolkit. Risk manager will re-clamp.
        if verdict["approved"] and result.get("action") == "buy":
            qa = _optional_float(result.get("quote_amount"))
            qty = _optional_float(result.get("quantity"))
            if (qa is None or qa <= 0) and (qty is None or qty <= 0):
                cap = _optional_float(verdict.get("quote_amount_max"))
                if cap is not None and cap > 0:
                    result["quote_amount"] = cap
                    self.logger.info(
                        f"Trader sizing fallback: quote_amount=null → "
                        f"allocator_cap {cap:.2f} for {ctx.pair}"
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
        system_prompt = TRADER_SYSTEM_PROMPT.format(
            tool_payload=json.dumps(tool_payload, default=str, indent=2)[:6000]
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
            return self._deterministic_fallback(tool_payload)

        if not isinstance(llm_response, dict) or "error" in llm_response:
            self.logger.warning(f"trader LLM error: {llm_response}")
            return self._deterministic_fallback(tool_payload)

        sanitized, schema_err = validate_strategist(llm_response)
        if schema_err:
            self.logger.warning(f"trader LLM schema invalid: {schema_err}")
            return self._deterministic_fallback(tool_payload)
        # `extra="ignore"` strips `strategy` — preserve it from the raw response.
        if "strategy" in llm_response and "strategy" not in sanitized:
            sanitized["strategy"] = str(llm_response.get("strategy") or "llm_strategist")
        sanitized.setdefault("strategy", "llm_strategist")
        return sanitized

    @staticmethod
    def _deterministic_fallback(tool_payload: dict) -> dict:
        """If the LLM is unavailable, synthesize a conservative proposal from
        ensemble + pattern. Never larger than the allocator cap."""
        ensemble = (tool_payload.get("strategy_signals") or {}).get("ensemble") or {}
        pattern = tool_payload.get("pattern") or {}
        market = tool_payload.get("market") or {}
        pair = market.get("pair", "")
        action = ensemble.get("action", "hold") or "hold"
        confidence = float(ensemble.get("confidence", 0.0) or 0.0)
        reason_bits = [f"deterministic fallback: ensemble={action}@{confidence:.2f}"]
        if pattern.get("available"):
            reason_bits.append(f"pattern={pattern.get('direction')}")
        if action == "buy" and confidence < 0.65:
            action = "hold"
            reason_bits.append("confidence below buy floor")
        return {
            "action": action,
            "pair": pair,
            "confidence": confidence,
            "quote_amount": None,
            "quantity": None,
            "stop_loss_price": None,
            "take_profit_price": None,
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
    def _snapshot_for_prompt(toolkit: TradingToolkit) -> dict:
        return {
            "market": toolkit.get_market_snapshot(),
            "strategy_signals": toolkit.get_strategy_signals(),
            "pattern": toolkit.get_pattern_signal(),
            "edges": toolkit.get_edge_stats(),
            "allocator": toolkit.get_allocator_weights(),
            "portfolio": toolkit.get_portfolio_state(),
        }


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


def _ensemble_actionable(ensemble: Optional[dict]) -> bool:
    if not isinstance(ensemble, dict):
        return False
    if (ensemble.get("action") or "hold").lower() == "hold":
        return False
    return float(ensemble.get("confidence", 0.0) or 0.0) >= 0.55


__all__ = ["TraderAgent"]
