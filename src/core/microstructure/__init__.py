"""Microstructure engines (Phase 3).

Pure-logic alpha sources that consume L2 order book snapshots,
funding rates, and cross-pair prices to surface fast-decaying signals
the LLM/quant strategies cannot see in 1m+ candles.

Engines are stateless transforms — they accept structured inputs and
return :class:`QuantSignal`-shaped payloads (or None). I/O lives in
the trading orchestrator, never here, so each engine is trivially
testable and composable.
"""

from .funding_basis_monitor import FundingBasisMonitor, FundingSnapshot
from .orderbook_imbalance import OrderBookImbalance, OrderBookSnapshot
from .triangular_arb import TriangularArbDetector, TriangleQuote

__all__ = [
    "FundingBasisMonitor",
    "FundingSnapshot",
    "OrderBookImbalance",
    "OrderBookSnapshot",
    "TriangularArbDetector",
    "TriangleQuote",
]
