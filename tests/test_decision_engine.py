"""Tests for src/core/decision_engine.py."""

from __future__ import annotations

import pytest

from src.core.capital_allocator import CapitalAllocator
from src.core.decision_engine import DecisionEngine, TradeProposal
from src.analysis.signal_edge_library import (
    InMemorySignalEdgeStore,
    SignalEdgeLibrary,
)


def _make_proposal(action="buy", confidence=0.8, strategy="ema_crossover",
                   regime="trending", ensemble=None, pattern=None):
    return TradeProposal(
        pair="BTC-USD",
        action=action,
        confidence=confidence,
        strategy=strategy,
        regime=regime,
        ensemble=ensemble,
        pattern_signal=pattern,
        current_price=100.0,
    )


def _engine(**overrides):
    edges = SignalEdgeLibrary(store=InMemorySignalEdgeStore(), exchange="test")
    allocator = CapitalAllocator()
    allocator.register(["ema_crossover", "bollinger_reversion", "pattern_engine"])
    return DecisionEngine(
        edges=edges, allocator=allocator, exchange="test", **overrides,
    ), edges, allocator


def test_envelope_rejects_low_confidence():
    eng, _, _ = _engine(min_confidence=0.6)
    v = eng.evaluate(_make_proposal(confidence=0.3), portfolio_value=1000, cash_balance=1000)
    assert not v.approved
    assert v.veto == "low_confidence"


def test_hold_short_circuits_to_approved_hold():
    eng, _, _ = _engine()
    v = eng.evaluate(_make_proposal(action="hold"), portfolio_value=1000, cash_balance=1000)
    assert v.approved
    assert v.action == "hold"


def test_edge_veto_only_after_min_samples():
    eng, edges, _ = _engine(min_edge_samples=10, min_edge_sharpe=0.10)
    # 5 samples — below threshold — no veto
    for _ in range(5):
        edges.record_sample(
            signal_name="ema_crossover", regime="trending",
            score=1.0, forward_return=-0.01, pair="BTC-USD",
        )
    v = eng.evaluate(_make_proposal(), portfolio_value=1000, cash_balance=1000)
    assert v.approved, v.reasons
    # Now add 30 more bad samples → expect veto
    for _ in range(30):
        edges.record_sample(
            signal_name="ema_crossover", regime="trending",
            score=1.0, forward_return=-0.01, pair="BTC-USD",
        )
    v = eng.evaluate(_make_proposal(), portfolio_value=1000, cash_balance=1000)
    assert not v.approved
    assert v.veto == "no_edge"


def test_ensemble_disagreement_vetoes_buy_without_pattern_confirm():
    eng, _, _ = _engine()
    ensemble = {"action": "sell", "agreement": 0.7, "n_strategies": 3}
    v = eng.evaluate(
        _make_proposal(ensemble=ensemble), portfolio_value=1000, cash_balance=1000,
    )
    assert not v.approved
    assert v.veto == "ensemble_disagreement"


def test_ensemble_disagreement_overridden_by_pattern_confirm():
    eng, _, _ = _engine()
    ensemble = {"action": "sell", "agreement": 0.7, "n_strategies": 3}
    pattern = {"available": True, "direction": "bullish", "confidence": 0.7}
    v = eng.evaluate(
        _make_proposal(ensemble=ensemble, pattern=pattern),
        portfolio_value=1000, cash_balance=1000,
    )
    assert v.approved


def test_allocator_zero_weight_vetoes():
    eng, _, allocator = _engine()
    # Force the registered strategy to weight 0 by mutating state directly.
    allocator.state.weights["ema_crossover"] = 0.0
    v = eng.evaluate(
        _make_proposal(strategy="ema_crossover"),
        portfolio_value=1000, cash_balance=1000,
    )
    assert not v.approved
    assert v.veto == "no_allocator_budget"


def test_approved_proposal_carries_allocator_budget_cap():
    eng, _, _ = _engine()
    v = eng.evaluate(_make_proposal(), portfolio_value=10_000, cash_balance=10_000)
    assert v.approved
    assert v.proposal["allocator_budget_cap"] > 0
    assert v.allocator_weight is not None
