"""
Route Finder — Discovers optimal swap routes across trading pairs.

Instead of always assuming sell→fiat→buy (2-leg), this module:
  1. Checks if a direct pair exists (1 leg — cheapest)
  2. Checks bridged routes via configured bridge currencies (2 legs)
  3. Includes fiat-routed fallback (2 legs)
  4. Estimates per-route cost: fee_pct + slippage_estimate
  5. Returns routes sorted by total_cost_pct (cheapest first)

Slippage model: (trade_amount / volume_24h) * slippage_factor
  — simple but directionally correct.  Heavy volume = lower slippage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.core.fee_manager import FeeManager
from src.utils.logger import get_logger

logger = get_logger("core.route_finder")


@dataclass
class RouteLeg:
    """A single trade leg within a swap route."""
    product_id: str           # e.g. "ETH-BTC"
    side: str                 # "buy" or "sell"
    base_currency: str        # e.g. "ETH"
    quote_currency: str       # e.g. "BTC"
    volume_24h: float = 0.0   # 24h volume in quote currency
    price: float = 0.0        # current price


@dataclass
class SwapRoute:
    """A complete swap route from sell_asset to buy_asset."""
    sell_asset: str           # e.g. "ALGO"
    buy_asset: str            # e.g. "ADA"
    route_type: str           # "direct", "bridged", "fiat"
    bridge_currency: str | None = None  # e.g. "BTC" for bridged routes
    legs: list[RouteLeg] = field(default_factory=list)
    n_legs: int = 0
    fee_pct: float = 0.0     # total fee % across all legs
    slippage_pct: float = 0.0  # estimated slippage %
    total_cost_pct: float = 0.0  # fee_pct + slippage_pct

    def __repr__(self) -> str:
        via = f" via {self.bridge_currency}" if self.bridge_currency else ""
        legs_str = " → ".join(f"{l.product_id}({l.side})" for l in self.legs)
        return (
            f"SwapRoute({self.sell_asset}→{self.buy_asset}{via} | "
            f"{self.route_type} | {self.n_legs} legs | "
            f"cost={self.total_cost_pct*100:.3f}% | {legs_str})"
        )


class RouteFinder:
    """
    Discovers and ranks swap routes between any two crypto assets.

    Uses the Coinbase product cache (10-min TTL) to find available pairs
    and estimates per-route cost including fees and slippage.
    """

    def __init__(
        self,
        coinbase_client,
        fee_manager: FeeManager,
        config: dict,
    ):
        self.coinbase = coinbase_client
        self.fee_manager = fee_manager

        routing_cfg = config.get("routing", {})
        self.bridge_currencies = routing_cfg.get(
            "bridge_currencies", ["EUR", "EURC", "USDC", "BTC"]
        )
        self.min_bridge_volume_24h = routing_cfg.get("min_bridge_volume_24h", 10000)
        self.slippage_factor = routing_cfg.get("slippage_factor", 0.001)

        # Internal pair index: rebuilt lazily from product cache
        self._pair_index: dict[str, dict] = {}  # product_id → product dict
        self._base_index: dict[str, list[dict]] = {}  # base_currency → [products]
        self._quote_index: dict[str, list[dict]] = {}  # quote_currency → [products]
        self._index_ts: float = 0.0
        self._INDEX_TTL: float = 600.0  # 10 min, matches product cache TTL

        logger.info(
            f"🛤️ Route Finder initialized: "
            f"bridges={self.bridge_currencies}, "
            f"min bridge vol={self.min_bridge_volume_24h}, "
            f"slippage factor={self.slippage_factor}"
        )

    def _rebuild_index(self) -> None:
        """Rebuild the pair index from the Coinbase product cache."""
        now = time.time()
        if self._pair_index and (now - self._index_ts) < self._INDEX_TTL:
            return  # Still fresh

        products = self.coinbase._refresh_product_cache()
        if not products:
            return

        pair_index: dict[str, dict] = {}
        base_index: dict[str, list[dict]] = {}
        quote_index: dict[str, list[dict]] = {}

        for prod in products:
            pid = prod.get("product_id", "")
            if not pid:
                continue
            # Only index online, tradable products
            if prod.get("trading_disabled", True):
                continue
            if prod.get("is_disabled", False):
                continue
            if str(prod.get("status", "")).lower() != "online":
                continue

            base = prod.get("base_currency_id", "")
            quote = prod.get("quote_currency_id", "")

            # Parse volume safely
            try:
                vol = float(prod.get("volume_24h", 0))
            except (ValueError, TypeError):
                vol = 0.0
            try:
                price = float(prod.get("price", 0))
            except (ValueError, TypeError):
                price = 0.0

            entry = {
                "product_id": pid,
                "base": base,
                "quote": quote,
                "volume_24h": vol,
                "price": price,
            }

            pair_index[pid] = entry
            base_index.setdefault(base, []).append(entry)
            quote_index.setdefault(quote, []).append(entry)

        self._pair_index = pair_index
        self._base_index = base_index
        self._quote_index = quote_index
        self._index_ts = now

        logger.debug(f"🛤️ Route index rebuilt: {len(pair_index)} tradable products")

    def _estimate_slippage(self, trade_amount: float, volume_24h: float) -> float:
        """Estimate slippage as fraction of trade amount vs daily volume."""
        if volume_24h <= 0:
            return self.slippage_factor * 10  # Penalize unknown volume heavily
        return (trade_amount / volume_24h) * self.slippage_factor

    def _find_pair(self, base: str, quote: str) -> dict | None:
        """Find a tradable pair by base and quote currency."""
        pid = f"{base}-{quote}"
        entry = self._pair_index.get(pid)
        if entry:
            return entry
        return None

    def find_routes(
        self,
        sell_asset: str,
        buy_asset: str,
        quote_amount: float,
    ) -> list[SwapRoute]:
        """
        Find all viable swap routes from sell_asset to buy_asset.

        Args:
            sell_asset: Base currency to sell (e.g. "ALGO")
            buy_asset: Base currency to buy (e.g. "ADA")
            quote_amount: Approximate trade size in quote currency (EUR)

        Returns:
            List of SwapRoute sorted by total_cost_pct (cheapest first)
        """
        self._rebuild_index()

        routes: list[SwapRoute] = []

        # ── 1. Direct pair (1 leg) ──
        # Check SELL-BUY (we sell) and BUY-SELL (we buy)
        direct_sell = self._find_pair(sell_asset, buy_asset)
        if direct_sell:
            fee_est = self.fee_manager.estimate_swap_fees(quote_amount, n_legs=1)
            slippage = self._estimate_slippage(quote_amount, direct_sell["volume_24h"])
            leg = RouteLeg(
                product_id=direct_sell["product_id"],
                side="sell",
                base_currency=sell_asset,
                quote_currency=buy_asset,
                volume_24h=direct_sell["volume_24h"],
                price=direct_sell["price"],
            )
            route = SwapRoute(
                sell_asset=sell_asset,
                buy_asset=buy_asset,
                route_type="direct",
                legs=[leg],
                n_legs=1,
                fee_pct=fee_est.total_fee_pct,
                slippage_pct=slippage,
                total_cost_pct=fee_est.total_fee_pct + slippage,
            )
            routes.append(route)

        direct_buy = self._find_pair(buy_asset, sell_asset)
        if direct_buy:
            fee_est = self.fee_manager.estimate_swap_fees(quote_amount, n_legs=1)
            slippage = self._estimate_slippage(quote_amount, direct_buy["volume_24h"])
            leg = RouteLeg(
                product_id=direct_buy["product_id"],
                side="buy",
                base_currency=buy_asset,
                quote_currency=sell_asset,
                volume_24h=direct_buy["volume_24h"],
                price=direct_buy["price"],
            )
            route = SwapRoute(
                sell_asset=sell_asset,
                buy_asset=buy_asset,
                route_type="direct",
                legs=[leg],
                n_legs=1,
                fee_pct=fee_est.total_fee_pct,
                slippage_pct=slippage,
                total_cost_pct=fee_est.total_fee_pct + slippage,
            )
            routes.append(route)

        # ── 2. Bridged routes via each configured bridge currency (2 legs) ──
        for bridge in self.bridge_currencies:
            if bridge in (sell_asset, buy_asset):
                continue  # No point bridging through self

            # Leg 1: sell_asset → bridge
            #   Check SELL-BRIDGE (sell) and BRIDGE-SELL (buy the bridge)
            leg1_pair = self._find_pair(sell_asset, bridge)
            leg1_side = "sell"
            if not leg1_pair:
                leg1_pair = self._find_pair(bridge, sell_asset)
                leg1_side = "buy"  # buying bridge using sell_asset
            if not leg1_pair:
                continue

            # Leg 2: bridge → buy_asset
            #   Check BUY-BRIDGE (buy with bridge as quote) and BRIDGE-BUY (sell bridge)
            leg2_pair = self._find_pair(buy_asset, bridge)
            leg2_side = "buy"  # buying buy_asset with bridge
            if not leg2_pair:
                leg2_pair = self._find_pair(bridge, buy_asset)
                leg2_side = "sell"  # selling bridge for buy_asset
            if not leg2_pair:
                continue

            # Volume filter — both legs must have sufficient liquidity
            if leg1_pair["volume_24h"] < self.min_bridge_volume_24h:
                continue
            if leg2_pair["volume_24h"] < self.min_bridge_volume_24h:
                continue

            # Cost estimation
            fee_est = self.fee_manager.estimate_swap_fees(quote_amount, n_legs=2)
            slippage1 = self._estimate_slippage(quote_amount, leg1_pair["volume_24h"])
            slippage2 = self._estimate_slippage(quote_amount, leg2_pair["volume_24h"])
            total_slippage = slippage1 + slippage2

            legs = [
                RouteLeg(
                    product_id=leg1_pair["product_id"],
                    side=leg1_side,
                    base_currency=leg1_pair["base"],
                    quote_currency=leg1_pair["quote"],
                    volume_24h=leg1_pair["volume_24h"],
                    price=leg1_pair["price"],
                ),
                RouteLeg(
                    product_id=leg2_pair["product_id"],
                    side=leg2_side,
                    base_currency=leg2_pair["base"],
                    quote_currency=leg2_pair["quote"],
                    volume_24h=leg2_pair["volume_24h"],
                    price=leg2_pair["price"],
                ),
            ]

            route = SwapRoute(
                sell_asset=sell_asset,
                buy_asset=buy_asset,
                route_type="bridged",
                bridge_currency=bridge,
                legs=legs,
                n_legs=2,
                fee_pct=fee_est.total_fee_pct,
                slippage_pct=total_slippage,
                total_cost_pct=fee_est.total_fee_pct + total_slippage,
            )
            routes.append(route)

        # Sort by total cost (cheapest first)
        routes.sort(key=lambda r: r.total_cost_pct)

        if routes:
            logger.info(
                f"🛤️ Routes {sell_asset}→{buy_asset}: {len(routes)} found | "
                f"best: {routes[0].route_type}"
                f"{f' via {routes[0].bridge_currency}' if routes[0].bridge_currency else ''} "
                f"cost={routes[0].total_cost_pct*100:.3f}%"
            )
        else:
            logger.debug(f"🛤️ Routes {sell_asset}→{buy_asset}: none found")

        return routes

    def get_route_summary(self, routes: list[SwapRoute]) -> str:
        """Format routes for logging / display."""
        if not routes:
            return "No routes found"
        lines = []
        for i, r in enumerate(routes, 1):
            via = f" via {r.bridge_currency}" if r.bridge_currency else ""
            legs_str = " → ".join(f"{l.product_id}" for l in r.legs)
            lines.append(
                f"  {i}. {r.route_type}{via}: {legs_str} | "
                f"fee={r.fee_pct*100:.2f}% slippage={r.slippage_pct*100:.3f}% "
                f"total={r.total_cost_pct*100:.3f}%"
            )
        return "\n".join(lines)
