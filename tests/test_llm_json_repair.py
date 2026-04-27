"""Tests for the hardened LLM JSON repair path.

The trader pipeline previously dropped high-conviction signals when the LLM
returned JS-style commented or expression-laden JSON (e.g. llama-3.3 free).
These tests pin the repair behaviour so regressions surface immediately.
"""

from __future__ import annotations

import pytest

from src.core.llm_client import LLMClient, _sanitize_jsonish, _eval_numeric_expr


def _client():
    # __new__ avoids the real provider chain; we only call helpers.
    return LLMClient.__new__(LLMClient)


def test_sanitize_strips_line_comments():
    raw = """{
      "action": "buy", // pick a buy
      "confidence": 0.8
    }"""
    assert "//" not in _sanitize_jsonish(raw)


def test_sanitize_strips_block_comments():
    raw = """{ /* model commentary */ "x": 1 /* tail */ }"""
    out = _sanitize_jsonish(raw)
    assert "/*" not in out and "*/" not in out


def test_sanitize_drops_trailing_commas():
    raw = '{"a": 1, "b": [1, 2, 3,], }'
    cleaned = _sanitize_jsonish(raw)
    import json
    assert json.loads(cleaned) == {"a": 1, "b": [1, 2, 3]}


def test_sanitize_evaluates_numeric_expressions():
    raw = '{"quantity": 26.21 * 0.2, "price": 5 + 3}'
    cleaned = _sanitize_jsonish(raw)
    import json
    parsed = json.loads(cleaned)
    assert parsed["quantity"] == pytest.approx(26.21 * 0.2)
    assert parsed["price"] == 8


def test_sanitize_leaves_strings_untouched():
    # Comment-like content inside a string literal must survive verbatim.
    raw = '{"reasoning": "stop / loss // 3% below"}'
    cleaned = _sanitize_jsonish(raw)
    import json
    assert json.loads(cleaned)["reasoning"] == "stop / loss // 3% below"


def test_eval_numeric_expr_rejects_names():
    assert _eval_numeric_expr("1 + cash") is None
    assert _eval_numeric_expr("__import__('os')") is None
    assert _eval_numeric_expr("1 + 2") == 3.0


def test_extract_json_handles_real_world_llama_output():
    # Verbatim shape from the FIL-EUR cycle that broke production:
    # comments + multiplied quantity expression.
    raw = """{
      "action": "buy",
      "pair": "FIL-EUR",
      "confidence": 0.8,
      "quote_amount": null,
      "quantity": 26.215799999999998 * 0.2, // 20% of cash balance
      "stop_loss_price": 0.78,              // 3.7% below current price
      "take_profit_price": 0.84,            // 5% above current price
      "strategy": "llm_strategist",
      "reasoning": "EMA + MACD bullish // confirmed"
    }"""
    parsed = _client()._extract_json(raw)
    assert parsed.get("action") == "buy"
    assert parsed.get("pair") == "FIL-EUR"
    assert parsed.get("quantity") == pytest.approx(26.215799999999998 * 0.2)
    assert parsed.get("stop_loss_price") == 0.78
    # Comment-like text inside the reasoning string must be preserved.
    assert "//" in parsed.get("reasoning", "")


def test_extract_json_returns_error_for_garbage():
    parsed = _client()._extract_json("totally not json at all")
    assert parsed.get("error") == "Failed to parse LLM response"
