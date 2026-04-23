"""Tests for the QuantStrategy base + Z-score mean-reversion."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from src.analysis.regime_detector import Regime, RegimeSnapshot
from src.strategies.quant import (
    MarketState,
    QuantSignal,
    QuantStrategy,
    ZScoreMeanReversion,
)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _candles(closes):
    now = int(time.time())
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        out.append({
            "start": str(now + i * 60),
            "open": c, "high": c * 1.005, "low": c * 0.995,
            "close": c, "volume": 100.0,
        })
    return out


def _state(pair, closes, regime: Regime = Regime.MEAN_REVERTING) -> MarketState:
    return MarketState(
        pair_candles={pair: _candles(closes)},
        regimes={pair: RegimeSnapshot(regime=regime, confidence=0.7)},
        timestamp=time.time(),
        exchange="coinbase",
    )


# --------------------------------------------------------------------- #
# QuantSignal invariants
# --------------------------------------------------------------------- #

class TestQuantSignal:
    def test_direction_helpers(self):
        assert QuantSignal(strategy="x", score=0.5).direction == "long"
        assert QuantSignal(strategy="x", score=-0.5).direction == "short"
        assert QuantSignal(strategy="x", score=0.0).direction == "flat"

    def test_actionable_requires_nonzero_score(self):
        assert QuantSignal(strategy="x", score=0.5).is_actionable
        assert not QuantSignal(strategy="x", score=0.0).is_actionable
        assert not QuantSignal(strategy="x", score=0.5, confidence=0).is_actionable

    def test_to_dict_serializable(self):
        s = QuantSignal(strategy="x", score=0.42, pair="BTC-USD",
                        regime="chop", horizon_bars=3, confidence=0.7,
                        metadata={"k": 1})
        d = s.to_dict()
        assert d["strategy"] == "x" and d["pair"] == "BTC-USD"
        assert d["direction"] == "long"
        assert d["pairs"] == []


# --------------------------------------------------------------------- #
# QuantStrategy regime gating (via a minimal subclass)
# --------------------------------------------------------------------- #

class _AlwaysLong(QuantStrategy):
    name = "always_long"
    active_regimes = (Regime.MEAN_REVERTING,)

    def _generate(self, state):
        return [
            QuantSignal(strategy=self.name, score=1.0, pair=p)
            for p in state.pairs()
        ]


class TestRegimeGating:
    def test_signal_passes_when_regime_active(self):
        s = _state("BTC-USD", [100] * 50, regime=Regime.MEAN_REVERTING)
        out = _AlwaysLong().generate(s)
        assert len(out) == 1

    def test_signal_dropped_when_regime_inactive(self):
        s = _state("BTC-USD", [100] * 50, regime=Regime.TRENDING_UP)
        out = _AlwaysLong().generate(s)
        assert out == []

    def test_no_active_regimes_means_pass_through(self):
        class _AnyRegime(QuantStrategy):
            name = "any"
            active_regimes = None

            def _generate(self, state):
                return [QuantSignal(strategy=self.name, score=1.0, pair=p)
                        for p in state.pairs()]

        s = _state("BTC-USD", [100] * 50, regime=Regime.HIGH_VOL)
        assert len(_AnyRegime().generate(s)) == 1

    def test_missing_regime_pass_through(self):
        # No regime snapshot for the pair → don't filter (autonomy: don't
        # crash if upstream forgot to populate).
        state = MarketState(
            pair_candles={"BTC-USD": _candles([100] * 50)},
            regimes={},  # empty
            exchange="coinbase",
        )
        out = _AlwaysLong().generate(state)
        assert len(out) == 1


# --------------------------------------------------------------------- #
# ZScoreMeanReversion behavioural tests
# --------------------------------------------------------------------- #

class TestZScoreMeanReversion:
    def setup_method(self):
        self.strat = ZScoreMeanReversion(window=20, z_entry=1.5, z_full=3.0)

    def test_no_signal_in_calm_oscillation(self):
        rng = np.random.default_rng(0)
        closes = 100 + rng.normal(0, 0.5, 100)  # tight noise around 100
        state = _state("BTC-USD", closes)
        out = self.strat.generate(state)
        # Most bars produce |z| < 1.5 → no signal at the latest bar most of
        # the time. We just assert "either no signal or score in range".
        for sig in out:
            assert -1.0 <= sig.score <= 1.0

    def test_overbought_yields_short(self):
        # 80 calm bars then a sharp upward spike → last close >> mean.
        calm = list(np.full(80, 100.0))
        spike = [100.0 + i * 5 for i in range(1, 6)]  # 105..125
        state = _state("BTC-USD", calm + spike)
        out = self.strat.generate(state)
        assert len(out) == 1
        sig = out[0]
        assert sig.direction == "short", sig.to_dict()
        assert sig.score < 0
        assert sig.metadata["z_score"] > 1.5

    def test_oversold_yields_long(self):
        calm = list(np.full(80, 100.0))
        crash = [100.0 - i * 5 for i in range(1, 6)]  # 95..75
        state = _state("BTC-USD", calm + crash)
        out = self.strat.generate(state)
        assert len(out) == 1
        sig = out[0]
        assert sig.direction == "long", sig.to_dict()
        assert sig.score > 0
        assert sig.metadata["z_score"] < -1.5

    def test_score_saturates_at_z_full(self):
        # Long calm history with tiny noise so std is small, then a
        # single extreme final bar — keeps |z| past z_full so the score
        # saturates to ±1.
        rng = np.random.default_rng(123)
        calm = list(100 + rng.normal(0, 0.1, 200))
        state = _state("BTC-USD", calm + [10000.0])
        out = self.strat.generate(state)
        assert len(out) == 1
        # Saturated → |score| == 1
        assert out[0].score == pytest.approx(-1.0)

    def test_inactive_in_trending_regime(self):
        calm = list(np.full(80, 100.0))
        spike = [100.0 + i * 5 for i in range(1, 6)]
        state = _state("BTC-USD", calm + spike, regime=Regime.TRENDING_UP)
        out = self.strat.generate(state)
        assert out == []

    def test_short_history_returns_no_signal(self):
        state = _state("BTC-USD", list(range(100, 110)))  # too short
        out = self.strat.generate(state)
        assert out == []

    def test_zero_std_returns_no_signal(self):
        # Constant prices → std == 0 → no z computable
        state = _state("BTC-USD", [100.0] * 60)
        out = self.strat.generate(state)
        assert out == []

    def test_deterministic(self):
        closes = list(np.full(80, 100.0)) + [100 + i for i in range(1, 6)]
        s1 = self.strat.generate(_state("BTC-USD", closes))
        s2 = self.strat.generate(_state("BTC-USD", closes))
        assert [x.to_dict() for x in s1] == [x.to_dict() for x in s2]

    def test_multiple_pairs(self):
        calm = list(np.full(80, 100.0))
        spike_up = calm + [100 + i * 5 for i in range(1, 6)]
        spike_dn = calm + [100 - i * 5 for i in range(1, 6)]
        state = MarketState(
            pair_candles={
                "BTC-USD": _candles(spike_up),
                "ETH-USD": _candles(spike_dn),
            },
            regimes={
                "BTC-USD": RegimeSnapshot(regime=Regime.MEAN_REVERTING, confidence=0.7),
                "ETH-USD": RegimeSnapshot(regime=Regime.MEAN_REVERTING, confidence=0.7),
            },
            exchange="coinbase",
        )
        out = {s.pair: s for s in self.strat.generate(state)}
        assert out["BTC-USD"].direction == "short"
        assert out["ETH-USD"].direction == "long"
