"""
Base interface for quantitative (LLM-free) trading strategies.

A QuantStrategy:
  * Receives a MarketState (pair → candles, pair → regime snapshot).
  * Emits zero or more QuantSignal objects per call.
  * Declares which regimes it is *active* in; the orchestrator and
    capital allocator may filter accordingly.
  * Owns no I/O, no global state, and is fully deterministic given the
    same MarketState — so backtests and live runs share semantics.

Signals are uniform across strategies — single-asset (``pair``) or
multi-asset (``pairs``, e.g. cointegration pairs trades). The unified
contract lets the Signal Edge Library score every strategy with the
same machinery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from src.analysis.regime_detector import Regime, RegimeSnapshot


# ====================================================================== #
# Inputs
# ====================================================================== #

@dataclass
class MarketState:
    """Per-cycle market snapshot consumed by every QuantStrategy.

    Attributes
    ----------
    pair_candles : dict[str, list[dict]]
        Coinbase-shape OHLCV candle lists keyed by pair (e.g. "BTC-USD").
    regimes : dict[str, RegimeSnapshot]
        Pre-computed regime per pair (RegimeDetector.detect output).
    timestamp : float
        Cycle timestamp (UNIX seconds).
    exchange : str
        Profile / domain key — preserves domain separation downstream.
    extras : dict
        Free-form metadata bag (e.g. funding rates, OB depth) optional
        signals can read; strategies tolerate missing keys.
    """

    pair_candles: dict[str, list[dict]] = field(default_factory=dict)
    regimes: dict[str, RegimeSnapshot] = field(default_factory=dict)
    timestamp: float = 0.0
    exchange: str = "coinbase"
    extras: dict[str, Any] = field(default_factory=dict)

    def candles(self, pair: str) -> list[dict]:
        return self.pair_candles.get(pair, [])

    def regime(self, pair: str) -> Optional[RegimeSnapshot]:
        return self.regimes.get(pair)

    def pairs(self) -> list[str]:
        return list(self.pair_candles.keys())


# ====================================================================== #
# Outputs
# ====================================================================== #

@dataclass(frozen=True)
class QuantSignal:
    """Uniform output of every QuantStrategy.

    ``score`` is the directional bias in [-1, 1]:
        > 0 → long bias (or "long pair[0] short pair[1]" for a spread)
        < 0 → short bias
        = 0 → flat / no position

    ``horizon_bars`` is how many bars forward the strategy expects its
    edge to play out — used by the SignalEdgeLibrary to align the
    realised forward return when scoring.
    """

    strategy: str
    score: float
    pair: str = ""  # primary symbol for single-asset strategies
    pairs: tuple[str, ...] = ()  # for multi-asset (e.g. cointegration)
    regime: str = ""
    horizon_bars: int = 1
    confidence: float = 1.0  # raw stat conviction; allocator may scale
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return abs(self.score) > 0 and self.confidence > 0

    @property
    def direction(self) -> str:
        if self.score > 0:
            return "long"
        if self.score < 0:
            return "short"
        return "flat"

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "score": round(self.score, 6),
            "pair": self.pair,
            "pairs": list(self.pairs),
            "regime": self.regime,
            "horizon_bars": self.horizon_bars,
            "confidence": round(self.confidence, 4),
            "direction": self.direction,
            "metadata": self.metadata,
        }


# ====================================================================== #
# Strategy base
# ====================================================================== #

class QuantStrategy(ABC):
    """Abstract base for deterministic quantitative strategies."""

    #: Unique strategy name — used as the key in the SignalEdgeLibrary.
    name: str = ""

    #: Forward-return horizon in bars used to evaluate edge.
    horizon_bars: int = 1

    #: Regimes in which this strategy is meaningful. ``None`` means
    #: "active in every regime" (rare; usually a strategy has a thesis).
    active_regimes: Optional[tuple[Regime, ...]] = None

    def __init__(self, **config: Any) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # The contract
    # ------------------------------------------------------------------ #

    @abstractmethod
    def _generate(self, state: MarketState) -> list[QuantSignal]:
        """Strategy-specific signal generation — implement in subclass."""

    # ------------------------------------------------------------------ #
    # Public entry point with regime gating + safety
    # ------------------------------------------------------------------ #

    def generate(self, state: MarketState) -> list[QuantSignal]:
        """Generate signals, filtered by active_regimes per pair.

        A signal whose pair regime is *not* in ``active_regimes`` is
        dropped silently — strategies stay opinionated about where they
        work without polluting their core logic with regime checks.
        """
        raw = self._generate(state) or []
        if self.active_regimes is None:
            return raw
        active = {r.value for r in self.active_regimes}

        filtered: list[QuantSignal] = []
        for sig in raw:
            # For single-asset strategies, gate by the pair's regime.
            primary = sig.pair or (sig.pairs[0] if sig.pairs else None)
            snap = state.regime(primary) if primary else None
            if snap is None or snap.regime.value in active:
                filtered.append(sig)
        return filtered


__all__ = ["MarketState", "QuantSignal", "QuantStrategy"]
