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


MARKET_ANALYSIS_SYSTEM_PROMPT_CRYPTO = """You are an expert cryptocurrency market analyst.
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

Be objective. Follow the technical indicators closely.
Don't artificially lower confidence if the indicators point clearly in one direction.
Balance risk with opportunity. While capital preservation is important, your goal is to find actionable trades, including short-term entries in choppy markets.
If a strategic context (daily/weekly/monthly plan) is provided, treat it as background regime information —
use it to calibrate your confidence but do not override clear technical evidence.

ACCOUNT-SIZE AWARENESS:
- When account context is provided, calibrate stop-loss and take-profit levels to be realistic for the account size.
- For micro/small accounts, tighter stops (closer to entry) reduce the capital at risk per trade.
- Suggest entry amounts that make sense relative to the portfolio (don't suggest €100 entries on a €7 account)."""

MARKET_ANALYSIS_SYSTEM_PROMPT_EQUITY = """You are an expert equities market analyst specializing in US and European stocks.
Your job is to analyze technical indicators, market data, and news to produce a clear assessment for individual stocks or ETFs.

Given the technical indicators and recent market news, provide your analysis as JSON:

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

Be objective. Follow the technical indicators closely.
Consider earnings, macro conditions, sector rotation, and institutional flows.
Balance risk with opportunity — equities are less volatile than crypto, so calibrate stop distances accordingly.
If a strategic context is provided, use it as regime background but do not override technical evidence.

ACCOUNT-SIZE AWARENESS:
- Calibrate position sizes and stop distances to the account size.
- For smaller accounts, consider fractional shares and minimum lot sizes.
- Suggest realistic entry amounts relative to the portfolio."""


def _get_system_prompt(exchange: str) -> str:
    """Return the appropriate system prompt based on exchange/asset class."""
    if exchange in ("ibkr", "nordnet"):
        return MARKET_ANALYSIS_SYSTEM_PROMPT_EQUITY
    return MARKET_ANALYSIS_SYSTEM_PROMPT_CRYPTO


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
        native_currency = context.get("native_currency", "USD")
        portfolio_value = context.get("portfolio_value", 0)
        cash_balance = context.get("cash_balance", 0)
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        trace_ctx = context.get("trace_ctx")
        exchange = context.get("exchange", "coinbase")

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
            native_currency=native_currency,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
            exchange=exchange,
        )

        # Create a tracing span for this LLM call
        span = None
        system_prompt = _get_system_prompt(exchange)
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                self.name,
                input_data={"system": system_prompt[:500], "user": user_message[:500]},
                model=self.llm.model,
            )

        llm_response = await self.llm.chat_json(
            system_prompt=system_prompt,
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
                    exchange=exchange,
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
        native_currency: str = "USD",
        portfolio_value: float = 0,
        cash_balance: float = 0,
        exchange: str = "coinbase",
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

        # Account context — lightweight, helps calibrate entry/SL/TP
        acct_section = ""
        pv = portfolio_value or 0
        if pv > 0:
            if pv < 50:
                bracket = "MICRO (< €50)"
            elif pv < 500:
                bracket = "SMALL (€50–€500)"
            elif pv < 5000:
                bracket = "MEDIUM (€500–€5K)"
            else:
                bracket = "LARGE (> €5K)"
            acct_section = (
                f"\nACCOUNT CONTEXT:\n"
                f"- Portfolio Value: {sym}{pv:,.2f} {native_currency}\n"
                f"- Available Cash: {sym}{cash_balance:,.2f} {native_currency}\n"
                f"- Account Bracket: {bracket}\n"
                f"- Calibrate entry sizes, stop-loss, and take-profit to this account size.\n"
            )

        # M32 fix: safe formatting for potentially missing numeric indicators
        _rsi = indicators.get('rsi')
        _rsi_str = f"{_rsi:.1f}" if isinstance(_rsi, (int, float)) else "N/A"
        _macd_hist = indicators.get('macd_histogram')
        _macd_hist_str = f"{_macd_hist:.4f}" if isinstance(_macd_hist, (int, float)) else "N/A"

        # None-safe formatting for BB, EMA, support/resistance, ATR
        _bb_upper = indicators.get('bb_upper')
        _bb_upper_str = f"{sym}{_bb_upper:,.2f}" if isinstance(_bb_upper, (int, float)) else "N/A"
        _bb_lower = indicators.get('bb_lower')
        _bb_lower_str = f"{sym}{_bb_lower:,.2f}" if isinstance(_bb_lower, (int, float)) else "N/A"
        _ema_9 = indicators.get('ema_9')
        _ema_9_str = f"{sym}{_ema_9:,.2f}" if isinstance(_ema_9, (int, float)) else "N/A"
        _ema_21 = indicators.get('ema_21')
        _ema_21_str = f"{sym}{_ema_21:,.2f}" if isinstance(_ema_21, (int, float)) else "N/A"
        _ema_50 = indicators.get('ema_50')
        _ema_50_str = f"{sym}{_ema_50:,.2f}" if isinstance(_ema_50, (int, float)) else "N/A"
        _support = indicators.get('support')
        _support_str = f"{sym}{_support:,.2f}" if isinstance(_support, (int, float)) else "N/A"
        _resistance = indicators.get('resistance')
        _resistance_str = f"{sym}{_resistance:,.2f}" if isinstance(_resistance, (int, float)) else "N/A"
        _atr = indicators.get('atr')
        _atr_str = f"{sym}{_atr:,.2f}" if isinstance(_atr, (int, float)) else "N/A"
        _vol_ratio = indicators.get('volume_ratio')
        _vol_ratio_str = f"{_vol_ratio:.2f}" if isinstance(_vol_ratio, (int, float)) else "N/A"

        _1h = price_changes.get('1h')
        _1h_str = f"{_1h:+.2%}" if isinstance(_1h, (int, float)) else "N/A"
        _24h = price_changes.get('24h')
        _24h_str = f"{_24h:+.2%}" if isinstance(_24h, (int, float)) else "N/A"

        return f"""Analyze {pair} at current price {sym}{price:,.2f}

TECHNICAL INDICATORS:
- RSI: {_rsi_str} ({indicators.get('rsi_signal', 'unknown')})
- MACD: {indicators.get('macd_signal', 'unknown')} (hist: {_macd_hist_str})
- Bollinger Bands: {indicators.get('bb_signal', 'unknown')} (upper: {_bb_upper_str}, lower: {_bb_lower_str})
- EMA Signal: {indicators.get('ema_signal', 'unknown')}
- EMA 9: {_ema_9_str} | EMA 21: {_ema_21_str} | EMA 50: {_ema_50_str}
- Volume: {indicators.get('volume_signal', 'unknown')} (ratio: {_vol_ratio_str}x average)
- Support: {_support_str} | Resistance: {_resistance_str}
- ATR: {_atr_str}

PRICE CHANGES:
- 1 hour: {_1h_str}
- 24 hours: {_24h_str}
{fg_section}{mtf_section}{sentiment_section}{strat_section}
RECENT {"EQUITY" if exchange in ("ibkr", "nordnet") else "CRYPTO"} NEWS:
{news}
{strategy_section}{acct_section}
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
        """Fallback signal based on technicals only (no LLM).
        
        Uses all available indicators for a comprehensive score:
        RSI, MACD, Bollinger Bands, EMA, Volume, ADX.
        """
        score = 0
        factors = []

        # RSI signal (weight: 2)
        rsi_signal = indicators.get("rsi_signal", "neutral")
        if rsi_signal == "oversold":
            score += 2
            factors.append("RSI oversold")
        elif rsi_signal == "overbought":
            score -= 2
            factors.append("RSI overbought")
        elif rsi_signal == "bullish":
            score += 1
            factors.append("RSI bullish")
        elif rsi_signal == "bearish":
            score -= 1
            factors.append("RSI bearish")

        # MACD signal (weight: 1)
        macd_signal = indicators.get("macd_signal", "neutral")
        if "bullish" in macd_signal:
            score += 1
            factors.append("MACD bullish")
        elif "bearish" in macd_signal:
            score -= 1
            factors.append("MACD bearish")

        # Bollinger Bands (weight: 1)
        bb_signal = indicators.get("bb_signal", "neutral")
        if bb_signal in ("oversold", "lower_band"):
            score += 1
            factors.append("BB lower band")
        elif bb_signal in ("overbought", "upper_band"):
            score -= 1
            factors.append("BB upper band")

        # EMA alignment (weight: 1)
        ema_signal = indicators.get("ema_signal", "neutral")
        if "bullish" in str(ema_signal):
            score += 1
            factors.append("EMA bullish")
        elif "bearish" in str(ema_signal):
            score -= 1
            factors.append("EMA bearish")

        # Volume confirmation (weight: 1)
        vol_signal = indicators.get("volume_signal", "normal")
        vol_ratio = indicators.get("volume_ratio", 1.0)
        if isinstance(vol_ratio, (int, float)) and vol_ratio > 1.5:
            # High volume confirms the direction
            if score > 0:
                score += 1
                factors.append("High volume confirms bullish")
            elif score < 0:
                score -= 1
                factors.append("High volume confirms bearish")

        # ADX trend strength (weight: modifier)
        adx = indicators.get("adx")
        if isinstance(adx, (int, float)) and adx > 25:
            factors.append(f"Strong trend (ADX={adx:.0f})")
            # Amplify directional signals in trending markets
            if abs(score) >= 2:
                score = int(score * 1.2)

        # Score → signal type mapping
        if score >= 4:
            signal_type = SignalType.STRONG_BUY
        elif score >= 2:
            signal_type = SignalType.BUY
        elif score >= 1:
            signal_type = SignalType.WEAK_BUY
        elif score <= -4:
            signal_type = SignalType.STRONG_SELL
        elif score <= -2:
            signal_type = SignalType.SELL
        elif score <= -1:
            signal_type = SignalType.WEAK_SELL
        else:
            signal_type = SignalType.NEUTRAL

        confidence = min(abs(score) * 0.15, 0.75)

        signal = Signal(
            pair=pair,
            current_price=price,
            signal_type=signal_type,
            confidence=confidence,
            reasoning=f"Technical-only fallback (LLM unavailable). Score={score}. Factors: {', '.join(factors) or 'none'}",
        )
        self.state.add_signal(signal)
        return {"signal": signal.model_dump(mode="json"), "fallback": True}
