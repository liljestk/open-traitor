"""Tests for the deterministic regime detector."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from src.analysis.regime_detector import (
    Regime,
    RegimeConfig,
    RegimeDetector,
    RegimeSnapshot,
    _hurst_exponent,
    _log_slope,
)


# --------------------------------------------------------------------- #
# Synthetic candle factories
# --------------------------------------------------------------------- #

def _make_candles(closes: np.ndarray, *, vol_pct: float = 0.005) -> list[dict]:
    """Wrap a close-price series in OHLCV candle dicts (Coinbase shape)."""
    now = int(time.time())
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        out.append(
            {
                "start": str(now + i * 60),
                "open": c,
                "high": c * (1 + vol_pct),
                "low": c * (1 - vol_pct),
                "close": c,
                "volume": 100.0,
            }
        )
    return out


def _trend_series(n: int, slope: float, start: float = 100.0, noise: float = 0.0,
                  seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.arange(n)
    base = start * np.exp(slope * x)
    if noise > 0:
        base = base * (1 + rng.normal(0, noise, size=n))
    return base


def _mean_revert_series(n: int, mean: float = 100.0, amp: float = 2.0,
                        period: int = 20, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.arange(n)
    return mean + amp * np.sin(2 * math.pi * x / period) + rng.normal(0, 0.1, n)


def _high_vol_series(n: int, mean: float = 100.0, vol: float = 0.05,
                     seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, vol, n)
    return mean * np.exp(np.cumsum(returns))


# --------------------------------------------------------------------- #
# Pure-function helpers
# --------------------------------------------------------------------- #

class TestLogSlope:
    def test_flat_series_zero_slope(self):
        slope, r2 = _log_slope(np.full(50, 100.0))
        assert abs(slope) < 1e-9

    def test_positive_trend(self):
        prices = _trend_series(100, slope=0.005)
        slope, r2 = _log_slope(prices)
        assert slope > 0
        assert r2 > 0.99  # noiseless line ≈ perfect fit

    def test_negative_trend(self):
        prices = _trend_series(100, slope=-0.003)
        slope, r2 = _log_slope(prices)
        assert slope < 0
        assert r2 > 0.99

    def test_too_short_returns_zeros(self):
        slope, r2 = _log_slope(np.array([1.0, 2.0]))
        assert slope == 0.0 and r2 == 0.0

    def test_handles_invalid_prices(self):
        prices = np.array([100, np.nan, np.inf, 0.0, -1.0, 101.0])
        slope, r2 = _log_slope(prices)
        # Only 2 valid prices remain; should return zeros (insufficient)
        assert slope == 0.0


class TestHurstExponent:
    def test_random_walk_near_half(self):
        rng = np.random.default_rng(42)
        # 1000-step geometric brownian motion → H ≈ 0.5
        returns = rng.normal(0, 0.01, 1000)
        prices = 100 * np.exp(np.cumsum(returns))
        h = _hurst_exponent(prices)
        assert 0.35 <= h <= 0.65, f"random walk Hurst out of band: {h}"

    def test_persistent_returns_higher_than_random(self):
        # Hurst is a property of returns. Generate an AR(1) process with
        # strongly positive autocorrelation: persistent returns → H > 0.5.
        rng = np.random.default_rng(7)
        n = 1500
        phi = 0.6
        eps = rng.normal(0, 0.01, n)
        r = np.zeros(n)
        for i in range(1, n):
            r[i] = phi * r[i - 1] + eps[i]
        prices = 100 * np.exp(np.cumsum(r))
        h = _hurst_exponent(prices)
        assert h > 0.55, f"persistent-returns Hurst too low: {h}"

    def test_anti_persistent_returns_lower_than_persistent(self):
        # R/S Hurst is known to be biased toward 0.5 for short series, so
        # rather than asserting an absolute floor we assert the *ordering*:
        # anti-persistent < persistent. That's the property we depend on.
        rng_a = np.random.default_rng(8)
        rng_b = np.random.default_rng(9)
        n = 1500
        eps_a = rng_a.normal(0, 0.01, n)
        eps_b = rng_b.normal(0, 0.01, n)
        r_anti = np.zeros(n)
        r_pers = np.zeros(n)
        for i in range(1, n):
            r_anti[i] = -0.6 * r_anti[i - 1] + eps_a[i]
            r_pers[i] = 0.6 * r_pers[i - 1] + eps_b[i]
        p_anti = 100 * np.exp(np.cumsum(r_anti))
        p_pers = 100 * np.exp(np.cumsum(r_pers))
        assert _hurst_exponent(p_anti) < _hurst_exponent(p_pers)

    def test_short_series_returns_default(self):
        assert _hurst_exponent(np.array([100.0, 101.0])) == 0.5

    def test_handles_zero_and_negative_prices(self):
        # Should not crash; falls back gracefully.
        h = _hurst_exponent(np.array([0.0, -1.0, np.nan]))
        assert h == 0.5


# --------------------------------------------------------------------- #
# RegimeDetector behavioural tests
# --------------------------------------------------------------------- #

class TestRegimeDetector:
    def setup_method(self):
        self.detector = RegimeDetector()

    def test_insufficient_candles_returns_chop_zero_confidence(self):
        snap = self.detector.detect([])
        assert snap.regime == Regime.CHOP
        assert snap.confidence == 0.0
        assert snap.notes == "insufficient_candles"

    def test_uptrend_classified_as_trending_up(self):
        # Use realistic intra-bar noise (1.5%) so ATR is dominated by
        # bar-range rather than trend drift; otherwise a clean trend
        # legitimately registers as a vol breakout.
        candles = _make_candles(
            _trend_series(250, slope=0.004, noise=0.002),
            vol_pct=0.015,
        )
        snap = self.detector.detect(candles)
        assert snap.regime == Regime.TRENDING_UP, snap.to_dict()
        assert snap.slope > 0
        assert snap.confidence >= 0.6

    def test_downtrend_classified_as_trending_down(self):
        candles = _make_candles(
            _trend_series(250, slope=-0.004, noise=0.002),
            vol_pct=0.015,
        )
        snap = self.detector.detect(candles)
        assert snap.regime == Regime.TRENDING_DOWN, snap.to_dict()
        assert snap.slope < 0

    def test_high_vol_overrides_direction(self):
        # Build a series where the *most recent* ATR is in the top 15% of
        # its trailing window. Easiest way: tack a vol-blowup onto the end
        # of a calm series so the rank is forced extreme.
        calm = _mean_revert_series(220, amp=0.2)
        blowup = _high_vol_series(40, mean=float(calm[-1]), vol=0.08, seed=99)
        candles = _make_candles(np.concatenate([calm, blowup]))
        snap = self.detector.detect(candles)
        assert snap.regime == Regime.HIGH_VOL, snap.to_dict()
        assert snap.atr_pct_rank >= 0.85

    def test_mean_reverting_classified(self):
        candles = _make_candles(_mean_revert_series(300, amp=1.5, period=25))
        snap = self.detector.detect(candles)
        # Either MEAN_REVERTING or CHOP is acceptable for sinusoidal series;
        # the contract is "not trending".
        assert snap.regime in (Regime.MEAN_REVERTING, Regime.CHOP), snap.to_dict()
        assert not snap.regime.is_trending

    def test_snapshot_serializes_to_dict(self):
        candles = _make_candles(_trend_series(120, slope=0.003))
        snap = self.detector.detect(candles)
        d = snap.to_dict()
        assert d["regime"] in {r.value for r in Regime}
        assert 0.0 <= d["confidence"] <= 1.0
        assert "atr_pct_rank" in d and 0.0 <= d["atr_pct_rank"] <= 1.0
        assert "hurst" in d

    def test_deterministic_for_same_input(self):
        candles = _make_candles(_trend_series(200, slope=0.003, seed=11))
        s1 = self.detector.detect(candles)
        s2 = self.detector.detect(candles)
        assert s1.to_dict() == s2.to_dict()

    def test_regime_enum_helpers(self):
        assert Regime.TRENDING_UP.is_trending
        assert Regime.TRENDING_DOWN.is_trending
        assert not Regime.MEAN_REVERTING.is_trending
        assert not Regime.HIGH_VOL.is_trending
        assert Regime.TRENDING_UP.is_directional
        assert not Regime.CHOP.is_directional

    def test_custom_config_is_respected(self):
        # Force every series to look like high_vol by setting a near-zero
        # threshold; it should always classify HIGH_VOL.
        cfg = RegimeConfig(atr_pct_rank_high_vol=0.0)
        det = RegimeDetector(cfg)
        candles = _make_candles(_trend_series(150, slope=0.001))
        snap = det.detect(candles)
        assert snap.regime == Regime.HIGH_VOL


# --------------------------------------------------------------------- #
# Snapshot dataclass invariants
# --------------------------------------------------------------------- #

def test_regime_snapshot_is_immutable():
    snap = RegimeSnapshot(regime=Regime.CHOP, confidence=0.5)
    with pytest.raises(Exception):
        snap.confidence = 0.9  # frozen=True
