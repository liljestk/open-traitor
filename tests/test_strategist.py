"""
Tests for StrategistAgent — signal filtering, prompt building, guard rails.

Uses importlib to avoid circular imports.
"""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _import_strategist():
    mod = importlib.import_module("src.agents.strategist")
    return mod.StrategistAgent, mod._get_strategy_prompt


def _make_strategist(
    min_confidence=0.7,
    style_modifiers=None,
    exchange="coinbase",
):
    config = {
        "trading": {
            "min_confidence": min_confidence,
            "style_modifiers": style_modifiers or [],
            "exchange": exchange,
        },
    }
    llm = MagicMock()
    llm.model = "test-model"
    llm.chat_json = AsyncMock(return_value={})
    state = MagicMock()
    state.current_prices = {"BTC-EUR": 50000}
    StrategistAgent, _ = _import_strategist()
    return StrategistAgent(llm, state, config)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════════════
# Signal filtering (quick skip)
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalFiltering:
    def test_weak_neutral_skipped(self):
        """Neutral signal below threshold should be skipped without LLM call."""
        s = _make_strategist(min_confidence=0.7)
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "neutral", "confidence": 0.3},
        }))
        assert result["action"] == "hold"
        s.llm.chat_json.assert_not_called()

    @patch("src.utils.llm_optimizer.get", return_value=["neutral", "weak_buy", "weak_sell"])
    def test_weak_buy_skipped(self, mock_opt):
        s = _make_strategist(min_confidence=0.7)
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "weak_buy", "confidence": 0.5},
        }))
        assert result["action"] == "hold"
        s.llm.chat_json.assert_not_called()

    def test_strong_buy_not_skipped(self):
        s = _make_strategist(min_confidence=0.7)
        s.llm.chat_json.return_value = {
            "action": "buy",
            "pair": "BTC-EUR",
            "confidence": 0.9,
            "reasoning": "test",
        }
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "strong_buy", "confidence": 0.85},
        }))
        s.llm.chat_json.assert_called_once()
        assert result["action"] == "buy"

    @patch("src.utils.llm_optimizer.get", return_value=["neutral", "weak_buy", "weak_sell", "buy"])
    def test_confidence_adjustment_raises_threshold(self, mock_opt):
        """Planning layer can raise threshold to avoid certain pairs."""
        s = _make_strategist(min_confidence=0.7)
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "buy", "confidence": 0.75},
            "confidence_adjustment": 0.1,  # Raises threshold to 0.8
        }))
        assert result["action"] == "hold"

    def test_confidence_adjustment_lowers_threshold(self):
        """Planning layer can also lower threshold to focus on a pair."""
        s = _make_strategist(min_confidence=0.7)
        s.llm.chat_json.return_value = {
            "action": "buy",
            "pair": "BTC-EUR",
            "confidence": 0.65,
            "reasoning": "lowered",
        }
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "buy", "confidence": 0.65},
            "confidence_adjustment": -0.1,  # Lowers threshold to 0.6
        }))
        s.llm.chat_json.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# High conviction only modifier
# ═══════════════════════════════════════════════════════════════════════════

class TestHighConvictionModifier:
    @patch("src.utils.llm_optimizer.get", return_value=["neutral", "weak_buy", "weak_sell", "buy", "sell"])
    def test_buy_signal_skipped(self, mock_opt):
        s = _make_strategist(style_modifiers=["high_conviction_only"])
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "buy", "confidence": 0.65},
        }))
        assert result["action"] == "hold"
        s.llm.chat_json.assert_not_called()

    @patch("src.utils.llm_optimizer.get", return_value=["neutral", "weak_buy", "weak_sell", "buy", "sell"])
    def test_sell_signal_skipped(self, mock_opt):
        s = _make_strategist(style_modifiers=["high_conviction_only"])
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "sell", "confidence": 0.65},
        }))
        assert result["action"] == "hold"


# ═══════════════════════════════════════════════════════════════════════════
# LLM response handling
# ═══════════════════════════════════════════════════════════════════════════

class TestLLMResponseHandling:
    def test_error_response_returns_hold(self):
        s = _make_strategist()
        s.llm.chat_json.return_value = {"error": "LLM timeout"}
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "strong_buy", "confidence": 0.9},
        }))
        assert result["action"] == "hold"
        assert "error" in result

    def test_pair_override_guard(self):
        """Strategist must not hallucinate a different pair."""
        s = _make_strategist()
        s.llm.chat_json.return_value = {
            "action": "buy",
            "pair": "ETH-EUR",  # Wrong pair!
            "confidence": 0.9,
            "reasoning": "hallucinated",
        }
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "strong_buy", "confidence": 0.9},
        }))
        # Guard should override pair back to BTC-EUR
        assert result["pair"] == "BTC-EUR"

    def test_confidence_clamped(self):
        """H5: Confidence should be clamped to [0, 1]."""
        s = _make_strategist()
        s.llm.chat_json.return_value = {
            "action": "buy",
            "pair": "BTC-EUR",
            "confidence": 1.5,  # Out of range
            "reasoning": "test",
        }
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "strong_buy", "confidence": 0.9},
        }))
        assert result["confidence"] == 1.0

    def test_confidence_clamped_negative(self):
        s = _make_strategist()
        s.llm.chat_json.return_value = {
            "action": "buy",
            "pair": "BTC-EUR",
            "confidence": -0.5,
            "reasoning": "test",
        }
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "strong_buy", "confidence": 0.9},
        }))
        assert result["confidence"] == 0.0

    def test_confidence_non_numeric_defaults_zero(self):
        s = _make_strategist()
        s.llm.chat_json.return_value = {
            "action": "buy",
            "pair": "BTC-EUR",
            "confidence": "high",
            "reasoning": "test",
        }
        result = _run(s.run({
            "signal": {"pair": "BTC-EUR", "signal_type": "strong_buy", "confidence": 0.9},
        }))
        assert result["confidence"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Strategy prompt selection
# ═══════════════════════════════════════════════════════════════════════════

class TestStrategyPrompt:
    def test_crypto_prompt(self):
        _, get_prompt = _import_strategist()
        prompt = get_prompt("coinbase")
        assert "small entries" in prompt.lower() or "fear" in prompt.lower()

    def test_equity_prompt(self):
        _, get_prompt = _import_strategist()
        prompt = get_prompt("ibkr")
        assert "fractional" in prompt.lower() or "earnings" in prompt.lower()
