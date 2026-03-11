"""
Walk-Forward Optimization (WFO) — Rolling in-sample/out-of-sample validation.

Prevents overfitting by simulating how a strategy would have been adapted in real-time.
The Walk-Forward Efficiency (WFE) ratio measures robustness:
  WFE = OOS performance / IS performance
  WFE >= 0.5 → strategy generalizes well
  WFE < 0.5 → likely curve-fitted
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from src.backtesting.engine import BacktestEngine, BacktestResult
from src.utils.logger import get_logger

logger = get_logger("backtesting.walk_forward")


@dataclass
class WFOWindowResult:
    """Result from a single IS/OOS window."""
    window_index: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    best_params: dict
    is_return: float
    is_sharpe: float
    oos_return: float
    oos_sharpe: float
    wfe: float  # Walk-forward efficiency for this window


@dataclass
class WFOResult:
    """Complete Walk-Forward Optimization results."""
    total_windows: int = 0
    avg_wfe: float = 0.0
    avg_oos_return: float = 0.0
    avg_oos_sharpe: float = 0.0
    combined_oos_return: float = 0.0
    is_robust: bool = False  # WFE >= 0.5
    windows: list[WFOWindowResult] = field(default_factory=list)
    param_grid: dict = field(default_factory=dict)
    best_overall_params: dict = field(default_factory=dict)


# Default parameter grid for optimization
DEFAULT_PARAM_GRID = {
    "position_size_pct": [0.05, 0.10, 0.15],
    "trailing_stop_pct": [0.02, 0.03, 0.05],
    "entry_threshold": [0.3, 0.4, 0.5],
    "stop_pct": [0.03, 0.04, 0.05],
    "target_pct": [0.05, 0.08, 0.10],
}


class WalkForwardOptimizer:
    """
    Walk-Forward Optimization engine.

    Process:
      1. Split data into rolling IS (in-sample) / OOS (out-of-sample) windows
      2. For each window: optimize parameters on IS, test on OOS
      3. Compute WFE = OOS return / IS return for each window
      4. Average WFE across all windows → overall robustness measure

    Usage:
        optimizer = WalkForwardOptimizer(config)
        result = optimizer.run(candles, pair="BTC-USD")
        optimizer.print_report(result)
    """

    def __init__(
        self,
        config: dict,
        is_window_size: int = 500,   # In-sample window (candles)
        oos_window_size: int = 150,  # Out-of-sample window (candles)
        step_size: int = 150,        # How far to roll forward each step
        param_grid: Optional[dict] = None,
        initial_balance: float = 10000.0,
    ):
        self.config = config
        self.is_window_size = is_window_size
        self.oos_window_size = oos_window_size
        self.step_size = step_size
        self.param_grid = param_grid or DEFAULT_PARAM_GRID
        self.initial_balance = initial_balance

    def run(
        self,
        candles: list[dict],
        pair: str = "BTC-USD",
    ) -> WFOResult:
        """
        Run Walk-Forward Optimization on historical candle data.

        Args:
            candles: Full historical candle dataset
            pair: Trading pair

        Returns:
            WFOResult with per-window and aggregate metrics
        """
        total_needed = self.is_window_size + self.oos_window_size
        if len(candles) < total_needed + 50:
            logger.error(
                f"Not enough candles for WFO: {len(candles)} "
                f"(need at least {total_needed + 50})"
            )
            return WFOResult()

        logger.info(
            f"🔬 Walk-Forward Optimization: {pair} | "
            f"{len(candles)} candles | IS={self.is_window_size} OOS={self.oos_window_size} "
            f"step={self.step_size}"
        )

        windows: list[WFOWindowResult] = []
        window_idx = 0
        start = 0

        while start + total_needed <= len(candles):
            is_candles = candles[start:start + self.is_window_size]
            oos_start_idx = start + self.is_window_size
            oos_candles = candles[oos_start_idx:oos_start_idx + self.oos_window_size]

            if len(is_candles) < 100 or len(oos_candles) < 50:
                break

            logger.info(
                f"  Window {window_idx}: IS candles [{start}:{start + self.is_window_size}] "
                f"→ OOS [{oos_start_idx}:{oos_start_idx + self.oos_window_size}]"
            )

            # Step 1: Optimize on IS data
            best_params, best_is_return, best_is_sharpe = self._optimize_is(
                is_candles, pair
            )

            # Step 2: Test best params on OOS data
            oos_result = self._run_with_params(oos_candles, pair, best_params)
            oos_return = oos_result.total_return_pct
            oos_sharpe = oos_result.sharpe_ratio

            # Step 3: Compute WFE
            if best_is_return != 0:
                wfe = oos_return / best_is_return if best_is_return > 0 else 0
            else:
                wfe = 0.0

            # Clamp WFE to reasonable range
            wfe = max(-2.0, min(wfe, 5.0))

            window_result = WFOWindowResult(
                window_index=window_idx,
                is_start=is_candles[0].get("start", ""),
                is_end=is_candles[-1].get("start", ""),
                oos_start=oos_candles[0].get("start", ""),
                oos_end=oos_candles[-1].get("start", ""),
                best_params=best_params,
                is_return=round(best_is_return, 6),
                is_sharpe=round(best_is_sharpe, 3),
                oos_return=round(oos_return, 6),
                oos_sharpe=round(oos_sharpe, 3),
                wfe=round(wfe, 3),
            )
            windows.append(window_result)

            logger.info(
                f"    IS return: {best_is_return*100:.2f}% | "
                f"OOS return: {oos_return*100:.2f}% | "
                f"WFE: {wfe:.2f}"
            )

            window_idx += 1
            start += self.step_size

        if not windows:
            return WFOResult()

        # Aggregate
        avg_wfe = sum(w.wfe for w in windows) / len(windows)
        avg_oos_return = sum(w.oos_return for w in windows) / len(windows)
        avg_oos_sharpe = sum(w.oos_sharpe for w in windows) / len(windows)

        # Combined OOS return (compounding)
        combined = 1.0
        for w in windows:
            combined *= (1 + w.oos_return)
        combined_oos_return = combined - 1.0

        # Best overall params = most frequently selected
        param_counts: dict[str, dict[Any, int]] = {}
        for w in windows:
            for k, v in w.best_params.items():
                if k not in param_counts:
                    param_counts[k] = {}
                param_counts[k][v] = param_counts[k].get(v, 0) + 1

        best_overall = {}
        for k, counts in param_counts.items():
            best_overall[k] = max(counts, key=counts.get)

        result = WFOResult(
            total_windows=len(windows),
            avg_wfe=round(avg_wfe, 3),
            avg_oos_return=round(avg_oos_return, 6),
            avg_oos_sharpe=round(avg_oos_sharpe, 3),
            combined_oos_return=round(combined_oos_return, 6),
            is_robust=avg_wfe >= 0.5,
            windows=windows,
            param_grid=self.param_grid,
            best_overall_params=best_overall,
        )

        return result

    def _optimize_is(
        self,
        candles: list[dict],
        pair: str,
    ) -> tuple[dict, float, float]:
        """
        Grid search over parameter combinations on in-sample data.
        Returns (best_params, best_return, best_sharpe).
        """
        # Generate all combinations
        param_names = list(self.param_grid.keys())
        param_values = list(self.param_grid.values())
        combinations = list(itertools.product(*param_values))

        best_sharpe = -999.0
        best_params = {}
        best_return = 0.0

        for combo in combinations:
            params = dict(zip(param_names, combo))
            try:
                result = self._run_with_params(candles, pair, params)
                # Optimize for Sharpe, not raw return (avoids high-vol strategies)
                if result.sharpe_ratio > best_sharpe:
                    best_sharpe = result.sharpe_ratio
                    best_params = params
                    best_return = result.total_return_pct
            except Exception as e:
                logger.debug(f"  IS run failed with params {params}: {e}")
                continue

        return best_params, best_return, best_sharpe

    def _run_with_params(
        self,
        candles: list[dict],
        pair: str,
        params: dict,
    ) -> BacktestResult:
        """Run a backtest with specific parameters."""
        engine = BacktestEngine(
            config=self.config,
            initial_balance=self.initial_balance,
            position_size_pct=params.get("position_size_pct", 0.10),
            trailing_stop_pct=params.get("trailing_stop_pct", 0.03),
            fee_pct=params.get("fee_pct", 0.006),
            slippage_pct=params.get("slippage_pct", 0.001),
        )

        # Patch entry threshold if provided
        original_evaluate = engine._evaluate_entry

        entry_threshold = params.get("entry_threshold", 0.4)
        stop_pct = params.get("stop_pct", 0.05)
        target_pct = params.get("target_pct", 0.08)

        def custom_evaluate(analysis):
            score = engine._mtf_scorer._score_timeframe(analysis)
            enter = score >= entry_threshold
            return {
                "enter": enter,
                "score": score,
                "stop_pct": stop_pct if score > 0.6 else stop_pct * 1.25,
                "target_pct": target_pct if score > 0.6 else target_pct * 1.33,
            }

        engine._evaluate_entry = custom_evaluate
        return engine.run(candles, pair=pair, warmup=50)

    def print_report(self, result: WFOResult) -> str:
        """Generate a human-readable WFO report."""
        robust_icon = "✅" if result.is_robust else "⚠️"

        report = f"""
{'='*60}
  🔬 WALK-FORWARD OPTIMIZATION REPORT
{'='*60}

  Windows:           {result.total_windows}
  Avg WFE:           {result.avg_wfe:.3f} {robust_icon} {'ROBUST' if result.is_robust else 'FRAGILE — likely overfitted'}
  Avg OOS Return:    {result.avg_oos_return*100:.2f}%
  Avg OOS Sharpe:    {result.avg_oos_sharpe:.3f}
  Combined OOS:      {result.combined_oos_return*100:.2f}%

  Best Overall Params: {result.best_overall_params}

  ── Per-Window Breakdown ──
"""
        for w in result.windows:
            report += (
                f"  Window {w.window_index}: "
                f"IS={w.is_return*100:+.2f}% (Sharpe {w.is_sharpe:.2f}) → "
                f"OOS={w.oos_return*100:+.2f}% (Sharpe {w.oos_sharpe:.2f}) | "
                f"WFE={w.wfe:.2f}\n"
            )
            report += f"    Params: {w.best_params}\n"

        report += f"\n{'='*60}\n"
        print(report)
        logger.info(report)
        return report
