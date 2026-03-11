"""
Pairs / Correlation Monitor (Statistical Arbitrage lite)

Monitors rolling correlations between top traded crypto pairs.
When a historically-correlated pair diverges beyond a z-score threshold,
generates signals for the expected convergence.

This is NOT a full stat-arb execution engine — it produces advisory
signals that the Strategist agent can incorporate alongside other evidence.

Usage:
  monitor = PairsCorrelationMonitor(config)
  correlation_report = monitor.compute_correlations(candle_map)
  signal = monitor.check_divergence("BTC-USD", "ETH-USD", candle_map)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("strategy.pairs_monitor")


@dataclass
class CorrelationPair:
    """Correlation info for a pair of assets."""
    asset_a: str
    asset_b: str
    correlation: float  # Pearson correlation coefficient (-1 to 1)
    rolling_mean_spread: float  # Mean of price ratio spread
    rolling_std_spread: float   # Std of price ratio spread
    current_spread: float       # Current price ratio
    z_score: float              # How many stds from mean
    signal: str = "neutral"     # "long_a_short_b", "long_b_short_a", "neutral"


@dataclass
class PairsReport:
    """Full correlation report across monitored pairs."""
    pairs: list[CorrelationPair] = field(default_factory=list)
    timestamp: str = ""
    divergence_alerts: list[str] = field(default_factory=list)


class PairsCorrelationMonitor:
    """
    Rolling correlation analysis between crypto pairs.

    Config (via config.strategies.pairs_monitor):
      lookback: 30 (candle periods for rolling window)
      z_threshold: 2.0 (z-score to trigger divergence alert)
      min_correlation: 0.7 (minimum historical correlation to consider)
      monitored_pairs: [["BTC-USD", "ETH-USD"], ["BTC-USD", "SOL-USD"], ...]
    """

    def __init__(self, config: dict):
        strat_cfg = config.get("strategies", {}).get("pairs_monitor", {})
        self.lookback = strat_cfg.get("lookback", 30)
        self.z_threshold = strat_cfg.get("z_threshold", 2.0)
        self.min_correlation = strat_cfg.get("min_correlation", 0.7)
        self.monitored_pairs: list[list[str]] = strat_cfg.get(
            "monitored_pairs",
            [
                ["BTC-USD", "ETH-USD"],
                ["ETH-USD", "SOL-USD"],
            ],
        )

    def _compute_pearson(self, xs: list[float], ys: list[float]) -> float:
        """Pure-Python Pearson correlation coefficient."""
        n = len(xs)
        if n < 3:
            return 0.0
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / (n - 1)
        std_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs) / (n - 1))
        std_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys) / (n - 1))
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)

    def _extract_closes(self, candles: list[dict], n: int) -> list[float]:
        """Extract last n close prices from candle list."""
        closes = []
        for c in candles:
            close = c.get("close") or c.get("Close")
            if close is not None:
                closes.append(float(close))
        return closes[-n:]

    def compute_correlations(
        self,
        candle_map: dict[str, list[dict]],
    ) -> PairsReport:
        """
        Compute pairwise correlations for all monitored pairs.

        Args:
            candle_map: {pair_symbol: [candle_dicts]} for each asset

        Returns:
            PairsReport with correlation details and any divergence alerts
        """
        from datetime import datetime, timezone

        report = PairsReport(timestamp=datetime.now(timezone.utc).isoformat())

        for pair_spec in self.monitored_pairs:
            if len(pair_spec) != 2:
                continue
            asset_a, asset_b = pair_spec[0], pair_spec[1]

            candles_a = candle_map.get(asset_a, [])
            candles_b = candle_map.get(asset_b, [])

            if not candles_a or not candles_b:
                continue

            closes_a = self._extract_closes(candles_a, self.lookback)
            closes_b = self._extract_closes(candles_b, self.lookback)

            min_len = min(len(closes_a), len(closes_b))
            if min_len < 10:
                logger.debug(f"Insufficient data for {asset_a}/{asset_b}: {min_len} periods")
                continue

            closes_a = closes_a[-min_len:]
            closes_b = closes_b[-min_len:]

            # Pearson correlation on return series (more stationary)
            returns_a = [
                (closes_a[i] - closes_a[i - 1]) / closes_a[i - 1]
                for i in range(1, len(closes_a))
                if closes_a[i - 1] != 0
            ]
            returns_b = [
                (closes_b[i] - closes_b[i - 1]) / closes_b[i - 1]
                for i in range(1, len(closes_b))
                if closes_b[i - 1] != 0
            ]

            ret_len = min(len(returns_a), len(returns_b))
            if ret_len < 5:
                continue

            returns_a = returns_a[-ret_len:]
            returns_b = returns_b[-ret_len:]

            correlation = self._compute_pearson(returns_a, returns_b)

            # Price ratio spread analysis
            ratios = [
                a / b if b != 0 else 0
                for a, b in zip(closes_a, closes_b)
            ]
            ratios = [r for r in ratios if r > 0]

            if len(ratios) < 5:
                continue

            mean_spread = sum(ratios) / len(ratios)
            std_spread = math.sqrt(
                sum((r - mean_spread) ** 2 for r in ratios) / (len(ratios) - 1)
            ) if len(ratios) > 1 else 0.001

            current_spread = ratios[-1] if ratios else mean_spread
            z_score = (current_spread - mean_spread) / std_spread if std_spread > 0 else 0.0

            # Determine signal
            signal = "neutral"
            if abs(correlation) >= self.min_correlation:
                if z_score > self.z_threshold:
                    signal = "long_b_short_a"  # A is overvalued relative to B
                elif z_score < -self.z_threshold:
                    signal = "long_a_short_b"  # A is undervalued relative to B

            cp = CorrelationPair(
                asset_a=asset_a,
                asset_b=asset_b,
                correlation=correlation,
                rolling_mean_spread=mean_spread,
                rolling_std_spread=std_spread,
                current_spread=current_spread,
                z_score=z_score,
                signal=signal,
            )
            report.pairs.append(cp)

            if signal != "neutral":
                alert_msg = (
                    f"DIVERGENCE: {asset_a}/{asset_b} z={z_score:+.2f} "
                    f"(corr={correlation:.2f}) → {signal}"
                )
                report.divergence_alerts.append(alert_msg)
                logger.info(alert_msg)

        return report

    def check_divergence(
        self,
        asset_a: str,
        asset_b: str,
        candle_map: dict[str, list[dict]],
    ) -> Optional[CorrelationPair]:
        """
        Check divergence for a specific pair.

        Returns CorrelationPair if there's a divergence signal, else None.
        """
        report = self.compute_correlations(candle_map)
        for cp in report.pairs:
            if (cp.asset_a == asset_a and cp.asset_b == asset_b) or \
               (cp.asset_a == asset_b and cp.asset_b == asset_a):
                if cp.signal != "neutral":
                    return cp
        return None

    def get_correlation_matrix(
        self,
        candle_map: dict[str, list[dict]],
    ) -> dict[str, dict[str, float]]:
        """
        Build a full correlation matrix from all available pairs.

        Returns: {asset_a: {asset_b: correlation, ...}, ...}
        """
        assets = list(candle_map.keys())
        matrix: dict[str, dict[str, float]] = {}

        for i, asset_a in enumerate(assets):
            matrix[asset_a] = {}
            for j, asset_b in enumerate(assets):
                if i == j:
                    matrix[asset_a][asset_b] = 1.0
                    continue

                closes_a = self._extract_closes(candle_map[asset_a], self.lookback)
                closes_b = self._extract_closes(candle_map[asset_b], self.lookback)

                min_len = min(len(closes_a), len(closes_b))
                if min_len < 10:
                    matrix[asset_a][asset_b] = 0.0
                    continue

                closes_a = closes_a[-min_len:]
                closes_b = closes_b[-min_len:]

                returns_a = [
                    (closes_a[k] - closes_a[k - 1]) / closes_a[k - 1]
                    for k in range(1, len(closes_a))
                    if closes_a[k - 1] != 0
                ]
                returns_b = [
                    (closes_b[k] - closes_b[k - 1]) / closes_b[k - 1]
                    for k in range(1, len(closes_b))
                    if closes_b[k - 1] != 0
                ]

                ret_len = min(len(returns_a), len(returns_b))
                if ret_len < 5:
                    matrix[asset_a][asset_b] = 0.0
                    continue

                matrix[asset_a][asset_b] = self._compute_pearson(
                    returns_a[-ret_len:], returns_b[-ret_len:]
                )

        return matrix
