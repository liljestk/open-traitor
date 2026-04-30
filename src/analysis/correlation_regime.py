"""
Correlation-regime detector.

Tracks the **average pairwise correlation** of an asset universe and
flags regime shifts when that average crosses a rolling-z threshold.

Why it matters
--------------
In normal markets, individual asset correlations are mixed, so the
average pairwise correlation hovers around a baseline (e.g. 0.3 for
crypto majors, 0.2 for diversified equities). During liquidations,
macro shocks, or contagion events, correlations spike toward 1 — the
classic "diversification fails when you need it most" pattern.

A rising z-score on the average pairwise correlation is one of the
earliest, cleanest signals of de-risking. The detector publishes::

    * ``avg_corr``     — current average pairwise correlation
    * ``z_score``      — rolling z-score of avg_corr against its history
    * ``regime``       — one of {normal, elevated, breakdown}
    * ``n_pairs``      — number of pairs averaged

Pure numpy. Reads the rolling correlation matrix already produced by
``cross_asset.compute_correlation_matrix`` (so it's free of additional
candle fetches).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence

from src.utils.logger import get_logger

logger = get_logger("analysis.correlation_regime")


# Rolling-history window size for z-score computation.
HISTORY_WINDOW: int = 60

# z-score thresholds for regime classification.
THRESHOLD_ELEVATED: float = 1.0
THRESHOLD_BREAKDOWN: float = 2.0

# Minimum pairs required to compute an average — fewer than this and
# the detector returns ``None``.
MIN_PAIRS: int = 6


@dataclass
class CorrelationRegimeSnapshot:
    """A single observation of the universe-wide correlation level."""

    avg_corr: float
    z_score: float
    regime: str        # "normal" | "elevated" | "breakdown"
    n_pairs: int
    history_n: int


def average_pairwise_correlation(
    rows: Sequence[dict],
) -> Optional[tuple[float, int]]:
    """Compute the mean of |pearson| across all rows.

    ``rows`` are correlation rows as produced by
    ``cross_asset.compute_correlation_matrix`` — each dict has at least
    a ``pearson`` field. Rows with non-finite pearson are skipped.

    Returns ``(mean, n)`` or ``None`` if fewer than ``MIN_PAIRS`` rows.
    """
    vals: list[float] = []
    for r in rows:
        p = r.get("pearson")
        if p is None:
            continue
        try:
            pf = float(p)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pf):
            continue
        vals.append(abs(pf))
    if len(vals) < MIN_PAIRS:
        return None
    return float(sum(vals) / len(vals)), len(vals)


def classify_regime(
    avg_corr: float,
    history: Sequence[float],
    *,
    elevated: float = THRESHOLD_ELEVATED,
    breakdown: float = THRESHOLD_BREAKDOWN,
) -> CorrelationRegimeSnapshot:
    """Classify the current regime from history of past avg correlations.

    With ``history_n < 5`` we cannot compute a stable z-score and emit
    ``regime="normal"`` with ``z_score=0``. Otherwise we use a simple
    z-score against the trailing window stdev/mean.
    """
    h = [float(v) for v in history if math.isfinite(v)]
    if len(h) < 5:
        return CorrelationRegimeSnapshot(
            avg_corr=avg_corr, z_score=0.0,
            regime="normal", n_pairs=0, history_n=len(h),
        )
    mu = statistics.fmean(h)
    sd = statistics.pstdev(h) if len(h) >= 2 else 0.0
    if sd <= 0:
        z = 0.0
    else:
        z = (avg_corr - mu) / sd
    if not math.isfinite(z):
        z = 0.0
    if z >= breakdown:
        regime = "breakdown"
    elif z >= elevated:
        regime = "elevated"
    else:
        regime = "normal"
    return CorrelationRegimeSnapshot(
        avg_corr=avg_corr, z_score=float(z),
        regime=regime, n_pairs=0, history_n=len(h),
    )


def detect_regime(
    correlation_rows: Sequence[dict],
    *,
    history: Sequence[float],
    elevated: float = THRESHOLD_ELEVATED,
    breakdown: float = THRESHOLD_BREAKDOWN,
) -> Optional[CorrelationRegimeSnapshot]:
    """High-level helper: compute snapshot from raw correlation rows.

    Returns ``None`` if too few pairs are available.
    """
    res = average_pairwise_correlation(correlation_rows)
    if res is None:
        return None
    avg, n_pairs = res
    snap = classify_regime(
        avg, history,
        elevated=elevated, breakdown=breakdown,
    )
    snap.n_pairs = n_pairs
    return snap


# ─── Persistence helper ─────────────────────────────────────────────────


def persist_regime_snapshot(
    stats_db,
    *,
    exchange: str,
    snapshot: CorrelationRegimeSnapshot,
) -> int:
    """Append the snapshot to the regime-events table."""
    return stats_db.insert_correlation_regime_event({
        "exchange": exchange,
        "avg_corr": snapshot.avg_corr,
        "z_score": snapshot.z_score,
        "regime": snapshot.regime,
        "n_pairs": snapshot.n_pairs,
        "history_n": snapshot.history_n,
    })


__all__ = [
    "HISTORY_WINDOW", "THRESHOLD_ELEVATED", "THRESHOLD_BREAKDOWN", "MIN_PAIRS",
    "CorrelationRegimeSnapshot",
    "average_pairwise_correlation",
    "classify_regime",
    "detect_regime",
    "persist_regime_snapshot",
]
