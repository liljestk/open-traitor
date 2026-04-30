"""
HAR-RV — Heterogeneous Autoregressive model of Realized Volatility.

Forecasts next-day realised volatility from three components::

    RV(t+1) = β0 + β_d · RV_daily(t) + β_w · RV_weekly(t) + β_m · RV_monthly(t) + ε

where::

    RV_daily(t)   = realised vol over day t
    RV_weekly(t)  = mean RV over the trailing 5 days
    RV_monthly(t) = mean RV over the trailing 22 days

Realised vol is computed from intraday returns (preferred) or daily
log returns. The forecast is a **single-step-ahead** prediction; for
multi-step forecasts iterate the equation.

Pure numpy / stdlib. No statsmodels / arch dependency.

Used by:
    * Vol-target sizer (when ``RISK_USE_HAR_RV=1``).
    * Dashboard volatility forecast panel.
    * Strategist context (forward vol regime).

References
----------
Corsi (2009), "A Simple Approximate Long-Memory Model of Realized Volatility",
Journal of Financial Econometrics, 7(2), 174-196.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import numpy as np

from src.utils.logger import get_logger

logger = get_logger("analysis.har_rv")


# Lookback days for the three RV components.
LAG_DAILY: int = 1
LAG_WEEKLY: int = 5
LAG_MONTHLY: int = 22

# Minimum number of complete (RV(t+1), RV_d, RV_w, RV_m) tuples needed
# to fit the model. With LAG_MONTHLY=22 this works out to ~50 trading
# days = ~10 weeks. A short window keeps forecasts adaptive but noisy;
# 60+ is the sensible floor.
MIN_OBSERVATIONS: int = 60

# Forecast bounds — keep multiplier consumers safe even if the fit
# explodes (e.g. on near-constant series).
_FORECAST_FLOOR = 1e-8
_FORECAST_CEIL = 10.0  # 1000% daily vol cap — utterly absurd, intentionally so


@dataclass
class HARRVForecast:
    """Single-symbol HAR-RV forecast."""

    symbol: str
    forecast_vol: float          # one-step-ahead RV prediction
    realized_vol_daily: float    # most recent daily RV
    realized_vol_weekly: float   # most recent 5-day mean RV
    realized_vol_monthly: float  # most recent 22-day mean RV
    beta_daily: float
    beta_weekly: float
    beta_monthly: float
    intercept: float
    r_squared: float
    sample_count: int


# ─── Realised vol from candles ───────────────────────────────────────────


def realised_vol_from_intraday(closes: Sequence[float]) -> float:
    """Compute realised vol from a sequence of intraday closes.

    RV is the square root of the sum of squared log returns over the
    period. Returns 0.0 if fewer than 2 valid closes.
    """
    pxs = [float(p) for p in closes if p and float(p) > 0]
    if len(pxs) < 2:
        return 0.0
    rs = [math.log(pxs[i] / pxs[i - 1]) for i in range(1, len(pxs))]
    return float(math.sqrt(sum(r * r for r in rs)))


def daily_rv_series_from_candles(
    candles: Sequence[dict],
    *,
    granularity_seconds: int = 3600,
) -> list[tuple[datetime, float]]:
    """Aggregate intraday candles into per-day realised vol.

    Bucket by UTC midnight. Each bucket's RV uses every close in the
    day. Returns ascending list of ``(day, rv)``.
    """
    by_day: dict[datetime, list[float]] = {}
    for c in candles:
        ts = c.get("ts")
        close = c.get("c") or c.get("close")
        if ts is None or close is None:
            continue
        try:
            cf = float(close)
        except (TypeError, ValueError):
            continue
        if cf <= 0 or not isinstance(ts, datetime):
            continue
        day = ts.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        by_day.setdefault(day, []).append(cf)
    out: list[tuple[datetime, float]] = []
    for day in sorted(by_day.keys()):
        rv = realised_vol_from_intraday(by_day[day])
        if rv > 0:
            out.append((day, rv))
    return out


def daily_rv_series_from_daily_closes(
    candles: Sequence[dict],
) -> list[tuple[datetime, float]]:
    """Build a daily-RV series from daily-bar closes (proxy: |log return|).

    When intraday data is unavailable we fall back to absolute log
    returns as a proxy for daily realised vol. This is a known weak
    estimator — recommend intraday whenever possible.
    """
    series: list[tuple[datetime, float]] = []
    prev: Optional[float] = None
    for c in candles:
        ts = c.get("ts")
        close = c.get("c") or c.get("close")
        if ts is None or close is None or not isinstance(ts, datetime):
            continue
        try:
            cf = float(close)
        except (TypeError, ValueError):
            continue
        if cf <= 0:
            prev = None
            continue
        day = ts.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        if prev is not None and prev > 0:
            series.append((day, abs(math.log(cf / prev))))
        prev = cf
    return series


# ─── HAR-RV fitting ──────────────────────────────────────────────────────


def _build_design_matrix(
    rv_series: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    """Build (X, y) where::

        y[i]    = rv_series[t]
        X[i, 0] = 1
        X[i, 1] = rv_series[t-1]               (daily)
        X[i, 2] = mean(rv_series[t-5 : t])     (weekly)
        X[i, 3] = mean(rv_series[t-22 : t])    (monthly)

    over indices ``t = LAG_MONTHLY .. len(rv_series) - 1``.
    """
    n = len(rv_series)
    if n <= LAG_MONTHLY + 1:
        return np.empty((0, 0)), np.empty((0,))
    rows_X: list[list[float]] = []
    ys: list[float] = []
    for t in range(LAG_MONTHLY, n):
        daily = rv_series[t - 1]
        weekly = sum(rv_series[t - LAG_WEEKLY:t]) / LAG_WEEKLY
        monthly = sum(rv_series[t - LAG_MONTHLY:t]) / LAG_MONTHLY
        rows_X.append([1.0, daily, weekly, monthly])
        ys.append(rv_series[t])
    return np.array(rows_X, dtype=float), np.array(ys, dtype=float)


def fit_har_rv(
    rv_series: Sequence[float],
    *,
    symbol: str = "",
) -> Optional[HARRVForecast]:
    """Fit HAR-RV on a daily realised-vol series and return a forecast.

    Returns ``None`` if there is insufficient data or the design matrix
    is singular.
    """
    rv = [float(v) for v in rv_series if v is not None and math.isfinite(v) and v > 0]
    if len(rv) < MIN_OBSERVATIONS + LAG_MONTHLY:
        return None
    X, y = _build_design_matrix(rv)
    if X.size == 0 or X.shape[0] < MIN_OBSERVATIONS:
        return None
    try:
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coef)):
        return None
    intercept = float(coef[0])
    b_d = float(coef[1])
    b_w = float(coef[2])
    b_m = float(coef[3])
    yhat = X @ coef
    resid = y - yhat
    sse = float(np.sum(resid * resid))
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - (sse / sst) if sst > 0 else 0.0
    # Build the one-step-ahead forecast using the most recent observation.
    last_d = rv[-1]
    last_w = sum(rv[-LAG_WEEKLY:]) / LAG_WEEKLY
    last_m = sum(rv[-LAG_MONTHLY:]) / LAG_MONTHLY
    forecast = intercept + b_d * last_d + b_w * last_w + b_m * last_m
    forecast = max(_FORECAST_FLOOR, min(_FORECAST_CEIL, float(forecast)))
    return HARRVForecast(
        symbol=symbol,
        forecast_vol=forecast,
        realized_vol_daily=float(last_d),
        realized_vol_weekly=float(last_w),
        realized_vol_monthly=float(last_m),
        beta_daily=b_d,
        beta_weekly=b_w,
        beta_monthly=b_m,
        intercept=intercept,
        r_squared=float(max(0.0, min(1.0, r2))),
        sample_count=int(X.shape[0]),
    )


# ─── Top-level: compute forecasts for a universe ─────────────────────────


def compute_har_rv_forecasts(
    exchange: str,
    symbols: Sequence[str],
    *,
    stats_db,
    window_days: int = 180,
    granularity: str = "ONE_HOUR",
    end: Optional[datetime] = None,
) -> list[HARRVForecast]:
    """Compute HAR-RV forecasts for each symbol in ``symbols``.

    Tries intraday candles first (preferred), falls back to daily closes.
    """
    if not symbols:
        return []
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=int(window_days) + LAG_MONTHLY + 5)
    out: list[HARRVForecast] = []
    for sym in symbols:
        try:
            candles = stats_db.get_candles_range(
                exchange=exchange, symbol=sym, granularity=granularity,
                start=start, end=end,
            )
        except Exception as e:
            logger.debug(f"compute_har_rv_forecasts({sym}) intraday fetch failed: {e}")
            candles = []
        rv_pairs = daily_rv_series_from_candles(candles) if candles else []
        if len(rv_pairs) < MIN_OBSERVATIONS + LAG_MONTHLY:
            # Fall back to daily closes.
            try:
                d_candles = stats_db.get_candles_range(
                    exchange=exchange, symbol=sym, granularity="ONE_DAY",
                    start=start, end=end,
                )
            except Exception:
                d_candles = []
            rv_pairs = daily_rv_series_from_daily_closes(d_candles)
        if len(rv_pairs) < MIN_OBSERVATIONS + LAG_MONTHLY:
            continue
        rv_series = [v for _, v in rv_pairs]
        fc = fit_har_rv(rv_series, symbol=sym)
        if fc is not None:
            out.append(fc)
    return out


# ─── Persistence helper ──────────────────────────────────────────────────


def persist_har_rv_forecasts(
    stats_db,
    *,
    exchange: str,
    forecasts: Sequence[HARRVForecast],
    horizon_days: int = 1,
) -> int:
    if not forecasts:
        return 0
    rows = [{
        "exchange": exchange,
        "symbol": f.symbol,
        "horizon_days": horizon_days,
        "forecast_vol": f.forecast_vol,
        "realized_vol_daily": f.realized_vol_daily,
        "realized_vol_weekly": f.realized_vol_weekly,
        "realized_vol_monthly": f.realized_vol_monthly,
        "beta_daily": f.beta_daily,
        "beta_weekly": f.beta_weekly,
        "beta_monthly": f.beta_monthly,
        "intercept": f.intercept,
        "model_r_squared": f.r_squared,
        "sample_count": f.sample_count,
    } for f in forecasts]
    return stats_db.upsert_har_rv_forecasts(rows)


__all__ = [
    "LAG_DAILY", "LAG_WEEKLY", "LAG_MONTHLY", "MIN_OBSERVATIONS",
    "HARRVForecast",
    "realised_vol_from_intraday",
    "daily_rv_series_from_candles",
    "daily_rv_series_from_daily_closes",
    "fit_har_rv",
    "compute_har_rv_forecasts",
    "persist_har_rv_forecasts",
]
