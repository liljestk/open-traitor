"""
Cointegration pairs strategy.

Thesis: two assets that are cointegrated share a stable long-run
relationship — their spread (A − β·B) is mean-reverting even if both
prices wander. When the spread departs far from its mean, a market-
neutral trade (long the cheap leg / short the rich leg) tends to
profit as the spread reverts.

Implementation:
  1. For every configured pair tuple (A, B), compute the OLS hedge
     ratio β over a lookback window from log prices.
  2. Compute the spread series and its rolling z-score.
  3. Run a quick stationarity check: rolling first-order autocorrelation
     of the spread should be < ``max_autocorr``. (We deliberately avoid
     statsmodels dependency — the Augmented Dickey-Fuller test is
     overkill for an online filter; AR(1) coef serves the same purpose.)
  4. If spread |z| > entry threshold, fire a signal:
        z > 0  → spread rich → short A / long B → score < 0 (favours B)
        z < 0  → spread cheap → long A / short B → score > 0
     The signal carries both legs in ``pairs`` so the executor can take
     the spread.

Pure-numpy. Active in MEAN_REVERTING and CHOP regimes (cointegrated
spreads are themselves mean-reverting structures, so trending regimes
in the underlyings are *fine* — what matters is the spread's regime,
which is handled by the autocorr filter).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from src.analysis.regime_detector import Regime
from src.analysis.technical import TechnicalAnalyzer

from .base import MarketState, QuantSignal, QuantStrategy


class CointegrationPairs(QuantStrategy):
    """Spread-z-score trader on a configurable list of cointegrated pairs."""

    name = "cointegration_pairs"
    horizon_bars = 8
    # Active in any regime — spread regime is checked separately.
    active_regimes = None

    def __init__(
        self,
        *,
        candidate_pairs: Optional[list[tuple[str, str]]] = None,
        lookback_bars: int = 60,
        z_entry: float = 2.0,
        z_full: float = 3.5,
        max_autocorr: float = 0.85,
        min_candles: Optional[int] = None,
    ) -> None:
        super().__init__(
            candidate_pairs=candidate_pairs or [],
            lookback_bars=lookback_bars,
            z_entry=z_entry,
            z_full=z_full,
            max_autocorr=max_autocorr,
        )
        self.candidate_pairs = list(candidate_pairs or [])
        self.lookback_bars = int(lookback_bars)
        self.z_entry = float(z_entry)
        self.z_full = float(z_full)
        self.max_autocorr = float(max_autocorr)
        self.min_candles = int(min_candles or lookback_bars + 5)
        self._ta = TechnicalAnalyzer()

    # ------------------------------------------------------------------ #

    def _generate(self, state: MarketState) -> list[QuantSignal]:
        out: list[QuantSignal] = []
        for a, b in self.candidate_pairs:
            sig = self._signal_for_pair(a, b, state)
            if sig is not None:
                out.append(sig)
        return out

    def _signal_for_pair(
        self, a: str, b: str, state: MarketState
    ) -> Optional[QuantSignal]:
        ca, cb = state.candles(a), state.candles(b)
        if not ca or not cb:
            return None
        if len(ca) < self.min_candles or len(cb) < self.min_candles:
            return None

        df_a = self._ta.candles_to_dataframe(ca)
        df_b = self._ta.candles_to_dataframe(cb)
        if df_a.empty or df_b.empty:
            return None

        # Align on overlapping timestamps so the spread is well-defined.
        n = min(len(df_a), len(df_b))
        ya = np.log(df_a["close"].astype(float).to_numpy()[-n:])
        yb = np.log(df_b["close"].astype(float).to_numpy()[-n:])
        if n < self.lookback_bars + 5:
            return None
        ya = ya[-self.lookback_bars - 5:]
        yb = yb[-self.lookback_bars - 5:]
        if not (np.all(np.isfinite(ya)) and np.all(np.isfinite(yb))):
            return None

        # OLS hedge ratio β: ya ≈ α + β·yb.
        beta, alpha = _ols_slope_intercept(yb, ya)
        if beta is None or not np.isfinite(beta):
            return None
        spread = ya - (alpha + beta * yb)

        # Stationarity proxy: AR(1) coefficient of the spread.
        ar1 = _ar1_coef(spread)
        if ar1 is None or ar1 >= self.max_autocorr:
            return None  # not mean-reverting enough

        # z-score of last spread observation vs. its window
        win = spread[-self.lookback_bars:]
        mu = float(np.mean(win))
        sd = float(np.std(win, ddof=1))
        if sd <= 0 or not np.isfinite(sd):
            return None
        z = (float(spread[-1]) - mu) / sd
        if abs(z) < self.z_entry:
            return None

        magnitude = min(1.0, (abs(z) - self.z_entry) / max(1e-9, self.z_full - self.z_entry))
        # z > 0 → A overpriced relative to B → short A long B → favour B.
        # In score terms: positive bias to (B, A) i.e. long-B short-A.
        # We convention: pairs=(A, B), score>0 means long A short B.
        score = -np.sign(z) * magnitude

        regime_a = state.regime(a)
        regime_value = regime_a.regime.value if regime_a else ""

        return QuantSignal(
            strategy=self.name,
            score=float(score),
            pair="",  # multi-asset signal
            pairs=(a, b),
            regime=regime_value,
            horizon_bars=self.horizon_bars,
            confidence=float(magnitude),
            metadata={
                "z_score": round(float(z), 4),
                "beta": round(float(beta), 6),
                "alpha": round(float(alpha), 6),
                "ar1": round(float(ar1), 4),
                "spread_mean": round(mu, 6),
                "spread_std": round(sd, 6),
                "lookback_bars": self.lookback_bars,
            },
        )


# --------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------- #

def _ols_slope_intercept(x: np.ndarray, y: np.ndarray) -> tuple[Optional[float], Optional[float]]:
    """OLS y = α + β·x. Returns (β, α) or (None, None) on degeneracy."""
    if x is None or y is None:
        return None, None
    if len(x) != len(y) or len(x) < 5:
        return None, None
    x_mean = x.mean()
    y_mean = y.mean()
    dx = x - x_mean
    denom = float((dx * dx).sum())
    if denom <= 0:
        return None, None
    beta = float((dx * (y - y_mean)).sum() / denom)
    alpha = float(y_mean - beta * x_mean)
    return beta, alpha


def _ar1_coef(series: np.ndarray) -> Optional[float]:
    """AR(1) coefficient: corr(s_t, s_{t-1}). High → trending spread."""
    s = np.asarray(series, dtype=float)
    s = s[np.isfinite(s)]
    if len(s) < 5:
        return None
    a, b = s[:-1], s[1:]
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


__all__ = ["CointegrationPairs"]
