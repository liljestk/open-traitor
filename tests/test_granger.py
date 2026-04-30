"""Unit tests for src.analysis.granger."""

from __future__ import annotations

import math

import numpy as np

from src.analysis.granger import (
    MIN_OBSERVATIONS_AFTER_LAGS,
    f_sf,
    granger_causality,
)


def test_f_sf_known_values():
    # Reference values cross-checked vs scipy.stats.f.sf
    assert abs(f_sf(1.0, 5, 10) - 0.46511942653780036) < 1e-9
    assert abs(f_sf(3.0, 2, 20) - 0.07253815028640571) < 1e-9
    # f.sf(0.0, 5, 10) == 1.0
    assert f_sf(0.0, 5, 10) == 1.0
    # f.sf(1e9, 5, 10) → ~0
    assert f_sf(1e9, 5, 10) < 1e-9


def test_f_sf_invalid_inputs():
    assert math.isnan(f_sf(1.0, 0, 5))
    assert math.isnan(f_sf(1.0, 5, 0))
    assert math.isnan(f_sf(float("nan"), 5, 5))


def test_granger_detects_known_causality():
    rng = np.random.default_rng(0)
    n = 400
    x = rng.normal(0.0, 1.0, n)
    y = np.zeros(n)
    # y is driven by lagged x with strong coefficient
    for t in range(2, n):
        y[t] = 0.5 * y[t - 1] + 0.8 * x[t - 1] + rng.normal(0.0, 0.5)
    res = granger_causality(x, y, lag=1, leader_name="X", follower_name="Y")
    assert res is not None
    assert res.p_value < 0.01
    assert res.significant is True


def test_granger_rejects_when_no_causality():
    rng = np.random.default_rng(1)
    n = 400
    x = rng.normal(0.0, 1.0, n)
    y = rng.normal(0.0, 1.0, n)  # independent
    res = granger_causality(x, y, lag=1)
    assert res is not None
    assert res.p_value > 0.05  # almost surely not significant


def test_granger_rejects_short_series():
    n = MIN_OBSERVATIONS_AFTER_LAGS  # too short after subtracting lag overhead
    x = [0.0] * n
    y = [0.0] * n
    assert granger_causality(x, y, lag=4) is None


def test_granger_invalid_inputs():
    assert granger_causality([1.0], [1.0, 2.0], lag=1) is None
    assert granger_causality([1.0] * 10, [1.0] * 10, lag=0) is None
