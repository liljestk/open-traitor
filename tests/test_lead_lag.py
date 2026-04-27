"""Tests for src/analysis/lead_lag.py — pure OLS helper."""
import math
import random
from src.analysis.lead_lag import _ols


def test_ols_perfect_line():
    # Add tiny epsilon so SSE > 0 and t-stat is computable.
    x = [float(i) for i in range(100)]
    y = [2.0 * xi + 1.0 + (1e-6 if i % 2 == 0 else -1e-6) for i, xi in enumerate(x)]
    res = _ols(x, y)
    assert res is not None
    assert abs(res["beta"] - 2.0) < 1e-6
    assert res["r_squared"] > 0.999
    assert res["sample_count"] == 100


def test_ols_noise_lowers_r2():
    rng = random.Random(0)
    x = [float(i) for i in range(200)]
    y = [0.5 * xi + rng.gauss(0, 5.0) for xi in x]
    res = _ols(x, y)
    assert res is not None
    assert 0.0 < res["r_squared"] < 1.0
    assert abs(res["beta"] - 0.5) < 0.2


def test_ols_too_few_samples():
    assert _ols([1.0, 2.0], [1.0, 2.0]) is None


def test_ols_constant_x_returns_none():
    assert _ols([1.0] * 100, [float(i) for i in range(100)]) is None


def test_ols_constant_y_zero_beta():
    res = _ols([float(i) for i in range(100)], [3.0] * 100)
    # SSE is 0 → returns None per impl
    assert res is None or abs(res["beta"]) < 1e-9
