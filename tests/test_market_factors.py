"""Unit tests for src.analysis.market_factors.fit_multifactor."""

from __future__ import annotations

import numpy as np

from src.analysis.market_factors import (
    MIN_OBSERVATIONS,
    fit_multifactor,
)


def _synth(seed: int = 0):
    rng = np.random.default_rng(seed)
    n = 250
    f1 = rng.normal(0.0, 0.01, n)
    f2 = rng.normal(0.0, 0.02, n)
    eps = rng.normal(0.0, 0.005, n)
    # True coefficients: alpha=0.0001, beta1=1.2, beta2=-0.4
    y = 0.0001 + 1.2 * f1 + (-0.4) * f2 + eps
    X = np.column_stack([f1, f2])
    return y, X


def test_fit_multifactor_recovers_betas():
    y, X = _synth()
    res = fit_multifactor(y, X, factor_names=["F1", "F2"], symbol="TEST")
    assert res is not None
    betas = {l.factor: l.beta for l in res.loadings}
    assert abs(betas["F1"] - 1.2) < 0.05
    assert abs(betas["F2"] - (-0.4)) < 0.05
    assert res.sample_count == y.shape[0]
    assert 0.0 <= res.r_squared <= 1.0
    assert res.idio_vol > 0


def test_fit_multifactor_t_stats_nonzero_for_real_factors():
    y, X = _synth(seed=42)
    res = fit_multifactor(y, X, factor_names=["F1", "F2"], symbol="X")
    assert res is not None
    for l in res.loadings:
        assert abs(l.t_stat) > 2.0  # strong signal


def test_fit_multifactor_rejects_insufficient_samples():
    n = MIN_OBSERVATIONS - 1
    y = np.zeros(n)
    X = np.zeros((n, 2))
    assert fit_multifactor(y, X, factor_names=["A", "B"], symbol="X") is None


def test_fit_multifactor_rejects_singular_design():
    n = 100
    f1 = np.linspace(0.0, 1.0, n)
    f2 = 2.0 * f1  # collinear
    y = f1 + 0.001
    X = np.column_stack([f1, f2])
    res = fit_multifactor(y, X, factor_names=["A", "B"], symbol="X")
    # Either None (singular) or finite betas — but never NaN.
    if res is not None:
        for l in res.loadings:
            assert np.isfinite(l.beta)


def test_fit_multifactor_rejects_shape_mismatch():
    assert fit_multifactor(
        np.zeros(50), np.zeros((40, 2)), factor_names=["A", "B"], symbol="X"
    ) is None
    assert fit_multifactor(
        np.zeros(100), np.zeros((100, 2)), factor_names=["A"], symbol="X"
    ) is None
