"""Unit tests for the news embedding helper.

Verifies:
* Hash fallback produces stable, normalised, NEWS_EMBED_DIM-length vectors.
* Repeated calls on the same text are deterministic.
* Different inputs produce different vectors.
* The Ollama path tolerates network failure and tripped circuits.
* embed_text never raises and never returns None.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_circuit():
    """Make sure each test starts with a fresh Ollama circuit state."""
    from src.utils import embeddings
    embeddings._ollama_unavailable = False
    yield
    embeddings._ollama_unavailable = False


def test_hash_embedding_dim_matches():
    from src.utils.embeddings import _hash_embedding, NEWS_EMBED_DIM
    v = _hash_embedding("hello world")
    assert len(v) == NEWS_EMBED_DIM


def test_hash_embedding_deterministic():
    from src.utils.embeddings import _hash_embedding
    a = _hash_embedding("BTC rallies on ETF approval")
    b = _hash_embedding("BTC rallies on ETF approval")
    assert a == b


def test_hash_embedding_distinct_inputs_differ():
    from src.utils.embeddings import _hash_embedding
    a = _hash_embedding("BTC rallies")
    b = _hash_embedding("ETH crashes")
    assert a != b


def test_hash_embedding_l2_normalised():
    from src.utils.embeddings import _hash_embedding
    v = _hash_embedding("the quick brown fox jumps")
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6


def test_embed_text_falls_back_when_ollama_down():
    from src.utils import embeddings
    # Force Ollama path to fail by tripping the breaker.
    embeddings._ollama_unavailable = True
    v = embeddings.embed_text("Apple beats earnings")
    assert len(v) == embeddings.NEWS_EMBED_DIM
    assert any(x != 0 for x in v)


def test_embed_text_empty_returns_zero_vector():
    from src.utils.embeddings import embed_text, NEWS_EMBED_DIM
    v = embed_text("")
    assert v == [0.0] * NEWS_EMBED_DIM


def test_embed_text_uses_ollama_when_available():
    from src.utils import embeddings

    fake = [0.1] * embeddings.NEWS_EMBED_DIM

    class _R:
        status_code = 200
        def json(self):
            return {"embedding": fake}

    with patch("requests.post", return_value=_R()):
        v = embeddings.embed_text("test")
    assert v == fake


def test_embed_text_trips_circuit_on_dim_mismatch():
    from src.utils import embeddings

    class _R:
        status_code = 200
        def json(self):
            return {"embedding": [0.0] * (embeddings.NEWS_EMBED_DIM + 1)}

    with patch("requests.post", return_value=_R()):
        v = embeddings.embed_text("hello")
    # Falls back to hash; circuit now open.
    assert len(v) == embeddings.NEWS_EMBED_DIM
    assert embeddings._ollama_unavailable is True


def test_embed_text_never_raises():
    from src.utils import embeddings
    with patch("requests.post", side_effect=Exception("boom")):
        v = embeddings.embed_text("non empty text")
    assert len(v) == embeddings.NEWS_EMBED_DIM
