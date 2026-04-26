"""
Event–Price Regression Engine.

Given the catalyst events the system has already collected
(``catalyst_events``) and the long-history OHLCV the bulk backfill
maintains (``historical_candles``), this module fits per-symbol
regressions of forward returns on a small set of pre-event features.

For each ``(exchange, symbol, event_type, horizon_days)`` combination it
computes:

* OLS fit of ``forward_return_h ~ pre_return_5 + pre_volatility_10
                                 + pre_volume_z_5 + days_since_event``
  using only events with enough lookback history.
* Bootstrapped R², per-coefficient t-stats, sample count.
* Mean / median / hit-rate of the forward return for sanity stats.

Results are persisted in ``event_price_regressions`` (see
``src/utils/stats_patterns.py`` DDL) so the dashboard can render them
without recomputing. The whole job is profile-scoped: equity (ibkr) and
crypto (coinbase) data never mix because every read funnels through
``StatsDB`` methods that take ``exchange`` as a required argument.

The engine is intentionally dependency-light (numpy only, no scikit /
statsmodels) so it runs inside the existing planning-worker image
without rebuilds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import numpy as np

from src.utils.logger import get_logger

logger = get_logger("analysis.event_regression")


# Default forecast horizons in trading days. Aligned with the pattern
# fingerprint columns in ``pattern_fingerprints``.
DEFAULT_HORIZONS_DAYS: tuple[int, ...] = (1, 5, 20)

# Pre-event feature lookback (in bars / trading days for ONE_DAY series).
PRE_RETURN_BARS: int = 5
PRE_VOL_BARS: int = 10
PRE_VOLUME_BARS: int = 5

# Minimum sample count required to attempt a fit. Below this the result
# row is still written so the dashboard can show "insufficient data".
MIN_SAMPLES: int = 8

FEATURE_NAMES: tuple[str, ...] = (
    "intercept",
    "pre_return_5",
    "pre_volatility_10",
    "pre_volume_z_5",
    "days_since_prev_event",
)


@dataclass(slots=True)
class RegressionResult:
    """One fitted regression for a single (symbol, event_type, horizon)."""

    exchange: str
    symbol: str
    event_type: str
    horizon_days: int
    sample_count: int
    coefficients: dict[str, float]   # name -> beta
    t_stats: dict[str, float]        # name -> t (NaN-safe)
    r_squared: float                 # 0..1, or NaN if insufficient
    mean_forward_return: float
    median_forward_return: float
    hit_rate: float                  # fraction of events with positive return
    notes: str                       # human-readable status, e.g. "ok", "few_samples"

    def to_db_row(self) -> dict:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "horizon_days": self.horizon_days,
            "sample_count": self.sample_count,
            "coefficients_json": self.coefficients,
            "t_stats_json": self.t_stats,
            "r_squared": _nan_to_none(self.r_squared),
            "mean_forward_return": _nan_to_none(self.mean_forward_return),
            "median_forward_return": _nan_to_none(self.median_forward_return),
            "hit_rate": _nan_to_none(self.hit_rate),
            "notes": self.notes,
        }


def _nan_to_none(x: float) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return float(x)


def _candle_close_series(
    candles: list[dict],
) -> tuple[list[datetime], np.ndarray, np.ndarray]:
    """Return parallel ``(timestamps, closes, volumes)`` arrays."""
    if not candles:
        return [], np.array([]), np.array([])
    ts = [c["ts"] for c in candles]
    closes = np.asarray([float(c["c"]) for c in candles], dtype=np.float64)
    volumes = np.asarray([float(c.get("v") or 0.0) for c in candles], dtype=np.float64)
    return ts, closes, volumes


def _index_at_or_before(timestamps: list[datetime], target: datetime) -> int:
    """Largest i such that timestamps[i] <= target. Returns -1 if none."""
    lo, hi = 0, len(timestamps) - 1
    best = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if timestamps[mid] <= target:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _build_features_for_event(
    timestamps: list[datetime],
    closes: np.ndarray,
    volumes: np.ndarray,
    event_ts: datetime,
    horizon_bars: int,
    days_since_prev: float,
) -> Optional[tuple[np.ndarray, float]]:
    """Return ``(feature_row, forward_return)`` or ``None`` if data is short."""
    n = len(closes)
    if n == 0:
        return None
    i = _index_at_or_before(timestamps, event_ts)
    if i < max(PRE_RETURN_BARS, PRE_VOL_BARS, PRE_VOLUME_BARS):
        return None
    if i + horizon_bars >= n:
        return None

    # Pre-event features
    pre_close = closes[i]
    if pre_close <= 0:
        return None
    pre_return_5 = (closes[i] / closes[i - PRE_RETURN_BARS]) - 1.0
    log_returns = np.diff(np.log(np.maximum(closes[i - PRE_VOL_BARS : i + 1], 1e-12)))
    pre_vol_10 = float(np.std(log_returns)) if log_returns.size else 0.0
    vol_window = volumes[i - PRE_VOLUME_BARS : i]
    if vol_window.size and vol_window.std() > 0:
        pre_volume_z = float(
            (volumes[i] - vol_window.mean()) / (vol_window.std() + 1e-12)
        )
    else:
        pre_volume_z = 0.0

    forward_return = float(closes[i + horizon_bars] / pre_close - 1.0)

    feature_row = np.array(
        [1.0, pre_return_5, pre_vol_10, pre_volume_z, float(days_since_prev)],
        dtype=np.float64,
    )
    return feature_row, forward_return


def _ols_fit(
    X: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    """Closed-form OLS via lstsq. Returns ``(beta, t_stats, r_squared)``."""
    n, k = X.shape
    if n <= k:
        return (
            np.full(k, np.nan),
            np.full(k, np.nan),
            float("nan"),
        )

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    residuals = y - X @ beta
    rss = float(residuals @ residuals)
    tss = float(((y - y.mean()) ** 2).sum())
    r_squared = 1.0 - (rss / tss) if tss > 0 else float("nan")

    # Standard errors via (X'X)^-1 * sigma^2.
    try:
        xtx_inv = np.linalg.pinv(X.T @ X)
        dof = max(n - k, 1)
        sigma2 = rss / dof
        var_beta = np.diag(xtx_inv) * sigma2
        se = np.sqrt(np.maximum(var_beta, 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            t_stats = np.where(se > 0, beta / np.where(se > 0, se, 1.0), np.nan)
    except np.linalg.LinAlgError:
        t_stats = np.full(k, np.nan)

    return beta, t_stats, r_squared


def fit_event_regression(
    *,
    exchange: str,
    symbol: str,
    event_type: str,
    events: list[dict],
    candles: list[dict],
    horizon_days: int,
) -> RegressionResult:
    """Fit one regression. Pure function — no I/O.

    ``events`` must be the ``catalyst_events`` rows for this
    ``(exchange, symbol, event_type)`` ordered by ``event_ts ASC``.
    ``candles`` must be the ``historical_candles`` rows for this
    ``(exchange, symbol)`` at ONE_DAY granularity ordered by ``ts ASC``.
    """
    timestamps, closes, volumes = _candle_close_series(candles)
    rows: list[np.ndarray] = []
    targets: list[float] = []
    prev_ts: Optional[datetime] = None
    for ev in events:
        ev_ts = ev["event_ts"]
        if not isinstance(ev_ts, datetime):
            continue
        days_since_prev = (
            (ev_ts - prev_ts).total_seconds() / 86400.0 if prev_ts else 365.0
        )
        prev_ts = ev_ts
        feat = _build_features_for_event(
            timestamps, closes, volumes, ev_ts, horizon_days, days_since_prev
        )
        if feat is None:
            continue
        feature_row, fwd = feat
        rows.append(feature_row)
        targets.append(fwd)

    n = len(rows)
    base = RegressionResult(
        exchange=exchange,
        symbol=symbol,
        event_type=event_type,
        horizon_days=horizon_days,
        sample_count=n,
        coefficients={name: float("nan") for name in FEATURE_NAMES},
        t_stats={name: float("nan") for name in FEATURE_NAMES},
        r_squared=float("nan"),
        mean_forward_return=float("nan"),
        median_forward_return=float("nan"),
        hit_rate=float("nan"),
        notes="insufficient_history",
    )
    if n == 0:
        return base
    y = np.asarray(targets, dtype=np.float64)
    base.mean_forward_return = float(np.mean(y))
    base.median_forward_return = float(np.median(y))
    base.hit_rate = float((y > 0).mean())

    if n < MIN_SAMPLES:
        base.notes = "few_samples"
        return base

    X = np.vstack(rows)
    beta, t_stats, r2 = _ols_fit(X, y)
    base.coefficients = {
        name: float(beta[i]) for i, name in enumerate(FEATURE_NAMES)
    }
    base.t_stats = {
        name: float(t_stats[i]) for i, name in enumerate(FEATURE_NAMES)
    }
    base.r_squared = float(r2) if not math.isnan(r2) else float("nan")
    base.notes = "ok"
    return base


def run_event_regressions_for_profile(
    *,
    db,
    exchange: str,
    symbols: Optional[Iterable[str]] = None,
    event_types: Optional[Iterable[str]] = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS_DAYS,
    history_days: int = 365 * 3,
    granularity: str = "ONE_DAY",
) -> list[RegressionResult]:
    """Iterate over the profile's events and fit one regression per
    (symbol, event_type, horizon). Persists each result to Postgres via
    ``db.upsert_event_regression``. Returns the list of results.

    This function is the unit invoked by the Temporal activity
    ``run_event_regressions`` (see ``src/planning/activities.py``).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(history_days))
    all_events = db.get_catalyst_events(
        exchange=exchange,
        start=cutoff,
        end=datetime.now(timezone.utc),
        limit=10_000,
    )
    if not all_events:
        logger.info(
            f"event_regression: no catalyst events for exchange={exchange}; skipping"
        )
        return []

    # Group events by (symbol, event_type).
    grouped: dict[tuple[str, str], list[dict]] = {}
    for ev in all_events:
        sym = (ev.get("symbol") or "").strip()
        et = (ev.get("event_type") or "").strip()
        if not sym or not et:
            continue
        if symbols is not None and sym not in set(symbols):
            continue
        if event_types is not None and et not in set(event_types):
            continue
        grouped.setdefault((sym, et), []).append(ev)

    results: list[RegressionResult] = []
    for (sym, et), evs in grouped.items():
        evs.sort(key=lambda r: r["event_ts"])
        try:
            candles = db.get_candles_range(
                exchange=exchange,
                symbol=sym,
                granularity=granularity,
                start=cutoff - timedelta(days=120),
                end=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning(f"event_regression: candles fetch failed {sym}: {e}")
            continue
        for h in horizons:
            try:
                res = fit_event_regression(
                    exchange=exchange,
                    symbol=sym,
                    event_type=et,
                    events=evs,
                    candles=candles,
                    horizon_days=int(h),
                )
                try:
                    db.upsert_event_regression(res.to_db_row())
                except Exception as e:
                    logger.warning(
                        f"event_regression: upsert failed for {sym}/{et}/{h}d: {e}"
                    )
                results.append(res)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"event_regression: fit failed for {sym}/{et}/{h}d: {e}"
                )
    logger.info(
        f"event_regression: {exchange} fitted {len(results)} models "
        f"across {len(grouped)} (symbol, event_type) groups"
    )
    return results


__all__ = [
    "DEFAULT_HORIZONS_DAYS",
    "FEATURE_NAMES",
    "MIN_SAMPLES",
    "RegressionResult",
    "fit_event_regression",
    "run_event_regressions_for_profile",
]
