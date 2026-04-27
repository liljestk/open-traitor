"""
Semantic news kNN prior.

For a given pair (e.g. "BTC-USD"), look up the K most-semantically-similar
historical news articles using pgvector cosine similarity over the
``news_articles.embedding`` column. Compute a prior on forward returns by
joining each neighbor against ``historical_candles`` to measure realised
post-publication drift over a configurable horizon.

Returns a structured dict the strategist can consume in its prompt context.

Domain-isolated by exchange. Cheap (one pgvector kNN + small drift query).
Cached briefly to avoid per-cycle round-trips on the same pair.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.utils.embeddings import embed_text
from src.utils.logger import get_logger

logger = get_logger("news_knn")


_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL = 60.0  # seconds


def _drift_for_neighbor(
    db,
    *,
    exchange: str,
    pair: str,
    anchor_ts: datetime,
    horizon_hours: int,
) -> Optional[float]:
    """Realised pct return of ``pair`` over [anchor_ts, anchor_ts+horizon]."""
    try:
        end = anchor_ts + timedelta(hours=horizon_hours)
        # Use ONE_HOUR if available, else ONE_DAY.
        for gran in ("ONE_HOUR", "ONE_DAY"):
            rows = db.get_candles_range(
                exchange, pair, gran,
                start=anchor_ts - timedelta(hours=2),
                end=end + timedelta(hours=2),
                limit=200,
            )
            if not rows or len(rows) < 2:
                continue
            # Find the candle closest to anchor_ts and to end.
            def _close_at(target: datetime) -> Optional[float]:
                best = None
                best_d = None
                for r in rows:
                    ts = r.get("ts")
                    if ts is None:
                        continue
                    d = abs((ts - target).total_seconds())
                    if best_d is None or d < best_d:
                        best_d = d
                        best = r.get("c")
                return best
            c0 = _close_at(anchor_ts)
            c1 = _close_at(end)
            if c0 and c1 and c0 > 0:
                return (c1 - c0) / c0
        return None
    except Exception as e:
        logger.debug(f"news_knn drift lookup failed: {e}")
        return None


def news_knn_prior(
    db,
    *,
    exchange: str,
    pair: str,
    query_text: str,
    horizon_hours: int = 24,
    k: int = 10,
    min_neighbors: int = 3,
) -> dict[str, Any]:
    """Build a kNN-based prior on forward returns for ``pair``.

    Returns::

        {
          "available": bool,
          "k_used": int,
          "mean_drift": float,        # average forward return across neighbors
          "median_drift": float,
          "hit_rate_up": float,       # fraction with positive drift
          "expected_direction": "long"|"short"|"neutral",
          "confidence": float,        # |mean_drift| × hit_consistency
          "neighbors": [{ts, title, similarity, drift}],
        }
    """
    if not query_text or not pair:
        return {"available": False, "reason": "missing inputs"}

    cache_key = (exchange, pair, query_text[:200], horizon_hours, k)
    cached = _CACHE.get(cache_key)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        vec = embed_text(query_text)
    except Exception as e:
        logger.warning(f"news_knn: embed failed: {e}")
        return {"available": False, "reason": f"embed_failed: {e}"}

    if vec is None or len(vec) == 0:
        return {"available": False, "reason": "empty embedding"}

    try:
        neighbors = db.search_similar_news(
            embedding=vec,
            k=int(k),
            min_similarity=0.0,
        )
    except Exception as e:
        logger.warning(f"news_knn: search failed: {e}")
        return {"available": False, "reason": f"search_failed: {e}"}

    if not neighbors:
        return {"available": False, "reason": "no neighbors"}

    drifts: list[float] = []
    enriched: list[dict] = []
    for n in neighbors:
        ts = n.get("published_at") or n.get("ts")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                ts = None
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        d = _drift_for_neighbor(
            db, exchange=exchange, pair=pair,
            anchor_ts=ts, horizon_hours=horizon_hours,
        )
        if d is None:
            continue
        drifts.append(d)
        enriched.append({
            "ts": ts.isoformat(),
            "title": (n.get("title") or "")[:160],
            "similarity": float(n.get("similarity") or 0.0),
            "drift": d,
        })

    if len(drifts) < min_neighbors:
        out = {
            "available": False,
            "reason": f"too_few_drifts ({len(drifts)} < {min_neighbors})",
            "k_used": len(neighbors),
        }
        _CACHE[cache_key] = (now_ts, out)
        return out

    drifts_sorted = sorted(drifts)
    mid = len(drifts_sorted) // 2
    median = drifts_sorted[mid] if len(drifts_sorted) % 2 else (
        (drifts_sorted[mid - 1] + drifts_sorted[mid]) / 2
    )
    mean = sum(drifts) / len(drifts)
    hit_up = sum(1 for d in drifts if d > 0) / len(drifts)
    consistency = abs(hit_up - 0.5) * 2  # 0..1
    confidence = max(0.0, min(1.0, abs(mean) * 5.0 * consistency))

    if mean > 0.002 and hit_up >= 0.6:
        direction = "long"
    elif mean < -0.002 and hit_up <= 0.4:
        direction = "short"
    else:
        direction = "neutral"

    out = {
        "available": True,
        "k_used": len(drifts),
        "mean_drift": round(mean, 6),
        "median_drift": round(median, 6),
        "hit_rate_up": round(hit_up, 3),
        "expected_direction": direction,
        "confidence": round(confidence, 3),
        "horizon_hours": horizon_hours,
        "neighbors": enriched[:5],  # top-5 for context
    }
    _CACHE[cache_key] = (now_ts, out)
    return out
