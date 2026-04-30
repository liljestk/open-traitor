"""
Multi-factor regression: market beta to macro factors.

For each tradeable symbol in the active universe, fit::

    r_asset(t) = α + Σ_k β_k · r_factor_k(t) + ε(t)

over a rolling daily window. Factors are configurable but default to
(SPX, VIX, DXY, BTC). Outputs per-factor ``beta``, ``t_stat``, model
``r_squared``, residual ``alpha`` (annualised), and ``idio_vol`` (the
stdev of residuals — the asset's *systematic-risk-stripped* volatility).

Pure numpy. Reads daily candles via ``StatsDB.get_candles_range``.
Persistence helper writes one row per (symbol, factor) pair into
``market_factor_loadings``.

Used by:
    * Risk manager — hedge sizing hint via SPX/BTC beta.
    * Strategist  — alpha screen (rank residual α / idio_vol).
    * Dashboard   — factor-loading heatmap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

import numpy as np

from src.utils.logger import get_logger

logger = get_logger("analysis.market_factors")


# Default factor universe. Yahoo Finance tickers — fetched via existing
# ``YahooChartSource`` for equity profile, or skipped (with no-op) for
# crypto-only universes. Caller can override via ``factors=``.
DEFAULT_FACTORS: tuple[str, ...] = ("^GSPC", "^VIX", "DX-Y.NYB", "BTC-USD")

# Minimum overlapping daily observations to publish a loading row.
MIN_OBSERVATIONS: int = 60

# Annualisation factor for daily series (252 trading days for equities,
# 365 for crypto). We use 252 by default — for crypto-only universes the
# absolute alpha will be slightly under-annualised but the *relative*
# ranking (the only thing the strategist uses) is unaffected.
TRADING_DAYS_PER_YEAR: int = 252


@dataclass
class FactorLoading:
    """One row of the per-symbol factor regression."""

    symbol: str
    factor: str
    beta: float
    t_stat: float
    sample_count: int


@dataclass
class FactorRegressionResult:
    """Full regression result for one symbol."""

    symbol: str
    sample_count: int
    r_squared: float
    alpha_annualised: float
    idio_vol: float
    loadings: list[FactorLoading] = field(default_factory=list)


# ─── Daily-return loading ────────────────────────────────────────────────


def _load_daily_returns(
    exchange: str,
    symbol: str,
    *,
    stats_db,
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    """Return ``{day_at_utc_midnight: log_return}`` for ``symbol``.

    Mirrors the convention used by ``cross_asset._load_daily_returns`` so
    series align across all analysis modules.
    """
    try:
        candles = stats_db.get_candles_range(
            exchange=exchange,
            symbol=symbol,
            granularity="ONE_DAY",
            start=start,
            end=end,
        )
    except Exception as e:
        logger.debug(f"market_factors._load_daily_returns({exchange},{symbol}) failed: {e}")
        return {}
    out: dict[datetime, float] = {}
    prev: Optional[float] = None
    for c in candles:
        ts = c.get("ts")
        close = c.get("c") or c.get("close")
        if ts is None or close is None:
            continue
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            prev = None
            continue
        if isinstance(ts, datetime):
            day = ts.astimezone(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        else:
            continue
        if prev is not None and prev > 0:
            out[day] = math.log(close / prev)
        prev = close
    return out


# ─── OLS core ────────────────────────────────────────────────────────────


def fit_multifactor(
    asset_returns: np.ndarray,
    factor_returns: np.ndarray,
    *,
    factor_names: Sequence[str],
    symbol: str,
) -> Optional[FactorRegressionResult]:
    """Fit ``r_asset = α + Σ β_k r_factor_k + ε`` by OLS.

    Parameters
    ----------
    asset_returns : (n,) array of asset daily log-returns.
    factor_returns : (n, k) array of factor daily log-returns.
    factor_names : length-k sequence of factor labels.
    symbol : symbol label for the result row.

    Returns
    -------
    ``FactorRegressionResult`` or ``None`` if the system is degenerate
    (singular design matrix, fewer than ``MIN_OBSERVATIONS`` samples,
    or zero residual variance).
    """
    if asset_returns.ndim != 1 or factor_returns.ndim != 2:
        return None
    n, k = factor_returns.shape
    if n != asset_returns.shape[0] or n < MIN_OBSERVATIONS or k == 0:
        return None
    if len(factor_names) != k:
        return None
    # Drop NaNs row-wise.
    mask = np.isfinite(asset_returns) & np.all(np.isfinite(factor_returns), axis=1)
    if int(mask.sum()) < MIN_OBSERVATIONS:
        return None
    y = asset_returns[mask]
    X_factors = factor_returns[mask]
    # Design matrix with intercept column.
    X = np.column_stack([np.ones(y.shape[0]), X_factors])
    try:
        # Normal equations via lstsq for stability.
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coef)):
        return None
    alpha = float(coef[0])
    betas = coef[1:]
    yhat = X @ coef
    resid = y - yhat
    sse = float(np.sum(resid * resid))
    if sse <= 0:
        return None
    n_eff = y.shape[0]
    p = X.shape[1]  # includes intercept
    if n_eff <= p:
        return None
    sigma2 = sse / (n_eff - p)
    # Coefficient covariance: σ² · (XᵀX)⁻¹
    try:
        xtx_inv = np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return None
    se = np.sqrt(np.maximum(sigma2 * np.diag(xtx_inv), 0.0))
    se_betas = se[1:]
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - (sse / sst) if sst > 0 else 0.0
    idio_vol = float(np.sqrt(sigma2))  # daily idiosyncratic stdev
    alpha_ann = alpha * TRADING_DAYS_PER_YEAR
    loadings: list[FactorLoading] = []
    for i, name in enumerate(factor_names):
        s = float(se_betas[i]) if i < len(se_betas) else 0.0
        b = float(betas[i])
        t = b / s if s > 0 else 0.0
        if not math.isfinite(t):
            t = 0.0
        loadings.append(FactorLoading(
            symbol=symbol, factor=name, beta=b, t_stat=t, sample_count=n_eff,
        ))
    return FactorRegressionResult(
        symbol=symbol,
        sample_count=n_eff,
        r_squared=float(max(0.0, min(1.0, r2))),
        alpha_annualised=float(alpha_ann),
        idio_vol=idio_vol,
        loadings=loadings,
    )


# ─── Top-level: compute factor loadings for a universe ───────────────────


def compute_factor_loadings(
    exchange: str,
    symbols: Sequence[str],
    *,
    stats_db,
    factors: Sequence[str] = DEFAULT_FACTORS,
    factor_exchange: Optional[str] = None,
    window_days: int = 252,
    end: Optional[datetime] = None,
) -> list[FactorRegressionResult]:
    """Fit a multi-factor regression for each symbol against ``factors``.

    Parameters
    ----------
    exchange : where the *target* candles live (e.g. ``"coinbase"``).
    symbols : tradeable universe.
    factors : factor tickers (e.g. ``("^GSPC", "^VIX")``).
    factor_exchange : where the *factor* candles live; defaults to
        the same as ``exchange``. For mixed-domain runs the caller
        supplies an external exchange (e.g. ``"yahoo"``) — but in this
        codebase we only persist factor candles into the same exchange
        store as the asset universe.
    window_days : daily lookback window.
    end : reference date (default: now UTC).

    Returns
    -------
    List of ``FactorRegressionResult`` — one per symbol where data was
    sufficient. Symbols with insufficient data are silently skipped.
    """
    if not symbols or not factors:
        return []
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=int(window_days) + 5)
    factor_exchange = factor_exchange or exchange

    factor_series: dict[str, dict[datetime, float]] = {}
    for f in factors:
        s = _load_daily_returns(
            factor_exchange, f, stats_db=stats_db, start=start, end=end,
        )
        if len(s) >= MIN_OBSERVATIONS:
            factor_series[f] = s
    if not factor_series:
        logger.info(
            "compute_factor_loadings: no factor data available "
            f"({factor_exchange}, {factors}); skipping."
        )
        return []
    factor_names = list(factor_series.keys())

    results: list[FactorRegressionResult] = []
    for sym in symbols:
        if sym in factors:
            # Don't regress a factor on itself.
            continue
        ar = _load_daily_returns(
            exchange, sym, stats_db=stats_db, start=start, end=end,
        )
        if len(ar) < MIN_OBSERVATIONS:
            continue
        # Align dates across asset + all factors.
        common_days = set(ar.keys())
        for fs in factor_series.values():
            common_days &= fs.keys()
        if len(common_days) < MIN_OBSERVATIONS:
            continue
        ordered = sorted(common_days)
        y = np.array([ar[d] for d in ordered], dtype=float)
        X = np.column_stack([
            [factor_series[f][d] for d in ordered] for f in factor_names
        ])
        res = fit_multifactor(y, X, factor_names=factor_names, symbol=sym)
        if res is not None:
            results.append(res)
    return results


# ─── Persistence helper ───────────────────────────────────────────────────


def persist_factor_loadings(
    stats_db,
    *,
    exchange: str,
    results: Iterable[FactorRegressionResult],
) -> int:
    """Flatten the results into per-(symbol,factor) rows and upsert."""
    rows: list[dict] = []
    for r in results:
        for ld in r.loadings:
            rows.append({
                "exchange": exchange,
                "symbol": r.symbol,
                "factor": ld.factor,
                "beta": ld.beta,
                "t_stat": ld.t_stat,
                "r_squared": r.r_squared,
                "alpha_annualised": r.alpha_annualised,
                "idio_vol": r.idio_vol,
                "sample_count": ld.sample_count,
            })
    if not rows:
        return 0
    return stats_db.upsert_market_factor_loadings(rows)


__all__ = [
    "DEFAULT_FACTORS",
    "MIN_OBSERVATIONS",
    "FactorLoading",
    "FactorRegressionResult",
    "fit_multifactor",
    "compute_factor_loadings",
    "persist_factor_loadings",
]
