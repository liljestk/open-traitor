"""
Volatility breakout / regime-switch strategy.

Thesis: when realised volatility expands sharply after a quiet period,
markets typically continue in the breakout direction for several bars
("vol breeds trend"). This is the empirically robust pattern behind
Donchian-channel and turtle-style trend systems.

Per pair, per cycle:
  1. Compute the rolling Bollinger-band width (BBW = (upper-lower)/middle).
  2. Compute its rolling percentile rank over a longer window.
  3. If the latest bar breaks the upper Bollinger and BBW is in the
     top quantile (vol expansion), fire LONG with magnitude scaled by
     how far above the band the close is.
  4. Symmetric for the lower band → SHORT.

Active regimes: HIGH_VOL plus TRENDING_UP/DOWN — these are the
conditions where vol breakouts most often persist. Skipped in
MEAN_REVERTING / CHOP where breakouts are mostly false.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.analysis.regime_detector import Regime
from src.analysis.technical import TechnicalAnalyzer

from .base import MarketState, QuantSignal, QuantStrategy


class VolatilityBreakout(QuantStrategy):
    """Bollinger-band breakout filtered by volatility-expansion regime."""

    name = "volatility_breakout"
    horizon_bars = 6
    active_regimes = (Regime.HIGH_VOL, Regime.TRENDING_UP, Regime.TRENDING_DOWN)

    def __init__(
        self,
        *,
        bb_period: int = 20,
        bb_std: float = 2.0,
        vol_window: int = 100,
        vol_quantile: float = 0.7,
        breakout_atr_mult_full: float = 1.5,
        min_candles: Optional[int] = None,
    ) -> None:
        super().__init__(
            bb_period=bb_period,
            bb_std=bb_std,
            vol_window=vol_window,
            vol_quantile=vol_quantile,
            breakout_atr_mult_full=breakout_atr_mult_full,
        )
        self.bb_period = int(bb_period)
        self.bb_std = float(bb_std)
        self.vol_window = int(vol_window)
        self.vol_quantile = float(vol_quantile)
        self.breakout_atr_mult_full = float(breakout_atr_mult_full)
        self.min_candles = int(min_candles or vol_window + bb_period + 5)
        self._ta = TechnicalAnalyzer({"bb_period": bb_period, "bb_std": bb_std})

    # ------------------------------------------------------------------ #

    def _generate(self, state: MarketState) -> list[QuantSignal]:
        out: list[QuantSignal] = []
        for pair in state.pairs():
            sig = self._signal_for_pair(pair, state)
            if sig is not None:
                out.append(sig)
        return out

    def _signal_for_pair(self, pair: str, state: MarketState) -> Optional[QuantSignal]:
        candles = state.candles(pair)
        if not candles or len(candles) < self.min_candles:
            return None
        df = self._ta.candles_to_dataframe(candles)
        if df.empty or len(df) < self.min_candles:
            return None

        upper, middle, lower = self._ta.compute_bollinger_bands(df)
        atr = self._ta.compute_atr(df, period=14)

        # Bollinger-band width as % of middle.
        width = (upper - lower) / middle.replace(0, np.nan)
        width = width.dropna()
        if len(width) < self.vol_window:
            return None

        last_w = float(width.iloc[-1])
        last_w_window = width.tail(self.vol_window)
        # Percentile rank of the latest BBW.
        rank = float((last_w_window <= last_w).mean())
        if rank < self.vol_quantile:
            return None  # not a vol-expansion regime

        last_close = float(df["close"].iloc[-1])
        last_upper = float(upper.iloc[-1])
        last_lower = float(lower.iloc[-1])
        last_atr = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else 0.0

        if last_atr <= 0:
            return None

        # Distance beyond the band, measured in ATRs → score magnitude.
        if last_close > last_upper:
            mult = (last_close - last_upper) / last_atr
            magnitude = min(1.0, mult / max(1e-9, self.breakout_atr_mult_full))
            if magnitude <= 0:
                return None
            score = +magnitude
        elif last_close < last_lower:
            mult = (last_lower - last_close) / last_atr
            magnitude = min(1.0, mult / max(1e-9, self.breakout_atr_mult_full))
            if magnitude <= 0:
                return None
            score = -magnitude
        else:
            return None  # inside bands — no breakout

        regime_snap = state.regime(pair)
        return QuantSignal(
            strategy=self.name,
            score=float(score),
            pair=pair,
            regime=regime_snap.regime.value if regime_snap else "",
            horizon_bars=self.horizon_bars,
            confidence=float(magnitude),
            metadata={
                "bb_upper": round(last_upper, 6),
                "bb_lower": round(last_lower, 6),
                "bbw_rank": round(rank, 4),
                "breakout_atr_mult": round(float(mult), 4),
                "last_close": round(last_close, 6),
                "atr": round(last_atr, 6),
            },
        )


__all__ = ["VolatilityBreakout"]
