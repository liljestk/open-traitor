"""
Smart execution planner (Phase 5).

Takes a high-level execution *intent* (pair, side, total_notional,
urgency) plus market microstructure (best bid/ask, top-of-book sizes,
ATR, spread) and produces a structured plan of child orders:

  • Tranched VWAP-style schedule across N child orders.
  • Limit-price drift towards mid as the slice ages.
  • Pre-trade order-book depth check — refuses if the book cannot
    absorb the slice within the configured slippage budget.
  • Optional partial-profit ladder for entries (multiple take-profit
    legs).

This module is a *pure planner* — it does not place orders, does not
talk to exchanges. The orchestrator's executor consumes
:class:`ExecutionPlan` and routes the children. Keeping execution
intelligence separate from order routing means we can:

  • Unit-test execution decisions deterministically.
  • Swap routing backends (REST/WebSocket/FIX) independently.
  • Replay/backtest execution against historical microstructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class MicroSnapshot:
    """Snapshot of market microstructure for one pair."""
    pair: str
    best_bid: float
    best_ask: float
    bid_depth: float                 # cumulative size on bid side, top N levels
    ask_depth: float
    atr: float = 0.0
    spread_bps: Optional[float] = None  # auto-computed if None

    @property
    def mid(self) -> float:
        return 0.5 * (self.best_bid + self.best_ask)

    @property
    def computed_spread_bps(self) -> float:
        if self.spread_bps is not None:
            return self.spread_bps
        m = self.mid
        if m <= 0:
            return float("inf")
        return (self.best_ask - self.best_bid) / m * 10_000.0


@dataclass(frozen=True)
class ChildOrder:
    """Single tranche in an execution plan."""
    pair: str
    side: str               # "buy" or "sell"
    size: float             # base-asset units (or notional units, executor's choice)
    limit_price: float
    delay_ms: int           # release this many ms after the previous slice
    drift_to_mid_after_ms: int  # cancel/replace toward mid after this many ms unfilled


@dataclass(frozen=True)
class ProfitLeg:
    take_profit_price: float
    fraction: float


@dataclass(frozen=True)
class ExecutionPlan:
    pair: str
    side: str
    total_size: float
    children: tuple[ChildOrder, ...]
    profit_legs: tuple[ProfitLeg, ...] = field(default=())
    rejected_reason: Optional[str] = None
    notes: dict = field(default_factory=dict)

    @property
    def is_executable(self) -> bool:
        return self.rejected_reason is None and len(self.children) > 0


class SmartExecutionPlanner:
    """Build a child-order schedule from a parent intent + microstructure."""

    def __init__(
        self,
        *,
        max_slices: int = 5,
        urgency_to_slices: tuple[int, int, int] = (5, 3, 1),  # low / med / high
        max_spread_bps: float = 30.0,
        max_slippage_bps: float = 15.0,
        depth_safety_factor: float = 3.0,  # require depth >= safety * slice size
        atr_drift_fraction: float = 0.25,  # initial limit offset = atr * frac
        min_drift_bps: float = 2.0,
        rebalance_after_ms: int = 1500,
        profit_ladder: tuple[float, ...] = (0.5, 0.5),  # ATR multiples
        profit_legs_fractions: tuple[float, ...] = (0.5, 0.5),
    ) -> None:
        if len(urgency_to_slices) != 3:
            raise ValueError("urgency_to_slices must have 3 entries (low,med,high)")
        if abs(sum(profit_legs_fractions) - 1.0) > 1e-6:
            raise ValueError("profit_legs_fractions must sum to 1")
        if len(profit_ladder) != len(profit_legs_fractions):
            raise ValueError("profit_ladder and profit_legs_fractions must align")
        self.max_slices = int(max_slices)
        self.urgency_to_slices = urgency_to_slices
        self.max_spread_bps = float(max_spread_bps)
        self.max_slippage_bps = float(max_slippage_bps)
        self.depth_safety_factor = float(depth_safety_factor)
        self.atr_drift_fraction = float(atr_drift_fraction)
        self.min_drift_bps = float(min_drift_bps)
        self.rebalance_after_ms = int(rebalance_after_ms)
        self.profit_ladder = profit_ladder
        self.profit_legs_fractions = profit_legs_fractions

    # ------------------------------------------------------------------ #

    def plan(
        self,
        *,
        pair: str,
        side: str,                     # "buy" or "sell"
        total_size: float,             # base-asset units
        snap: MicroSnapshot,
        urgency: str = "medium",       # "low" | "medium" | "high"
        attach_profit_ladder: bool = False,
    ) -> ExecutionPlan:
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if total_size <= 0:
            return ExecutionPlan(pair, side, 0.0, (), rejected_reason="non_positive_size")

        # 1. Spread sanity.
        sp = snap.computed_spread_bps
        if sp > self.max_spread_bps:
            return ExecutionPlan(
                pair, side, total_size, (),
                rejected_reason=f"spread_too_wide ({sp:.1f}bps > {self.max_spread_bps})",
                notes={"spread_bps": sp},
            )

        # 2. Slice count by urgency.
        n = self._slices_for(urgency)
        n = max(1, min(n, self.max_slices))

        slice_size = total_size / n

        # 3. Pre-trade depth check.
        avail_depth = snap.ask_depth if side == "buy" else snap.bid_depth
        if avail_depth < self.depth_safety_factor * slice_size:
            return ExecutionPlan(
                pair, side, total_size, (),
                rejected_reason=(
                    f"insufficient_depth (avail={avail_depth:.6g} "
                    f"< need={self.depth_safety_factor * slice_size:.6g})"
                ),
                notes={"avail_depth": avail_depth, "slice_size": slice_size},
            )

        # 4. Build child orders.
        children = self._build_children(side, snap, slice_size, n)

        # 5. Optional take-profit ladder (entries only).
        legs: tuple[ProfitLeg, ...] = ()
        if attach_profit_ladder and snap.atr > 0:
            legs = self._profit_ladder(side, snap, total_size)

        return ExecutionPlan(
            pair=pair, side=side, total_size=total_size,
            children=tuple(children), profit_legs=legs,
            notes={
                "spread_bps": round(sp, 3),
                "slice_size": round(slice_size, 8),
                "urgency": urgency,
            },
        )

    # ------------------------------------------------------------------ #

    def _slices_for(self, urgency: str) -> int:
        u = (urgency or "medium").lower()
        if u == "low":
            return self.urgency_to_slices[0]
        if u == "high":
            return self.urgency_to_slices[2]
        return self.urgency_to_slices[1]

    def _build_children(
        self, side: str, snap: MicroSnapshot, slice_size: float, n: int,
    ) -> list[ChildOrder]:
        mid = snap.mid
        if mid <= 0:
            return []
        # Initial limit price is *passive*: away from mid by max(min_drift_bps, atr-frac).
        atr_drift = snap.atr * self.atr_drift_fraction
        min_drift = mid * (self.min_drift_bps / 10_000.0)
        drift = max(atr_drift, min_drift)

        # Total intended duration scales with n (more slices = patient).
        # Spacing: 2 * rebalance_after for low urgency; 1 * for high.
        spacing_ms = self.rebalance_after_ms if n <= 2 else self.rebalance_after_ms * 2

        children: list[ChildOrder] = []
        for i in range(n):
            if side == "buy":
                # Start below mid; later slices push closer to mid.
                ramp = i / max(1, n - 1) if n > 1 else 0.0
                price = mid - drift + ramp * drift  # i=0 → mid-drift, i=n-1 → mid
            else:
                ramp = i / max(1, n - 1) if n > 1 else 0.0
                price = mid + drift - ramp * drift  # i=0 → mid+drift, i=n-1 → mid
            # Clamp to inside book on first slice for safety.
            if side == "buy":
                price = min(price, snap.best_ask)
            else:
                price = max(price, snap.best_bid)
            children.append(ChildOrder(
                pair=snap.pair,
                side=side,
                size=round(slice_size, 8),
                limit_price=round(float(price), 8),
                delay_ms=0 if i == 0 else spacing_ms,
                drift_to_mid_after_ms=self.rebalance_after_ms,
            ))
        return children

    def _profit_ladder(
        self, side: str, snap: MicroSnapshot, total_size: float,
    ) -> tuple[ProfitLeg, ...]:
        mid = snap.mid
        legs: list[ProfitLeg] = []
        for atr_mult, frac in zip(self.profit_ladder, self.profit_legs_fractions):
            if side == "buy":
                tp = mid + atr_mult * snap.atr
            else:
                tp = mid - atr_mult * snap.atr
            legs.append(ProfitLeg(
                take_profit_price=round(float(tp), 8),
                fraction=float(frac),
            ))
        return tuple(legs)


__all__ = [
    "SmartExecutionPlanner", "ExecutionPlan", "ChildOrder",
    "ProfitLeg", "MicroSnapshot",
]
