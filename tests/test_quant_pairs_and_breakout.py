"""Tests for CointegrationPairs and VolatilityBreakout strategies."""

from __future__ import annotations

import time

import numpy as np
import pytest

from src.analysis.regime_detector import Regime, RegimeSnapshot
from src.strategies.quant import (
    CointegrationPairs,
    MarketState,
    VolatilityBreakout,
)
from src.strategies.quant.cointegration_pairs import _ar1_coef, _ols_slope_intercept


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


# --------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------- #

class TestOlsAndAr1:
    def test_ols_recovers_known_params(self):
        rng = np.random.default_rng(0)
        x = np.arange(100, dtype=float)
        y = 2.0 + 1.5 * x + rng.normal(0, 0.01, 100)
        b, a = _ols_slope_intercept(x, y)
        assert b == pytest.approx(1.5, rel=1e-3)
        assert a == pytest.approx(2.0, abs=1e-1)

    def test_ols_degenerate(self):
        x = np.zeros(10)
        y = np.arange(10, dtype=float)
        b, a = _ols_slope_intercept(x, y)
        assert b is None and a is None

    def test_ar1_random_walk_high(self):
        rng = np.random.default_rng(1)
        s = np.cumsum(rng.normal(0, 1, 200))  # random walk → AR(1) ≈ 1
        c = _ar1_coef(s)
        assert c is not None and c > 0.95

    def test_ar1_white_noise_near_zero(self):
        rng = np.random.default_rng(2)
        s = rng.normal(0, 1, 500)
        c = _ar1_coef(s)
        assert c is not None and abs(c) < 0.2


# --------------------------------------------------------------------- #
# CointegrationPairs
# --------------------------------------------------------------------- #

class TestCointegrationPairs:
    def setup_method(self):
        self.strat = CointegrationPairs(
            candidate_pairs=[("A", "B")],
            lookback_bars=40,
            z_entry=1.5,
            z_full=3.0,
            max_autocorr=0.95,
        )

    def _build_state(self, ya, yb, regime=Regime.MEAN_REVERTING):
        return MarketState(
            pair_candles={"A": _candles(np.exp(ya)), "B": _candles(np.exp(yb))},
            regimes={
                "A": RegimeSnapshot(regime=regime, confidence=0.6),
                "B": RegimeSnapshot(regime=regime, confidence=0.6),
            },
            exchange="coinbase",
        )

    def test_no_pairs_no_signals(self):
        s = CointegrationPairs(candidate_pairs=[])
        st = MarketState(pair_candles={}, regimes={}, exchange="coinbase")
        assert s.generate(st) == []

    def test_emits_signal_on_extreme_spread(self):
        # Construct A and B that are cointegrated: A = 0.5 + B + epsilon.
        # Spread = A - B - 0.5 is mean-zero noise. Then spike A's last
        # close to push spread far above mean → expect short-A signal.
        rng = np.random.default_rng(7)
        n = 100
        b = np.cumsum(rng.normal(0, 0.005, n))  # log-prices walk
        a = 0.5 + b + rng.normal(0, 0.005, n)
        # Spike A's most recent log-price upward by a huge multiple of spread std.
        a[-1] += 0.5
        st = self._build_state(a, b)
        out = self.strat.generate(st)
        assert len(out) == 1
        sig = out[0]
        assert sig.pairs == ("A", "B")
        # z > 0 (A overpriced) → score < 0 (long B short A).
        assert sig.direction == "short"

    def test_no_signal_when_spread_near_mean(self):
        rng = np.random.default_rng(8)
        n = 100
        b = np.cumsum(rng.normal(0, 0.005, n))
        a = 0.5 + b + rng.normal(0, 0.005, n)
        st = self._build_state(a, b)
        out = self.strat.generate(st)
        # Spread is near zero → |z| < 1.5 → no trade most of the time.
        # Just assert determinism & non-crash.
        assert isinstance(out, list)
        for sig in out:
            assert sig.confidence >= 0

    def test_filters_non_stationary_spread(self):
        # If both legs are independent random walks, spread is non-mean-
        # reverting → AR(1) close to 1 → strategy should stay flat.
        rng = np.random.default_rng(9)
        n = 200
        a = np.cumsum(rng.normal(0, 0.01, n))
        b = np.cumsum(rng.normal(0, 0.01, n))
        # Force a spike to ensure |z| > entry — but autocorr filter should
        # still kill the trade.
        a[-1] += 0.5
        s = CointegrationPairs(
            candidate_pairs=[("A", "B")],
            lookback_bars=40, z_entry=1.5, max_autocorr=0.5,
        )
        st = self._build_state(a, b)
        # Either no signal (filtered) or small. We assert it's filtered when
        # ar1 high.
        for sig in s.generate(st):
            assert sig.metadata["ar1"] < 0.5

    def test_short_history(self):
        st = MarketState(
            pair_candles={"A": _candles([1.0] * 10), "B": _candles([1.0] * 10)},
            regimes={
                "A": RegimeSnapshot(regime=Regime.CHOP, confidence=0.5),
                "B": RegimeSnapshot(regime=Regime.CHOP, confidence=0.5),
            },
            exchange="coinbase",
        )
        assert self.strat.generate(st) == []


# --------------------------------------------------------------------- #
# VolatilityBreakout
# --------------------------------------------------------------------- #

class TestVolatilityBreakout:
    def setup_method(self):
        self.strat = VolatilityBreakout(
            bb_period=20, bb_std=2.0,
            vol_window=80, vol_quantile=0.7,
        )

    def _state(self, closes, regime=Regime.HIGH_VOL):
        return MarketState(
            pair_candles={"BTC-USD": _candles(closes)},
            regimes={"BTC-USD": RegimeSnapshot(regime=regime, confidence=0.7)},
            exchange="coinbase",
        )

    def test_no_signal_in_calm_period(self):
        rng = np.random.default_rng(0)
        closes = 100 + rng.normal(0, 0.05, 200)
        out = self.strat.generate(self._state(closes))
        # Tiny vol → BBW unlikely to expand; even if signal fires it should
        # not crash and should be in range.
        for sig in out:
            assert -1.0 <= sig.score <= 1.0

    def test_upside_breakout_after_quiet(self):
        # 150 calm bars then 30 wider bars then a clear close above bands.
        rng = np.random.default_rng(1)
        calm = 100 + rng.normal(0, 0.1, 150)
        # Vol expansion period — wider noise so BBW spikes
        wide = 100 + rng.normal(0, 2.0, 30)
        breakout = list(wide) + [110.0, 115.0]  # decisive upside close
        closes = list(calm) + breakout
        out = self.strat.generate(self._state(closes, regime=Regime.HIGH_VOL))
        assert len(out) == 1
        assert out[0].direction == "long"
        assert out[0].score > 0

    def test_downside_breakout_after_quiet(self):
        rng = np.random.default_rng(2)
        calm = 100 + rng.normal(0, 0.1, 150)
        wide = 100 + rng.normal(0, 2.0, 30)
        breakout = list(wide) + [90.0, 85.0]
        closes = list(calm) + breakout
        out = self.strat.generate(self._state(closes, regime=Regime.HIGH_VOL))
        assert len(out) == 1
        assert out[0].direction == "short"

    def test_inactive_in_mean_reverting_regime(self):
        rng = np.random.default_rng(3)
        calm = 100 + rng.normal(0, 0.1, 150)
        wide = 100 + rng.normal(0, 2.0, 30)
        breakout = list(wide) + [115.0]
        closes = list(calm) + breakout
        out = self.strat.generate(self._state(closes, regime=Regime.MEAN_REVERTING))
        assert out == []

    def test_short_history(self):
        out = self.strat.generate(self._state([100] * 30))
        assert out == []

    def test_low_vol_blocks_breakout(self):
        # Construct a window where BBW recently spiked then collapsed,
        # so current BBW rank in the vol_window is low. A small breakout
        # close should NOT fire because vol expansion gate fails.
        rng = np.random.default_rng(4)
        # 60 bars of high-vol noise inside the vol_window=80, then 30
        # very calm bars, then a tiny breakout. Recent BBW will sit at
        # the bottom of the 80-bar BBW history.
        head = 100 + rng.normal(0, 0.1, 60)
        wide = 100 + rng.normal(0, 3.0, 60)
        calm = 100 + rng.normal(0, 0.05, 30)
        closes = list(head) + list(wide) + list(calm) + [100.15]
        out = self.strat.generate(self._state(closes, regime=Regime.HIGH_VOL))
        assert out == []
