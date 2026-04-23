"""Tests for SmartExecutionPlanner (Phase 5)."""

from __future__ import annotations

import pytest

from src.core.smart_execution import (
    ChildOrder,
    MicroSnapshot,
    ProfitLeg,
    SmartExecutionPlanner,
)


def _snap(
    *,
    bid=99.99, ask=100.01,
    bid_depth=10.0, ask_depth=10.0, atr=0.5,
):
    return MicroSnapshot(
        pair="BTC-USD",
        best_bid=bid, best_ask=ask,
        bid_depth=bid_depth, ask_depth=ask_depth, atr=atr,
    )


# --------------------------------------------------------------------- #

class TestSmartExecutionPlanner:
    def setup_method(self):
        self.p = SmartExecutionPlanner(
            max_spread_bps=50.0, max_slippage_bps=20.0,
            depth_safety_factor=2.0, urgency_to_slices=(4, 2, 1),
        )

    def test_invalid_init(self):
        with pytest.raises(ValueError):
            SmartExecutionPlanner(urgency_to_slices=(1, 2))
        with pytest.raises(ValueError):
            SmartExecutionPlanner(profit_legs_fractions=(0.3, 0.3))
        with pytest.raises(ValueError):
            SmartExecutionPlanner(profit_ladder=(0.5,), profit_legs_fractions=(0.5, 0.5))

    def test_invalid_side(self):
        with pytest.raises(ValueError):
            self.p.plan(pair="X", side="long", total_size=1.0, snap=_snap())

    def test_zero_size_rejected(self):
        plan = self.p.plan(pair="X", side="buy", total_size=0.0, snap=_snap())
        assert not plan.is_executable
        assert "non_positive" in plan.rejected_reason

    def test_wide_spread_rejected(self):
        snap = _snap(bid=99.0, ask=101.0)  # 200 bps spread
        plan = self.p.plan(pair="X", side="buy", total_size=1.0, snap=snap)
        assert not plan.is_executable and "spread" in plan.rejected_reason

    def test_thin_book_rejected(self):
        snap = _snap(ask_depth=0.5)
        plan = self.p.plan(
            pair="X", side="buy", total_size=2.0,
            snap=snap, urgency="medium",
        )
        assert not plan.is_executable and "depth" in plan.rejected_reason

    def test_buy_plan_low_urgency_more_slices(self):
        plan = self.p.plan(
            pair="BTC-USD", side="buy", total_size=2.0,
            snap=_snap(ask_depth=100), urgency="low",
        )
        assert plan.is_executable
        assert len(plan.children) == 4
        for c in plan.children:
            assert c.side == "buy"
            assert c.limit_price <= _snap().best_ask  # inside or at ask
            assert c.size == pytest.approx(0.5, abs=1e-6)

    def test_buy_plan_high_urgency_one_slice(self):
        plan = self.p.plan(
            pair="X", side="buy", total_size=1.0,
            snap=_snap(ask_depth=100), urgency="high",
        )
        assert plan.is_executable and len(plan.children) == 1

    def test_drift_ramps_toward_mid(self):
        plan = self.p.plan(
            pair="X", side="buy", total_size=4.0,
            snap=_snap(ask_depth=100, atr=2.0), urgency="low",
        )
        prices = [c.limit_price for c in plan.children]
        # For buy, prices should ramp upward (toward mid).
        assert prices == sorted(prices)

    def test_sell_drift_ramps_downward(self):
        plan = self.p.plan(
            pair="X", side="sell", total_size=4.0,
            snap=_snap(bid_depth=100, atr=2.0), urgency="low",
        )
        prices = [c.limit_price for c in plan.children]
        assert prices == sorted(prices, reverse=True)

    def test_first_slice_zero_delay(self):
        plan = self.p.plan(
            pair="X", side="buy", total_size=2.0,
            snap=_snap(ask_depth=100), urgency="low",
        )
        assert plan.children[0].delay_ms == 0
        assert all(c.delay_ms > 0 for c in plan.children[1:])

    def test_profit_ladder_attached(self):
        plan = self.p.plan(
            pair="X", side="buy", total_size=1.0,
            snap=_snap(ask_depth=100, atr=1.0), urgency="medium",
            attach_profit_ladder=True,
        )
        assert plan.is_executable
        assert len(plan.profit_legs) == 2
        assert sum(l.fraction for l in plan.profit_legs) == pytest.approx(1.0)
        # For a buy, take-profits should be above mid.
        for leg in plan.profit_legs:
            assert leg.take_profit_price > _snap().mid

    def test_profit_ladder_for_sell(self):
        plan = self.p.plan(
            pair="X", side="sell", total_size=1.0,
            snap=_snap(bid_depth=100, atr=1.0), urgency="medium",
            attach_profit_ladder=True,
        )
        for leg in plan.profit_legs:
            assert leg.take_profit_price < _snap().mid

    def test_no_profit_ladder_when_atr_zero(self):
        plan = self.p.plan(
            pair="X", side="buy", total_size=1.0,
            snap=_snap(ask_depth=100, atr=0.0), urgency="medium",
            attach_profit_ladder=True,
        )
        assert plan.is_executable and plan.profit_legs == ()

    def test_total_size_preserved(self):
        plan = self.p.plan(
            pair="X", side="buy", total_size=3.0,
            snap=_snap(ask_depth=100), urgency="low",
        )
        assert sum(c.size for c in plan.children) == pytest.approx(3.0, abs=1e-6)
