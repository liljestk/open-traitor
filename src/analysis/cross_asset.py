"""
Cross-asset correlation, lead-lag, and cluster analytics.

Pure-numpy implementation that runs inside the planning worker without
new heavy dependencies. Everything is exchange-scoped — equity (ibkr) and
crypto (coinbase) data never mix because every read funnels through
``StatsDB.get_candles_range(exchange, symbol, …)``.

Public entry points:

* ``compute_correlation_matrix(exchange, symbols, *, window_days, stats_db)``
  → list[dict] rows ready for ``StatsDB.upsert_asset_correlations``.

* ``compute_clusters_from_correlations(rows, *, threshold)``
  → list[dict] cluster definitions ready for
  ``StatsDB.replace_asset_clusters``.

* ``fit_cross_event_regressions(exchange, *, stats_db, …)``
  → fits ``forward_return_target ~ pre_return_driver`` for every
  (driver_event_type, target_symbol) pair where the two symbols are in
  the same cluster (or where pearson ≥ threshold). Persists each result
  via ``StatsDB.upsert_cross_event_regression``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

import numpy as np

from src.utils.logger import get_logger
from src.utils.stats_correlations import categorise_event_type

logger = get_logger("analysis.cross_asset")


# Default window used for the nightly correlation snapshot.
DEFAULT_WINDOW_DAYS: int = 180

# Minimum number of overlapping daily observations required to publish a
# correlation row. Below this we skip the pair to avoid noise.
MIN_OBSERVATIONS: int = 30

# Lead-lag scan range (in trading days, both directions).
LEAD_LAG_RANGE: int = 5

# Clustering threshold: pearson ≥ this is treated as an edge for the
# union-find pass.
CLUSTER_PEARSON_THRESHOLD: float = 0.65


# ─── Daily-return loading ─────────────────────────────────────────────────


def _load_daily_returns(
    exchange: str,
    symbol: str,
    *,
    stats_db,
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    """Return ``{date_at_midnight_utc: log_return}`` for ``symbol``.

    Uses ``ONE_DAY`` candles. Missing data → empty dict (caller skips).
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
        logger.debug(f"_load_daily_returns({exchange}, {symbol}) failed: {e}")
        return {}
    out: dict[datetime, float] = {}
    prev_close: Optional[float] = None
    for c in candles:
        ts = c.get("ts")
        close = c.get("c")
        if ts is None or close is None or float(close) <= 0:
            continue
        # Normalise to date-at-UTC-midnight so series align across symbols.
        if isinstance(ts, datetime):
            day = ts.astimezone(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        else:
            continue
        if prev_close is not None and prev_close > 0:
            out[day] = math.log(float(close) / prev_close)
        prev_close = float(close)
    return out


def _aligned_returns(
    series_a: dict[datetime, float],
    series_b: dict[datetime, float],
) -> tuple[np.ndarray, np.ndarray]:
    """Return arrays of returns on overlapping dates only."""
    common = sorted(series_a.keys() & series_b.keys())
    if not common:
        return np.array([]), np.array([])
    a = np.array([series_a[d] for d in common], dtype=float)
    b = np.array([series_b[d] for d in common], dtype=float)
    return a, b


# ─── Correlation primitives ───────────────────────────────────────────────


def _pearson(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    if len(a) < 2 or len(b) < 2:
        return None
    sa, sb = a.std(), b.std()
    if sa == 0 or sb == 0:
        return None
    r = float(np.corrcoef(a, b)[0, 1])
    if math.isnan(r):
        return None
    return r


def _spearman(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    if len(a) < 2:
        return None
    # Rank-based — ties broken by stable sort. Sufficient for monitoring.
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return _pearson(ra, rb)


def _lead_lag(
    a: np.ndarray,
    b: np.ndarray,
    max_lag: int = LEAD_LAG_RANGE,
) -> tuple[int, Optional[float]]:
    """Return ``(best_lag, best_correlation)``.

    A positive lag means ``a`` *leads* ``b`` by that many days
    (``a[t]`` correlates with ``b[t+lag]``).
    """
    if len(a) < max_lag * 2 + 4:
        return 0, _pearson(a, b)
    best_lag = 0
    best_r: Optional[float] = _pearson(a, b)
    for lag in range(1, max_lag + 1):
        # a leads b
        r_pos = _pearson(a[:-lag], b[lag:])
        if r_pos is not None and (best_r is None or abs(r_pos) > abs(best_r)):
            best_r, best_lag = r_pos, lag
        # b leads a
        r_neg = _pearson(a[lag:], b[:-lag])
        if r_neg is not None and (best_r is None or abs(r_neg) > abs(best_r)):
            best_r, best_lag = r_neg, -lag
    return best_lag, best_r


# ─── Public: pairwise correlation matrix ──────────────────────────────────


def compute_correlation_matrix(
    exchange: str,
    symbols: Sequence[str],
    *,
    stats_db,
    window_days: int = DEFAULT_WINDOW_DAYS,
    end: Optional[datetime] = None,
) -> list[dict]:
    """Compute pairwise correlations + lead-lag scores.

    Returns a list of dicts ready for ``upsert_asset_correlations``.
    Symbols with no candle data are silently skipped.
    """
    if len(symbols) < 2:
        return []
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=window_days + LEAD_LAG_RANGE + 2)

    series: dict[str, dict[datetime, float]] = {}
    for sym in symbols:
        s = _load_daily_returns(
            exchange, sym, stats_db=stats_db, start=start, end=end,
        )
        if len(s) >= MIN_OBSERVATIONS:
            series[sym] = s

    out: list[dict] = []
    syms = sorted(series.keys())
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            xa, xb = _aligned_returns(series[a], series[b])
            if len(xa) < MIN_OBSERVATIONS:
                continue
            pearson = _pearson(xa, xb)
            spearman = _spearman(xa, xb)
            lag, lag_r = _lead_lag(xa, xb)
            out.append({
                "base_symbol": a,
                "peer_symbol": b,
                "window_days": int(window_days),
                "pearson": pearson,
                "spearman": spearman,
                "lead_lag_days": int(lag),
                "lead_lag_score": lag_r,
                "sample_count": int(len(xa)),
            })
    logger.info(
        f"compute_correlation_matrix({exchange}): "
        f"{len(syms)} symbols → {len(out)} pairs"
    )
    return out


# ─── Public: agglomerative clustering via union-find ──────────────────────


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self._parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            out[self.find(x)].append(x)
        return out


def compute_clusters_from_correlations(
    rows: Sequence[dict],
    *,
    threshold: float = CLUSTER_PEARSON_THRESHOLD,
    min_size: int = 2,
) -> list[dict]:
    """Group symbols whose absolute pearson ≥ threshold into clusters.

    Returns ``[{cluster_id, symbols, cohesion, label}]`` ready for
    ``StatsDB.replace_asset_clusters``. ``cohesion`` is the mean absolute
    pearson among the cluster's internal edges.
    """
    if not rows:
        return []
    universe: set[str] = set()
    edges: list[tuple[str, str, float]] = []
    for r in rows:
        a, b = r.get("base_symbol"), r.get("peer_symbol")
        p = r.get("pearson")
        if not a or not b or p is None:
            continue
        universe.add(a)
        universe.add(b)
        if abs(float(p)) >= threshold:
            edges.append((a, b, float(p)))
    if not universe:
        return []
    uf = _UnionFind(universe)
    for a, b, _ in edges:
        uf.union(a, b)
    groups = uf.groups()
    # Pre-index edges by canonical pair for cohesion lookup.
    edge_map: dict[tuple[str, str], float] = {}
    for r in rows:
        a, b = r.get("base_symbol"), r.get("peer_symbol")
        p = r.get("pearson")
        if not a or not b or p is None:
            continue
        key = (a, b) if a < b else (b, a)
        edge_map[key] = abs(float(p))
    out: list[dict] = []
    cluster_id = 0
    # Stable order: largest cluster first, then alphabetical first symbol.
    for members in sorted(
        groups.values(), key=lambda m: (-len(m), m[0] if m else "")
    ):
        if len(members) < min_size:
            continue
        cluster_id += 1
        # Cohesion = mean abs(pearson) over all *internal* edges.
        internal: list[float] = []
        ms = sorted(members)
        for i, x in enumerate(ms):
            for y in ms[i + 1:]:
                key = (x, y) if x < y else (y, x)
                if key in edge_map:
                    internal.append(edge_map[key])
        cohesion = float(np.mean(internal)) if internal else None
        out.append({
            "cluster_id": cluster_id,
            "symbols": ms,
            "cohesion": cohesion,
            "label": _label_cluster(ms),
        })
    logger.info(
        f"compute_clusters_from_correlations: "
        f"{len(universe)} symbols → {len(out)} clusters"
    )
    return out


def _label_cluster(members: Sequence[str]) -> str:
    """Best-effort human label for a cluster (top-3 members by alpha)."""
    if not members:
        return ""
    head = sorted(members)[:3]
    suffix = f" +{len(members) - 3}" if len(members) > 3 else ""
    return ", ".join(head) + suffix


# ─── Public: cross-event regressions ──────────────────────────────────────


def _ols_simple(
    x: np.ndarray, y: np.ndarray
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Univariate OLS. Returns (intercept, beta, r_squared, t_stat_beta)."""
    n = len(x)
    if n < 4:
        return None, None, None, None
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return None, None, None, None
    x_mean, y_mean = x.mean(), y.mean()
    cov = float(np.mean((x - x_mean) * (y - y_mean)))
    var_x = float(np.var(x))
    if var_x == 0:
        return None, None, None, None
    beta = cov / var_x
    intercept = y_mean - beta * x_mean
    pred = intercept + beta * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    # t-stat for beta
    if n > 2:
        sigma2 = ss_res / (n - 2)
        sxx = float(np.sum((x - x_mean) ** 2))
        if sxx > 0 and sigma2 > 0:
            se_beta = math.sqrt(sigma2 / sxx)
            t_stat = beta / se_beta if se_beta > 0 else None
        else:
            t_stat = None
    else:
        t_stat = None
    return float(intercept), float(beta), r_sq, t_stat


def fit_cross_event_regressions(
    exchange: str,
    *,
    stats_db,
    horizons: tuple[int, ...] = (1, 5, 20),
    pre_window_days: int = 5,
    history_lookback_days: int = 1825,  # 5y
    min_correlation: float = 0.5,
    min_samples: int = 6,
) -> list[dict]:
    """Fit per ``(driver, driver_event_type, target, horizon)`` regressions.

    Logic:
    1. Pull the latest correlation rows for the exchange.
    2. For each pair with abs(pearson) ≥ ``min_correlation`` (in either
       direction → driver+target / target+driver are tried separately), look
       up every event on the driver, compute the driver's pre-event log
       return over ``pre_window_days`` and the target's forward log return
       over ``horizon_days``. Fit univariate OLS.
    3. Persist via ``StatsDB.upsert_cross_event_regression``.

    Returns a list of result-dicts (also persisted).
    """
    correlations = stats_db.get_asset_correlations(
        exchange=exchange,
        min_abs_pearson=float(min_correlation),
        limit=10_000,
    )
    if not correlations:
        logger.info(f"fit_cross_event_regressions({exchange}): no correlations")
        return []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=history_lookback_days)

    # Cache daily-close series per symbol so we read each only once.
    closes_cache: dict[str, list[tuple[datetime, float]]] = {}

    def _closes(sym: str) -> list[tuple[datetime, float]]:
        if sym not in closes_cache:
            try:
                rows = stats_db.get_candles_range(
                    exchange=exchange,
                    symbol=sym,
                    granularity="ONE_DAY",
                    start=start,
                    end=end,
                )
            except Exception as e:
                logger.debug(f"_closes({sym}) failed: {e}")
                rows = []
            closes_cache[sym] = [
                (
                    r["ts"].astimezone(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0,
                    ),
                    float(r["c"]),
                )
                for r in rows
                if r.get("ts") and r.get("c") and float(r["c"]) > 0
            ]
        return closes_cache[sym]

    def _close_at(closes: list[tuple[datetime, float]], target: datetime
                  ) -> Optional[float]:
        """Closest close at or before ``target`` (within 5 days)."""
        if not closes:
            return None
        best: Optional[tuple[datetime, float]] = None
        for ts, px in closes:
            if ts > target:
                break
            best = (ts, px)
        if best is None:
            return None
        if (target - best[0]).days > 5:
            return None
        return best[1]

    results: list[dict] = []
    # Build pair list both directions: each correlation row gives 2 (driver,
    # target) candidates.
    pairs: list[tuple[str, str]] = []
    for r in correlations:
        a, b = r["base_symbol"], r["peer_symbol"]
        pairs.append((a, b))
        pairs.append((b, a))

    seen: set[tuple[str, str]] = set()
    for driver, target in pairs:
        if driver == target or (driver, target) in seen:
            continue
        seen.add((driver, target))
        events = stats_db.get_catalyst_events(
            exchange=exchange,
            symbol=driver,
            start=start,
            end=end,
            limit=5000,
        )
        if not events:
            continue
        # Group events by event_type → fit one regression per type per horizon.
        by_type: dict[str, list[datetime]] = defaultdict(list)
        for ev in events:
            ts = ev.get("event_ts")
            if isinstance(ts, datetime):
                by_type[ev.get("event_type", "other")].append(
                    ts.astimezone(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0,
                    )
                )
        d_closes = _closes(driver)
        t_closes = _closes(target)
        if not d_closes or not t_closes:
            continue
        for et, dates in by_type.items():
            if len(dates) < min_samples:
                continue
            for horizon in horizons:
                xs: list[float] = []
                ys: list[float] = []
                for d in dates:
                    pre_anchor = d - timedelta(days=pre_window_days)
                    p_pre = _close_at(d_closes, pre_anchor)
                    p_event_d = _close_at(d_closes, d)
                    p_event_t = _close_at(t_closes, d)
                    p_post_t = _close_at(t_closes, d + timedelta(days=horizon))
                    if (
                        p_pre is None
                        or p_event_d is None
                        or p_event_t is None
                        or p_post_t is None
                        or p_pre <= 0
                        or p_event_t <= 0
                    ):
                        continue
                    xs.append(math.log(p_event_d / p_pre))
                    ys.append(math.log(p_post_t / p_event_t))
                if len(xs) < min_samples:
                    continue
                xa, ya = np.array(xs), np.array(ys)
                intercept, beta, r2, t_stat = _ols_simple(xa, ya)
                row = {
                    "exchange": exchange,
                    "driver_symbol": driver,
                    "driver_event_type": et,
                    "target_symbol": target,
                    "horizon_days": int(horizon),
                    "sample_count": int(len(xs)),
                    "beta": beta,
                    "intercept": intercept,
                    "r_squared": r2,
                    "t_stat_beta": t_stat,
                    "mean_forward_return": float(np.mean(ya)),
                    "hit_rate": float(np.mean(ya > 0)),
                    "notes": categorise_event_type(et),
                }
                try:
                    stats_db.upsert_cross_event_regression(row)
                except Exception as e:
                    logger.warning(
                        f"upsert_cross_event_regression failed "
                        f"({driver}->{target}, {et}, h={horizon}): {e}"
                    )
                results.append(row)
    logger.info(
        f"fit_cross_event_regressions({exchange}): "
        f"{len(results)} regressions persisted"
    )
    return results
