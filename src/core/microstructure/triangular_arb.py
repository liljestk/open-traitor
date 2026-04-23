"""
Triangular arbitrage detector.

Given three pairs forming a closed cycle (A/B, B/C, A/C) and quotes
(best bid + best ask for each), check whether the round-trip net of
fees and slippage yields a positive profit. Pure math, no I/O.

Cycle (canonical): start with X units of asset A.
  Path 1 (forward):  A → B  → C → A
                     X / ask(A/B) → / ask(B/C) → * bid(A/C) → A_out
  Path 2 (reverse):  A → C → B → A
                     X * bid(A/C) → * bid(B/C) → * bid(A/B) → A_out

Profit = A_out / X − 1, after subtracting fee_bps per leg (3 legs)
and a slippage haircut.

Returns the most profitable direction (or None) when net edge >
``min_edge_bps``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TriangleQuote:
    """Best bid/ask for a pair."""
    pair: str           # e.g. "BTC-USD"
    bid: float          # buyer-side, sell asset @ bid
    ask: float          # seller-side, buy asset @ ask


@dataclass(frozen=True)
class ArbOpportunity:
    cycle: tuple[str, str, str]    # asset cycle A→B→C
    direction: str                 # "forward" or "reverse"
    edge_bps: float                # net edge in bps after fees
    legs: tuple[str, str, str]     # pair symbols traversed
    fee_bps_per_leg: float
    exchange: str = "coinbase"


class TriangularArbDetector:
    """Detect profitable triangular arb cycles on snapshot quotes."""

    def __init__(
        self,
        *,
        fee_bps_per_leg: float = 5.0,
        slippage_bps_per_leg: float = 2.0,
        min_edge_bps: float = 5.0,
    ) -> None:
        self.fee_bps_per_leg = float(fee_bps_per_leg)
        self.slippage_bps_per_leg = float(slippage_bps_per_leg)
        self.min_edge_bps = float(min_edge_bps)

    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        ab: TriangleQuote,  # base=A, quote=B
        bc: TriangleQuote,  # base=B, quote=C
        ac: TriangleQuote,  # base=A, quote=C
        cycle: tuple[str, str, str] = ("A", "B", "C"),
        exchange: str = "coinbase",
    ) -> Optional[ArbOpportunity]:
        if not _valid(ab) or not _valid(bc) or not _valid(ac):
            return None

        # Per-leg cost factor as multiplier on output: 1 - (fee + slip)/10000.
        leg_cost = 1.0 - (self.fee_bps_per_leg + self.slippage_bps_per_leg) / 10_000.0

        # Forward A→B→C→A:
        #   A → B: sell A for B at ab.bid → out_b = 1 * ab.bid * leg_cost
        #   B → C: sell B for C at bc.bid → out_c = out_b * bc.bid * leg_cost
        #   C → A: buy A with C at ac.ask → out_a = out_c / ac.ask * leg_cost
        out_a_fwd = (1.0 * ab.bid * leg_cost) * bc.bid * leg_cost / ac.ask * leg_cost
        edge_fwd_bps = (out_a_fwd - 1.0) * 10_000.0

        # Reverse A→C→B→A:
        #   A → C: sell A for C at ac.bid → out_c = 1 * ac.bid * leg_cost
        #   C → B: buy B with C at bc.ask → out_b = out_c / bc.ask * leg_cost
        #   B → A: buy A with B at ab.ask → out_a = out_b / ab.ask * leg_cost
        out_a_rev = (1.0 * ac.bid * leg_cost) / bc.ask * leg_cost / ab.ask * leg_cost
        edge_rev_bps = (out_a_rev - 1.0) * 10_000.0

        if edge_fwd_bps >= edge_rev_bps and edge_fwd_bps >= self.min_edge_bps:
            return ArbOpportunity(
                cycle=cycle,
                direction="forward",
                edge_bps=round(edge_fwd_bps, 4),
                legs=(ab.pair, bc.pair, ac.pair),
                fee_bps_per_leg=self.fee_bps_per_leg,
                exchange=exchange,
            )
        if edge_rev_bps > edge_fwd_bps and edge_rev_bps >= self.min_edge_bps:
            return ArbOpportunity(
                cycle=cycle,
                direction="reverse",
                edge_bps=round(edge_rev_bps, 4),
                legs=(ac.pair, bc.pair, ab.pair),
                fee_bps_per_leg=self.fee_bps_per_leg,
                exchange=exchange,
            )
        return None


def _valid(q: TriangleQuote) -> bool:
    return (
        q is not None
        and q.bid is not None and q.ask is not None
        and q.bid > 0 and q.ask > 0
        and q.ask >= q.bid
    )


__all__ = ["TriangularArbDetector", "TriangleQuote", "ArbOpportunity"]
