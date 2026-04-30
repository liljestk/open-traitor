"""
Granger causality F-test (pure numpy).

Tests whether past values of ``x`` help predict ``y`` *beyond* what
``y``'s own past already provides. The null hypothesis::

    H0: x does not Granger-cause y
        i.e. coefficients on lagged x are jointly zero.

Test statistic::

    F = ((SSR_restricted − SSR_full) / m) / (SSR_full / (n − 2m − 1))

with degrees of freedom (m, n − 2m − 1) under the null. We compute a
p-value from the F-distribution survival function via a series-based
approximation that avoids scipy.

Used by:
    * Lead-lag pipeline — promote OLS edges to "Granger-significant"
      when p < 0.05.
    * Dashboard — display causality strength & p-values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from src.utils.logger import get_logger

logger = get_logger("analysis.granger")


MIN_OBSERVATIONS_AFTER_LAGS: int = 30


@dataclass
class GrangerResult:
    """Granger causality test result for x → y at one lag."""

    leader: str
    follower: str
    lag: int
    f_stat: float
    p_value: float
    n: int
    significant: bool   # convenience: p_value < 0.05


# ─── F-distribution p-value (series approximation) ───────────────────────


def _log_beta(a: float, b: float) -> float:
    """log(B(a, b)) = lgamma(a) + lgamma(b) − lgamma(a + b)."""
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betainc_regularized(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b).

    Uses the continued-fraction expansion (Numerical Recipes §6.4),
    valid for 0 ≤ x ≤ 1, a > 0, b > 0. Pure-Python; sufficient
    precision (≈1e-10) for p-values in the relevant range.
    """
    if x < 0.0 or x > 1.0 or a <= 0 or b <= 0:
        return float("nan")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    # Use symmetry to keep CF in its convergent regime.
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _betainc_regularized(b, a, 1.0 - x)
    log_bt = (
        a * math.log(x) + b * math.log(1.0 - x)
        - math.log(a) - _log_beta(a, b)
    )
    bt = math.exp(log_bt)
    # Continued fraction (Lentz's method).
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    fpmin = 1e-300
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        # Even step.
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        # Odd step.
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return bt * h


def f_sf(f: float, dfn: int, dfd: int) -> float:
    """Survival function of the F distribution (1 − CDF).

    P(F > f) for F ~ F(dfn, dfd). Returns NaN on invalid input.
    """
    if dfn <= 0 or dfd <= 0 or not math.isfinite(f):
        return float("nan")
    if f <= 0.0:
        return 1.0
    # Standard relation: P(F > f) = I_{dfd/(dfd + dfn·f)}(dfd/2, dfn/2)
    x = dfd / (dfd + dfn * f)
    return _betainc_regularized(dfd / 2.0, dfn / 2.0, x)


# ─── OLS helper ──────────────────────────────────────────────────────────


def _ols_sse(X: np.ndarray, y: np.ndarray) -> Optional[float]:
    """Return SSE (sum of squared residuals) of the OLS fit y ~ X."""
    if X.shape[0] < X.shape[1]:
        return None
    try:
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coef)):
        return None
    resid = y - X @ coef
    return float(np.sum(resid * resid))


# ─── Granger causality ───────────────────────────────────────────────────


def granger_causality(
    leader: Sequence[float],
    follower: Sequence[float],
    *,
    lag: int,
    leader_name: str = "x",
    follower_name: str = "y",
) -> Optional[GrangerResult]:
    """Test whether ``leader`` Granger-causes ``follower`` at lag ``lag``.

    Compares::

        Restricted: y_t = α + Σ_{i=1..lag} a_i · y_{t-i}
        Full:       y_t = α + Σ a_i y_{t-i} + Σ b_i x_{t-i}

    Both series must have the same length ≥ ``2·lag + MIN_OBSERVATIONS_AFTER_LAGS``.

    Returns ``None`` if data is insufficient or the regression is degenerate.
    """
    if lag < 1:
        return None
    x = np.asarray([float(v) for v in leader], dtype=float)
    y = np.asarray([float(v) for v in follower], dtype=float)
    if x.shape != y.shape:
        return None
    n_total = x.shape[0]
    n_eff = n_total - lag
    if n_eff < MIN_OBSERVATIONS_AFTER_LAGS + (2 * lag + 1):
        return None
    # Build lag matrices.
    Y = y[lag:]
    rows = n_eff
    Y_lags = np.zeros((rows, lag))
    X_lags = np.zeros((rows, lag))
    for i in range(1, lag + 1):
        Y_lags[:, i - 1] = y[lag - i: n_total - i]
        X_lags[:, i - 1] = x[lag - i: n_total - i]
    intercept = np.ones((rows, 1))
    Xr = np.hstack([intercept, Y_lags])              # restricted
    Xf = np.hstack([intercept, Y_lags, X_lags])      # full
    sse_r = _ols_sse(Xr, Y)
    sse_f = _ols_sse(Xf, Y)
    if sse_r is None or sse_f is None or sse_f <= 0:
        return None
    # Numerator & denominator dfs.
    df_num = lag
    df_den = rows - (2 * lag + 1)
    if df_den <= 0:
        return None
    numerator = (sse_r - sse_f) / df_num
    denominator = sse_f / df_den
    if denominator <= 0 or not math.isfinite(denominator):
        return None
    f_stat = float(numerator / denominator)
    if not math.isfinite(f_stat) or f_stat < 0:
        f_stat = 0.0
    p_val = f_sf(f_stat, df_num, df_den)
    if not math.isfinite(p_val):
        p_val = 1.0
    return GrangerResult(
        leader=leader_name,
        follower=follower_name,
        lag=lag,
        f_stat=f_stat,
        p_value=float(p_val),
        n=rows,
        significant=p_val < 0.05,
    )


# ─── Universe scan ───────────────────────────────────────────────────────


def scan_granger_universe(
    return_series: dict[str, list[float]],
    *,
    lags: Sequence[int] = (1, 2, 4, 12, 24),
    significance: float = 0.05,
) -> list[GrangerResult]:
    """Scan every ordered pair (leader, follower) over the given lags.

    Caller supplies aligned return series (same length, same time grid,
    e.g. hourly returns over a fixed window). Pairs/lags below the
    minimum sample threshold are skipped.

    Returns the full list of significant results sorted by ascending
    p-value.
    """
    out: list[GrangerResult] = []
    symbols = sorted(return_series.keys())
    for leader in symbols:
        for follower in symbols:
            if leader == follower:
                continue
            x = return_series[leader]
            y = return_series[follower]
            if len(x) != len(y):
                continue
            for lag in lags:
                res = granger_causality(
                    x, y, lag=lag,
                    leader_name=leader, follower_name=follower,
                )
                if res is None:
                    continue
                if res.p_value <= significance:
                    out.append(res)
    out.sort(key=lambda r: r.p_value)
    return out


def persist_granger_results(
    stats_db,
    *,
    exchange: str,
    results: Sequence[GrangerResult],
) -> int:
    if not results:
        return 0
    rows = [{
        "exchange": exchange,
        "leader": r.leader,
        "follower": r.follower,
        "lag_hours": r.lag,
        "f_stat": r.f_stat,
        "p_value": r.p_value,
        "sample_count": r.n,
    } for r in results]
    return stats_db.upsert_granger_results(rows)


__all__ = [
    "GrangerResult",
    "f_sf",
    "granger_causality",
    "scan_granger_universe",
    "persist_granger_results",
]
