"""
Quantitative strategy substrate.

This subpackage holds **deterministic, numerical** strategies that:

  * Have NO LLM dependency.
  * Are regime-aware (each declares the regimes it is meaningful in).
  * Emit ``QuantSignal`` objects on a unified contract so the capital
    allocator and the Signal Edge Library can score them uniformly.

Public exports:
    - ``QuantSignal``        — the unified output dataclass.
    - ``MarketState``        — input snapshot for one cycle.
    - ``QuantStrategy``      — abstract base class.
    - ``ZScoreMeanReversion``— first concrete strategy.
"""

from .base import MarketState, QuantSignal, QuantStrategy
from .momentum_factor import MomentumFactor
from .zscore_mean_reversion import ZScoreMeanReversion

__all__ = [
    "MarketState",
    "QuantSignal",
    "QuantStrategy",
    "MomentumFactor",
    "ZScoreMeanReversion",
]
