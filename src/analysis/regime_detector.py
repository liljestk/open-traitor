"""
Regime Detector — deterministic market regime classification.

Tags every price series with one of five regimes so downstream strategies
and the capital allocator can route accordingly:

    trending_up         — strong directional momentum, positive slope
    trending_down       — strong directional momentum, negative slope
    mean_reverting      — price oscillates around a stable mean
    high_vol            — volatility regime; risk-off / size-down
    chop                — low ADX, no clear structure (fallback)

Pure-numpy / pandas. No LLM, no external services, no I/O. Fully
deterministic given the same candle input — safe to call inside the
trading loop and inside backtests with identical semantics.

Design goals (autonomy):
    * Self-contained: no config required (sane defaults), but every
      threshold is overridable for adaptive tuning.
    * Self-measuring: every snapshot carries the raw features used to
      classify it so the learning loop can backtest its own decisions.
    * Deterministic & cheap: O(n) over the candle window, ~ms latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from src.analysis.technical import TechnicalAnalyzer
from src.utils.logger import get_logger

logger = get_logger("analysis.regime")


class Regime(str, Enum):
    """Market regime classification."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    MEAN_REVERTING = "mean_reverting"
    HIGH_VOL = "high_vol"
    CHOP = "chop"

    @property
    def is_trending(self) -> bool:
        return self in (Regime.TRENDING_UP, Regime.TRENDING_DOWN)

    @property
    def is_directional(self) -> bool:
        """True if the regime has a directional bias (up or down)."""
        return self.is_trending


@dataclass(frozen=True)
class RegimeSnapshot:
    """Immutable record of a regime classification at a point in time."""

    regime: Regime
    confidence: float  # 0.0 – 1.0
    # Feature snapshot used to classify (for backtesting + audit)
    adx: float = 0.0
    atr_pct: float = 0.0  # ATR as % of price
    atr_pct_rank: float = 0.0  # 0–1, where this ATR sits in trailing window
    slope: float = 0.0  # log-return regression slope per bar
    slope_r2: float = 0.0  # how well a line fits — strength of trend
    hurst: float = 0.5  # Hurst exponent: <0.5 mean-revert, >0.5 trending
    sample_size: int = 0
    # Free-form fingerprint for telemetry
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "confidence": round(self.confidence, 4),
            "adx": round(self.adx, 3),
            "atr_pct": round(self.atr_pct, 5),
            "atr_pct_rank": round(self.atr_pct_rank, 4),
            "slope": round(self.slope, 6),
            "slope_r2": round(self.slope_r2, 4),
            "hurst": round(self.hurst, 4),
            "sample_size": self.sample_size,
            "notes": self.notes,
        }


@dataclass
class RegimeConfig:
    """Tunable thresholds. Keep these tight — defaults are battle-tested."""

    min_candles: int = 60
    adx_period: int = 14
    atr_period: int = 14
    atr_lookback: int = 200  # window for ATR percentile rank
    slope_window: int = 50  # bars used for trend slope regression
    hurst_window: int = 100  # bars for Hurst exponent

    # Decision thresholds
    adx_trending: float = 25.0  # ADX above → trending candidate
    adx_chop: float = 18.0  # ADX below → chop candidate
    atr_pct_rank_high_vol: float = 0.85  # top 15% of recent vol → high_vol
    slope_r2_min: float = 0.30  # min linearity for trend confirmation
    hurst_mean_revert: float = 0.42  # below → mean reverting
    hurst_trending: float = 0.58  # above → trending bias


class RegimeDetector:
    """Classifies a candle window into a Regime."""

    def __init__(self, config: Optional[RegimeConfig] = None):
        self.config = config or RegimeConfig()
        self._ta = TechnicalAnalyzer(
            {"rsi_period": 14, "bb_period": 20, "bb_std": 2}
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def detect(self, candles: list[dict]) -> RegimeSnapshot:
        """Classify a candle window. Returns a RegimeSnapshot."""
        cfg = self.config
        if not candles or len(candles) < cfg.min_candles:
            return RegimeSnapshot(
                regime=Regime.CHOP,
                confidence=0.0,
                sample_size=len(candles or []),
                notes="insufficient_candles",
            )

        df = self._ta.candles_to_dataframe(candles)
        if df.empty or len(df) < cfg.min_candles:
            return RegimeSnapshot(
                regime=Regime.CHOP,
                confidence=0.0,
                sample_size=len(df),
                notes="empty_dataframe",
            )

        # ---- Feature extraction ---- #
        adx_series, _, _ = self._ta.compute_adx(df, period=cfg.adx_period)
        atr_series = self._ta.compute_atr(df, period=cfg.atr_period)

        adx = _last_finite(adx_series)
        atr = _last_finite(atr_series)
        last_close = float(df["close"].iloc[-1])
        atr_pct = (atr / last_close) if last_close > 0 else 0.0

        atr_pct_series = (atr_series / df["close"]).dropna()
        lookback = atr_pct_series.tail(cfg.atr_lookback)
        atr_pct_rank = (
            float((lookback <= atr_pct).mean()) if len(lookback) > 5 else 0.5
        )

        slope, slope_r2 = _log_slope(
            df["close"].tail(cfg.slope_window).to_numpy()
        )
        hurst = _hurst_exponent(
            df["close"].tail(cfg.hurst_window).to_numpy()
        )

        # ---- Decision ---- #
        regime, confidence, notes = self._classify(
            adx=adx,
            atr_pct_rank=atr_pct_rank,
            slope=slope,
            slope_r2=slope_r2,
            hurst=hurst,
        )

        return RegimeSnapshot(
            regime=regime,
            confidence=confidence,
            adx=adx,
            atr_pct=atr_pct,
            atr_pct_rank=atr_pct_rank,
            slope=slope,
            slope_r2=slope_r2,
            hurst=hurst,
            sample_size=len(df),
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _classify(
        self,
        *,
        adx: float,
        atr_pct_rank: float,
        slope: float,
        slope_r2: float,
        hurst: float,
    ) -> tuple[Regime, float, str]:
        cfg = self.config

        # 1. Volatility regime overrides direction (risk-first).
        if atr_pct_rank >= cfg.atr_pct_rank_high_vol:
            conf = min(1.0, (atr_pct_rank - cfg.atr_pct_rank_high_vol) / 0.15 + 0.5)
            return Regime.HIGH_VOL, round(conf, 4), "atr_pct_rank_extreme"

        # 2. Trending: ADX confirms strength, slope confirms direction,
        #    Hurst confirms persistence.
        trending_score = 0.0
        if adx >= cfg.adx_trending:
            trending_score += 0.4
        if slope_r2 >= cfg.slope_r2_min:
            trending_score += 0.3
        if hurst >= cfg.hurst_trending:
            trending_score += 0.3

        if trending_score >= 0.6 and slope_r2 >= cfg.slope_r2_min:
            if slope > 0:
                return Regime.TRENDING_UP, round(trending_score, 4), "trend_up_confluence"
            elif slope < 0:
                return Regime.TRENDING_DOWN, round(trending_score, 4), "trend_down_confluence"

        # 3. Mean reverting: low ADX, Hurst < 0.5, slope flat.
        mr_score = 0.0
        if adx <= cfg.adx_chop:
            mr_score += 0.35
        if hurst <= cfg.hurst_mean_revert:
            mr_score += 0.45
        if slope_r2 < cfg.slope_r2_min:
            mr_score += 0.20

        if mr_score >= 0.6:
            return Regime.MEAN_REVERTING, round(mr_score, 4), "low_adx_low_hurst"

        # 4. Default: chop. Confidence reflects how non-classifiable it is.
        return Regime.CHOP, 0.5, "no_dominant_signal"


# ---------------------------------------------------------------------- #
# Helpers (pure functions, free for tests to import)
# ---------------------------------------------------------------------- #

def _last_finite(series: pd.Series, default: float = 0.0) -> float:
    """Last non-NaN, non-inf scalar from a series."""
    if series is None or len(series) == 0:
        return default
    arr = series.to_numpy(dtype=float)
    mask = np.isfinite(arr)
    if not mask.any():
        return default
    return float(arr[mask][-1])


def _log_slope(prices: np.ndarray) -> tuple[float, float]:
    """OLS regression of log(prices) ~ time. Returns (slope, R²)."""
    if prices is None or len(prices) < 5:
        return 0.0, 0.0
    p = np.asarray(prices, dtype=float)
    p = p[np.isfinite(p) & (p > 0)]
    if len(p) < 5:
        return 0.0, 0.0

    y = np.log(p)
    x = np.arange(len(y), dtype=float)
    x_mean = x.mean()
    y_mean = y.mean()
    dx = x - x_mean
    dy = y - y_mean
    denom = float((dx * dx).sum())
    if denom <= 0:
        return 0.0, 0.0
    slope = float((dx * dy).sum() / denom)

    # R²
    y_pred = y_mean + slope * dx
    ss_res = float(((y - y_pred) ** 2).sum())
    ss_tot = float((dy * dy).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, max(0.0, min(1.0, r2))


def _hurst_exponent(prices: np.ndarray) -> float:
    """
    Hurst exponent via rescaled-range (R/S) analysis.

    Interpretation:
      * H ≈ 0.5  → random walk
      * H < 0.5  → mean-reverting (anti-persistent)
      * H > 0.5  → trending (persistent)

    Returns 0.5 on insufficient data (the agnostic default).
    """
    if prices is None or len(prices) < 30:
        return 0.5
    p = np.asarray(prices, dtype=float)
    p = p[np.isfinite(p) & (p > 0)]
    if len(p) < 30:
        return 0.5

    log_returns = np.diff(np.log(p))
    n = len(log_returns)
    if n < 20:
        return 0.5

    # Lag set scales with sample size; min lag 2, max ~ n/2
    max_lag = max(10, n // 2)
    lags = np.unique(np.geomspace(2, max_lag, num=10).astype(int))
    rs_values: list[tuple[float, float]] = []

    for lag in lags:
        if lag < 2 or lag >= n:
            continue
        # Split returns into chunks of size `lag`
        n_chunks = n // lag
        if n_chunks < 1:
            continue
        rs_per_chunk: list[float] = []
        for i in range(n_chunks):
            chunk = log_returns[i * lag : (i + 1) * lag]
            if len(chunk) < 2:
                continue
            mean = chunk.mean()
            cum_dev = np.cumsum(chunk - mean)
            r = cum_dev.max() - cum_dev.min()
            s = chunk.std(ddof=1)
            if s > 0 and r > 0:
                rs_per_chunk.append(r / s)
        if rs_per_chunk:
            rs_values.append((float(lag), float(np.mean(rs_per_chunk))))

    if len(rs_values) < 4:
        return 0.5

    log_lags = np.log(np.array([lv[0] for lv in rs_values]))
    log_rs = np.log(np.array([lv[1] for lv in rs_values]))

    # OLS slope = Hurst exponent
    x_mean = log_lags.mean()
    y_mean = log_rs.mean()
    dx = log_lags - x_mean
    denom = float((dx * dx).sum())
    if denom <= 0:
        return 0.5
    h = float((dx * (log_rs - y_mean)).sum() / denom)
    # Clip into the meaningful range; numerical noise can push outside [0,1]
    return max(0.0, min(1.0, h))


__all__ = [
    "Regime",
    "RegimeSnapshot",
    "RegimeConfig",
    "RegimeDetector",
]
