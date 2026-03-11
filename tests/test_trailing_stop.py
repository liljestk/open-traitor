"""
Tests for src/core/trailing_stop.py — Trailing stops and tiered exits.
"""
from __future__ import annotations

import pytest

from src.core.trailing_stop import TrailingStop, TrailingStopManager, StopTier


# ═══════════════════════════════════════════════════════════════════════════
# TrailingStop — Long positions
# ═══════════════════════════════════════════════════════════════════════════

class TestTrailingStopLong:
    def test_initial_state(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        assert ts.pair == "BTC-EUR"
        assert ts.entry_price == 50000
        assert ts.stop_price == pytest.approx(48500)  # 50000 * 0.97
        assert ts.triggered is False
        assert ts.side == "long"

    def test_stop_trails_up(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        ts.update(55000)  # Price goes up
        assert ts.highest_price == 55000
        assert ts.stop_price == pytest.approx(55000 * 0.97)
        assert ts.triggered is False

    def test_stop_does_not_trail_down(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        ts.update(55000)
        old_stop = ts.stop_price
        ts.update(53000)  # Price drops but above stop
        assert ts.stop_price == pytest.approx(old_stop)  # Stop stays

    def test_trigger_on_price_drop(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        ts.update(55000)  # Trail up
        triggered = ts.update(55000 * 0.97 - 1)  # Below stop
        assert triggered is True
        assert ts.triggered is True
        assert ts.trigger_price is not None

    def test_custom_initial_stop(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03,
                          initial_stop=49000)
        assert ts.stop_price == 49000

    def test_already_triggered_returns_true(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        ts.update(48000)  # Trigger
        assert ts.update(47000) is True  # Still triggered


# ═══════════════════════════════════════════════════════════════════════════
# TrailingStop — Short positions
# ═══════════════════════════════════════════════════════════════════════════

class TestTrailingStopShort:
    def test_initial_state_short(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03, side="short")
        assert ts.stop_price == pytest.approx(51500)  # 50000 * 1.03
        assert ts.side == "short"

    def test_stop_trails_down(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03, side="short")
        ts.update(45000)  # Price drops (good for short)
        assert ts.lowest_price == 45000
        assert ts.stop_price == pytest.approx(45000 * 1.03)

    def test_trigger_on_price_rise(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03, side="short")
        triggered = ts.update(52000)  # Above stop — triggered
        assert triggered is True


# ═══════════════════════════════════════════════════════════════════════════
# Tiered partial exits
# ═══════════════════════════════════════════════════════════════════════════

class TestTieredExits:
    def test_tier_triggers_on_profit(self):
        tiers = [{"trigger_pct": 0.03, "exit_fraction": 0.33}]
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03,
                          tiers=tiers, total_quantity=1.0)
        ts.update(51500 + 1)  # 3% above entry
        exits = ts.get_pending_tier_exits()
        assert len(exits) == 1
        assert exits[0]["exit_quantity"] == pytest.approx(0.33)
        assert ts.remaining_quantity == pytest.approx(0.67)

    def test_tier_triggers_only_once(self):
        tiers = [{"trigger_pct": 0.03, "exit_fraction": 0.33}]
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03,
                          tiers=tiers, total_quantity=1.0)
        ts.update(51600)
        ts.get_pending_tier_exits()  # Clear
        ts.update(52000)
        exits = ts.get_pending_tier_exits()
        assert len(exits) == 0  # Already triggered

    def test_multiple_tiers(self):
        tiers = [
            {"trigger_pct": 0.03, "exit_fraction": 0.33},
            {"trigger_pct": 0.06, "exit_fraction": 0.50},
        ]
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03,
                          tiers=tiers, total_quantity=1.0)
        # Trigger first tier
        ts.update(51600)
        exits1 = ts.get_pending_tier_exits()
        assert len(exits1) == 1

        # Trigger second tier
        ts.update(53100)
        exits2 = ts.get_pending_tier_exits()
        assert len(exits2) == 1
        assert ts.remaining_quantity < 0.67  # Both tiers have eaten into qty

    def test_no_tiers_no_exits(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        ts.update(55000)
        exits = ts.get_pending_tier_exits()
        assert exits == []


# ═══════════════════════════════════════════════════════════════════════════
# Serialization
# ═══════════════════════════════════════════════════════════════════════════

class TestSerialization:
    def test_to_dict_long(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03)
        d = ts.to_dict()
        assert d["pair"] == "BTC-EUR"
        assert d["entry_price"] == 50000
        assert "highest_price" in d
        assert "lowest_price" not in d

    def test_to_dict_short(self):
        ts = TrailingStop("BTC-EUR", entry_price=50000, trail_pct=0.03, side="short")
        d = ts.to_dict()
        assert "lowest_price" in d
        assert "highest_price" not in d


# ═══════════════════════════════════════════════════════════════════════════
# TrailingStopManager
# ═══════════════════════════════════════════════════════════════════════════

class TestTrailingStopManager:
    def test_add_and_get_stop(self):
        mgr = TrailingStopManager(default_trail_pct=0.03)
        mgr.add_stop("BTC-EUR", 50000)
        stop = mgr.get_stop("BTC-EUR")
        assert stop is not None
        assert stop["pair"] == "BTC-EUR"

    def test_get_nonexistent_stop(self):
        mgr = TrailingStopManager()
        assert mgr.get_stop("FAKE-EUR") is None

    def test_remove_stop(self):
        mgr = TrailingStopManager()
        mgr.add_stop("BTC-EUR", 50000)
        mgr.remove_stop("BTC-EUR")
        assert mgr.get_stop("BTC-EUR") is None

    def test_update_prices_triggers(self):
        mgr = TrailingStopManager(default_trail_pct=0.05)
        mgr.add_stop("BTC-EUR", 50000)
        triggered = mgr.update_prices({"BTC-EUR": 47000})  # Below 5% trail
        assert len(triggered) == 1

    def test_update_prices_no_trigger(self):
        mgr = TrailingStopManager(default_trail_pct=0.05)
        mgr.add_stop("BTC-EUR", 50000)
        triggered = mgr.update_prices({"BTC-EUR": 55000})
        assert len(triggered) == 0

    def test_get_all_stops(self):
        mgr = TrailingStopManager()
        mgr.add_stop("BTC-EUR", 50000)
        mgr.add_stop("ETH-EUR", 3000)
        all_stops = mgr.get_all_stops()
        assert len(all_stops) == 2
        assert "BTC-EUR" in all_stops

    def test_active_count(self):
        mgr = TrailingStopManager(default_trail_pct=0.05)
        mgr.add_stop("BTC-EUR", 50000)
        mgr.add_stop("ETH-EUR", 3000)
        assert mgr.get_active_count() == 2
        mgr.update_prices({"BTC-EUR": 40000})  # Trigger BTC
        assert mgr.get_active_count() == 1

    def test_tighten_to_breakeven(self):
        mgr = TrailingStopManager()
        mgr.add_stop("BTC-EUR", 50000, trail_pct=0.05)
        result = mgr.tighten_to_breakeven("BTC-EUR")
        assert result is not None
        assert result["stop_price"] == 50000

    def test_tighten_nonexistent(self):
        mgr = TrailingStopManager()
        assert mgr.tighten_to_breakeven("FAKE") is None

    def test_get_pending_tier_exits(self):
        mgr = TrailingStopManager(enable_tiers=True)
        mgr.add_stop("BTC-EUR", 50000, total_quantity=1.0)
        mgr.update_prices({"BTC-EUR": 51600})  # Trigger 3% tier
        exits = mgr.get_pending_tier_exits()
        assert len(exits) >= 1

    def test_default_tiers_used_when_enabled(self):
        mgr = TrailingStopManager(enable_tiers=True)
        stop = mgr.add_stop("BTC-EUR", 50000, total_quantity=1.0)
        assert len(stop.tiers) == len(TrailingStopManager.DEFAULT_TIERS)
