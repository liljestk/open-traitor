"""Tests for RouteFinder — swap route discovery and cost estimation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.core.route_finder import RouteFinder, SwapRoute, RouteLeg
from src.core.fee_manager import FeeManager, FeeEstimate


def _make_product(pid: str, vol: float = 50000, price: float = 100.0, online: bool = True) -> dict:
    base, quote = pid.split("-")
    return {
        "product_id": pid,
        "base_currency_id": base,
        "quote_currency_id": quote,
        "volume_24h": str(vol),
        "price": str(price),
        "trading_disabled": not online,
        "is_disabled": not online,
        "status": "online" if online else "offline",
    }


@pytest.fixture
def fee_mgr():
    return FeeManager({"fees": {"trade_fee_pct": 0.006, "maker_fee_pct": 0.004}})


@pytest.fixture
def products():
    return [
        _make_product("BTC-EUR", vol=1_000_000, price=50000),
        _make_product("ETH-EUR", vol=500_000, price=3000),
        _make_product("SOL-EUR", vol=100_000, price=100),
        _make_product("ETH-BTC", vol=200_000, price=0.06),
        _make_product("SOL-USDC", vol=80_000, price=100),
        _make_product("BTC-USDC", vol=900_000, price=50000),
        _make_product("ADA-EUR", vol=50_000, price=0.50),
        _make_product("ALGO-EUR", vol=30_000, price=0.20),
    ]


@pytest.fixture
def rf(fee_mgr, products):
    mock_client = MagicMock()
    mock_client._refresh_product_cache.return_value = products
    finder = RouteFinder(mock_client, fee_mgr, {"routing": {
        "bridge_currencies": ["EUR", "USDC", "BTC"],
        "min_bridge_volume_24h": 10000,
        "slippage_factor": 0.001,
    }})
    return finder


# ═══════════════════════════════════════════════════════════════════════
# Direct Routes
# ═══════════════════════════════════════════════════════════════════════

class TestDirectRoutes:
    def test_direct_pair_found(self, rf):
        routes = rf.find_routes("BTC", "EUR", 1000)
        direct = [r for r in routes if r.route_type == "direct"]
        assert len(direct) >= 1
        assert direct[0].n_legs == 1

    def test_direct_both_directions(self, rf):
        """ETH-BTC pair should yield a direct route ETH→BTC."""
        routes = rf.find_routes("ETH", "BTC", 500)
        direct = [r for r in routes if r.route_type == "direct"]
        assert len(direct) >= 1

    def test_no_direct_pair(self, rf):
        routes = rf.find_routes("ADA", "ALGO", 100)
        direct = [r for r in routes if r.route_type == "direct"]
        assert len(direct) == 0


# ═══════════════════════════════════════════════════════════════════════
# Bridged Routes
# ═══════════════════════════════════════════════════════════════════════

class TestBridgedRoutes:
    def test_bridged_via_eur(self, rf):
        """ADA→ALGO should bridge via EUR."""
        routes = rf.find_routes("ADA", "ALGO", 100)
        bridged = [r for r in routes if r.route_type == "bridged"]
        assert len(bridged) >= 1
        eur_bridges = [r for r in bridged if r.bridge_currency == "EUR"]
        assert len(eur_bridges) >= 1
        assert eur_bridges[0].n_legs == 2

    def test_self_bridge_skipped(self, rf):
        """Should not bridge BTC→ETH via BTC."""
        routes = rf.find_routes("BTC", "ETH", 500)
        for r in routes:
            if r.route_type == "bridged":
                assert r.bridge_currency not in ("BTC", "ETH")


# ═══════════════════════════════════════════════════════════════════════
# Sorting & Cost
# ═══════════════════════════════════════════════════════════════════════

class TestCostSorting:
    def test_routes_sorted_by_cost(self, rf):
        routes = rf.find_routes("ADA", "ALGO", 100)
        if len(routes) > 1:
            for i in range(len(routes) - 1):
                assert routes[i].total_cost_pct <= routes[i + 1].total_cost_pct

    def test_cost_includes_fee_and_slippage(self, rf):
        routes = rf.find_routes("BTC", "EUR", 1000)
        for r in routes:
            assert r.total_cost_pct == pytest.approx(r.fee_pct + r.slippage_pct)
            assert r.fee_pct > 0


# ═══════════════════════════════════════════════════════════════════════
# Slippage Estimation
# ═══════════════════════════════════════════════════════════════════════

class TestSlippage:
    def test_slippage_increases_with_size(self, rf):
        rf._rebuild_index()
        s1 = rf._estimate_slippage(100, 100_000)
        s2 = rf._estimate_slippage(10_000, 100_000)
        assert s2 > s1

    def test_zero_volume_penalized(self, rf):
        s = rf._estimate_slippage(100, 0)
        assert s > 0.005  # Heavy penalty

    def test_high_volume_low_slippage(self, rf):
        s = rf._estimate_slippage(100, 10_000_000)
        assert s < 0.0001


# ═══════════════════════════════════════════════════════════════════════
# Index Management
# ═══════════════════════════════════════════════════════════════════════

class TestIndex:
    def test_disabled_products_excluded(self, fee_mgr):
        prods = [
            _make_product("FOO-EUR", online=True),
            _make_product("BAR-EUR", online=False),
        ]
        mock_client = MagicMock()
        mock_client._refresh_product_cache.return_value = prods
        rf = RouteFinder(mock_client, fee_mgr, {})
        rf._rebuild_index()
        assert "FOO-EUR" in rf._pair_index
        assert "BAR-EUR" not in rf._pair_index


# ═══════════════════════════════════════════════════════════════════════
# Route Summary
# ═══════════════════════════════════════════════════════════════════════

class TestRouteSummary:
    def test_no_routes(self, rf):
        assert rf.get_route_summary([]) == "No routes found"

    def test_has_content(self, rf):
        routes = rf.find_routes("ADA", "ALGO", 100)
        if routes:
            summary = rf.get_route_summary(routes)
            assert "fee=" in summary
            assert "slippage=" in summary


# ═══════════════════════════════════════════════════════════════════════
# SwapRoute repr
# ═══════════════════════════════════════════════════════════════════════

class TestSwapRouteRepr:
    def test_repr(self):
        route = SwapRoute(
            sell_asset="A", buy_asset="B", route_type="direct",
            legs=[RouteLeg(product_id="A-B", side="sell", base_currency="A", quote_currency="B")],
            n_legs=1, fee_pct=0.006, slippage_pct=0.001, total_cost_pct=0.007,
        )
        r = repr(route)
        assert "A→B" in r
        assert "direct" in r
