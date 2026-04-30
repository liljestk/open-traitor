"""Unit tests for src.analysis.correlation_regime."""

from __future__ import annotations

from src.analysis.correlation_regime import (
    MIN_PAIRS,
    average_pairwise_correlation,
    classify_regime,
    detect_regime,
)


def _rows(values):
    return [{"pearson": v} for v in values]


def test_average_pairwise_correlation_basic():
    rows = _rows([0.1, -0.2, 0.3, 0.4, -0.5, 0.6])
    res = average_pairwise_correlation(rows)
    assert res is not None
    avg, n = res
    assert n == 6
    assert abs(avg - sum(abs(v) for v in [0.1, -0.2, 0.3, 0.4, -0.5, 0.6]) / 6) < 1e-9


def test_average_pairwise_correlation_skips_non_finite():
    rows = _rows([0.1, None, float("nan"), 0.2, 0.3, 0.4, 0.5, 0.6])
    res = average_pairwise_correlation(rows)
    assert res is not None
    _, n = res
    assert n == 6


def test_average_pairwise_correlation_min_pairs():
    rows = _rows([0.1] * (MIN_PAIRS - 1))
    assert average_pairwise_correlation(rows) is None


def test_classify_regime_thresholds():
    history = [0.30] * 30
    # Low → normal
    snap = classify_regime(0.31, history)
    assert snap.regime == "normal"
    # No history → normal
    snap = classify_regime(0.95, [])
    assert snap.regime == "normal"


def test_classify_regime_breakdown():
    # Stable history at 0.3 ± small noise; current value way out → breakdown
    history = [0.30 + (i % 3) * 0.001 for i in range(60)]
    snap = classify_regime(0.95, history)
    assert snap.z_score > 2.0
    assert snap.regime == "breakdown"


def test_classify_regime_elevated():
    history = [0.30 + (i % 3) * 0.005 for i in range(60)]
    # Choose a value giving z between 1 and 2.
    import statistics
    mu = statistics.fmean(history)
    sd = statistics.pstdev(history)
    target = mu + 1.5 * sd
    snap = classify_regime(target, history)
    assert 1.0 <= snap.z_score < 2.0
    assert snap.regime == "elevated"


def test_detect_regime_end_to_end():
    rows = _rows([0.4] * 8)
    history = [0.30] * 30
    snap = detect_regime(rows, history=history)
    assert snap is not None
    assert snap.n_pairs == 8
    assert snap.regime in {"normal", "elevated", "breakdown"}


def test_detect_regime_insufficient_pairs():
    rows = _rows([0.4] * (MIN_PAIRS - 1))
    assert detect_regime(rows, history=[0.3] * 30) is None
