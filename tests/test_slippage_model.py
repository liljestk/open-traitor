"""Unit tests for src.analysis.slippage_model."""

from __future__ import annotations

import math

import numpy as np

from src.analysis.slippage_model import (
    MIN_FILLS,
    PREDICTION_CEIL_BPS,
    SlippageImpactModel,
    fit_slippage_model,
)


def _synth_fills(n=200, seed=0):
    rng = np.random.default_rng(seed)
    fills = []
    for _ in range(n):
        notional = rng.uniform(100, 10_000)
        adv = rng.uniform(50_000, 500_000)
        vol = rng.uniform(0.005, 0.05)
        # True: 1.0 + 50.0*(N/ADV) + 100.0*vol + noise
        slip = 1.0 + 50.0 * (notional / adv) + 100.0 * vol + rng.normal(0, 0.5)
        fills.append({
            "notional": notional, "adv": adv,
            "realised_vol": vol, "slippage_bps": slip,
        })
    return fills


def test_fit_slippage_model_recovers_coefficients():
    m = fit_slippage_model(_synth_fills())
    assert m is not None
    assert abs(m.alpha - 1.0) < 1.0
    assert abs(m.beta_size - 50.0) < 5.0
    assert abs(m.beta_vol - 100.0) < 10.0
    assert m.r_squared > 0.5


def test_predict_is_bounded_and_nonnegative():
    m = SlippageImpactModel(
        alpha=-1000.0, beta_size=1.0, beta_vol=1.0,
        r_squared=0.0, sample_count=100,
    )
    # Should be clipped to 0
    assert m.predict(notional=1000, adv=100_000, realised_vol=0.01) == 0.0
    # Massive blow-up should be capped
    big = SlippageImpactModel(
        alpha=10_000.0, beta_size=1e9, beta_vol=0.0,
        r_squared=0.0, sample_count=100,
    )
    assert big.predict(notional=1e9, adv=1.0, realised_vol=0.0) == PREDICTION_CEIL_BPS


def test_predict_handles_invalid_inputs():
    m = SlippageImpactModel(
        alpha=1.0, beta_size=1.0, beta_vol=1.0,
        r_squared=0.5, sample_count=50,
    )
    assert m.predict(notional=0, adv=100.0, realised_vol=0.01) == 0.0
    assert m.predict(notional=100, adv=0, realised_vol=0.01) == 0.0
    assert m.predict(notional=float("nan"), adv=100, realised_vol=0.01) == 0.0


def test_fit_slippage_model_rejects_insufficient_fills():
    fills = _synth_fills(n=MIN_FILLS - 1)
    assert fit_slippage_model(fills) is None


def test_fit_slippage_model_skips_invalid_rows():
    valid = _synth_fills(n=MIN_FILLS + 5)
    invalid = [{"notional": "x", "adv": 100, "slippage_bps": 1}] * 5
    m = fit_slippage_model(invalid + valid)
    assert m is not None
    assert m.sample_count >= MIN_FILLS
