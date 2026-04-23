"""
Signal Edge Library — quantified, regime-conditional alpha attribution.

This is the substrate the new quantitative tier (and eventually the LLM
advisory layer) plug into. Every named signal — EMA cross, RSI
divergence, sentiment delta, funding-rate flip, on-chain whale move,
whatever — is registered here and continuously scored:

    * For every (signal, regime, profile) triple, the library tracks the
      realized forward returns of bars where the signal fired.
    * From that history it computes EdgeStats: sample count, win rate,
      mean return, Sharpe.
    * From EdgeStats it derives dynamic weights so the ensemble reweights
      itself toward whatever is currently working — *without* a human in
      the loop. That is the autonomy requirement.

Storage is pluggable:

    * InMemorySignalEdgeStore — used by tests and by the backtester to
      isolate runs.
    * PostgresSignalEdgeStore — used in production, profile-scoped
      (``exchange`` column per repo convention so domain separation
      tests stay green).

Pure data layer. No I/O on the hot path other than store calls.
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from src.utils.logger import get_logger

logger = get_logger("analysis.signal_edge")


# ====================================================================== #
# Data model
# ====================================================================== #

@dataclass(frozen=True)
class SignalSample:
    """One observation: signal X fired at bar T, realized return was Y."""

    signal_name: str
    regime: str  # Regime.value
    direction: str  # "long" | "short" | "flat"
    score: float  # raw signal value, conventionally clipped to [-1, 1]
    forward_return: float  # realized return over the evaluation horizon
    pair: str
    exchange: str  # profile / domain key
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "signal_name": self.signal_name,
            "regime": self.regime,
            "direction": self.direction,
            "score": round(self.score, 6),
            "forward_return": round(self.forward_return, 6),
            "pair": self.pair,
            "exchange": self.exchange,
            "timestamp": round(self.timestamp, 3),
        }


@dataclass(frozen=True)
class EdgeStats:
    """Per (signal, regime) edge summary computed from samples."""

    signal_name: str
    regime: str
    exchange: str
    n_samples: int
    win_rate: float  # fraction of samples with positive direction-adjusted return
    avg_return: float  # mean direction-adjusted return per fired bar
    sharpe: float  # mean / std (annualisation left to consumers)
    last_updated: float = field(default_factory=time.time)

    @property
    def is_actionable(self) -> bool:
        """True when there are enough samples and edge is positive."""
        return self.n_samples >= 30 and self.sharpe > 0.1 and self.avg_return > 0

    def to_dict(self) -> dict:
        return {
            "signal_name": self.signal_name,
            "regime": self.regime,
            "exchange": self.exchange,
            "n_samples": self.n_samples,
            "win_rate": round(self.win_rate, 4),
            "avg_return": round(self.avg_return, 6),
            "sharpe": round(self.sharpe, 4),
            "last_updated": round(self.last_updated, 3),
        }


# ====================================================================== #
# Storage interface + in-memory backend (tests, backtester)
# ====================================================================== #

class SignalEdgeStore(ABC):
    """Persistence interface for signal samples + edge stats."""

    @abstractmethod
    def add_sample(self, sample: SignalSample) -> None: ...

    @abstractmethod
    def get_edge(
        self,
        signal_name: str,
        regime: str,
        exchange: str,
        lookback_days: int = 30,
        now_ts: Optional[float] = None,
    ) -> EdgeStats: ...

    @abstractmethod
    def all_edges(
        self,
        regime: str,
        exchange: str,
        lookback_days: int = 30,
        now_ts: Optional[float] = None,
    ) -> list[EdgeStats]: ...

    @abstractmethod
    def list_signals(self, exchange: str) -> list[str]: ...


class InMemorySignalEdgeStore(SignalEdgeStore):
    """Thread-unsafe in-memory store for tests + backtests."""

    def __init__(self) -> None:
        # Keyed by (exchange, signal_name); list of samples.
        self._samples: dict[tuple[str, str], list[SignalSample]] = defaultdict(list)

    def add_sample(self, sample: SignalSample) -> None:
        self._samples[(sample.exchange, sample.signal_name)].append(sample)

    def get_edge(
        self,
        signal_name: str,
        regime: str,
        exchange: str,
        lookback_days: int = 30,
        now_ts: Optional[float] = None,
    ) -> EdgeStats:
        cutoff = (now_ts if now_ts is not None else time.time()) - lookback_days * 86400.0
        samples = [
            s for s in self._samples.get((exchange, signal_name), [])
            if s.regime == regime and s.timestamp >= cutoff
        ]
        return _compute_edge(signal_name, regime, exchange, samples)

    def all_edges(
        self,
        regime: str,
        exchange: str,
        lookback_days: int = 30,
        now_ts: Optional[float] = None,
    ) -> list[EdgeStats]:
        cutoff = (now_ts if now_ts is not None else time.time()) - lookback_days * 86400.0
        names = [name for ex, name in self._samples.keys() if ex == exchange]
        out: list[EdgeStats] = []
        for name in sorted(set(names)):
            samples = [
                s for s in self._samples[(exchange, name)]
                if s.regime == regime and s.timestamp >= cutoff
            ]
            if samples:
                out.append(_compute_edge(name, regime, exchange, samples))
        return out

    def list_signals(self, exchange: str) -> list[str]:
        return sorted({name for ex, name in self._samples.keys() if ex == exchange})


# ====================================================================== #
# Postgres-backed backend (production)
# ====================================================================== #

class PostgresSignalEdgeStore(SignalEdgeStore):
    """Postgres-backed store. Lazy schema init on first use.

    Uses the same connection pool as src.utils.stats.StatsDB so we share
    a single pool process-wide. Profile/domain separation enforced via
    the ``exchange`` column (matches repo convention).
    """

    def __init__(self, stats_db=None) -> None:
        # Lazy import to keep this module importable without psycopg2 in
        # test environments where Postgres is unavailable.
        if stats_db is None:
            from src.utils.stats import get_stats_db  # type: ignore
            stats_db = get_stats_db()
        self._db = stats_db
        self._ensure_schema()

    # ------------------------------------------------------------------ #

    def _ensure_schema(self) -> None:
        with self._db._get_conn() as conn:  # type: ignore[attr-defined]
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS signal_samples (
                        id BIGSERIAL PRIMARY KEY,
                        ts DOUBLE PRECISION NOT NULL,
                        exchange TEXT NOT NULL DEFAULT 'coinbase',
                        signal_name TEXT NOT NULL,
                        regime TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        score DOUBLE PRECISION NOT NULL,
                        forward_return DOUBLE PRECISION NOT NULL,
                        pair TEXT NOT NULL
                    )
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_signal_samples_lookup "
                    "ON signal_samples(exchange, signal_name, regime, ts DESC)"
                )

    # ------------------------------------------------------------------ #

    def add_sample(self, sample: SignalSample) -> None:
        with self._db._get_conn() as conn:  # type: ignore[attr-defined]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO signal_samples
                      (ts, exchange, signal_name, regime, direction,
                       score, forward_return, pair)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sample.timestamp,
                        sample.exchange,
                        sample.signal_name,
                        sample.regime,
                        sample.direction,
                        sample.score,
                        sample.forward_return,
                        sample.pair,
                    ),
                )

    def get_edge(
        self,
        signal_name: str,
        regime: str,
        exchange: str,
        lookback_days: int = 30,
        now_ts: Optional[float] = None,
    ) -> EdgeStats:
        cutoff = (now_ts if now_ts is not None else time.time()) - lookback_days * 86400.0
        with self._db._get_conn() as conn:  # type: ignore[attr-defined]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT direction, score, forward_return
                    FROM signal_samples
                    WHERE exchange = %s AND signal_name = %s
                      AND regime = %s AND ts >= %s
                    """,
                    (exchange, signal_name, regime, cutoff),
                )
                rows = cur.fetchall() or []

        # Reuse pure stats helper so semantics match in-memory backend.
        samples = [
            SignalSample(
                signal_name=signal_name,
                regime=regime,
                direction=r["direction"],
                score=float(r["score"]),
                forward_return=float(r["forward_return"]),
                pair="",
                exchange=exchange,
            )
            for r in rows
        ]
        return _compute_edge(signal_name, regime, exchange, samples)

    def all_edges(
        self,
        regime: str,
        exchange: str,
        lookback_days: int = 30,
        now_ts: Optional[float] = None,
    ) -> list[EdgeStats]:
        cutoff = (now_ts if now_ts is not None else time.time()) - lookback_days * 86400.0
        with self._db._get_conn() as conn:  # type: ignore[attr-defined]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT signal_name, direction, score, forward_return
                    FROM signal_samples
                    WHERE exchange = %s AND regime = %s AND ts >= %s
                    """,
                    (exchange, regime, cutoff),
                )
                rows = cur.fetchall() or []

        grouped: dict[str, list[SignalSample]] = defaultdict(list)
        for r in rows:
            grouped[r["signal_name"]].append(
                SignalSample(
                    signal_name=r["signal_name"],
                    regime=regime,
                    direction=r["direction"],
                    score=float(r["score"]),
                    forward_return=float(r["forward_return"]),
                    pair="",
                    exchange=exchange,
                )
            )
        return [
            _compute_edge(name, regime, exchange, samples)
            for name, samples in sorted(grouped.items())
            if samples
        ]

    def list_signals(self, exchange: str) -> list[str]:
        with self._db._get_conn() as conn:  # type: ignore[attr-defined]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT signal_name FROM signal_samples "
                    "WHERE exchange = %s ORDER BY signal_name",
                    (exchange,),
                )
                rows = cur.fetchall() or []
        return [r["signal_name"] for r in rows]


# ====================================================================== #
# Pure helpers
# ====================================================================== #

def _compute_edge(
    signal_name: str,
    regime: str,
    exchange: str,
    samples: list[SignalSample],
) -> EdgeStats:
    """Reduce a list of samples into an EdgeStats record."""
    if not samples:
        return EdgeStats(
            signal_name=signal_name, regime=regime, exchange=exchange,
            n_samples=0, win_rate=0.0, avg_return=0.0, sharpe=0.0,
        )

    # Direction-adjusted return: long signals reward up moves, short
    # signals reward down moves, flat signals contribute zero.
    adjusted: list[float] = []
    for s in samples:
        if s.direction == "long":
            adjusted.append(s.forward_return)
        elif s.direction == "short":
            adjusted.append(-s.forward_return)
        # flat → 0 contribution; deliberately *included* in sample count
        # because "fired flat → got nothing" is informative.
        else:
            adjusted.append(0.0)

    n = len(adjusted)
    mean = sum(adjusted) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in adjusted) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    sharpe = (mean / std) if std > 1e-12 else 0.0
    wins = sum(1 for x in adjusted if x > 0)
    win_rate = wins / n

    return EdgeStats(
        signal_name=signal_name,
        regime=regime,
        exchange=exchange,
        n_samples=n,
        win_rate=win_rate,
        avg_return=mean,
        sharpe=sharpe,
    )


# ====================================================================== #
# Library — registry + scoring + dynamic weights
# ====================================================================== #

# Type alias: a signal generator takes candles and returns a score in
# [-1, 1] (or any real number; consumers may clip). Convention:
#   score >  0 → bullish (long bias)
#   score == 0 → no signal (flat)
#   score <  0 → bearish (short bias)
SignalFn = Callable[[list[dict]], float]


class SignalEdgeLibrary:
    """Registry of named signals + persistent edge tracking + dynamic weights.

    Autonomy contract:
      * ``record_sample`` is called by the backtester / live loop after
        every fired bar with the realized forward return → the system
        teaches itself.
      * ``weights`` returns regime-conditional weights derived from the
        store; if nothing is known, falls back to equal weight rather
        than crashing. Self-healing.
    """

    def __init__(
        self,
        store: Optional[SignalEdgeStore] = None,
        exchange: str = "coinbase",
    ) -> None:
        self.store: SignalEdgeStore = store or InMemorySignalEdgeStore()
        self.exchange = exchange
        self._signals: dict[str, SignalFn] = {}

    # ------------------------------------------------------------------ #
    # Registration & scoring
    # ------------------------------------------------------------------ #

    def register(self, name: str, fn: SignalFn) -> None:
        if not name or not callable(fn):
            raise ValueError("signal name and callable required")
        self._signals[name] = fn

    def registered(self) -> list[str]:
        return sorted(self._signals.keys())

    def compute(self, name: str, candles: list[dict]) -> float:
        if name not in self._signals:
            raise KeyError(f"signal not registered: {name}")
        try:
            value = float(self._signals[name](candles) or 0.0)
        except Exception as exc:
            logger.warning("signal_compute_failed name=%s err=%s", name, exc)
            return 0.0
        if not math.isfinite(value):
            return 0.0
        # Clip to convention range so downstream weighting is well-defined.
        return max(-1.0, min(1.0, value))

    def compute_all(self, candles: list[dict]) -> dict[str, float]:
        return {name: self.compute(name, candles) for name in self._signals}

    # ------------------------------------------------------------------ #
    # Sample persistence + edge lookup
    # ------------------------------------------------------------------ #

    def record_sample(
        self,
        *,
        signal_name: str,
        regime: str,
        score: float,
        forward_return: float,
        pair: str,
        timestamp: Optional[float] = None,
    ) -> SignalSample:
        if not math.isfinite(score) or not math.isfinite(forward_return):
            raise ValueError("non-finite score / forward_return")
        direction = "long" if score > 0 else ("short" if score < 0 else "flat")
        sample = SignalSample(
            signal_name=signal_name,
            regime=regime,
            direction=direction,
            score=float(score),
            forward_return=float(forward_return),
            pair=pair,
            exchange=self.exchange,
            timestamp=timestamp if timestamp is not None else time.time(),
        )
        self.store.add_sample(sample)
        return sample

    def edge(self, signal_name: str, regime: str, lookback_days: int = 30) -> EdgeStats:
        return self.store.get_edge(signal_name, regime, self.exchange, lookback_days)

    def all_edges(self, regime: str, lookback_days: int = 30) -> list[EdgeStats]:
        return self.store.all_edges(regime, self.exchange, lookback_days)

    # ------------------------------------------------------------------ #
    # Dynamic weighting — the autonomy payoff
    # ------------------------------------------------------------------ #

    def weights(
        self,
        regime: str,
        *,
        lookback_days: int = 30,
        min_samples: int = 30,
        prior_weight: float = 0.05,
    ) -> dict[str, float]:
        """Regime-conditional dynamic weights for every registered signal.

        Logic:
          * Pull EdgeStats per signal for ``regime``.
          * Each signal contributes max(0, sharpe) to the raw weight.
          * Signals below ``min_samples`` get a small prior weight so
            they still get *some* allocation (exploration).
          * Normalised so they sum to 1.0; equal weight if every signal
            is below the min.

        This is the Bayesian-ish online weighting that lets the ensemble
        re-allocate toward whatever is currently working in the active
        regime — without any human action.
        """
        registered = self.registered()
        if not registered:
            return {}

        edges = {e.signal_name: e for e in self.all_edges(regime, lookback_days)}
        raw: dict[str, float] = {}
        for name in registered:
            e = edges.get(name)
            if e is None or e.n_samples < min_samples:
                raw[name] = prior_weight  # exploration prior
            else:
                raw[name] = max(0.0, e.sharpe)

        total = sum(raw.values())
        if total <= 0:
            n = len(registered)
            return {name: 1.0 / n for name in registered}
        return {name: w / total for name, w in raw.items()}

    # ------------------------------------------------------------------ #
    # Convenience: compute combined directional score using current weights
    # ------------------------------------------------------------------ #

    def combined_score(
        self,
        candles: list[dict],
        regime: str,
        *,
        lookback_days: int = 30,
        min_samples: int = 30,
    ) -> float:
        """Weighted sum of all signal scores → final directional bias.

        Returns a value in [-1, 1]. Consumers map to {long, short, flat}.
        """
        if not self._signals:
            return 0.0
        scores = self.compute_all(candles)
        weights = self.weights(
            regime, lookback_days=lookback_days, min_samples=min_samples
        )
        combined = sum(scores.get(n, 0.0) * weights.get(n, 0.0) for n in self._signals)
        return max(-1.0, min(1.0, combined))


__all__ = [
    "SignalSample",
    "EdgeStats",
    "SignalEdgeStore",
    "InMemorySignalEdgeStore",
    "PostgresSignalEdgeStore",
    "SignalEdgeLibrary",
    "SignalFn",
]
