"""Unit tests for the event–price regression engine.

Tests the pure-function fitting path with synthetic candle/event data
so they run without a database. Persistence wiring is exercised
indirectly via the StatsDB schema test in test_domain_separation.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.analysis.event_regression import (
    DEFAULT_HORIZONS_DAYS,
    FEATURE_NAMES,
    MIN_SAMPLES,
    fit_event_regression,
)


def _make_candles(n: int = 200, seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    base_ts = datetime(2023, 1, 1, tzinfo=timezone.utc)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=n)))
    vols = rng.lognormal(mean=10.0, sigma=0.3, size=n)
    return [
        {
            "ts": base_ts + timedelta(days=i),
            "o": float(closes[i]),
            "h": float(closes[i] * 1.01),
            "l": float(closes[i] * 0.99),
            "c": float(closes[i]),
            "v": float(vols[i]),
        }
        for i in range(n)
    ]


def _make_events(candles: list[dict], every: int = 12) -> list[dict]:
    """One event every ``every`` bars, well inside the candle range."""
    return [
        {
            "event_id": f"ev-{i}",
            "exchange": "ibkr",
            "symbol": "TEST",
            "event_type": "earnings",
            "event_ts": candles[i]["ts"],
        }
        for i in range(20, len(candles) - 30, every)
    ]


def test_fit_returns_well_formed_result_with_enough_samples():
    candles = _make_candles()
    events = _make_events(candles, every=8)
    res = fit_event_regression(
        exchange="ibkr",
        symbol="TEST",
        event_type="earnings",
        events=events,
        candles=candles,
        horizon_days=5,
    )
    assert res.exchange == "ibkr"
    assert res.symbol == "TEST"
    assert res.event_type == "earnings"
    assert res.horizon_days == 5
    assert res.sample_count >= MIN_SAMPLES
    assert set(res.coefficients) == set(FEATURE_NAMES)
    assert set(res.t_stats) == set(FEATURE_NAMES)
    assert res.notes == "ok"
    # R² is finite and in [-inf, 1]; for noise it should be small but not NaN.
    assert res.r_squared is None or res.r_squared <= 1.0
    # Hit-rate is a probability.
    assert 0.0 <= res.hit_rate <= 1.0


def test_fit_with_few_samples_marks_notes():
    candles = _make_candles()
    events = _make_events(candles, every=80)  # → very few events
    res = fit_event_regression(
        exchange="ibkr",
        symbol="TEST",
        event_type="earnings",
        events=events,
        candles=candles,
        horizon_days=5,
    )
    assert res.sample_count < MIN_SAMPLES
    assert res.notes in {"few_samples", "insufficient_history"}


def test_fit_with_no_candles_returns_insufficient():
    res = fit_event_regression(
        exchange="coinbase",
        symbol="BTC-USD",
        event_type="halving",
        events=[],
        candles=[],
        horizon_days=1,
    )
    assert res.sample_count == 0
    assert res.notes == "insufficient_history"
    assert res.to_db_row()["r_squared"] is None


def test_to_db_row_round_trips_dicts():
    candles = _make_candles()
    events = _make_events(candles, every=6)
    res = fit_event_regression(
        exchange="ibkr",
        symbol="TEST",
        event_type="earnings",
        events=events,
        candles=candles,
        horizon_days=1,
    )
    row = res.to_db_row()
    assert row["exchange"] == "ibkr"
    assert row["horizon_days"] == 1
    assert isinstance(row["coefficients_json"], dict)
    assert isinstance(row["t_stats_json"], dict)
    assert "intercept" in row["coefficients_json"]


def test_signal_recovery_when_pre_return_predicts_forward():
    """If forward returns mechanically follow pre-event returns,
    the OLS β on ``pre_return_5`` should be positive and large."""
    base_ts = datetime(2023, 1, 1, tzinfo=timezone.utc)
    rng = np.random.default_rng(42)
    n = 400
    closes = np.zeros(n)
    closes[0] = 100.0
    for i in range(1, n):
        closes[i] = closes[i - 1] * (1 + rng.normal(0, 0.005))
    candles = [
        {
            "ts": base_ts + timedelta(days=i),
            "o": closes[i], "h": closes[i] * 1.01, "l": closes[i] * 0.99,
            "c": closes[i], "v": 1000.0,
        }
        for i in range(n)
    ]
    # Inject a forward-bias: at each event, force the next 5 closes to drift
    # in the same direction as the prior 5-bar return.
    events = []
    for idx in range(30, n - 20, 6):
        prior = closes[idx] / closes[idx - 5] - 1.0
        # Apply scaled drift to the next 5 bars.
        bias = prior * 2.0
        for j in range(1, 6):
            closes[idx + j] = closes[idx + j - 1] * (1 + bias / 5.0)
        candles[idx + j]["c"] = closes[idx + j]
        events.append({
            "event_id": f"ev-{idx}",
            "exchange": "ibkr",
            "symbol": "TEST",
            "event_type": "earnings",
            "event_ts": candles[idx]["ts"],
        })
    # Re-sync close prices into candles
    for i in range(n):
        candles[i]["c"] = float(closes[i])

    res = fit_event_regression(
        exchange="ibkr",
        symbol="TEST",
        event_type="earnings",
        events=events,
        candles=candles,
        horizon_days=5,
    )
    assert res.notes == "ok"
    beta_pre = res.coefficients["pre_return_5"]
    # The mechanical bias makes pre_return_5 a strong positive predictor.
    assert beta_pre > 0.5, f"expected positive β, got {beta_pre}"


def test_default_horizons_are_sensible():
    assert DEFAULT_HORIZONS_DAYS == (1, 5, 20)
