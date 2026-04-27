"""Lightweight text embedding helper.

Provides a single function ``embed_text(text) -> list[float]`` that:

1. Tries the configured Ollama embedding model (default
   ``nomic-embed-text`` → 768 dims). One HTTP call, short timeout.
2. Falls back to a deterministic, dependency-free token-hash bag-of-words
   embedding (``_hash_embedding``) so the news pipeline keeps producing
   embeddings even when Ollama is offline. The hash embedding has the
   same dimensionality so DB rows remain compatible.

This module is deliberately self-contained — no network state, no global
client — so importing it is safe at module-load time and has zero side
effects.

Configuration via env vars:
- ``OLLAMA_BASE_URL``      (default ``http://localhost:11434``)
- ``NEWS_EMBED_MODEL``     (default ``nomic-embed-text``)
- ``NEWS_EMBED_DIM``       (default ``768``; must match the vector column)
- ``NEWS_EMBED_TIMEOUT``   (seconds, default ``5``)
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("utils.embeddings")

# Hard-coded so callers can import the constant without reading env at
# import time. Operators changing this MUST also drop+recreate the
# ``news_articles.embedding`` column to match (see stats_news.py).
NEWS_EMBED_DIM: int = int(os.environ.get("NEWS_EMBED_DIM", "768"))
_OLLAMA_BASE: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
_EMBED_MODEL: str = os.environ.get("NEWS_EMBED_MODEL", "nomic-embed-text")
_EMBED_TIMEOUT: float = float(os.environ.get("NEWS_EMBED_TIMEOUT", "5"))

# Module-level kill switch flipped to True after a single failed Ollama
# round-trip so we don't spam the network for every article in a batch.
_ollama_unavailable: bool = False


def _try_ollama(text: str) -> Optional[list[float]]:
    """Single Ollama embedding call. Returns vector or None on failure."""
    global _ollama_unavailable
    if _ollama_unavailable:
        return None
    try:
        r = requests.post(
            f"{_OLLAMA_BASE}/api/embeddings",
            json={"model": _EMBED_MODEL, "prompt": text},
            timeout=_EMBED_TIMEOUT,
        )
        if r.status_code != 200:
            _ollama_unavailable = True
            logger.warning(
                f"Ollama embeddings returned HTTP {r.status_code}; "
                f"falling back to hash embedding for this run"
            )
            return None
        data = r.json()
        emb = data.get("embedding") or []
        if not emb:
            return None
        if len(emb) != NEWS_EMBED_DIM:
            # First run with a new model: log once, then degrade.
            _ollama_unavailable = True
            logger.warning(
                f"Ollama embedding dim {len(emb)} != configured "
                f"NEWS_EMBED_DIM={NEWS_EMBED_DIM}. Falling back to hash."
            )
            return None
        return [float(x) for x in emb]
    except Exception as e:
        _ollama_unavailable = True
        logger.info(f"Ollama embeddings unavailable ({e}); using hash fallback")
        return None


def _hash_embedding(text: str, dim: int = NEWS_EMBED_DIM) -> list[float]:
    """Deterministic hashed bag-of-tokens embedding.

    Each token contributes ±1 to a single bucket determined by SHA-1 of
    the token. The vector is then L2-normalised. This is *not* a good
    semantic embedding, but it preserves token overlap (cosine similarity
    grows with shared tokens) so the column is never NULL and basic
    duplicate detection still works when the LLM is offline.
    """
    vec = [0.0] * dim
    if not text:
        return vec
    for tok in text.lower().split():
        h = hashlib.sha1(tok.encode("utf-8", errors="ignore")).digest()
        # First 4 bytes → bucket; 5th byte sign.
        bucket = int.from_bytes(h[:4], "big") % dim
        sign = -1.0 if (h[4] & 1) else 1.0
        vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def embed_text(text: str) -> list[float]:
    """Return a ``NEWS_EMBED_DIM``-length embedding for ``text``.

    Tries Ollama first, falls back to a deterministic hash embedding so
    callers always get a usable vector. The function never raises.
    """
    text = (text or "").strip()
    if not text:
        return [0.0] * NEWS_EMBED_DIM
    out = _try_ollama(text)
    if out is not None:
        return out
    return _hash_embedding(text)


def reset_ollama_circuit() -> None:
    """Test/operator hook: re-enable Ollama after the circuit tripped."""
    global _ollama_unavailable
    _ollama_unavailable = False
