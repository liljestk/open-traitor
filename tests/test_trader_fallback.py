"""Tests for TraderAgent._deterministic_fallback's stronger logic.

When the LLM is unavailable, the fallback should still respect a high-conviction
analyst signal rather than blindly mirroring an inactive ensemble.
"""

from __future__ import annotations

import pytest

from src.agents.trader import TraderAgent


def _payload(*, ensemble_action="hold", ensemble_conf=0.0,
             analyst="neutral", analyst_conf=0.0,
             pattern_dir=None, pattern_avail=False,
             stop_loss=None, take_profit=None, current_price=1.0,
             pair="FIL-EUR"):
    return {
        "market": {
            "pair": pair,
            "current_price": current_price,
            "signal_type": analyst,
            "signal_confidence": analyst_conf,
            "suggested_stop_loss": stop_loss,
            "suggested_take_profit": take_profit,
        },
        "strategy_signals": {
            "ensemble": {"action": ensemble_action, "confidence": ensemble_conf},
        },
        "pattern": {"available": pattern_avail, "direction": pattern_dir or ""},
    }


def test_fallback_promotes_high_conviction_analyst_buy_when_ensemble_inactive():
    """The FIL-EUR regression case: ensemble hold@0, analyst buy@0.80 → buy."""
    out = TraderAgent._deterministic_fallback(_payload(
        ensemble_action="hold", ensemble_conf=0.0,
        analyst="buy", analyst_conf=0.80,
        stop_loss=0.795, take_profit=0.825, current_price=0.81,
    ))
    assert out["action"] == "buy"
    assert out["confidence"] == pytest.approx(0.80)
    assert out["stop_loss_price"] == pytest.approx(0.795)
    assert out["take_profit_price"] == pytest.approx(0.825)
    assert "analyst=buy" in out["reasoning"]


def test_fallback_holds_when_pattern_contradicts_analyst():
    out = TraderAgent._deterministic_fallback(_payload(
        analyst="buy", analyst_conf=0.85,
        pattern_avail=True, pattern_dir="bearish",
    ))
    assert out["action"] == "hold"
    assert "contradicted by pattern" in out["reasoning"]


def test_fallback_respects_buy_floor():
    # Analyst confidence < 0.70 → not promoted (analyst buy gate).
    out = TraderAgent._deterministic_fallback(_payload(
        analyst="buy", analyst_conf=0.65,
    ))
    assert out["action"] == "hold"


def test_fallback_uses_ensemble_when_actionable():
    out = TraderAgent._deterministic_fallback(_payload(
        ensemble_action="buy", ensemble_conf=0.75,
        analyst="neutral", analyst_conf=0.0,
    ))
    assert out["action"] == "buy"
    assert "ensemble=buy" in out["reasoning"]


def test_fallback_holds_when_no_signal_available():
    out = TraderAgent._deterministic_fallback(_payload())
    assert out["action"] == "hold"
    assert out["confidence"] == 0.0


def test_fallback_synthesizes_stop_when_analyst_lacks_one():
    out = TraderAgent._deterministic_fallback(_payload(
        analyst="buy", analyst_conf=0.80,
        stop_loss=None, take_profit=None, current_price=10.0,
    ))
    assert out["action"] == "buy"
    # 3% below current price as a conservative default.
    assert out["stop_loss_price"] == pytest.approx(9.7)
