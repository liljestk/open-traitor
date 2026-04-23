"""
Cross-sectional momentum factor.

Thesis: in trending regimes, the strongest recent performers tend to
keep outperforming over the next ``horizon_bars`` (and the weakest
keep underperforming). This is the cross-sectional momentum factor —
one of the most robust premia in published asset-pricing literature
(Jegadeesh & Titman, 1993; Asness et al., 2013).

Per cycle:
  1. For every pair with sufficient history, compute the past-window
     log return (``lookback_bars`` long).
  2. Rank pairs cross-sectionally.
  3. Top quantile gets a long signal scaled by its z-rank; bottom
     quantile gets a symmetric short signal. Middle pairs flat.

Pure-numpy / pandas. No I/O. Deterministic. Active in TRENDING_UP and
TRENDING_DOWN regimes (skips MEAN_REVERTING, CHOP, HIGH_VOL since
momentum is unreliable there).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from src.analysis.regime_detector import Regime
from src.analysis.technical import TechnicalAnalyzer

from .base import MarketState, QuantSignal, QuantStrategy


class MomentumFactor(QuantStrategy):
    """Long winners / short losers cross-sectionally."""

    name = "momentum_factor"
    horizon_bars = 10
    active_regimes = (Regime.TRENDING_UP, Regime.TRENDING_DOWN)

    def __init__(
        self,
        *,
        lookback_bars: int = 30,
        top_quantile: float = 0.30,
        min_pairs: int = 3,
        min_candles: Optional[int] = None,
    ) -> None:
        """
        Parameters
        ----------
        lookback_bars : int
            Window for the momentum return measurement (default 30 bars).
        top_quantile : float
            Fraction (per side) tagged as winners / losers. 0.30 means
            top 30 % long, bottom 30 % short.
        min_pairs : int
            Below this many eligible pairs the strategy emits nothing —
            cross-sectional ranking is meaningless on tiny universes.
        min_candles : int, optional
            Per-pair minimum candles (default = lookback_bars + 5).
        """
        if not (0.0 < top_quantile < 0.5):
            raise ValueError("top_quantile must be in (0, 0.5)")
        super().__init__(
            lookback_bars=lookback_bars,
            top_quantile=top_quantile,
            min_pairs=min_pairs,
        )
        self.lookback_bars = int(lookback_bars)
        self.top_quantile = float(top_quantile)
        self.min_pairs = int(min_pairs)
        self.min_candles = int(min_candles or lookback_bars + 5)
        self._ta = TechnicalAnalyzer()

    # ------------------------------------------------------------------ #

    def _generate(self, state: MarketState) -> list[QuantSignal]:
        # Step 1: compute past-window return per eligible pair.
        returns: dict[str, float] = {}
        for pair in state.pairs():
            r = self._lookback_return(state.candles(pair))
            if r is not None:
                returns[pair] = r

        if len(returns) < self.min_pairs:
            return []

        # Step 2: cross-sectional rank → percentile in [0,1].
        sorted_pairs = sorted(returns.items(), key=lambda kv: kv[1])
        n = len(sorted_pairs)
        rank_pct: dict[str, float] = {}
        # Use mid-rank to be deterministic on ties.
        for i, (pair, _) in enumerate(sorted_pairs):
            rank_pct[pair] = (i + 0.5) / n  # 0..1 strictly inside

        # Step 3: build signals at the tails.
        out: list[QuantSignal] = []
        for pair, ret in returns.items():
            p = rank_pct[pair]
            # Long tail: above (1 - top_quantile)
            if p >= 1.0 - self.top_quantile:
                # Map p ∈ [1-q, 1] → score ∈ (0, 1]
                score = (p - (1.0 - self.top_quantile)) / max(1e-9, self.top_quantile)
                score = min(1.0, max(0.0, score))
                if score > 0:
                    out.append(self._build_signal(pair, +score, ret, state))
            # Short tail: below top_quantile
            elif p <= self.top_quantile:
                score = (self.top_quantile - p) / max(1e-9, self.top_quantile)
                score = min(1.0, max(0.0, score))
                if score > 0:
                    out.append(self._build_signal(pair, -score, ret, state))
            # Middle pairs: flat — emit nothing.

        return out

    # ------------------------------------------------------------------ #

    def _lookback_return(self, candles: list[dict]) -> Optional[float]:
        if not candles or len(candles) < self.min_candles:
            return None
        df = self._ta.candles_to_dataframe(candles)
        if df.empty or len(df) < self.lookback_bars + 1:
            return None
        close = df["close"].astype(float).to_numpy()
        last = close[-1]
        prev = close[-1 - self.lookback_bars]
        if not np.isfinite(last) or not np.isfinite(prev) or prev <= 0 or last <= 0:
            return None
        return float(np.log(last / prev))

    def _build_signal(
        self,
        pair: str,
        score: float,
        lookback_return: float,
        state: MarketState,
    ) -> QuantSignal:
        regime_snap = state.regime(pair)
        return QuantSignal(
            strategy=self.name,
            score=float(score),
            pair=pair,
            regime=regime_snap.regime.value if regime_snap else "",
            horizon_bars=self.horizon_bars,
            confidence=float(min(1.0, abs(score))),
            metadata={
                "lookback_return": round(lookback_return, 6),
                "lookback_bars": self.lookback_bars,
                "top_quantile": self.top_quantile,
            },
        )


__all__ = ["MomentumFactor"]
