"""
Z-score mean-reversion strategy.

Thesis: in mean-reverting / chop regimes, prices oscillate around a
local mean. When the close is N standard deviations *above* the
rolling mean, fade it (short bias); when N below, buy it (long bias).
The score is a smooth function of |z| so weak deviations get small
weight and extreme deviations get full weight.

Active regimes: MEAN_REVERTING, CHOP.

Pure-numpy. No I/O. Deterministic.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.analysis.regime_detector import Regime
from src.analysis.technical import TechnicalAnalyzer

from .base import MarketState, QuantSignal, QuantStrategy


class ZScoreMeanReversion(QuantStrategy):
    """Long when oversold (z << 0), short when overbought (z >> 0)."""

    name = "zscore_mean_reversion"
    horizon_bars = 5  # mean reversion typically plays out within ~5 bars
    active_regimes = (Regime.MEAN_REVERTING, Regime.CHOP)

    def __init__(
        self,
        *,
        window: int = 30,
        z_entry: float = 1.5,
        z_full: float = 3.0,
        min_candles: Optional[int] = None,
    ) -> None:
        """
        Parameters
        ----------
        window : int
            Lookback for rolling mean & std.
        z_entry : float
            |z| at which the signal first turns on (score ≈ ``z_entry/z_full``).
        z_full : float
            |z| at which the signal saturates to ±1.0.
        min_candles : int, optional
            Minimum candles required (default = max(window+5, 35)).
        """
        super().__init__(window=window, z_entry=z_entry, z_full=z_full)
        self.window = int(window)
        self.z_entry = float(z_entry)
        self.z_full = float(z_full)
        self.min_candles = int(min_candles or max(window + 5, 35))
        self._ta = TechnicalAnalyzer()

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

        close = df["close"].astype(float)
        roll_mean = close.rolling(self.window).mean()
        roll_std = close.rolling(self.window).std(ddof=1)

        last_close = float(close.iloc[-1])
        last_mean = float(roll_mean.iloc[-1])
        last_std = float(roll_std.iloc[-1])

        if not np.isfinite(last_std) or last_std <= 0:
            return None

        z = (last_close - last_mean) / last_std

        # Map |z| → magnitude: 0 below z_entry, 1 at/above z_full, linear in between.
        abs_z = abs(z)
        if abs_z < self.z_entry:
            return None
        magnitude = min(1.0, (abs_z - self.z_entry) / max(1e-9, self.z_full - self.z_entry))
        # Score: long when z<0 (price below mean → buy), short when z>0.
        score = -np.sign(z) * magnitude

        regime_snap = state.regime(pair)
        regime_value = regime_snap.regime.value if regime_snap else ""

        return QuantSignal(
            strategy=self.name,
            score=float(score),
            pair=pair,
            regime=regime_value,
            horizon_bars=self.horizon_bars,
            confidence=float(magnitude),
            metadata={
                "z_score": round(float(z), 4),
                "rolling_mean": round(last_mean, 6),
                "rolling_std": round(last_std, 6),
                "last_close": round(last_close, 6),
                "window": self.window,
            },
        )


__all__ = ["ZScoreMeanReversion"]
