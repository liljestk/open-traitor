"""
Backtesting Engine — Replay historical data through the trading pipeline.
Validates strategies before risking real or even paper money.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.analysis.technical import TechnicalAnalyzer
from src.analysis.multi_timeframe import MultiTimeframeAnalyzer
from src.utils.logger import get_logger
from src.utils.helpers import format_currency, format_percentage

logger = get_logger("backtesting.engine")


@dataclass
class BacktestPosition:
    """A simulated position during backtesting."""
    pair: str
    side: str  # "long"
    entry_price: float
    quantity: float
    entry_time: str
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_pct: float = 0.03
    highest_price: float = 0.0
    closed: bool = False
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    entry_fee: float = 0.0


@dataclass
class BacktestResult:
    """Complete results from a backtest run."""
    start_date: str = ""
    end_date: str = ""
    initial_balance: float = 10000.0
    final_balance: float = 0.0
    total_return_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_hold_time_hours: float = 0.0
    # Buy-and-hold benchmark
    benchmark_return_pct: float = 0.0
    alpha: float = 0.0  # strategy return - benchmark return
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    # Cost sensitivity (populated by cost_sensitivity_analysis)
    cost_sensitivity: list = field(default_factory=list)


class BacktestEngine:
    """
    Backtesting engine that replays historical candle data
    through the technical analysis pipeline.

    Usage:
        engine = BacktestEngine(config)
        candles = coinbase.get_candles("BTC-USD", granularity="ONE_HOUR", limit=1000)
        result = engine.run(candles, pair="BTC-USD")
        engine.print_report(result)
    """

    def __init__(
        self,
        config: dict,
        initial_balance: float = 10000.0,
        position_size_pct: float = 0.10,
        max_positions: int = 3,
        trailing_stop_pct: float = 0.03,
        fee_pct: float = 0.006,
        slippage_pct: float = 0.001,
    ):
        self.config = config
        self.initial_balance = initial_balance
        self.position_size_pct = position_size_pct
        self.max_positions = max_positions
        self.trailing_stop_pct = trailing_stop_pct
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct

        tech_config = config.get("analysis", {}).get("technical", {})
        self.technical = TechnicalAnalyzer(tech_config)
        # Reuse scoring logic from multi-timeframe analyzer
        self._mtf_scorer = MultiTimeframeAnalyzer(config)

    def run(
        self,
        candles: list[dict],
        pair: str = "BTC-USD",
        warmup: int = 50,
    ) -> BacktestResult:
        """
        Run a backtest on historical candle data.

        Args:
            candles: List of candle dicts with open, high, low, close, volume, start
            pair: Trading pair name
            warmup: Number of candles to use for indicator warmup (not traded)

        Returns:
            BacktestResult with all metrics
        """
        if len(candles) < warmup + 10:
            logger.error(f"Not enough candles: {len(candles)} (need at least {warmup + 10})")
            return BacktestResult()

        logger.info(
            f"🔬 Starting backtest: {pair} | {len(candles)} candles | "
            f"Balance: {format_currency(self.initial_balance)}"
        )

        # State
        balance = self.initial_balance
        positions: list[BacktestPosition] = []
        closed_positions: list[BacktestPosition] = []
        equity_curve: list[dict] = []
        peak_equity = self.initial_balance
        max_drawdown = 0.0

        # Walk through candles (after warmup)
        for i in range(warmup, len(candles)):
            # Get historical window for analysis
            window = candles[max(0, i - 200):i + 1]
            current_candle = candles[i]

            current_price = float(current_candle.get("close", 0))
            current_high = float(current_candle.get("high", current_price))
            current_low = float(current_candle.get("low", current_price))
            current_time = current_candle.get("start", "")

            if current_price <= 0:
                continue

            # Update trailing stops and check stop-loss/take-profit
            for pos in positions:
                if pos.closed:
                    continue

                # Update trailing stop
                if current_high > pos.highest_price:
                    pos.highest_price = current_high

                trailing_stop = pos.highest_price * (1 - pos.trailing_pct)

                # Check exits
                exit_price = None
                exit_reason = ""

                if pos.stop_loss > 0 and current_low <= pos.stop_loss:
                    exit_price = pos.stop_loss
                    exit_reason = "stop_loss"
                elif trailing_stop > pos.stop_loss and current_low <= trailing_stop:
                    exit_price = trailing_stop
                    exit_reason = "trailing_stop"
                elif pos.take_profit > 0 and current_high >= pos.take_profit:
                    exit_price = pos.take_profit
                    exit_reason = "take_profit"

                if exit_price:
                    # Apply slippage to exit (selling slightly lower)
                    exit_price *= (1 - self.slippage_pct)
                    fee = exit_price * pos.quantity * self.fee_pct
                    pnl = (exit_price - pos.entry_price) * pos.quantity - pos.entry_fee - fee
                    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price

                    pos.closed = True
                    pos.exit_price = exit_price
                    pos.exit_time = current_time
                    pos.exit_reason = exit_reason
                    pos.pnl = pnl
                    pos.pnl_pct = pnl_pct
                    balance += (exit_price * pos.quantity) - fee
                    closed_positions.append(pos)

            # Remove closed positions
            positions = [p for p in positions if not p.closed]

            # Run technical analysis on the window
            analysis = self.technical.analyze(window)
            if "error" in analysis:
                continue

            # Decide whether to enter a position
            if len(positions) < self.max_positions:
                entry_signal = self._evaluate_entry(analysis)

                if entry_signal["enter"]:
                    # Apply slippage to entry (buying slightly higher)
                    entry_price = current_price * (1 + self.slippage_pct)

                    # Calculate position size
                    quote_amount = balance * self.position_size_pct
                    if quote_amount < 10:  # Minimum trade
                        continue

                    quantity = quote_amount / entry_price
                    fee = quote_amount * self.fee_pct
                    balance -= quote_amount + fee

                    # Calculate stops
                    stop_loss = entry_price * (1 - entry_signal.get("stop_pct", 0.05))
                    take_profit = entry_price * (1 + entry_signal.get("target_pct", 0.08))

                    pos = BacktestPosition(
                        pair=pair,
                        side="long",
                        entry_price=entry_price,
                        quantity=quantity,
                        entry_time=current_time,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        trailing_pct=self.trailing_stop_pct,
                        highest_price=current_price,
                        entry_fee=fee,
                    )
                    positions.append(pos)

            # Calculate equity (balance + open positions value)
            positions_value = sum(
                p.quantity * current_price for p in positions if not p.closed
            )
            equity = balance + positions_value

            # Track drawdown
            if equity > peak_equity:
                peak_equity = equity
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)

            equity_curve.append({
                "time": current_time,
                "equity": equity,
                "balance": balance,
                "positions": len(positions),
                "drawdown": drawdown,
            })

        # Close remaining positions at last price
        last_price = float(candles[-1].get("close", 0))
        for pos in positions:
            if not pos.closed:
                fee = last_price * pos.quantity * self.fee_pct
                pnl = (last_price - pos.entry_price) * pos.quantity - fee
                pos.closed = True
                pos.exit_price = last_price
                pos.exit_time = candles[-1].get("start", "")
                pos.exit_reason = "backtest_end"
                pos.pnl = pnl
                pos.pnl_pct = (last_price - pos.entry_price) / pos.entry_price
                balance += (last_price * pos.quantity) - fee
                closed_positions.append(pos)

        # Compile results
        result = self._compile_results(
            closed_positions, equity_curve, balance, max_drawdown, candles
        )
        return result

    def _evaluate_entry(self, analysis: dict) -> dict:
        """
        Evaluate whether to enter a position based on technical analysis.
        Uses the same scoring logic as MultiTimeframeAnalyzer for consistency.
        Returns entry signal with stop and target percentages.
        """
        # Reuse the shared scoring function from multi-timeframe
        score = self._mtf_scorer._score_timeframe(analysis)

        # Entry threshold
        enter = score >= 0.4

        return {
            "enter": enter,
            "score": score,
            "stop_pct": 0.04 if score > 0.6 else 0.05,
            "target_pct": 0.06 if score > 0.6 else 0.08,
        }

    def _compile_results(
        self,
        trades: list[BacktestPosition],
        equity_curve: list[dict],
        final_balance: float,
        max_drawdown: float,
        candles: list[dict],
    ) -> BacktestResult:
        """Compile all backtest metrics including risk-adjusted returns."""
        import math

        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        total_wins = sum(t.pnl for t in winners) if winners else 0
        total_losses = abs(sum(t.pnl for t in losers)) if losers else 0

        total_return = (final_balance - self.initial_balance) / self.initial_balance

        # ── Sharpe & Sortino from equity curve ──
        sharpe = 0.0
        sortino = 0.0
        calmar = 0.0
        if len(equity_curve) >= 2:
            equities = [e["equity"] for e in equity_curve]
            # Per-period returns
            returns = [
                (equities[i] - equities[i - 1]) / equities[i - 1]
                for i in range(1, len(equities))
                if equities[i - 1] > 0
            ]
            if returns:
                mean_ret = sum(returns) / len(returns)
                # Std dev of all returns (Sharpe)
                variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
                std_ret = math.sqrt(variance) if variance > 0 else 0
                # Downside deviation (Sortino) — only negative returns
                downside = [r for r in returns if r < 0]
                down_var = sum(r ** 2 for r in downside) / len(returns) if downside else 0
                down_std = math.sqrt(down_var) if down_var > 0 else 0

                # Annualize: assume ~8760 candle-periods per year for hourly data
                # (365 × 24). Adjust by sqrt for volatility.
                annualization = math.sqrt(len(returns))  # simple: total periods
                sharpe = (mean_ret / std_ret * annualization) if std_ret > 0 else 0
                sortino = (mean_ret / down_std * annualization) if down_std > 0 else 0

        # Calmar = annualized return / max drawdown
        if max_drawdown > 0 and len(equity_curve) >= 2:
            # Rough annualization based on candle count
            n_periods = len(equity_curve)
            annualized_return = total_return * (8760 / max(n_periods, 1))
            calmar = annualized_return / max_drawdown

        # ── Buy-and-hold benchmark ──
        benchmark_return = 0.0
        if candles and len(candles) >= 2:
            first_price = float(candles[0].get("close", 0))
            last_price = float(candles[-1].get("close", 0))
            if first_price > 0:
                benchmark_return = (last_price - first_price) / first_price

        alpha = total_return - benchmark_return

        result = BacktestResult(
            start_date=candles[0].get("start", "") if candles else "",
            end_date=candles[-1].get("start", "") if candles else "",
            initial_balance=self.initial_balance,
            final_balance=final_balance,
            total_return_pct=total_return,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=len(winners) / len(trades) if trades else 0,
            avg_win=total_wins / len(winners) if winners else 0,
            avg_loss=total_losses / len(losers) if losers else 0,
            max_drawdown_pct=max_drawdown,
            sharpe_ratio=round(sharpe, 3),
            sortino_ratio=round(sortino, 3),
            calmar_ratio=round(calmar, 3),
            profit_factor=total_wins / total_losses if total_losses > 0 else float("inf"),
            largest_win=max((t.pnl for t in winners), default=0),
            largest_loss=min((t.pnl for t in losers), default=0),
            benchmark_return_pct=round(benchmark_return, 6),
            alpha=round(alpha, 6),
            trades=[self._pos_to_dict(t) for t in trades],
            equity_curve=equity_curve,
        )

        return result

    def _pos_to_dict(self, pos: BacktestPosition) -> dict:
        return {
            "pair": pos.pair,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": pos.exit_price,
            "quantity": pos.quantity,
            "pnl": pos.pnl,
            "pnl_pct": pos.pnl_pct,
            "exit_reason": pos.exit_reason,
            "entry_time": pos.entry_time,
            "exit_time": pos.exit_time,
        }

    def print_report(self, result: BacktestResult) -> str:
        """Generate a human-readable backtest report."""
        report = f"""
{'='*60}
  📊 BACKTEST REPORT
{'='*60}

  Period:        {result.start_date} — {result.end_date}
  Initial:       {format_currency(result.initial_balance)}
  Final:         {format_currency(result.final_balance)}
  Return:        {format_percentage(result.total_return_pct)}

  Total Trades:  {result.total_trades}
  Winners:       {result.winning_trades} ({format_percentage(result.win_rate)})
  Losers:        {result.losing_trades}

  Avg Win:       {format_currency(result.avg_win)}
  Avg Loss:      {format_currency(result.avg_loss)}
  Largest Win:   {format_currency(result.largest_win)}
  Largest Loss:  {format_currency(result.largest_loss)}

  ── Risk-Adjusted Metrics ──
  Max Drawdown:  {format_percentage(result.max_drawdown_pct)}
  Profit Factor: {result.profit_factor:.2f}
  Sharpe Ratio:  {result.sharpe_ratio:.3f}
  Sortino Ratio: {result.sortino_ratio:.3f}
  Calmar Ratio:  {result.calmar_ratio:.3f}

  ── Benchmark ──
  Buy & Hold:    {format_percentage(result.benchmark_return_pct)}
  Strategy α:    {format_percentage(result.alpha)}

{'='*60}
"""
        print(report)
        logger.info(report)
        return report

    def save_results(self, result: BacktestResult, filepath: str = "data/backtest_results.json") -> None:
        """Save backtest results to a JSON file."""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            json.dump({
                "start_date": result.start_date,
                "end_date": result.end_date,
                "initial_balance": result.initial_balance,
                "final_balance": result.final_balance,
                "total_return_pct": result.total_return_pct,
                "total_trades": result.total_trades,
                "win_rate": result.win_rate,
                "max_drawdown_pct": result.max_drawdown_pct,
                "sharpe_ratio": result.sharpe_ratio,
                "sortino_ratio": result.sortino_ratio,
                "calmar_ratio": result.calmar_ratio,
                "profit_factor": result.profit_factor,
                "benchmark_return_pct": result.benchmark_return_pct,
                "alpha": result.alpha,
                "cost_sensitivity": result.cost_sensitivity,
                "trades": result.trades,
            }, f, indent=2, default=str)
        logger.info(f"Results saved to {filepath}")

    # =========================================================================
    # Cost Sensitivity Analysis (Phase 1.3)
    # =========================================================================

    def cost_sensitivity_analysis(
        self,
        candles: list[dict],
        pair: str = "BTC-USD",
        fee_range: tuple[float, ...] = (0.001, 0.002, 0.004, 0.006, 0.008, 0.010),
        slippage_range: tuple[float, ...] = (0.0005, 0.001, 0.002, 0.005),
    ) -> list[dict]:
        """
        Sweep fee and slippage parameters to find the break-even point.

        Returns a list of {fee_pct, slippage_pct, return_pct, profitable} dicts.
        """
        results = []
        original_fee = self.fee_pct
        original_slip = self.slippage_pct

        for fee in fee_range:
            for slip in slippage_range:
                self.fee_pct = fee
                self.slippage_pct = slip
                try:
                    r = self.run(candles, pair=pair)
                    results.append({
                        "fee_pct": fee,
                        "slippage_pct": slip,
                        "return_pct": round(r.total_return_pct, 6),
                        "sharpe": r.sharpe_ratio,
                        "trades": r.total_trades,
                        "profitable": r.total_return_pct > 0,
                    })
                except Exception as e:
                    logger.warning(f"Cost sensitivity run failed (fee={fee}, slip={slip}): {e}")

        # Restore original values
        self.fee_pct = original_fee
        self.slippage_pct = original_slip

        # Find break-even fee threshold
        for r in sorted(results, key=lambda x: x["fee_pct"]):
            if not r["profitable"]:
                logger.info(f"⚠️ Break-even fee threshold: ~{r['fee_pct']*100:.2f}%")
                break

        return results
