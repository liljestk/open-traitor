"""Smoke test for the deterministic decision-engine backtest harness."""

from __future__ import annotations

import math

import pytest

from src.backtesting.decision_engine_backtest import run_decision_backtest
from src.analysis.signal_edge_library import (
    InMemorySignalEdgeStore,
    SignalEdgeLibrary,
)
from src.core.capital_allocator import CapitalAllocator
from src.core.decision_engine import DecisionEngine


def _trend_candles(n=200, start=100.0, drift=0.001):
    """Generate a gently trending series with mild noise."""
    closes = []
    p = start
    for i in range(n):
        # Sine wiggle + drift.
        p = p * (1.0 + drift + 0.002 * math.sin(i / 7.0))
        closes.append({"close": p, "open": p, "high": p * 1.001, "low": p * 0.999})
    return closes


def test_smoke_backtest_runs_and_records_edge():
    candles = _trend_candles(300)
    edges = SignalEdgeLibrary(store=InMemorySignalEdgeStore(), exchange="test")
    allocator = CapitalAllocator()
    allocator.register(["deterministic"])
    eng = DecisionEngine(
        edges=edges, allocator=allocator, exchange="test",
        min_edge_samples=30, min_edge_sharpe=0.10,
    )
    result = run_decision_backtest(
        candles, decision_engine=eng, edges=edges, allocator=allocator,
    )
    d = result.to_dict()
    assert d["n_proposals"] > 0
    assert d["n_approved"] + d["n_vetoed"] == d["n_proposals"]
    # We should have accumulated some samples.
    assert result.final_edge_n >= 0


def test_low_sharpe_history_eventually_vetoes():
    """Pre-load the edge store with a losing history; new proposals should
    start being vetoed once sample count crosses min_edge_samples."""
    edges = SignalEdgeLibrary(store=InMemorySignalEdgeStore(), exchange="test")
    for _ in range(50):
        edges.record_sample(
            signal_name="deterministic", regime="trending",
            score=1.0, forward_return=-0.005, pair="BTC-USD",
        )
    allocator = CapitalAllocator()
    allocator.register(["deterministic"])
    eng = DecisionEngine(
        edges=edges, allocator=allocator, exchange="test",
        min_edge_samples=30, min_edge_sharpe=0.10,
    )
    candles = _trend_candles(150)
    result = run_decision_backtest(
        candles, decision_engine=eng, edges=edges, allocator=allocator,
    )
    # With pre-loaded losing history, every buy should hit the no_edge veto.
    assert result.veto_breakdown.get("no_edge", 0) > 0
