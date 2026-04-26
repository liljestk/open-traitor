"""Unit tests for cross-asset analytics primitives.

Pure-python tests of:
* event-type → category normalisation
* pearson / lead-lag detection on synthetic series
* union-find clustering from correlation rows
* simple OLS used by ``fit_cross_event_regressions``
* high-level ``compute_correlation_matrix`` + clustering against a
  StatsDB stub that returns deterministic candle series.

No real DB or Temporal — everything runs in-process.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# categorise_event_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("earnings", "earnings"),
        ("EARNINGS_RELEASE", "earnings"),
        ("ex_dividend", "dividend"),
        ("dividend", "dividend"),
        ("product_release", "product"),
        ("launch", "product"),
        ("macro", "macro"),
        ("FOMC", "macro"),
        ("cpi", "macro"),
        ("halving", "halving"),
        ("listing", "listing"),
        ("regulatory", "regulatory"),
        ("ETF", "regulatory"),
        ("upgrade", "onchain"),
        ("burn", "onchain"),
        ("randomthing", "other"),
        ("", "other"),
    ],
)
def test_categorise_event_type(raw, expected):
    from src.utils.stats_correlations import categorise_event_type
    assert categorise_event_type(raw) == expected


# ---------------------------------------------------------------------------
# Pearson / lead-lag primitives
# ---------------------------------------------------------------------------

def test_pearson_perfect_positive():
    from src.analysis.cross_asset import _pearson
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    y = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
    assert _pearson(x, y) == pytest.approx(1.0)


def test_pearson_perfect_negative():
    from src.analysis.cross_asset import _pearson
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    y = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
    assert _pearson(x, y) == pytest.approx(-1.0)


def test_pearson_zero_variance_returns_none():
    from src.analysis.cross_asset import _pearson
    x = np.array([1.0, 1.0, 1.0, 1.0])
    y = np.array([1.0, 2.0, 3.0, 4.0])
    assert _pearson(x, y) is None


def test_lead_lag_detects_a_leads_b():
    """Construct a synthetic series where a leads b by exactly 2 days."""
    from src.analysis.cross_asset import _lead_lag
    rng = np.random.default_rng(42)
    n = 200
    a = rng.normal(0, 1, n)
    # b copies a with a 2-day lag, plus small noise
    b = np.concatenate([np.zeros(2), a[:-2]]) + rng.normal(0, 0.05, n)
    lag, r = _lead_lag(a, b, max_lag=5)
    # a leads b by ~2 days → positive lag
    assert lag == 2
    assert r is not None and r > 0.9


def test_lead_lag_zero_when_synchronous():
    from src.analysis.cross_asset import _lead_lag
    rng = np.random.default_rng(7)
    a = rng.normal(0, 1, 100)
    b = a + rng.normal(0, 0.01, 100)  # near-perfect sync
    lag, r = _lead_lag(a, b, max_lag=5)
    assert lag == 0
    assert r is not None and r > 0.99


# ---------------------------------------------------------------------------
# Simple OLS used by cross-event regression
# ---------------------------------------------------------------------------

def test_ols_simple_recovers_known_beta():
    from src.analysis.cross_asset import _ols_simple
    rng = np.random.default_rng(123)
    n = 200
    x = rng.normal(0, 1, n)
    true_beta = 0.7
    true_intercept = 0.05
    y = true_intercept + true_beta * x + rng.normal(0, 0.1, n)
    intercept, beta, r2, t_stat = _ols_simple(x, y)
    assert beta is not None and abs(beta - true_beta) < 0.05
    assert intercept is not None and abs(intercept - true_intercept) < 0.05
    assert r2 is not None and r2 > 0.95
    assert t_stat is not None and t_stat > 10  # huge signal-to-noise


def test_ols_simple_zero_variance_returns_none():
    from src.analysis.cross_asset import _ols_simple
    x = np.zeros(20)
    y = np.linspace(0, 1, 20)
    out = _ols_simple(x, y)
    assert out == (None, None, None, None)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def test_clustering_groups_high_correlation_pairs():
    from src.analysis.cross_asset import compute_clusters_from_correlations
    rows = [
        # ETH cluster
        {"base_symbol": "ETH-USD", "peer_symbol": "ARB-USD", "pearson": 0.85},
        {"base_symbol": "ARB-USD", "peer_symbol": "OP-USD",  "pearson": 0.80},
        # BTC isolate
        {"base_symbol": "BTC-USD", "peer_symbol": "DOGE-USD", "pearson": 0.30},
        # Stablecoin pair
        {"base_symbol": "USDC-USD", "peer_symbol": "USDT-USD", "pearson": 0.95},
    ]
    clusters = compute_clusters_from_correlations(rows, threshold=0.65)
    members = sorted([sorted(c["symbols"]) for c in clusters])
    assert ["ARB-USD", "ETH-USD", "OP-USD"] in members
    assert ["USDC-USD", "USDT-USD"] in members
    # Single-symbol or below-threshold pairs must not appear
    flat = {s for c in clusters for s in c["symbols"]}
    assert "BTC-USD" not in flat
    assert "DOGE-USD" not in flat


def test_clustering_cohesion_is_mean_abs_pearson():
    from src.analysis.cross_asset import compute_clusters_from_correlations
    rows = [
        {"base_symbol": "A", "peer_symbol": "B", "pearson": 0.80},
        {"base_symbol": "B", "peer_symbol": "C", "pearson": 0.90},
        {"base_symbol": "A", "peer_symbol": "C", "pearson": 0.70},
    ]
    clusters = compute_clusters_from_correlations(rows, threshold=0.65)
    assert len(clusters) == 1
    assert clusters[0]["cohesion"] == pytest.approx(0.80, abs=1e-6)


def test_clustering_ignores_below_threshold():
    from src.analysis.cross_asset import compute_clusters_from_correlations
    rows = [
        {"base_symbol": "X", "peer_symbol": "Y", "pearson": 0.40},
        {"base_symbol": "Y", "peer_symbol": "Z", "pearson": 0.30},
    ]
    clusters = compute_clusters_from_correlations(rows, threshold=0.65)
    assert clusters == []


def test_clustering_handles_empty_rows():
    from src.analysis.cross_asset import compute_clusters_from_correlations
    assert compute_clusters_from_correlations([]) == []


# ---------------------------------------------------------------------------
# compute_correlation_matrix end-to-end (with a stub DB)
# ---------------------------------------------------------------------------

class _StubStatsDB:
    """Returns synthetic ONE_DAY candles for a fixed set of symbols."""

    def __init__(self):
        # Deterministic series: A is base; B mirrors A; C is independent.
        rng = np.random.default_rng(0)
        n = 250
        base_returns = rng.normal(0, 0.01, n)
        # Construct closes from returns (start at 100).
        a = 100.0 * np.exp(np.cumsum(base_returns))
        b = 100.0 * np.exp(np.cumsum(base_returns + rng.normal(0, 0.001, n)))
        c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._candles = {
            "A-USD": [
                {"ts": start + timedelta(days=i), "c": float(a[i])}
                for i in range(n)
            ],
            "B-USD": [
                {"ts": start + timedelta(days=i), "c": float(b[i])}
                for i in range(n)
            ],
            "C-USD": [
                {"ts": start + timedelta(days=i), "c": float(c[i])}
                for i in range(n)
            ],
        }

    def get_candles_range(self, exchange, symbol, granularity, start, end):
        return self._candles.get(symbol, [])


def test_compute_correlation_matrix_high_for_correlated_pair():
    from src.analysis.cross_asset import compute_correlation_matrix
    db = _StubStatsDB()
    rows = compute_correlation_matrix(
        exchange="coinbase",
        symbols=["A-USD", "B-USD", "C-USD"],
        stats_db=db,
        window_days=240,
        end=datetime(2024, 9, 1, tzinfo=timezone.utc),
    )
    by_pair = {(r["base_symbol"], r["peer_symbol"]): r for r in rows}
    ab = by_pair[("A-USD", "B-USD")]
    ac = by_pair[("A-USD", "C-USD")]
    assert ab["pearson"] is not None and ab["pearson"] > 0.95
    assert ac["pearson"] is not None and abs(ac["pearson"]) < 0.5
    assert ab["sample_count"] >= 30


def test_compute_correlation_matrix_skips_symbols_without_data():
    from src.analysis.cross_asset import compute_correlation_matrix
    db = _StubStatsDB()
    rows = compute_correlation_matrix(
        exchange="coinbase",
        symbols=["A-USD", "B-USD", "MISSING-USD"],
        stats_db=db,
        window_days=240,
        end=datetime(2024, 9, 1, tzinfo=timezone.utc),
    )
    syms = {s for r in rows for s in (r["base_symbol"], r["peer_symbol"])}
    assert "MISSING-USD" not in syms
    assert {"A-USD", "B-USD"}.issubset(syms)


# ---------------------------------------------------------------------------
# Taxonomy seeder helper
# ---------------------------------------------------------------------------

def test_strip_quote_handles_dashed_pairs():
    from src.analysis.taxonomy_seeder import _strip_quote
    assert _strip_quote("ETH-USD") == "ETH"
    assert _strip_quote("BTC-USDC") == "BTC"


def test_strip_quote_handles_concatenated_pairs():
    from src.analysis.taxonomy_seeder import _strip_quote
    assert _strip_quote("ETHUSD") == "ETH"
    assert _strip_quote("BTCUSDT") == "BTC"


def test_seed_crypto_taxonomy_routes_known_ecosystems():
    from src.analysis.taxonomy_seeder import seed_crypto_taxonomy

    captured: list[dict] = []

    class _DB:
        def upsert_asset_taxonomy(self, rows):
            captured.extend(rows)
            return len(rows)

    n = seed_crypto_taxonomy("coinbase", ["ETH-USD", "USDC-USD", "DOGE-USD", "WHATEVER-USD"], stats_db=_DB())
    assert n == 4
    by_sym = {r["symbol"]: r for r in captured}
    assert by_sym["ETH-USD"]["ecosystem"] == "ETH-L1"
    assert by_sym["USDC-USD"]["ecosystem"] == "stablecoin"
    assert by_sym["DOGE-USD"]["ecosystem"] == "meme"
    assert by_sym["WHATEVER-USD"]["ecosystem"] == "crypto-other"
    # asset_class is always 'crypto' for this seeder
    assert all(r["asset_class"] == "crypto" for r in captured)
