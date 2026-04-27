"""
Lead-lag matrix computation.

Nightly OLS regression of asset-B's hourly return on lagged asset-A returns.
Sparse pair-wise: for each (leader, follower) pair within the active universe
and at lags ∈ {1, 2, 4, 12, 24} hours, fit::

    r_follower(t) = α + β · r_leader(t − lag) + ε

Persist (β, t-stat, R², n) into ``lead_lag_matrix`` for any cell with
|t_stat| ≥ 2.0 to keep the table compact.

Designed to run from a Temporal activity at low frequency (nightly).
Pure stdlib + reads from ``historical_candles``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from src.utils.logger import get_logger

logger = get_logger("lead_lag")

LAGS_HOURS: tuple[int, ...] = (1, 2, 4, 12, 24)
DEFAULT_T_STAT_THRESHOLD = 2.0
DEFAULT_LOOKBACK_DAYS = 90
MIN_SAMPLES = 200


def _hourly_returns(candles: list[dict]) -> list[tuple[datetime, float]]:
    """Convert OHLCV rows into (ts, log-return) sorted ascending."""
    out: list[tuple[datetime, float]] = []
    prev = None
    for c in candles:
        ts = c.get("ts")
        px = c.get("c") or c.get("close")
        if ts is None or px is None:
            continue
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            prev = None
            continue
        if prev is not None and prev > 0:
            out.append((ts, math.log(px / prev)))
        prev = px
    return out


def _ols(x: list[float], y: list[float]) -> dict | None:
    """OLS y = α + β x. Returns dict with β, t_stat, r_squared, n."""
    n = len(x)
    if n < 30 or n != len(y):
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    if sxx <= 0:
        return None
    beta = sxy / sxx
    alpha = my - beta * mx
    # residuals & SE
    resid = [yi - (alpha + beta * xi) for xi, yi in zip(x, y)]
    sse = sum(r * r for r in resid)
    if sse <= 0 or n <= 2:
        return None
    sigma2 = sse / (n - 2)
    se_beta = math.sqrt(sigma2 / sxx) if sxx > 0 else 0.0
    if se_beta <= 0:
        return None
    t_stat = beta / se_beta
    sst = sum((yi - my) ** 2 for yi in y)
    r2 = 1.0 - (sse / sst) if sst > 0 else 0.0
    return {
        "beta": beta,
        "t_stat": t_stat,
        "r_squared": r2,
        "sample_count": n,
    }


def compute_lead_lag(
    db,
    *,
    exchange: str,
    universe: Iterable[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lags_hours: tuple[int, ...] = LAGS_HOURS,
    t_stat_threshold: float = DEFAULT_T_STAT_THRESHOLD,
    granularity: str = "ONE_HOUR",
) -> dict:
    """Compute lead-lag relationships for all asset pairs in ``universe``.

    Returns ``{rows_written: int, cells_tested: int, cells_significant: int}``.
    """
    universe = sorted({s for s in universe if s})
    if len(universe) < 2:
        return {"rows_written": 0, "cells_tested": 0, "cells_significant": 0}

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=int(lookback_days))

    # Cache returns per symbol so we only fetch each candle series once.
    returns: dict[str, list[tuple[datetime, float]]] = {}
    for sym in universe:
        try:
            cs = db.get_candles_range(
                exchange, sym, granularity, start=since, end=until,
            )
        except Exception as e:
            logger.debug(f"lead_lag: candles fetch failed for {sym}: {e}")
            continue
        rs = _hourly_returns(cs)
        if len(rs) >= MIN_SAMPLES:
            returns[sym] = rs

    if len(returns) < 2:
        logger.info(
            f"lead_lag: not enough series with ≥{MIN_SAMPLES} returns "
            f"(have {len(returns)})"
        )
        return {"rows_written": 0, "cells_tested": 0, "cells_significant": 0}

    rows_to_write: list[dict] = []
    tested = 0
    significant = 0

    syms = list(returns.keys())
    for leader in syms:
        leader_idx = {ts: r for ts, r in returns[leader]}
        for follower in syms:
            if leader == follower:
                continue
            follower_series = returns[follower]
            for lag in lags_hours:
                lag_delta = timedelta(hours=lag)
                xs: list[float] = []
                ys: list[float] = []
                for ts, fr in follower_series:
                    lr = leader_idx.get(ts - lag_delta)
                    if lr is None:
                        continue
                    xs.append(lr)
                    ys.append(fr)
                if len(xs) < MIN_SAMPLES:
                    continue
                tested += 1
                fit = _ols(xs, ys)
                if fit is None:
                    continue
                if abs(fit["t_stat"]) < t_stat_threshold:
                    continue
                significant += 1
                rows_to_write.append({
                    "leader": leader,
                    "follower": follower,
                    "lag_minutes": int(lag * 60),
                    **fit,
                })

    written = 0
    if rows_to_write:
        try:
            written = db.upsert_lead_lag(exchange, rows_to_write)
        except Exception as e:
            logger.warning(f"lead_lag: upsert failed: {e}")

    logger.info(
        f"lead_lag: {exchange} tested={tested} significant={significant} "
        f"rows_written={written}"
    )
    return {
        "rows_written": written,
        "cells_tested": tested,
        "cells_significant": significant,
    }
