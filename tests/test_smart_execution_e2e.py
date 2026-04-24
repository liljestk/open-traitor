"""End-to-end smart execution test (Phase 16 residual).

Validates that ``SmartExecutionPlanner`` produces an executable child-order
schedule that a mock exchange can replay tranche-by-tranche, that maker-only
limit prices are respected, and that risk rules (spread/depth gates) reject
hostile microstructure rather than dump market orders into thin books.
"""

from __future__ import annotations

import pytest

from src.core.smart_execution import (
    ExecutionPlan,
    MicroSnapshot,
    SmartExecutionPlanner,
)


class _MockExchange:
    """Minimal exchange double that records each child fill."""

    def __init__(self, *, fill_ratio: float = 1.0):
        self.fill_ratio = fill_ratio
        self.placed: list[dict] = []

    def place_limit_order(self, *, pair: str, side: str, size: float,
                          limit_price: float, post_only: bool = True):
        # Maker-only must always be true for the planner's children.
        assert post_only, "smart-execution children must be maker-only"
        filled = size * self.fill_ratio
        self.placed.append({
            "pair": pair, "side": side, "size": size,
            "limit_price": limit_price, "filled": filled,
        })
        return {"status": "filled", "filled_size": filled,
                "fill_price": limit_price}


def _execute(plan: ExecutionPlan, exch: _MockExchange) -> float:
    """Walk a plan and accumulate filled base-asset units."""
    total_filled = 0.0
    for child in plan.children:
        res = exch.place_limit_order(
            pair=child.pair, side=child.side, size=child.size,
            limit_price=child.limit_price, post_only=True,
        )
        total_filled += float(res["filled_size"])
    return total_filled


# ---------------------------------------------------------------------- #
# Happy path
# ---------------------------------------------------------------------- #

def test_planner_e2e_buy_executes_full_size():
    snap = MicroSnapshot(
        pair="BTC-USD", best_bid=100.0, best_ask=100.05,
        bid_depth=50.0, ask_depth=50.0, atr=2.0,
    )
    planner = SmartExecutionPlanner(max_slices=5)
    plan = planner.plan(pair="BTC-USD", side="buy",
                        total_size=1.0, snap=snap, urgency="medium")
    assert plan.is_executable
    assert len(plan.children) == 3  # medium urgency → 3 slices
    # All children sized equally
    sizes = [c.size for c in plan.children]
    assert all(abs(s - sizes[0]) < 1e-6 for s in sizes)
    # Limits sit at or below ask (we're a maker buyer)
    for c in plan.children:
        assert c.side == "buy"
        assert c.limit_price <= snap.best_ask + 1e-9

    exch = _MockExchange()
    filled = _execute(plan, exch)
    assert abs(filled - 1.0) < 1e-6
    assert len(exch.placed) == 3


def test_planner_e2e_sell_full_size_and_post_only():
    snap = MicroSnapshot(
        pair="ETH-USD", best_bid=2000.0, best_ask=2000.4,
        bid_depth=80.0, ask_depth=80.0, atr=10.0,
    )
    planner = SmartExecutionPlanner()
    plan = planner.plan(pair="ETH-USD", side="sell",
                        total_size=4.0, snap=snap, urgency="low")
    assert plan.is_executable
    # Sell-side limit prices must be at or above bid (maker)
    for c in plan.children:
        assert c.side == "sell"
        assert c.limit_price >= snap.best_bid - 1e-9
    exch = _MockExchange()
    assert abs(_execute(plan, exch) - 4.0) < 1e-6


# ---------------------------------------------------------------------- #
# Hostile microstructure → planner refuses
# ---------------------------------------------------------------------- #

def test_planner_rejects_wide_spread():
    snap = MicroSnapshot(
        pair="XYZ-USD", best_bid=100.0, best_ask=110.0,  # 952bps spread
        bid_depth=10_000, ask_depth=10_000,
    )
    plan = SmartExecutionPlanner().plan(
        pair="XYZ-USD", side="buy", total_size=1.0, snap=snap,
    )
    assert not plan.is_executable
    assert "spread_too_wide" in plan.rejected_reason
    # Executor must not place any orders against a rejected plan
    exch = _MockExchange()
    if plan.is_executable:  # defensive guard mirrors prod executor
        _execute(plan, exch)
    assert exch.placed == []


def test_planner_rejects_thin_book():
    snap = MicroSnapshot(
        pair="THIN-USD", best_bid=10.0, best_ask=10.01,
        bid_depth=0.5, ask_depth=0.5, atr=0.1,
    )
    plan = SmartExecutionPlanner(depth_safety_factor=3.0).plan(
        pair="THIN-USD", side="buy", total_size=1.0, snap=snap,
        urgency="high",  # tries 1 slice → still need 3× depth
    )
    assert not plan.is_executable
    assert "insufficient_depth" in plan.rejected_reason


# ---------------------------------------------------------------------- #
# Profit ladder integration
# ---------------------------------------------------------------------- #

def test_profit_ladder_legs_sum_to_total():
    snap = MicroSnapshot(
        pair="BTC-USD", best_bid=100.0, best_ask=100.05,
        bid_depth=50.0, ask_depth=50.0, atr=2.0,
    )
    plan = SmartExecutionPlanner().plan(
        pair="BTC-USD", side="buy", total_size=2.0, snap=snap,
        attach_profit_ladder=True,
    )
    assert plan.is_executable
    assert len(plan.profit_legs) > 0
    total_frac = sum(leg.fraction for leg in plan.profit_legs)
    assert abs(total_frac - 1.0) < 1e-6
    # TP prices must be above entry mid for a long
    for leg in plan.profit_legs:
        assert leg.take_profit_price > snap.mid


# ---------------------------------------------------------------------- #
# Determinism / replay safety
# ---------------------------------------------------------------------- #

def test_planner_is_deterministic_given_same_input():
    snap = MicroSnapshot(
        pair="BTC-USD", best_bid=100.0, best_ask=100.05,
        bid_depth=50.0, ask_depth=50.0, atr=2.0,
    )
    planner = SmartExecutionPlanner()
    p1 = planner.plan(pair="BTC-USD", side="buy",
                      total_size=1.0, snap=snap, urgency="medium")
    p2 = planner.plan(pair="BTC-USD", side="buy",
                      total_size=1.0, snap=snap, urgency="medium")
    assert [c.limit_price for c in p1.children] == \
           [c.limit_price for c in p2.children]
    assert [c.size for c in p1.children] == [c.size for c in p2.children]


def test_zero_size_is_rejected_cleanly():
    snap = MicroSnapshot(
        pair="BTC-USD", best_bid=100.0, best_ask=100.05,
        bid_depth=50.0, ask_depth=50.0,
    )
    plan = SmartExecutionPlanner().plan(
        pair="BTC-USD", side="buy", total_size=0.0, snap=snap,
    )
    assert not plan.is_executable
    assert plan.rejected_reason == "non_positive_size"


def test_invalid_side_raises():
    snap = MicroSnapshot(
        pair="BTC-USD", best_bid=100.0, best_ask=100.05,
        bid_depth=50.0, ask_depth=50.0,
    )
    with pytest.raises(ValueError):
        SmartExecutionPlanner().plan(
            pair="BTC-USD", side="hodl", total_size=1.0, snap=snap,
        )
