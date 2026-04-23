"""
Order book imbalance engine.

Inputs an L2 snapshot (top-N bid/ask levels) and emits a directional
short-horizon signal when one side of the book dominates by enough
size to predict next-bar drift.

Standard OBI metric:
    obi = (bid_size - ask_size) / (bid_size + ask_size)
where ``bid_size`` and ``ask_size`` are summed over the configurable
top ``depth_levels`` levels (default 10).

We additionally weight by distance from mid (closer levels weigh
more) to reduce sensitivity to far-out spoofing orders. The output
``score`` is in [-1, 1] (long when bids dominate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Top-of-book L2 snapshot."""
    pair: str
    bids: list[tuple[float, float]]  # [(price, size), ...] descending price
    asks: list[tuple[float, float]]  # [(price, size), ...] ascending price
    timestamp: float = 0.0
    exchange: str = "coinbase"


@dataclass(frozen=True)
class OrderBookSignal:
    pair: str
    score: float            # [-1, 1]; +ve → bid pressure → long bias
    obi: float              # raw imbalance
    weighted_obi: float     # depth-weighted imbalance
    spread_bps: float
    mid: float
    depth_levels: int
    exchange: str
    timestamp: float


class OrderBookImbalance:
    """Stateless OBI computer."""

    def __init__(
        self,
        *,
        depth_levels: int = 10,
        min_score_emit: float = 0.15,
        max_spread_bps: float = 25.0,
        decay_per_level: float = 0.85,
    ) -> None:
        self.depth_levels = int(depth_levels)
        self.min_score_emit = float(min_score_emit)
        self.max_spread_bps = float(max_spread_bps)
        self.decay_per_level = float(decay_per_level)

    # ------------------------------------------------------------------ #

    def evaluate(self, snap: OrderBookSnapshot) -> Optional[OrderBookSignal]:
        if not snap.bids or not snap.asks:
            return None

        best_bid = snap.bids[0][0]
        best_ask = snap.asks[0][0]
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return None  # crossed/empty book — bail

        mid = 0.5 * (best_bid + best_ask)
        spread_bps = (best_ask - best_bid) / mid * 10_000.0
        if spread_bps > self.max_spread_bps:
            return None  # too wide → noise dominates

        depth = self.depth_levels
        bid_sizes = np.array([s for _, s in snap.bids[:depth]], dtype=float)
        ask_sizes = np.array([s for _, s in snap.asks[:depth]], dtype=float)
        if bid_sizes.sum() <= 0 or ask_sizes.sum() <= 0:
            return None

        # Plain OBI.
        obi = float((bid_sizes.sum() - ask_sizes.sum()) / (bid_sizes.sum() + ask_sizes.sum()))

        # Depth-weighted OBI: level i (0-indexed) gets weight decay^i.
        n = max(len(bid_sizes), len(ask_sizes))
        w = np.power(self.decay_per_level, np.arange(n))
        bw = (bid_sizes * w[:len(bid_sizes)]).sum()
        aw = (ask_sizes * w[:len(ask_sizes)]).sum()
        weighted_obi = float((bw - aw) / (bw + aw)) if (bw + aw) > 0 else 0.0

        # Score = weighted OBI clipped, with magnitude floor.
        score = max(-1.0, min(1.0, weighted_obi))
        if abs(score) < self.min_score_emit:
            return None

        return OrderBookSignal(
            pair=snap.pair,
            score=score,
            obi=obi,
            weighted_obi=weighted_obi,
            spread_bps=spread_bps,
            mid=mid,
            depth_levels=depth,
            exchange=snap.exchange,
            timestamp=snap.timestamp,
        )


__all__ = ["OrderBookImbalance", "OrderBookSnapshot", "OrderBookSignal"]
