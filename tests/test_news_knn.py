"""Tests for news_knn — fakes embedding + db.search_similar_news + candles."""
from datetime import datetime, timezone

from src.utils import news_knn as nk_mod


class _FakeDB:
    def __init__(self, neighbors, candles):
        self._n = neighbors
        self._c = candles
    def search_similar_news(self, *args, **kwargs):
        return self._n
    def get_candles_range(self, exchange, pair, gran, **kw):
        return self._c


def _patch_embed(monkeypatch, vec):
    monkeypatch.setattr(nk_mod, "embed_text", lambda _t: vec)


def test_no_query_returns_unavailable():
    out = nk_mod.news_knn_prior(_FakeDB([], []), exchange="coinbase",
                                 pair="BTC-USD", query_text="")
    assert out["available"] is False


def test_empty_embedding(monkeypatch):
    _patch_embed(monkeypatch, [])
    nk_mod._CACHE.clear()
    out = nk_mod.news_knn_prior(_FakeDB([], []), exchange="coinbase",
                                 pair="BTC-USD", query_text="x")
    assert out["available"] is False


def test_returns_neighbors_with_drift(monkeypatch):
    _patch_embed(monkeypatch, [0.1] * 10)
    nk_mod._CACHE.clear()
    t0 = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    candles = [
        {"ts": t0,                                "c": 100.0},
        {"ts": t0.replace(hour=23),               "c": 110.0},
    ]
    neighbors = [
        {"ts": t0, "title": "Big rally", "similarity": 0.95},
        {"ts": t0, "title": "Bullish flow", "similarity": 0.91},
        {"ts": t0, "title": "ETF inflow", "similarity": 0.88},
    ]
    db = _FakeDB(neighbors, candles)
    out = nk_mod.news_knn_prior(db, exchange="coinbase",
                                 pair="BTC-USD", query_text="rally",
                                 horizon_hours=24, k=3, min_neighbors=2)
    assert out["available"] is True
    assert out["k_used"] >= 2
    assert out["mean_drift"] > 0
    assert out["expected_direction"] == "long"
