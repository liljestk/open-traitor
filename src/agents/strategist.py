"""
Strategist Agent — Generates trading strategies and specific trade proposals
based on market analysis signals and active tasks.
"""

from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.models.trade import TradeAction
from src.utils.logger import get_logger

logger = get_logger("agent.strategist")


STRATEGY_SYSTEM_PROMPT = """You are a cryptocurrency trading strategist.
Based on the market analysis signal and any active user tasks, decide what action to take.

Consider:
1. The signal confidence and type
2. Current portfolio positions
3. Active tasks from the user (spending limits, specific instructions)
4. Risk management parameters
5. Recent trade history (avoid overtrading)
6. Recent trade outcomes for this pair — adapt if similar setups have repeatedly lost
7. Strategic context from longer-term planning (daily/weekly/monthly) — use as regime background, not hard override
8. Actual Coinbase holdings — you may propose selling pre-existing holdings, not just bot-opened ones

Respond with JSON:

{
    "action": "buy" | "sell" | "hold",
    "pair": "BTC-EUR",
    "confidence": 0.0-1.0,
    "quote_amount": amount_in_quote_currency_or_null,
    "quantity": crypto_quantity_or_null,
    "stop_loss_price": price_or_null,
    "take_profit_price": price_or_null,
    "reasoning": "Why this action",
    "task_alignment": "How this relates to user's active tasks (if any)"
}

Rules:
- If confidence is below 0.6, always recommend "hold"
- Never exceed spending limits specified in tasks
- Always set stop-loss for buy orders
- Consider the current portfolio before adding more of the same asset
- If the signal is neutral but there's no compelling reason to trade, hold
- Capital preservation is always the top priority
- For sells of existing holdings, use the quantity shown in ACTUAL COINBASE HOLDINGS"""


class StrategistAgent(BaseAgent):
    """Generates specific trade proposals from market signals."""

    def __init__(self, llm, state, config):
        super().__init__("strategist", llm, state, config)
        self.min_confidence = config.get("trading", {}).get("min_confidence", 0.7)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Generate a trade proposal based on market analysis.

        Context expected:
            - signal: dict (from MarketAnalystAgent)
            - active_tasks: list[dict] (user-defined tasks)
            - current_balance: dict
            - open_positions: dict
            - recent_outcomes: str (optional, from stats_db.get_recent_outcomes)
            - strategic_context: str (optional, from planning workflows)
            - cycle_id: str (optional, for reasoning persistence)
            - stats_db: StatsDB instance (optional)
            - live_holdings_summary: str (optional, formatted Coinbase holdings)
            - native_currency: str (optional, e.g. "EUR")
            - currency_symbol: str (optional, e.g. "€")
        """
        signal = context.get("signal", {})
        active_tasks = context.get("active_tasks", [])
        balance = context.get("current_balance", {})
        positions = context.get("open_positions", {})
        recent_trades = context.get("recent_trades", [])
        recent_outcomes = context.get("recent_outcomes", "")
        strategic_context = context.get("strategic_context", "")
        live_holdings_summary = context.get("live_holdings_summary", "")
        currency_symbol = context.get("currency_symbol", "$")
        native_currency = context.get("native_currency", "USD")
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        trace_ctx = context.get("trace_ctx")

        pair = signal.get("pair", "BTC-USD")
        signal_type = signal.get("signal_type", "neutral")
        confidence = signal.get("confidence", 0)
        price = signal.get("current_price", 0)

        # Quick filter: if signal is too weak, don't bother the LLM
        if confidence < self.min_confidence and signal_type == "neutral":
            self.logger.debug(f"Signal too weak ({confidence:.2f}), holding")
            return {
                "action": "hold",
                "reason": f"Signal confidence {confidence:.2f} below threshold {self.min_confidence}",
            }

        # Build context for LLM
        user_message = self._build_strategy_prompt(
            signal, active_tasks, balance, positions, recent_trades,
            recent_outcomes=recent_outcomes,
            strategic_context=strategic_context,
            live_holdings_summary=live_holdings_summary,
            currency_symbol=currency_symbol,
            native_currency=native_currency,
        )

        # Create a tracing span for this LLM call
        span = None
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                self.name,
                input_data={"system": STRATEGY_SYSTEM_PROMPT[:500], "user": user_message[:500]},
                model=self.llm.model,
            )

        llm_response = await self.llm.chat_json(
            system_prompt=STRATEGY_SYSTEM_PROMPT,
            user_message=user_message,
            span=span,
            agent_name=self.name,
        )

        if "error" in llm_response:
            self.logger.warning(f"Strategy generation failed: {llm_response}")
            return {"action": "hold", "error": llm_response["error"]}

        # Persist reasoning trace
        if stats_db and cycle_id:
            try:
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair=pair,
                    agent_name="strategist",
                    reasoning_json=llm_response,
                    signal_type=signal_type,
                    confidence=float(llm_response.get("confidence", 0)),
                    langfuse_trace_id=span.trace_id if span else None,
                    langfuse_span_id=span.span_id if span else None,
                    prompt_tokens=span.prompt_tokens if span else 0,
                    completion_tokens=span.completion_tokens if span else 0,
                    latency_ms=span.latency_ms if span else 0.0,
                    raw_prompt=user_message[:1000],
                )
            except Exception as e:
                self.logger.debug(f"Failed to save reasoning trace: {e}")

        action = llm_response.get("action", "hold")
        self.logger.info(
            f"📋 Strategy: {action.upper()} {pair} | "
            f"Confidence: {llm_response.get('confidence', 0):.0%} | "
            f"Reason: {llm_response.get('reasoning', 'N/A')[:100]}"
        )

        return llm_response

    def _build_strategy_prompt(
        self,
        signal: dict,
        tasks: list,
        balance: dict,
        positions: dict,
        recent_trades: list,
        recent_outcomes: str = "",
        strategic_context: str = "",
        live_holdings_summary: str = "",
        currency_symbol: str = "$",
        native_currency: str = "USD",
    ) -> str:
        """Build the strategy generation prompt."""
        pair = signal.get("pair", "?")
        price = signal.get("current_price", 0)
        sym = currency_symbol

        tasks_text = "No active tasks."
        if tasks:
            tasks_text = "\n".join(
                f"- {t.get('description', t)}" for t in tasks
            )

        positions_text = "No open positions."
        if positions:
            positions_text = "\n".join(
                f"- {p}: {q:.6f}" for p, q in positions.items() if q > 0
            )

        recent_text = "No recent trades."
        if recent_trades:
            recent_text = "\n".join(
                f"- {t}" for t in recent_trades[-5:]
            )

        outcomes_section = (
            f"\nRECENT TRADE OUTCOMES FOR {pair} (last closed trades with reasoning):\n{recent_outcomes}\n"
            if recent_outcomes else ""
        )
        strategy_section = (
            f"\nSTRATEGIC CONTEXT (daily/weekly/monthly planning layer):\n{strategic_context}\n"
            if strategic_context else ""
        )

        # Cash display: use live balances if available, fallback to legacy
        cash_display = f"{sym}{balance.get(native_currency, balance.get('USD', 0)):,.2f} {native_currency}"

        # Live holdings section
        holdings_section = ""
        if live_holdings_summary:
            holdings_section = f"\n{live_holdings_summary}\n"

        return f"""MARKET SIGNAL for {pair}:
- Type: {signal.get('signal_type', 'neutral')}
- Confidence: {signal.get('confidence', 0):.0%}
- Current Price: {sym}{price:,.2f}
- Market Condition: {signal.get('market_condition', 'unknown')}
- Reasoning: {signal.get('reasoning', 'N/A')}
- Suggested Entry: {signal.get('suggested_entry', 'N/A')}
- Suggested Stop-Loss: {signal.get('suggested_stop_loss', 'N/A')}
- Suggested Take-Profit: {signal.get('suggested_take_profit', 'N/A')}

PORTFOLIO:
- Cash: {cash_display}
- Bot-Tracked Positions:
{positions_text}
{holdings_section}
ACTIVE USER TASKS:
{tasks_text}

RECENT TRADES:
{recent_text}
{outcomes_section}{strategy_section}
What action should we take? Respond with JSON."""
