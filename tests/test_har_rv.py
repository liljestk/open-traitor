"""Unit tests for src.analysis.har_rv."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np

from src.analysis.har_rv import (
    LAG_MONTHLY,
    MIN_OBSERVATIONS,
    daily_rv_series_from_candles,
    daily_rv_series_from_daily_closes,
    fit_har_rv,
    realised_vol_from_intraday,
)


def test_realised_vol_from_intraday_basic():
    closes = [100.0, 101.0, 100.5, 102.0]
    rv = realised_vol_from_intraday(closes)
    assert rv > 0
    # Sum of squared log returns
    expected = math.sqrt(
        math.log(101.0 / 100.0) ** 2
        + math.log(100.5 / 101.0) ** 2
        + math.log(102.0 / 100.5) ** 2
    )
    assert abs(rv - expected) < 1e-12


def test_realised_vol_handles_short_series():
    assert realised_vol_from_intraday([]) == 0.0
    assert realised_vol_from_intraday([100.0]) == 0.0
    assert realised_vol_from_intraday([0.0, -1.0]) == 0.0


def test_fit_har_rv_returns_forecast():
    # Build a stable RV series with mild AR structure
    rng = np.random.default_rng(0)
    n = MIN_OBSERVATIONS + LAG_MONTHLY + 20
    rv = np.zeros(n)
    rv[0] = 0.02
    for t in range(1, n):
        rv[t] = max(0.001, 0.7 * rv[t - 1] + rng.normal(0.005, 0.003))
    res = fit_har_rv(rv.tolist(), symbol="X")
    assert res is not None
    assert res.forecast_vol > 0
    assert math.isfinite(res.beta_daily)
    assert math.isfinite(res.beta_weekly)
    assert math.isfinite(res.beta_monthly)
    assert 0.0 <= res.r_squared <= 1.0


def test_fit_har_rv_rejects_short_series():
    rv = [0.02] * (MIN_OBSERVATIONS + LAG_MONTHLY - 1)
    assert fit_har_rv(rv, symbol="X") is None


def test_daily_rv_series_from_candles_buckets_by_day():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = []
    for h in range(48):
        candles.append({"ts": base + timedelta(hours=h), "c": 100 + h * 0.1})
    series = daily_rv_series_from_candles(candles)
    assert len(series) == 2
    for day, rv in series:
        assert rv > 0


def test_daily_rv_series_from_daily_closes_proxy():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = [{"ts": base + timedelta(days=i), "c": 100 + i} for i in range(5)]
    series = daily_rv_series_from_daily_closes(candles)
    # First entry has no prev so 4 entries.
    assert len(series) == 4
    for _, rv in series:
        assert rv > 0
