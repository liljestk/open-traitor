"""Tests for the Catalyst Pattern Engine.

Covers:
* Fingerprint determinism
* Scale invariance (BTC-priced vs AAPL-priced same shape ⇒ same vector)
* No look-ahead (post-anchor candles are ignored)
* Forward-return labelling
* aggregate_outcome math (similarity weighting, direction, confidence)
* fuse_with_sentiment Bayesian shrink
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.analysis.pattern_engine import (
    FORWARD_HORIZONS,
    PRE_WINDOW_BARS,
    PatternOutcome,
    aggregate_outcome,
    compute_forward_returns,
    extract_fingerprint,
    fuse_with_sentiment,
)
from src.utils.stats_patterns import PATTERN_VECTOR_DIM


# ───────────────────────── helpers ──────────────────────────


def _make_candles(
    *, base_price: float = 100.0, n: int = 60, seed: int = 0,
    drift: float = 0.0, vol: float = 0.01,
) -> list[dict]:
    """Synthesise daily candles with deterministic random walks."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(loc=drift, scale=vol, size=n)
    closes = base_price * np.exp(np.cumsum(rets))
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i, c in enumerate(closes):
        ts = start + timedelta(days=i)
        candles.append({
            "ts": ts,
            "o": float(c),
            "h": float(c) * 1.005,
            "l": float(c) * 0.995,
            "c": float(c),
            "v": 1_000.0 + 50.0 * (i % 7),
        })
    return candles


# ───────────────────────── fingerprint tests ──────────────────────────


def test_fingerprint_dim_and_normalisation():
    candles = _make_candles(n=60, seed=1)
    anchor = candles[40]["ts"]
    vec = extract_fingerprint(candles, anchor)
    assert vec is not None
    assert vec.shape == (PATTERN_VECTOR_DIM,)
    assert vec.dtype == np.float32
    # L2-normalised (cosine ≡ dot product).
    norm = float(np.linalg.norm(vec))
    assert abs(norm - 1.0) < 1e-5


def test_fingerprint_deterministic():
    candles = _make_candles(n=60, seed=2)
    anchor = candles[40]["ts"]
    v1 = extract_fingerprint(candles, anchor)
    v2 = extract_fingerprint(candles, anchor)
    assert np.array_equal(v1, v2)


def test_fingerprint_scale_invariant():
    """A BTC-priced ($60k) vs AAPL-priced ($150) series with IDENTICAL
    log-return shape must produce nearly-identical fingerprints — that's
    the whole point of the encoding."""
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0, 0.015, size=60)

    def _series(base: float) -> list[dict]:
        closes = base * np.exp(np.cumsum(rets))
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        return [
            {
                "ts": start + timedelta(days=i),
                "o": float(c), "h": float(c) * 1.01, "l": float(c) * 0.99,
                "c": float(c), "v": 1_000.0,
            }
            for i, c in enumerate(closes)
        ]

    btc = _series(60_000.0)
    aapl = _series(150.0)
    anchor = btc[40]["ts"]
    v_btc = extract_fingerprint(btc, anchor)
    v_aapl = extract_fingerprint(aapl, anchor)
    cos = float(np.dot(v_btc, v_aapl))
    assert cos > 0.999, f"expected near-1 cosine sim, got {cos}"


def test_fingerprint_no_lookahead():
    """Candles after the anchor must NOT change the encoding."""
    candles = _make_candles(n=60, seed=3)
    anchor = candles[40]["ts"]
    base = extract_fingerprint(candles, anchor)

    # Mutate post-anchor candles to extreme values.
    poisoned = [dict(c) for c in candles]
    for i in range(41, 60):
        poisoned[i]["c"] = 1e6
        poisoned[i]["v"] = 1e9
    vec2 = extract_fingerprint(poisoned, anchor)
    assert np.allclose(base, vec2, atol=1e-6)


def test_fingerprint_returns_none_when_too_few_bars():
    candles = _make_candles(n=10, seed=4)
    anchor = candles[5]["ts"]
    assert extract_fingerprint(candles, anchor) is None


# ───────────────────────── forward-return tests ──────────────────────────


def test_compute_forward_returns_basic():
    candles = _make_candles(n=80, seed=5)
    anchor = candles[40]["ts"]
    fwd = compute_forward_returns(candles, anchor, horizons_bars=FORWARD_HORIZONS)
    # All horizons present and finite.
    assert set(fwd.keys()) == set(FORWARD_HORIZONS.keys())
    for h, v in fwd.items():
        assert v is not None
        assert np.isfinite(v)


def test_compute_forward_returns_handles_missing_horizon():
    candles = _make_candles(n=42, seed=6)
    anchor = candles[40]["ts"]
    fwd = compute_forward_returns(candles, anchor, horizons_bars={"1d": 1, "5d": 5, "20d": 20})
    assert fwd["1d"] is not None
    # 20-day horizon goes past the end ⇒ None.
    assert fwd["20d"] is None


# ───────────────────────── aggregate_outcome tests ──────────────────────────


def test_aggregate_outcome_empty():
    o = aggregate_outcome([], horizon="5d")
    assert o.n_matches == 0
    assert o.confidence == 0.0
    assert o.direction == "neutral"


def test_aggregate_outcome_bullish_direction():
    matches = [
        {"similarity": 0.95, "forward_return_1d": 0.01, "forward_return_5d": 0.05, "forward_return_20d": 0.10},
        {"similarity": 0.90, "forward_return_1d": 0.02, "forward_return_5d": 0.06, "forward_return_20d": 0.12},
        {"similarity": 0.85, "forward_return_1d": 0.015, "forward_return_5d": 0.04, "forward_return_20d": 0.09},
        {"similarity": 0.80, "forward_return_1d": 0.008, "forward_return_5d": 0.07, "forward_return_20d": 0.11},
    ]
    o = aggregate_outcome(matches, horizon="5d")
    assert o.direction == "bullish"
    assert o.expected_drift["5d"] > 0.04
    assert o.confidence > 0.0
    assert o.n_matches == 4


def test_aggregate_outcome_bearish_direction():
    matches = [
        {"similarity": 0.9, "forward_return_5d": -0.05},
        {"similarity": 0.85, "forward_return_5d": -0.07},
        {"similarity": 0.8, "forward_return_5d": -0.04},
    ]
    o = aggregate_outcome(matches, horizon="5d", min_matches=3)
    assert o.direction == "bearish"
    assert o.expected_drift["5d"] < 0.0


def test_aggregate_outcome_similarity_weighting():
    """Higher-similarity matches should dominate the average."""
    matches = [
        {"similarity": 0.99, "forward_return_5d": 0.10},
        {"similarity": 0.05, "forward_return_5d": -0.10},
    ]
    o = aggregate_outcome(matches, horizon="5d", min_matches=1)
    assert o.expected_drift["5d"] > 0.05  # dominated by the high-sim positive


def test_aggregate_outcome_confidence_grows_with_n():
    base = {"similarity": 0.9, "forward_return_5d": 0.05, "forward_return_1d": 0.01, "forward_return_20d": 0.08}
    o_few = aggregate_outcome([dict(base) for _ in range(3)], horizon="5d")
    o_many = aggregate_outcome([dict(base) for _ in range(40)], horizon="5d")
    assert o_many.confidence >= o_few.confidence


# ───────────────────────── sentiment fusion tests ──────────────────────────


def test_fuse_with_sentiment_agreement_amplifies():
    out = PatternOutcome(
        expected_drift={"5d": 0.05},
        dispersion={"5d": 0.02},
        n_matches=10,
        confidence=0.5,
        direction="bullish",
    )
    fused = fuse_with_sentiment(out, sentiment_score=0.8, horizon="5d")
    assert fused.confidence >= out.confidence


def test_fuse_with_sentiment_disagreement_dampens():
    out = PatternOutcome(
        expected_drift={"5d": 0.05},
        dispersion={"5d": 0.02},
        n_matches=10,
        confidence=0.7,
        direction="bullish",
    )
    fused = fuse_with_sentiment(out, sentiment_score=-0.8, horizon="5d")
    assert fused.confidence <= out.confidence


def test_fuse_with_sentiment_none_is_identity():
    out = PatternOutcome(
        expected_drift={"5d": 0.05},
        dispersion={"5d": 0.02},
        n_matches=10,
        confidence=0.6,
        direction="bullish",
    )
    fused = fuse_with_sentiment(out, sentiment_score=None, horizon="5d")
    assert fused.confidence == pytest.approx(out.confidence)
    assert fused.direction == out.direction
