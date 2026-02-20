"""
Deterministic Strategy Modules — Backtestable, reproducible alpha generation.

Strategies run alongside (not replacing) the LLM strategist. The LLM acts as a
meta-strategy: it receives deterministic signals and decides final weighting.
"""

from .base import BaseStrategy, StrategySignal, StrategyType
from .ema_crossover import EMACrossoverStrategy
from .bollinger_reversion import BollingerReversionStrategy
from .pairs_monitor import PairsCorrelationMonitor

__all__ = [
    "BaseStrategy",
    "StrategySignal",
    "StrategyType",
    "EMACrossoverStrategy",
    "BollingerReversionStrategy",
    "PairsCorrelationMonitor",
]
