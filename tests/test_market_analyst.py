"""Tests for MarketAnalystAgent — technical analysis, LLM fallback, signal building, prompt."""
import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _import_market_analyst():
    mod = importlib.import_module("src.agents.market_analyst")
    return mod.MarketAnalystAgent, mod._get_system_prompt, mod._CRYPTO_NOTES, mod._EQUITY_NOTES


# ── System prompt selection ──────────────────────────────────────────────

class TestSystemPrompt:
    def test_crypto_prompt(self):
        _, get_prompt, crypto_notes, _ = _import_market_analyst()
        prompt = get_prompt("coinbase")
        assert "artificially lower confidence" in prompt  # crypto note
        assert "earnings" not in prompt

    def test_equity_prompt(self):
        _, get_prompt, _, equity_notes = _import_market_analyst()
        prompt = get_prompt("ibkr")
        assert "earnings" in prompt or "sector rotation" in prompt
        assert "artificially lower" not in prompt


# ── _build_signal ────────────────────────────────────────────────────────

class TestBuildSignal:
    def setup_method(self):
        MA, _, _, _ = _import_market_analyst()
        self.agent = MA(
            llm=MagicMock(),
            state=MagicMock(),
            config={"analysis": {"technical": {}}},
        )

    def test_valid_signal(self):
        llm = {
            "signal_type": "buy",
            "confidence": 0.85,
            "market_condition": "bullish",
            "sentiment_overall": "bullish",
            "sentiment_score": 0.6,
            "key_factors": ["RSI oversold"],
            "reasoning": "Strong buy",
            "suggested_entry": 100.0,
            "suggested_stop_loss": 95.0,
            "suggested_take_profit": 110.0,
        }
        indicators = {
            "rsi": 30.0,
            "rsi_signal": "oversold",
            "macd_signal": "bullish",
        }
        signal = self.agent._build_signal("BTC-USD", 50000.0, indicators, llm)
        assert signal.pair == "BTC-USD"
        assert signal.signal_type.value == "buy"
        assert signal.confidence == 0.85
        assert signal.current_price == 50000.0
        assert signal.suggested_entry == 100.0

    def test_invalid_signal_type_defaults_neutral(self):
        llm = {"signal_type": "INVALID", "confidence": 0.5}
        signal = self.agent._build_signal("BTC-USD", 50000.0, {}, llm)
        assert signal.signal_type.value == "neutral"

    def test_confidence_clamped(self):
        llm = {"signal_type": "buy", "confidence": 1.5}
        signal = self.agent._build_signal("BTC-USD", 100.0, {}, llm)
        assert signal.confidence == 1.0

        llm["confidence"] = -0.5
        signal = self.agent._build_signal("BTC-USD", 100.0, {}, llm)
        assert signal.confidence == 0.0

    def test_invalid_market_condition(self):
        llm = {"market_condition": "VERY_BAD"}
        signal = self.agent._build_signal("BTC-USD", 100.0, {}, llm)
        assert signal.market_condition.value == "unknown"


# ── _technical_only_signal (LLM fallback) ────────────────────────────────

class TestTechnicalOnlySignal:
    def setup_method(self):
        MA, _, _, _ = _import_market_analyst()
        self.state = MagicMock()
        self.agent = MA(
            llm=MagicMock(),
            state=self.state,
            config={"analysis": {"technical": {}}},
        )

    def test_neutral_no_indicators(self):
        result = self.agent._technical_only_signal("BTC-USD", 100.0, {})
        sig = result["signal"]
        assert sig["signal_type"] == "neutral"
        assert result["fallback"] is True

    def test_strong_buy(self):
        indicators = {
            "rsi_signal": "oversold",      # +2
            "macd_signal": "bullish",       # +1
            "bb_signal": "oversold",        # +1
            "ema_signal": "bullish",        # +1
            "volume_ratio": 2.0,            # +1 (confirms bullish)
            "adx": 30,                      # amplify
        }
        result = self.agent._technical_only_signal("BTC-USD", 100.0, indicators)
        sig = result["signal"]
        assert sig["signal_type"] in ("strong_buy", "buy")

    def test_sell_signal(self):
        indicators = {
            "rsi_signal": "overbought",     # -2
            "macd_signal": "bearish",       # -1
            "bb_signal": "overbought",      # -1
            "ema_signal": "bearish",        # -1
        }
        result = self.agent._technical_only_signal("BTC-USD", 100.0, indicators)
        sig = result["signal"]
        assert sig["signal_type"] in ("sell", "strong_sell")

    def test_weak_signals(self):
        indicators = {"rsi_signal": "bullish"}  # +1 only
        result = self.agent._technical_only_signal("BTC-USD", 100.0, indicators)
        assert result["signal"]["signal_type"] == "weak_buy"

    def test_confidence_bounded(self):
        # Even with max indicators, confidence <= 0.75
        indicators = {
            "rsi_signal": "oversold",
            "macd_signal": "bullish",
            "bb_signal": "oversold",
            "ema_signal": "bullish",
            "volume_ratio": 3.0,
            "adx": 40,
        }
        result = self.agent._technical_only_signal("BTC-USD", 100.0, indicators)
        assert result["signal"]["confidence"] <= 0.75

    def test_volume_confirms_bearish(self):
        indicators = {
            "rsi_signal": "overbought",
            "macd_signal": "bearish",
            "volume_ratio": 2.0,  # high volume confirms bearish
        }
        result = self.agent._technical_only_signal("BTC-USD", 100.0, indicators)
        sig = result["signal"]
        assert "bearish" in sig["signal_type"] or sig["signal_type"] in ("sell", "strong_sell")

    def test_state_add_signal_called(self):
        self.agent._technical_only_signal("BTC-USD", 100.0, {})
        self.state.add_signal.assert_called_once()


# ── _build_analysis_prompt ───────────────────────────────────────────────

class TestBuildAnalysisPrompt:
    def setup_method(self):
        MA, _, _, _ = _import_market_analyst()
        self.agent = MA(
            llm=MagicMock(),
            state=MagicMock(),
            config={"analysis": {"technical": {}}},
        )

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_basic_prompt(self, mock_opt):
        prompt = self.agent._build_analysis_prompt(
            pair="BTC-USD",
            price=50000.0,
            indicators={"rsi": 45.0, "rsi_signal": "neutral"},
            price_changes={"1h": 0.01, "24h": -0.03},
            news="Bitcoin is up today.",
        )
        assert "BTC-USD" in prompt
        assert "$50,000.00" in prompt
        assert "45.0" in prompt
        assert "+1.00%" in prompt
        assert "Bitcoin is up today." in prompt

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_prompt_with_optional_sections(self, mock_opt):
        prompt = self.agent._build_analysis_prompt(
            pair="ETH-EUR",
            price=3000.0,
            indicators={},
            price_changes={},
            news="Ethereum news",
            fear_greed="Fear & Greed: 25 (Extreme Fear)",
            multi_timeframe="4H bullish, 1D neutral",
            sentiment="Bullish sentiment",
            strategic_context="Weekly plan: accumulate ETH",
            currency_symbol="€",
            portfolio_value=5000.0,
            cash_balance=1000.0,
            exchange="coinbase",
        )
        assert "FEAR & GREED" in prompt
        assert "MULTI-TIMEFRAME" in prompt
        assert "SENTIMENT ANALYSIS" in prompt
        assert "STRATEGIC CONTEXT" in prompt
        assert "ACCOUNT CONTEXT" in prompt
        assert "€" in prompt
        assert "CRYPTO NEWS" in prompt

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_ibkr_prompt(self, mock_opt):
        prompt = self.agent._build_analysis_prompt(
            pair="AAPL",
            price=180.0,
            indicators={},
            price_changes={},
            news="Apple earnings beat",
            exchange="ibkr",
        )
        assert "EQUITY NEWS" in prompt

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_news_truncation(self, mock_opt):
        long_news = "x" * 5000
        # Default max is 1500
        mock_opt.side_effect = lambda k, default=None: 200 if k == "news_max_chars" else default
        prompt = self.agent._build_analysis_prompt(
            pair="BTC-USD",
            price=100.0,
            indicators={},
            price_changes={},
            news=long_news,
        )
        assert "[...truncated]" in prompt

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_none_safe_indicators(self, mock_opt):
        # Indicators with None values should produce N/A
        indicators = {"rsi": None, "bb_upper": None, "atr": None, "volume_ratio": None}
        prompt = self.agent._build_analysis_prompt(
            pair="BTC-USD", price=100.0, indicators=indicators,
            price_changes={"1h": None, "24h": None}, news="test",
        )
        assert "N/A" in prompt

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_account_brackets(self, mock_opt):
        for pv, bracket in [(30, "MICRO"), (200, "SMALL"), (2000, "MEDIUM"), (10000, "LARGE")]:
            prompt = self.agent._build_analysis_prompt(
                pair="BTC-USD", price=100.0, indicators={}, price_changes={},
                news="n", portfolio_value=pv,
            )
            assert bracket in prompt

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_strategy_signals_section(self, mock_opt):
        strat = {
            "mean_reversion": {
                "action": "buy",
                "confidence": 0.75,
                "market_regime": "ranging",
                "reasoning": "Price below mean",
            }
        }
        prompt = self.agent._build_analysis_prompt(
            pair="BTC-USD", price=100.0, indicators={}, price_changes={},
            news="n", strategy_signals=strat,
        )
        assert "DETERMINISTIC STRATEGY SIGNALS" in prompt
        assert "mean_reversion" in prompt


# ── run() integration ────────────────────────────────────────────────────

class TestMarketAnalystRun:
    def setup_method(self):
        MA, _, _, _ = _import_market_analyst()
        self.llm = MagicMock()
        self.llm.chat_json = AsyncMock(return_value={
            "signal_type": "buy",
            "confidence": 0.8,
            "market_condition": "bullish",
            "sentiment_overall": "bullish",
            "sentiment_score": 0.5,
            "key_factors": ["Strong RSI"],
            "reasoning": "Good setup",
        })
        self.llm.model = "test-model"
        self.state = MagicMock()
        self.agent = MA(
            llm=self.llm,
            state=self.state,
            config={"analysis": {"technical": {}}},
        )

    def _run(self, context):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.agent.run(context))
        finally:
            loop.close()

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_successful_analysis(self, mock_opt):
        candles = [
            {"open": 100 + i, "high": 105 + i, "low": 95 + i, "close": 102 + i, "volume": 1000 + i * 10}
            for i in range(220)
        ]
        result = self._run({"pair": "BTC-USD", "candles": candles})
        assert "signal" in result
        assert result["signal"]["signal_type"] == "buy"
        self.state.add_signal.assert_called_once()

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_llm_error_fallback(self, mock_opt):
        self.llm.chat_json = AsyncMock(return_value={"error": "LLM unavailable"})
        candles = [
            {"open": 100 + i, "high": 105 + i, "low": 95 + i, "close": 102 + i, "volume": 1000}
            for i in range(220)
        ]
        result = self._run({"pair": "BTC-USD", "candles": candles})
        assert result.get("fallback") is True

    def test_technical_analysis_error(self):
        # Empty candles → technical analysis error
        result = self._run({"pair": "BTC-USD", "candles": []})
        assert "error" in result

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_reasoning_persistence(self, mock_opt):
        mock_db = MagicMock()
        candles = [
            {"open": 100 + i, "high": 105 + i, "low": 95 + i, "close": 102 + i, "volume": 1000}
            for i in range(220)
        ]
        result = self._run({
            "pair": "BTC-USD",
            "candles": candles,
            "cycle_id": "test-cycle",
            "stats_db": mock_db,
        })
        mock_db.save_reasoning.assert_called_once()
        call_kwargs = mock_db.save_reasoning.call_args
        assert call_kwargs[1]["cycle_id"] == "test-cycle" or call_kwargs.kwargs.get("cycle_id") == "test-cycle"

    @patch("src.utils.llm_optimizer.get", side_effect=lambda k, default=None: default)
    def test_reasoning_persistence_failure_ignored(self, mock_opt):
        mock_db = MagicMock()
        mock_db.save_reasoning.side_effect = Exception("DB error")
        candles = [
            {"open": 100 + i, "high": 105 + i, "low": 95 + i, "close": 102 + i, "volume": 1000}
            for i in range(220)
        ]
        # Should not raise
        result = self._run({
            "pair": "BTC-USD",
            "candles": candles,
            "cycle_id": "test-cycle",
            "stats_db": mock_db,
        })
        assert "signal" in result
