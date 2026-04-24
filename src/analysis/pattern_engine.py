"""
Catalyst Pattern Engine.

Generic, symbol-agnostic price-pattern matching around catalyst events.

Encoding: each historical event is encoded as a fixed-length 64-dim
fingerprint of the *pre-event window* only (no look-ahead). Forward
returns at 1d / 5d / 20d horizons are stored as **labels** alongside the
vector, not features. At query time the same encoding is run on the most
recent 30 bars before an upcoming catalyst — the resulting vector is
matched against the historical store via pgvector cosine similarity, and
the label-weighted average gives the expected drift.

Fingerprint layout (64 floats, all scale-invariant):
  [0..29]   z-scored log-returns over the 30 bars preceding the anchor
  [30..59]  z-scored log-volume-changes over the same 30 bars
  [60]      pre-window total log-return (signed)
  [61]      pre-window realised volatility (std of log returns)
  [62]      RSI(14) at anchor in [-1, 1]   (rsi/50 - 1)
  [63]      slope of an OLS linear fit of log-prices over the pre-window,
            normalised by pre-vol so it is comparable across symbols.

This keeps the encoding deterministic, robust to scale (BTC vs AAPL), and
purely backwards-looking — preventing label leakage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

import numpy as np

from src.utils.logger import get_logger
from src.utils.stats import StatsDB
from src.utils.stats_patterns import PATTERN_VECTOR_DIM

logger = get_logger("analysis.pattern_engine")


# ─── Public constants ──────────────────────────────────────────────────────


PRE_WINDOW_BARS: int = 30      # bars used to encode the fingerprint
FORWARD_HORIZONS: dict[str, int] = {"1d": 1, "5d": 5, "20d": 20}


# ─── Fingerprint extraction ────────────────────────────────────────────────


def extract_fingerprint(
    candles: Sequence[dict],
    anchor_ts: datetime,
    pre_bars: int = PRE_WINDOW_BARS,
) -> Optional[np.ndarray]:
    """Encode the ``pre_bars`` candles immediately preceding ``anchor_ts``.

    Returns a 64-dim float32 numpy array, or ``None`` if there are not
    enough valid candles. The vector is L2-normalised so cosine similarity
    in pgvector behaves correctly.
    """
    if not candles:
        return None
    # Filter and order strictly by timestamp ascending.
    rows: list[tuple[datetime, float, float]] = []
    for c in candles:
        ts = c.get("ts")
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            close = float(c["c"])
            vol = float(c.get("v", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if close <= 0:
            continue
        if ts > anchor_ts:
            continue
        rows.append((ts, close, vol))
    if len(rows) < pre_bars + 1:
        return None
    rows.sort(key=lambda r: r[0])
    rows = rows[-(pre_bars + 1):]  # need pre_bars returns ⇒ pre_bars+1 closes
    closes = np.array([r[1] for r in rows], dtype=np.float64)
    vols = np.array([r[2] for r in rows], dtype=np.float64)

    # Log-returns of closes.
    log_ret = np.diff(np.log(closes))
    if len(log_ret) != pre_bars:
        return None
    # z-score the returns.
    ret_mu = float(log_ret.mean())
    ret_sd = float(log_ret.std(ddof=0))
    if ret_sd <= 1e-12:
        ret_z = np.zeros_like(log_ret)
    else:
        ret_z = (log_ret - ret_mu) / ret_sd

    # Volume z-score (use log1p so spikes don't blow up the std).
    log_vol = np.log1p(np.maximum(vols[1:], 0))
    vmu = float(log_vol.mean())
    vsd = float(log_vol.std(ddof=0))
    if vsd <= 1e-12:
        vol_z = np.zeros_like(log_vol)
    else:
        vol_z = (log_vol - vmu) / vsd

    # Total pre-window log-return + realised vol.
    total_ret = float(np.log(closes[-1] / closes[0]))
    realised_vol = ret_sd

    # RSI(14) at anchor (Wilder's smoothing) — input is `closes`.
    rsi_at_anchor = _rsi_at_last(closes, period=14)
    rsi_norm = (rsi_at_anchor / 50.0) - 1.0  # ∈ [-1, 1]

    # OLS slope of log-prices, normalised by realised vol.
    log_p = np.log(closes)
    x = np.arange(len(log_p), dtype=np.float64)
    x_mean = x.mean()
    y_mean = log_p.mean()
    denom = float(((x - x_mean) ** 2).sum())
    raw_slope = float(((x - x_mean) * (log_p - y_mean)).sum() / denom) if denom > 0 else 0.0
    slope_norm = raw_slope / max(realised_vol, 1e-6)
    # Clip extremes so a single outlier symbol can't dominate the fingerprint.
    slope_norm = float(np.clip(slope_norm, -10.0, 10.0))

    summary = np.array([
        np.tanh(total_ret * 4.0),    # squashed to [-1, 1]
        np.tanh(realised_vol * 30.0),
        rsi_norm,
        np.tanh(slope_norm / 5.0),
    ], dtype=np.float64)

    vec = np.concatenate([ret_z, vol_z, summary]).astype(np.float32)
    if vec.size != PATTERN_VECTOR_DIM:
        # Defensive — should never happen given pre_bars=30.
        if vec.size > PATTERN_VECTOR_DIM:
            vec = vec[:PATTERN_VECTOR_DIM]
        else:
            vec = np.pad(vec, (0, PATTERN_VECTOR_DIM - vec.size))
    # L2-normalise so cosine similarity ≡ dot product.
    norm = float(np.linalg.norm(vec))
    if norm > 1e-12:
        vec = vec / norm
    return vec


def _rsi_at_last(closes: np.ndarray, period: int = 14) -> float:
    """Wilder's RSI at the last index of ``closes``. Returns 50 if undefined."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Wilder smoothing: simple average of the first `period`, then
    # recursive update.
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


# ─── Forward-return labelling ──────────────────────────────────────────────


def compute_forward_returns(
    candles: Sequence[dict],
    anchor_ts: datetime,
    horizons_bars: dict[str, int] = FORWARD_HORIZONS,
) -> dict[str, Optional[float]]:
    """Compute log-returns over each named horizon AFTER ``anchor_ts``.

    ``horizons_bars`` maps a label (e.g. "5d") to a number of bars at the
    series' native granularity. Daily candles ⇒ "5d" means 5 bars.
    """
    if not candles:
        return {k: None for k in horizons_bars}
    rows = sorted(
        (c for c in candles if isinstance(c.get("ts"), datetime)),
        key=lambda c: c["ts"],
    )
    # Find the first close at-or-after anchor.
    base_close: Optional[float] = None
    base_idx: Optional[int] = None
    for i, c in enumerate(rows):
        ts = c["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= anchor_ts:
            try:
                base_close = float(c["c"])
                base_idx = i
                break
            except (KeyError, TypeError, ValueError):
                continue
    if base_close is None or base_idx is None or base_close <= 0:
        return {k: None for k in horizons_bars}
    out: dict[str, Optional[float]] = {}
    for label, bars in horizons_bars.items():
        target_idx = base_idx + bars
        if target_idx >= len(rows):
            out[label] = None
            continue
        try:
            target_close = float(rows[target_idx]["c"])
            if target_close <= 0:
                out[label] = None
            else:
                out[label] = float(math.log(target_close / base_close))
        except (KeyError, TypeError, ValueError):
            out[label] = None
    return out


# ─── Indexing & matching ───────────────────────────────────────────────────


def index_event(
    db: StatsDB,
    exchange: str,
    symbol: str,
    event_id: Optional[int],
    event_type: str,
    anchor_ts: datetime,
    granularity: str = "ONE_DAY",
    pre_bars: int = PRE_WINDOW_BARS,
    horizons_bars: dict[str, int] = FORWARD_HORIZONS,
    sample_meta: Optional[dict] = None,
) -> Optional[int]:
    """Compute fingerprint + forward returns for one historical event and
    upsert into ``pattern_fingerprints``. Returns the row id, or ``None``
    if there isn't enough surrounding data."""
    pre_start = anchor_ts - timedelta(days=pre_bars + 5)
    post_end = anchor_ts + timedelta(days=max(horizons_bars.values()) + 5)
    candles = db.get_candles_range(
        exchange=exchange,
        symbol=symbol,
        granularity=granularity,
        start=pre_start,
        end=post_end,
    )
    if not candles:
        return None
    vec = extract_fingerprint(candles, anchor_ts, pre_bars=pre_bars)
    if vec is None:
        return None
    fwd = compute_forward_returns(candles, anchor_ts, horizons_bars=horizons_bars)
    return db.upsert_pattern_fingerprint(
        exchange=exchange,
        symbol=symbol,
        event_id=event_id,
        event_type=event_type,
        anchor_ts=anchor_ts,
        window_pre_days=pre_bars,
        window_post_days=max(horizons_bars.values()),
        vector=vec.tolist(),
        forward_returns=fwd,
        sample_meta=sample_meta or {},
    )


def index_all_events(
    db: StatsDB,
    exchange: str,
    symbols: Optional[Iterable[str]] = None,
    granularity: str = "ONE_DAY",
    event_type: Optional[str] = None,
    min_anchor_age_days: int = 30,
) -> dict:
    """Index every catalyst whose anchor is older than
    ``min_anchor_age_days`` (so 20-day forward returns are observable).
    Returns ``{indexed, skipped}``."""
    end = datetime.now(timezone.utc) - timedelta(days=int(min_anchor_age_days))
    indexed = 0
    skipped = 0
    sym_list = list(symbols) if symbols else None
    if sym_list:
        events: list[dict] = []
        for sym in sym_list:
            events.extend(db.get_catalyst_events(
                exchange=exchange, symbol=sym, end=end, event_type=event_type, limit=5000,
            ))
    else:
        events = db.get_catalyst_events(
            exchange=exchange, end=end, event_type=event_type, limit=20000,
        )
    for ev in events:
        try:
            row_id = index_event(
                db=db,
                exchange=ev["exchange"],
                symbol=ev["symbol"],
                event_id=int(ev["id"]),
                event_type=ev["event_type"],
                anchor_ts=ev["event_ts"],
                granularity=granularity,
                sample_meta={"source": ev.get("source", "")},
            )
            if row_id is not None:
                indexed += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            logger.debug(f"index_event({ev.get('symbol')}@{ev.get('event_ts')}) failed: {e}")
    return {"indexed": indexed, "skipped": skipped}


# ─── Aggregation ───────────────────────────────────────────────────────────


@dataclass
class PatternOutcome:
    """Aggregated forward-return forecast from nearest-neighbour matches."""

    expected_drift: dict[str, float] = field(default_factory=dict)
    dispersion: dict[str, float] = field(default_factory=dict)
    n_matches: int = 0
    confidence: float = 0.0
    direction: str = "neutral"     # bullish | bearish | neutral
    matches: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "expected_drift": self.expected_drift,
            "dispersion": self.dispersion,
            "n_matches": self.n_matches,
            "confidence": self.confidence,
            "direction": self.direction,
            "matches": self.matches,
        }


def aggregate_outcome(
    matches: list[dict],
    horizon: str = "5d",
    min_matches: int = 3,
) -> PatternOutcome:
    """Compute similarity-weighted expected forward return + dispersion.

    Confidence ∈ [0, 1] is shaped by:
      * Number of matches (tanh saturating at 20).
      * Average similarity of contributing matches.
      * Inverse-dispersion penalty (tighter cluster ⇒ higher confidence).
    """
    if not matches:
        return PatternOutcome()
    horizon_key = f"forward_return_{horizon}"
    weights: list[float] = []
    rets_per_horizon: dict[str, list[float]] = {h: [] for h in FORWARD_HORIZONS}
    weights_per_horizon: dict[str, list[float]] = {h: [] for h in FORWARD_HORIZONS}
    sims: list[float] = []
    for m in matches:
        sim = float(m.get("similarity", 0.0))
        if sim < 0:
            sim = 0.0
        sims.append(sim)
        for h in FORWARD_HORIZONS:
            r = m.get(f"forward_return_{h}")
            if r is None:
                continue
            try:
                rv = float(r)
            except (TypeError, ValueError):
                continue
            rets_per_horizon[h].append(rv)
            weights_per_horizon[h].append(sim)

    expected: dict[str, float] = {}
    dispersion: dict[str, float] = {}
    for h in FORWARD_HORIZONS:
        rs = np.array(rets_per_horizon[h], dtype=np.float64)
        ws = np.array(weights_per_horizon[h], dtype=np.float64)
        if rs.size == 0 or ws.sum() <= 0:
            continue
        ws = ws / ws.sum()
        mu = float((rs * ws).sum())
        var = float((ws * (rs - mu) ** 2).sum())
        expected[h] = mu
        dispersion[h] = math.sqrt(max(var, 0.0))

    n = len(matches)
    avg_sim = float(np.mean(sims)) if sims else 0.0
    primary_disp = dispersion.get(horizon, float("inf"))
    primary_mu = expected.get(horizon, 0.0)

    # Confidence: scale by (n_matches saturation) × (avg similarity) × (signal/noise).
    n_factor = math.tanh(n / 20.0)
    sn = abs(primary_mu) / (primary_disp + 1e-6) if primary_disp != float("inf") else 0.0
    sn_factor = math.tanh(sn / 1.5)
    enough = 1.0 if n >= min_matches else (n / float(min_matches))
    confidence = max(0.0, min(1.0, n_factor * avg_sim * sn_factor * enough))

    if primary_mu > 0.005:
        direction = "bullish"
    elif primary_mu < -0.005:
        direction = "bearish"
    else:
        direction = "neutral"

    # Trim matches we surface upstream so the payload stays small.
    surfaced = [
        {
            "id": m.get("id"),
            "symbol": m.get("symbol"),
            "event_type": m.get("event_type"),
            "anchor_ts": (
                m["anchor_ts"].isoformat()
                if isinstance(m.get("anchor_ts"), datetime)
                else m.get("anchor_ts")
            ),
            "similarity": float(m.get("similarity", 0.0)),
            "forward_return_1d": m.get("forward_return_1d"),
            "forward_return_5d": m.get("forward_return_5d"),
            "forward_return_20d": m.get("forward_return_20d"),
        }
        for m in matches[:10]
    ]

    return PatternOutcome(
        expected_drift=expected,
        dispersion=dispersion,
        n_matches=n,
        confidence=confidence,
        direction=direction,
        matches=surfaced,
    )


# ─── Sentiment fusion ──────────────────────────────────────────────────────


def fuse_with_sentiment(
    outcome: PatternOutcome,
    sentiment_score: Optional[float],
    horizon: str = "5d",
    sentiment_weight: float = 0.4,
) -> PatternOutcome:
    """Bayesian-style shrink toward zero when current sentiment contradicts
    the historical drift.

    ``sentiment_score`` ∈ [-1, +1] (matches ``news_bias`` convention).
    """
    if outcome.n_matches == 0 or sentiment_score is None:
        return outcome
    s = float(np.clip(sentiment_score, -1.0, 1.0))
    mu = outcome.expected_drift.get(horizon)
    if mu is None:
        return outcome
    # Agreement ∈ [-1, +1]: +1 if same sign as historical drift, −1 if opposite.
    agree = 1.0 if (mu * s) >= 0 else -1.0
    shrink = 1.0 + sentiment_weight * agree * abs(s)
    new_mu = mu * shrink
    outcome.expected_drift[horizon] = new_mu
    # Update derived fields.
    if new_mu > 0.005:
        outcome.direction = "bullish"
    elif new_mu < -0.005:
        outcome.direction = "bearish"
    else:
        outcome.direction = "neutral"
    if agree < 0:
        outcome.confidence *= 1.0 - 0.5 * abs(s)
    return outcome


# ─── Top-level inference helper ────────────────────────────────────────────


def predict_for_upcoming(
    db: StatsDB,
    exchange: str,
    symbol: str,
    upcoming_event_ts: datetime,
    event_type: str,
    granularity: str = "ONE_DAY",
    sentiment_score: Optional[float] = None,
    k: int = 20,
) -> PatternOutcome:
    """End-to-end: build the current fingerprint, look up nearest neighbours,
    aggregate, fuse with sentiment, return a ``PatternOutcome``."""
    pre_start = upcoming_event_ts - timedelta(days=PRE_WINDOW_BARS + 5)
    candles = db.get_candles_range(
        exchange=exchange,
        symbol=symbol,
        granularity=granularity,
        start=pre_start,
        end=upcoming_event_ts,
    )
    if not candles:
        return PatternOutcome()
    vec = extract_fingerprint(candles, upcoming_event_ts)
    if vec is None:
        return PatternOutcome()
    matches = db.find_similar_fingerprints(
        exchange=exchange,
        query_vector=vec.tolist(),
        k=int(k),
        event_type=event_type,
        exclude_symbol=symbol,
        exclude_anchor_after=upcoming_event_ts,
    )
    outcome = aggregate_outcome(matches, horizon="5d")
    return fuse_with_sentiment(outcome, sentiment_score, horizon="5d")
