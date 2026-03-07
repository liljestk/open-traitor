"""
Strategist Agent — Generates trading strategies and specific trade proposals
based on market analysis signals and active tasks.
"""

from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.models.trade import TradeAction
from src.utils.logger import get_logger
from src.utils import llm_optimizer

logger = get_logger("agent.strategist")


STRATEGY_SYSTEM_PROMPT = """You are a trading strategist. Decide buy/sell/hold from the market signal and user tasks.

Consider: signal confidence/type; portfolio positions; task spending limits; risk params; recent trade history (avoid overtrading); past outcomes (adapt to repeated losses); strategic context = regime background; live holdings (may propose sells).
{asset_class_notes}

Respond with JSON:
{{
    "action": "buy"|"sell"|"hold",
    "pair": "<exact pair from MARKET SIGNAL>",
    "confidence": 0.0-1.0,
    "quote_amount": amount_or_null,
    "quantity": quantity_or_null,
    "stop_loss_price": price_or_null,
    "take_profit_price": price_or_null,
    "reasoning": "concise reason",
    "task_alignment": "relation to active tasks"
}}

Rules:
- Be decisive; lean towards action when confidence exceeds threshold.
- Never exceed task spending limits.
- Always set stop-loss for buys.
- Don't add to a position already held unless conviction is high.
- For existing-holding sells, use quantity from ACTUAL HOLDINGS.
- CRITICAL: Only trade if expected move CLEARLY exceeds breakeven in FEE CONTEXT — otherwise hold.

Respond ONLY with valid JSON."""

_CRYPTO_STRATEGY_NOTES = "Don't hold out of fear — take small entries on decent setups."
_EQUITY_STRATEGY_NOTES = "Fractional shares supported (IBKR) — precise quantities. Consider earnings/ex-div dates, macro, sector concentration."


def _get_strategy_prompt(exchange: str) -> str:
    """Return the appropriate system prompt based on exchange/asset class."""
    notes = _EQUITY_STRATEGY_NOTES if exchange == "ibkr" else _CRYPTO_STRATEGY_NOTES
    return STRATEGY_SYSTEM_PROMPT.format(asset_class_notes=notes)


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
        portfolio_value = context.get("portfolio_value", 0)
        cash_balance = context.get("cash_balance", 0)
        sentiment_data = context.get("sentiment", {})
        strategy_signals = context.get("strategy_signals", {})
        fee_context = context.get("fee_context", {})
        prediction_accuracy = context.get("prediction_accuracy")
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        trace_ctx = context.get("trace_ctx")
        exchange = context.get("exchange", "coinbase")

        pair = signal.get("pair", "BTC-USD")
        signal_type = signal.get("signal_type", "neutral")
        confidence = signal.get("confidence", 0)
        price = signal.get("current_price", 0)

        # Per-pair confidence adjustment from planning context
        # Positive = harder to trade (avoid pair), negative = easier (focus pair)
        confidence_adj = context.get("confidence_adjustment", 0.0)
        effective_min_confidence = max(0.1, min(0.98, self.min_confidence + confidence_adj))

        # Quick filter: skip LLM if signal is weak/non-actionable and below threshold.
        # The skip set is hot-reloadable from the optimizer (30s cache).
        _NON_ACTIONABLE = set(llm_optimizer.get("strategist_skip_signals", ["neutral", "weak_buy", "weak_sell"]))
        if confidence < effective_min_confidence and signal_type in _NON_ACTIONABLE:
            self.logger.debug(
                f"Signal too weak ({signal_type}/{confidence:.2f}), skipping LLM "
                f"(threshold={effective_min_confidence:.2f}, adj={confidence_adj:+.2f})"
            )
            return {
                "action": "hold",
                "reason": (
                    f"Signal {signal_type} confidence {confidence:.2f} below threshold "
                    f"{effective_min_confidence:.2f}"
                    + (f" (plan adj: {confidence_adj:+.2f})" if confidence_adj else "")
                ),
            }

        # Build context for LLM
        user_message = self._build_strategy_prompt(
            signal, active_tasks, balance, positions, recent_trades,
            recent_outcomes=recent_outcomes,
            strategic_context=strategic_context,
            live_holdings_summary=live_holdings_summary,
            currency_symbol=currency_symbol,
            native_currency=native_currency,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
            sentiment_data=sentiment_data,
            strategy_signals=strategy_signals,
            fee_context=fee_context,
            prediction_accuracy=prediction_accuracy,
        )

        # Create a tracing span for this LLM call
        span = None
        system_prompt = _get_strategy_prompt(exchange)
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                self.name,
                input_data={"system": system_prompt[:500], "user": user_message[:500]},
                model=self.llm.model,
            )

        llm_response = await self.llm.chat_json(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=600,
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
                    exchange=exchange,
                )
            except Exception as e:
                self.logger.debug(f"Failed to save reasoning trace: {e}")

        # Guard: force pair to match the pipeline's analyzed pair.
        # The LLM sometimes hallucinate a different pair from the portfolio.
        proposed_pair = llm_response.get("pair", "")
        if proposed_pair and proposed_pair != pair:
            self.logger.warning(
                f"⚠️ Strategist proposed {proposed_pair} but pipeline is analyzing {pair} — "
                f"overriding to {pair}"
            )
            llm_response["pair"] = pair

        # H5: Clamp confidence to [0.0, 1.0] to prevent LLM hallucinated values
        raw_conf = llm_response.get("confidence", 0)
        try:
            llm_response["confidence"] = max(0.0, min(1.0, float(raw_conf)))
        except (ValueError, TypeError):
            llm_response["confidence"] = 0.0

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
        portfolio_value: float = 0,
        cash_balance: float = 0,
        sentiment_data: dict | None = None,
        strategy_signals: dict | None = None,
        fee_context: dict | None = None,
        prediction_accuracy: dict | None = None,
    ) -> str:
        """Build the strategy generation prompt."""
        pair = signal.get("pair", "?")
        price = signal.get("current_price", 0)
        sym = currency_symbol

        tasks_text = "No active tasks."
        if tasks:
            tasks_text = "\n".join(
                f"- {t.get('description', t) if isinstance(t, dict) else t}" for t in tasks  # M33 fix
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

        # Cap strategic context per optimizer setting (30s hot-reload cache)
        ctx_max = llm_optimizer.get("strategic_context_max_chars", 800)
        if strategic_context and len(strategic_context) > ctx_max:
            strategic_context = strategic_context[:ctx_max].rstrip() + " [...]"
        strategy_section = (
            f"\nSTRATEGIC CONTEXT (daily/weekly/monthly planning layer):\n{strategic_context}\n"
            if strategic_context else ""
        )

        # Cash display: use live balances if available, fallback to legacy
        cash_display = f"{sym}{balance.get(native_currency, balance.get('USD', 0)):,.2f} {native_currency}"

        # Portfolio value display
        pv = portfolio_value or 0
        pv_display = f"{sym}{pv:,.2f} {native_currency}"
        # Account size bracket for the LLM
        if pv < 50:
            acct_bracket = "MICRO (< €50) — trade smallest viable amounts, even €0.50 is fine"
        elif pv < 500:
            acct_bracket = "SMALL (€50–€500) — keep trades to 5–15% of portfolio"
        elif pv < 5000:
            acct_bracket = "MEDIUM (€500–€5K) — standard 2–10% position sizing"
        else:
            acct_bracket = "LARGE (> €5K) — conservative 2–5% per position"

        # Live holdings section
        holdings_section = ""
        if live_holdings_summary:
            holdings_section = f"\n{live_holdings_summary}\n"

        accuracy_section = self._format_prediction_accuracy(prediction_accuracy)

        return f"""MARKET SIGNAL for {pair}:
- Type: {signal.get('signal_type', 'neutral')}
- Confidence: {signal.get('confidence', 0):.0%}
- Current Price: {sym}{price:,.2f}
- Market Condition: {signal.get('market_condition', 'unknown')}
- Reasoning: {(signal.get('reasoning', 'N/A') or 'N/A')[:150]}
- Suggested Entry: {signal.get('suggested_entry', 'N/A')}
- Suggested Stop-Loss: {signal.get('suggested_stop_loss', 'N/A')}
- Suggested Take-Profit: {signal.get('suggested_take_profit', 'N/A')}
{accuracy_section}
PORTFOLIO:
- Total Portfolio Value: {pv_display}
- Available Cash: {cash_display}
- Account Size: {acct_bracket}
- Bot-Tracked Positions:
{positions_text}
{holdings_section}
ACTIVE USER TASKS:
{tasks_text}

RECENT TRADES:
{recent_text}
{outcomes_section}{strategy_section}
{self._format_fee_context(fee_context)}
{self._format_sentiment_strategy(sentiment_data, strategy_signals)}
What action should we take? Respond with JSON."""

    @staticmethod
    def _format_prediction_accuracy(accuracy: dict | None) -> str:
        """Format historical prediction accuracy as a prompt section."""
        if not accuracy:
            return ""
        acc_24h = accuracy.get("accuracy_24h_pct")
        if acc_24h is None:
            return ""
        acc_1h = accuracy.get("accuracy_1h_pct")
        trend = accuracy.get("trend", "unknown")
        evaluated = accuracy.get("evaluated_24h", 0)
        recent_acc = accuracy.get("recent_accuracy_24h_pct")
        older_acc = accuracy.get("older_accuracy_24h_pct")
        pair = accuracy.get("pair", "this pair")

        trend_label = {
            "improving": "improving ↑",
            "degrading": "degrading ↓",
            "stable": "stable →",
            "insufficient_data": "insufficient data",
        }.get(trend, trend)

        lines = [
            f"ANALYST TRACK RECORD for {pair} (last 30 days, {evaluated} evaluated predictions):",
            f"- 24h direction accuracy: {acc_24h:.0f}%",
        ]
        if acc_1h is not None:
            lines.append(f"- 1h direction accuracy: {acc_1h:.0f}%")
        if recent_acc is not None and older_acc is not None:
            lines.append(
                f"- Accuracy trend: {trend_label} (last 7d: {recent_acc:.0f}% vs prior: {older_acc:.0f}%)"
            )
        elif trend != "insufficient_data":
            lines.append(f"- Accuracy trend: {trend_label}")

        if acc_24h < 45:
            lines.append(
                "- NOTE: Below-chance accuracy — treat this signal with extra skepticism, lean toward hold unless conviction is very high"
            )
        elif acc_24h > 65:
            lines.append("- NOTE: Strong track record — this signal can be weighted more heavily")

        return "\n".join(lines)

    @staticmethod
    def _format_fee_context(fee_context: dict | None) -> str:
        """Format fee context for the prompt."""
        if not fee_context:
            return ""
        rt = fee_context.get("round_trip_fee_pct", 0)
        be = fee_context.get("breakeven_pct", 0)
        mg = fee_context.get("min_gain_pct", 0)
        return (
            f"FEE CONTEXT (IMPORTANT — trades that ignore fees LOSE money):\n"
            f"- Round-trip fee (buy+sell): {rt*100:.2f}%\n"
            f"- Breakeven price move needed: {be*100:.2f}%\n"
            f"- Minimum expected gain to trade: {mg*100:.2f}%\n"
            f"- Do NOT propose a trade unless you expect the price to move MORE than {be*100:.1f}%\n"
        )

    @staticmethod
    def _format_sentiment_strategy(
        sentiment_data: dict | None,
        strategy_signals: dict | None,
    ) -> str:
        """Format sentiment + deterministic strategy signals for the prompt."""
        parts: list[str] = []

        if sentiment_data and isinstance(sentiment_data, dict):
            label = sentiment_data.get("sentiment_label", sentiment_data.get("label", ""))
            score = sentiment_data.get("sentiment_score", sentiment_data.get("score", 0))
            n = sentiment_data.get("total_articles", sentiment_data.get("count", 0))
            if n:
                parts.append(
                    f"SENTIMENT ANALYSIS:\n"
                    f"- Label: {label}  Score: {score:.2f}  Articles: {n}"
                )

        if strategy_signals:
            lines = ["DETERMINISTIC STRATEGY SIGNALS (rule-based, no LLM):"]
            for name, sig in strategy_signals.items():
                if isinstance(sig, dict):
                    lines.append(
                        f"- {name}: {sig.get('action', 'hold').upper()} "
                        f"(confidence={sig.get('confidence', 0):.0%}, "
                        f"regime={sig.get('market_regime', '?')}) — "
                        f"{sig.get('reasoning', 'N/A')[:120]}"
                    )
                else:
                    lines.append(f"- {name}: {sig}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)
