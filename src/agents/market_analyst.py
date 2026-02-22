"""
Market Analyst Agent — Analyzes market data using technical indicators
and news sentiment, then produces a comprehensive market assessment.
"""

from __future__ import annotations

import json
from typing import Any

from src.agents.base_agent import BaseAgent
from src.analysis.technical import TechnicalAnalyzer
from src.models.signal import (
    MarketCondition,
    Signal,
    SignalType,
    TechnicalSignals,
    SentimentSignals,
)
from src.utils.logger import get_logger

logger = get_logger("agent.market_analyst")


MARKET_ANALYSIS_SYSTEM_PROMPT = """You are an expert cryptocurrency market analyst.
Your job is to analyze technical indicators and news sentiment to produce a clear market assessment.

Given the technical indicators and recent crypto news, provide your analysis as JSON:

{
    "signal_type": "strong_buy" | "buy" | "weak_buy" | "neutral" | "weak_sell" | "sell" | "strong_sell",
    "confidence": 0.0-1.0,
    "market_condition": "strongly_bullish" | "bullish" | "slightly_bullish" | "neutral" | "slightly_bearish" | "bearish" | "strongly_bearish" | "volatile",
    "sentiment_overall": "bullish" | "bearish" | "neutral",
    "sentiment_score": -1.0 to 1.0,
    "key_factors": ["factor1", "factor2", ...],
    "reasoning": "Detailed explanation of your analysis",
    "suggested_entry": price or null,
    "suggested_stop_loss": price or null,
    "suggested_take_profit": price or null
}

Be conservative. When uncertain, lean towards "neutral".
Never recommend a confidence above 0.85 unless the signal is extremely strong across ALL indicators.
Always consider risk-reward ratios. Capital preservation is priority #1.
If a strategic context (daily/weekly/monthly plan) is provided, treat it as background regime information —
use it to calibrate your confidence but do not override clear technical evidence."""


class MarketAnalystAgent(BaseAgent):
    """Analyzes market data and produces trading signals."""

    def __init__(self, llm, state, config):
        super().__init__("market_analyst", llm, state, config)
        self.technical = TechnicalAnalyzer(config.get("analysis", {}).get("technical", {}))

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Analyze market data for a given trading pair.

        Context expected:
            - pair: str (e.g., "BTC-USD")
            - candles: list[dict] (OHLCV data)
            - news_headlines: str (recent news)
            - fear_greed: str (optional, fear & greed summary)
            - multi_timeframe: str (optional, multi-TF confluence summary)
            - strategic_context: str (optional, daily/weekly/monthly plan text)
            - currency_symbol: str (optional, e.g. "€")
            - cycle_id: str (optional, used to correlate reasoning with trade outcomes)
            - stats_db: StatsDB instance (optional, for persisting reasoning)
        """
        pair = context.get("pair", "BTC-USD")
        candles = context.get("candles", [])
        news_headlines = context.get("news_headlines", "No news available.")
        fear_greed = context.get("fear_greed", "")
        multi_timeframe = context.get("multi_timeframe", "")
        sentiment = context.get("sentiment", "")
        strategy_signals = context.get("strategy_signals", {})
        strategic_context = context.get("strategic_context", "")
        currency_symbol = context.get("currency_symbol", "$")
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        trace_ctx = context.get("trace_ctx")

        # Step 1: Technical analysis (no LLM needed)
        tech_analysis = self.technical.analyze(candles)
        if "error" in tech_analysis:
            self.logger.warning(f"Technical analysis failed: {tech_analysis['error']}")
            return {"error": tech_analysis["error"]}

        current_price = tech_analysis["current_price"]
        indicators = tech_analysis["indicators"]
        price_changes = tech_analysis["price_changes"]

        # Step 2: Send to LLM for combined analysis
        user_message = self._build_analysis_prompt(
            pair, current_price, indicators, price_changes, news_headlines,
            fear_greed=fear_greed,
            multi_timeframe=multi_timeframe,
            sentiment=sentiment,
            strategy_signals=strategy_signals,
            strategic_context=strategic_context,
            currency_symbol=currency_symbol,
        )

        # Create a tracing span for this LLM call
        span = None
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                self.name,
                input_data={"system": MARKET_ANALYSIS_SYSTEM_PROMPT[:500], "user": user_message[:500]},
                model=self.llm.model,
            )

        llm_response = await self.llm.chat_json(
            system_prompt=MARKET_ANALYSIS_SYSTEM_PROMPT,
            user_message=user_message,
            span=span,
            agent_name=self.name,
        )

        if "error" in llm_response:
            self.logger.warning(f"LLM analysis failed: {llm_response['error']}")
            # Fall back to pure technical analysis
            return self._technical_only_signal(pair, current_price, indicators)

        # Step 3: Persist reasoning trace
        if stats_db and cycle_id:
            try:
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair=pair,
                    agent_name="market_analyst",
                    reasoning_json=llm_response,
                    signal_type=llm_response.get("signal_type", ""),
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

        # Step 4: Build signal
        signal = self._build_signal(pair, current_price, indicators, llm_response)
        self.state.add_signal(signal)

        self.logger.info(f"📊 {signal.to_summary()}")

        return {
            "signal": signal.model_dump(mode="json"),
            "technical": tech_analysis,
            "llm_analysis": llm_response,
        }

    def _build_analysis_prompt(
        self,
        pair: str,
        price: float,
        indicators: dict,
        price_changes: dict,
        news: str,
        fear_greed: str = "",
        multi_timeframe: str = "",
        sentiment: str = "",
        strategy_signals: dict | None = None,
        strategic_context: str = "",
        currency_symbol: str = "$",
    ) -> str:
        """Build the analysis prompt for the LLM."""
        sym = currency_symbol
        fg_section = f"\nFEAR & GREED INDEX:\n{fear_greed}\n" if fear_greed else ""
        mtf_section = f"\nMULTI-TIMEFRAME CONFLUENCE:\n{multi_timeframe}\n" if multi_timeframe else ""
        sentiment_section = f"\nSENTIMENT ANALYSIS:\n{sentiment}\n" if sentiment else ""

        # Format deterministic strategy signals
        strat_section = ""
        if strategy_signals:
            lines = []
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
            strat_section = "\nDETERMINISTIC STRATEGY SIGNALS (rule‑based, no LLM):\n" + "\n".join(lines) + "\n"

        strategy_section = (
            f"\nSTRATEGIC CONTEXT (from planning layer — use as background regime info):\n{strategic_context}\n"
            if strategic_context else ""
        )

        # M32 fix: safe formatting for potentially missing numeric indicators
        _rsi = indicators.get('rsi')
        _rsi_str = f"{_rsi:.1f}" if isinstance(_rsi, (int, float)) else "N/A"
        _macd_hist = indicators.get('macd_histogram')
        _macd_hist_str = f"{_macd_hist:.4f}" if isinstance(_macd_hist, (int, float)) else "N/A"

        return f"""Analyze {pair} at current price {sym}{price:,.2f}

TECHNICAL INDICATORS:
- RSI: {_rsi_str} ({indicators.get('rsi_signal', 'unknown')})
- MACD: {indicators.get('macd_signal', 'unknown')} (hist: {_macd_hist_str})
- Bollinger Bands: {indicators.get('bb_signal', 'unknown')} (upper: {sym}{indicators.get('bb_upper', 0):,.2f}, lower: {sym}{indicators.get('bb_lower', 0):,.2f})
- EMA Signal: {indicators.get('ema_signal', 'unknown')}
- EMA 9: {sym}{indicators.get('ema_9', 0):,.2f} | EMA 21: {sym}{indicators.get('ema_21', 0):,.2f} | EMA 50: {sym}{indicators.get('ema_50', 0):,.2f}
- Volume: {indicators.get('volume_signal', 'unknown')} (ratio: {indicators.get('volume_ratio', 1):.2f}x average)
- Support: {sym}{indicators.get('support', 0):,.2f} | Resistance: {sym}{indicators.get('resistance', 0):,.2f}
- ATR: {sym}{indicators.get('atr', 0):,.2f}

PRICE CHANGES:
- 1 hour: {price_changes.get('1h', 0):+.2%}
- 24 hours: {price_changes.get('24h', 0):+.2%}
{fg_section}{mtf_section}{sentiment_section}{strat_section}
RECENT CRYPTO NEWS:
{news}
{strategy_section}
Provide your analysis as JSON."""

    def _build_signal(
        self,
        pair: str,
        price: float,
        indicators: dict,
        llm_analysis: dict,
    ) -> Signal:
        """Build a Signal object from LLM response."""
        try:
            signal_type = SignalType(llm_analysis.get("signal_type", "neutral"))
        except ValueError:
            signal_type = SignalType.NEUTRAL

        try:
            market_condition = MarketCondition(llm_analysis.get("market_condition", "unknown"))
        except ValueError:
            market_condition = MarketCondition.UNKNOWN

        return Signal(
            pair=pair,
            current_price=price,
            signal_type=signal_type,
            confidence=float(llm_analysis.get("confidence", 0)),
            market_condition=market_condition,
            technical=TechnicalSignals(
                rsi=indicators.get("rsi"),
                rsi_signal=indicators.get("rsi_signal"),
                macd=indicators.get("macd"),
                macd_signal_line=indicators.get("macd_signal_line"),
                macd_histogram=indicators.get("macd_histogram"),
                macd_signal=indicators.get("macd_signal"),
                bb_upper=indicators.get("bb_upper"),
                bb_middle=indicators.get("bb_middle"),
                bb_lower=indicators.get("bb_lower"),
                bb_signal=indicators.get("bb_signal"),
                ema_9=indicators.get("ema_9"),
                ema_21=indicators.get("ema_21"),
                ema_50=indicators.get("ema_50"),
                ema_200=indicators.get("ema_200"),
                ema_signal=indicators.get("ema_signal"),
                volume_trend=indicators.get("volume_signal"),
                support_level=indicators.get("support"),
                resistance_level=indicators.get("resistance"),
                atr=indicators.get("atr"),
            ),
            sentiment=SentimentSignals(
                overall_sentiment=llm_analysis.get("sentiment_overall"),
                sentiment_score=float(llm_analysis.get("sentiment_score", 0)),
                key_factors=llm_analysis.get("key_factors", []),
            ),
            suggested_entry=llm_analysis.get("suggested_entry"),
            suggested_stop_loss=llm_analysis.get("suggested_stop_loss"),
            suggested_take_profit=llm_analysis.get("suggested_take_profit"),
            reasoning=llm_analysis.get("reasoning", ""),
        )

    def _technical_only_signal(
        self, pair: str, price: float, indicators: dict
    ) -> dict:
        """Fallback signal based on technicals only (no LLM)."""
        # Simple scoring system
        score = 0
        rsi_signal = indicators.get("rsi_signal", "neutral")
        if rsi_signal == "oversold":
            score += 2
        elif rsi_signal == "bearish":
            score -= 1
        elif rsi_signal == "overbought":
            score -= 2
        elif rsi_signal == "bullish":
            score += 1

        macd_signal = indicators.get("macd_signal", "neutral")
        if "bullish" in macd_signal:
            score += 1
        elif "bearish" in macd_signal:
            score -= 1

        if score >= 2:
            signal_type = SignalType.BUY
        elif score <= -2:
            signal_type = SignalType.SELL
        else:
            signal_type = SignalType.NEUTRAL

        signal = Signal(
            pair=pair,
            current_price=price,
            signal_type=signal_type,
            confidence=min(abs(score) * 0.2, 0.6),  # Low confidence for fallback
            reasoning="Technical-only analysis (LLM unavailable)",
        )
        self.state.add_signal(signal)
        return {"signal": signal.model_dump(mode="json"), "fallback": True}
