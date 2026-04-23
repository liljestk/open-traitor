"""Tests for the cross-sectional MomentumFactor strategy."""

from __future__ import annotations

import time

import numpy as np
import pytest

from src.analysis.regime_detector import Regime, RegimeSnapshot
from src.strategies.quant import MarketState, MomentumFactor


def _candles(closes):
    now = int(time.time())
    return [
        {
            "start": str(now + i * 60),
            "open": float(c), "high": float(c) * 1.005,
            "low": float(c) * 0.995, "close": float(c), "volume": 100.0,
        }
        for i, c in enumerate(closes)
    ]


def _state_with(pairs_returns, *, regime=Regime.TRENDING_UP, n_bars=80):
    """Build a MarketState where each pair has a constant log-return per bar."""
    pair_candles = {}
    regimes = {}
    for pair, log_return in pairs_returns.items():
        closes = 100 * np.exp(np.arange(n_bars) * log_return)
        pair_candles[pair] = _candles(closes)
        regimes[pair] = RegimeSnapshot(regime=regime, confidence=0.7)
    return MarketState(
        pair_candles=pair_candles,
        regimes=regimes,
        timestamp=time.time(),
        exchange="coinbase",
    )


# --------------------------------------------------------------------- #

class TestMomentumFactor:
    def setup_method(self):
        self.strat = MomentumFactor(lookback_bars=20, top_quantile=0.30, min_pairs=3)

    def test_invalid_quantile_raises(self):
        with pytest.raises(ValueError):
            MomentumFactor(top_quantile=0.0)
        with pytest.raises(ValueError):
            MomentumFactor(top_quantile=0.5)
        with pytest.raises(ValueError):
            MomentumFactor(top_quantile=0.6)

    def test_emits_nothing_below_min_pairs(self):
        state = _state_with({"BTC-USD": 0.01, "ETH-USD": -0.01})
        # Only 2 pairs but min_pairs=3
        assert self.strat.generate(state) == []

    def test_winners_long_losers_short(self):
        # 5 pairs with monotonically increasing returns. With
        # top_quantile=0.30 and n=5 → top 1.5 (mid-rank ≥ 0.7) long,
        # bottom 1.5 short.
        state = _state_with({
            "A": -0.02,
            "B": -0.005,
            "C":  0.000,
            "D":  0.005,
            "E":  0.02,
        })
        signals = {s.pair: s for s in self.strat.generate(state)}
        # Worst (A) should be short, best (E) should be long.
        assert "A" in signals and signals["A"].direction == "short"
        assert "E" in signals and signals["E"].direction == "long"
        # Middle pair "C" must NOT have a signal (flat).
        assert "C" not in signals

    def test_score_ordering_matches_rank(self):
        # 10 pairs with strictly increasing returns; the top should have
        # higher |score| than the second-from-top.
        returns = {f"P{i}": 0.001 * (i - 5) for i in range(10)}
        state = _state_with(returns)
        signals = {s.pair: s for s in self.strat.generate(state)}
        # Sanity: at least the best gets long, worst gets short.
        assert signals["P9"].direction == "long"
        assert signals["P0"].direction == "short"
        # |score| of best > |score| of any weaker long.
        long_signals = sorted(
            [s for s in signals.values() if s.direction == "long"],
            key=lambda s: s.metadata["lookback_return"],
        )
        scores = [s.score for s in long_signals]
        assert scores == sorted(scores)  # monotone in lookback_return

    def test_inactive_in_mean_reverting(self):
        state = _state_with(
            {"A": -0.02, "B": 0.0, "C": 0.02, "D": 0.04, "E": -0.04},
            regime=Regime.MEAN_REVERTING,
        )
        # active_regimes = (TRENDING_UP, TRENDING_DOWN); regime gating drops all.
        assert self.strat.generate(state) == []

    def test_short_history_excluded(self):
        # 4 pairs, one with too short history → falls below min_pairs.
        state = _state_with({"A": 0.01, "B": -0.01, "C": 0.005})
        # Override one pair's candles to be too short.
        state.pair_candles["C"] = _candles([100, 101, 102])
        # Now only 2 eligible pairs → min_pairs=3 → no signals.
        out = self.strat.generate(state)
        assert out == []

    def test_deterministic(self):
        state = _state_with({
            "A": -0.02, "B": -0.005, "C": 0.0, "D": 0.005, "E": 0.02,
        })
        out1 = sorted([(s.pair, s.score) for s in self.strat.generate(state)])
        out2 = sorted([(s.pair, s.score) for s in self.strat.generate(state)])
        assert out1 == out2

    def test_metadata_populated(self):
        state = _state_with({
            "A": -0.02, "B": -0.005, "C": 0.0, "D": 0.005, "E": 0.02,
        })
        for sig in self.strat.generate(state):
            assert "lookback_return" in sig.metadata
            assert sig.metadata["lookback_bars"] == 20
            assert 0 < sig.metadata["top_quantile"] < 0.5

    def test_active_in_trending_down(self):
        state = _state_with(
            {"A": -0.02, "B": -0.005, "C": 0.0, "D": 0.005, "E": 0.02},
            regime=Regime.TRENDING_DOWN,
        )
        out = self.strat.generate(state)
        assert len(out) > 0
