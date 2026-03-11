"""
Paper-trading simulation for the Coinbase client.

Handles simulated order execution (market + limit), paper accounts,
and mock market data generation for testing without API credentials.
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("core.coinbase.paper")


class CoinbasePaperMixin:
    """Mixin providing paper-trading order execution and mock data.

    Expects the host class to provide:
      - self._paper_balance: dict[str, float]
      - self._paper_balance_lock: threading.Lock
      - self._paper_orders: list[dict]
      - self._paper_fee_pct: float
      - self._paper_slippage_pct: float
      - self._max_paper_orders: int
      - self._last_prices: dict[str, float]
      - self.get_current_price(pair) -> float
    """

    # =========================================================================
    # Paper Account Helpers
    # =========================================================================

    def _get_paper_accounts(self) -> list[dict]:
        """Get paper trading accounts."""
        accounts = []
        for currency, amount in self._paper_balance.items():
            accounts.append({
                "uuid": f"paper-{currency.lower()}",
                "name": f"Paper {currency}",
                "currency": currency,
                "available_balance": {
                    "value": str(amount),
                    "currency": currency,
                },
            })
        return accounts

    # =========================================================================
    # Paper Market Orders
    # =========================================================================

    def _paper_market_buy(
        self,
        product_id: str,
        quote_size: Optional[str] = None,
        base_size: Optional[str] = None,
    ) -> dict:
        """Execute a paper trading market buy."""
        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        if not quote_size and not base_size:
            return {"success": False, "error": "Must specify quote_size or base_size"}

        # Apply slippage: buys fill slightly above mid-price
        fill_price = price * (1.0 + self._paper_slippage_pct)
        if quote_size:
            quantity = float(quote_size) / fill_price
            quote_amount = float(quote_size)
        elif base_size:
            quantity = float(base_size)
            quote_amount = quantity * fill_price

        fee = round(quote_amount * self._paper_fee_pct, 8)
        total_cost = round(quote_amount + fee, 8)

        with self._paper_balance_lock:
            quote_bal = self._paper_balance.get(quote_currency, 0)
            if quote_bal < total_cost:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient balance. "
                        f"Have: {quote_bal:,.2f} {quote_currency}, "
                        f"Need: {total_cost:,.2f} {quote_currency} (incl. fee)"
                    ),
                }

            self._paper_balance[quote_currency] = round(quote_bal - total_cost, 8)
            self._paper_balance[base_currency] = round(
                self._paper_balance.get(base_currency, 0) + quantity, 8
            )

            order_id = str(uuid.uuid4())
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "side": "BUY",
                "type": "MARKET",
                "status": "FILLED",
                "filled_size": str(quantity),
                "filled_value": str(quote_amount),
                "average_filled_price": str(fill_price),
                "fee": str(fee),
                "created_time": datetime.now(timezone.utc).isoformat(),
            }
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper BUY: {quantity:.6f} {base_currency} @ {fill_price:,.2f} "
            f"{quote_currency} (mid={price:,.2f}, slippage={self._paper_slippage_pct:.2%}, "
            f"{quote_amount:,.2f} + {fee:.2f} fee {quote_currency})"
        )
        return {"success": True, "order": order}

    def _paper_market_sell(self, product_id: str, base_size: str) -> dict:
        """Execute a paper trading market sell."""
        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"
        quantity = float(base_size)

        # Apply slippage: sells fill slightly below mid-price
        fill_price = price * (1.0 - self._paper_slippage_pct)
        quote_amount = quantity * fill_price
        fee = round(quote_amount * self._paper_fee_pct, 8)

        with self._paper_balance_lock:
            base_bal = self._paper_balance.get(base_currency, 0)
            if base_bal < quantity:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient {base_currency} balance. "
                        f"Have: {base_bal:.6f}, Need: {quantity:.6f}"
                    ),
                }

            self._paper_balance[base_currency] = round(base_bal - quantity, 8)
            self._paper_balance[quote_currency] = round(
                self._paper_balance.get(quote_currency, 0) + quote_amount, 8
            )
            self._paper_balance[quote_currency] = round(
                self._paper_balance[quote_currency] - fee, 8
            )

            order_id = str(uuid.uuid4())
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "side": "SELL",
                "type": "MARKET",
                "status": "FILLED",
                "filled_size": str(quantity),
                "filled_value": str(quote_amount),
                "average_filled_price": str(fill_price),
                "fee": str(fee),
                "created_time": datetime.now(timezone.utc).isoformat(),
            }
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper SELL: {quantity:.6f} {base_currency} @ {fill_price:,.2f} "
            f"{quote_currency} (mid={price:,.2f}, slippage={self._paper_slippage_pct:.2%}, "
            f"{quote_amount:,.2f} - {fee:.2f} fee {quote_currency})"
        )
        return {"success": True, "order": order}

    # =========================================================================
    # Paper Limit Orders
    # =========================================================================

    def _paper_limit_buy(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
    ) -> dict:
        """Simulate a paper limit buy (fills immediately at limit or better)."""
        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        lim_price = float(limit_price)
        quantity = float(base_size)

        with self._paper_balance_lock:
            # Limit buy only fills if market price <= limit price
            if price > lim_price:
                order_id = str(uuid.uuid4())
                order = {
                    "order_id": order_id,
                    "product_id": product_id,
                    "side": "BUY",
                    "type": "LIMIT",
                    "status": "OPEN",
                    "limit_price": limit_price,
                    "base_size": base_size,
                    "created_time": datetime.now(timezone.utc).isoformat(),
                }
                self._paper_orders.append(order)
                logger.info(
                    f"📝 Paper Limit BUY resting: {quantity:.6f} {base_currency} "
                    f"@ {lim_price:,.2f} {quote_currency} (market={price:,.2f})"
                )
                return {"success": True, "order": order}

            # Fills at limit price (or market if better)
            fill_price = min(price, lim_price)
            quote_amount = quantity * fill_price

            # Use lower maker fee for limit orders
            maker_fee_pct = self._paper_fee_pct * 0.5
            fee = round(quote_amount * maker_fee_pct, 8)

            quote_bal = self._paper_balance.get(quote_currency, 0)
            if quote_bal < quote_amount + fee:
                return {
                    "success": False,
                    "error": f"Insufficient {quote_currency} balance for limit buy",
                }

            self._paper_balance[quote_currency] = round(
                quote_bal - quote_amount - fee, 8
            )
            self._paper_balance[base_currency] = round(
                self._paper_balance.get(base_currency, 0) + quantity, 8
            )

            order_id = str(uuid.uuid4())
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "side": "BUY",
                "type": "LIMIT",
                "status": "FILLED",
                "filled_size": str(quantity),
                "filled_value": str(quote_amount),
                "average_filled_price": str(fill_price),
                "fee": str(fee),
                "created_time": datetime.now(timezone.utc).isoformat(),
            }
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper Limit BUY filled: {quantity:.6f} {base_currency} "
            f"@ {fill_price:,.2f} {quote_currency} (maker fee={fee:.2f})"
        )
        return {"success": True, "order": order}

    def _paper_limit_sell(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
    ) -> dict:
        """Simulate a paper limit sell (fills immediately at limit or better)."""
        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        lim_price = float(limit_price)
        quantity = float(base_size)

        with self._paper_balance_lock:
            if self._paper_balance.get(base_currency, 0) < quantity:
                return {
                    "success": False,
                    "error": f"Insufficient {base_currency} balance for limit sell",
                }

            # Limit sell only fills if market price >= limit price
            if price < lim_price:
                order_id = str(uuid.uuid4())
                order = {
                    "order_id": order_id,
                    "product_id": product_id,
                    "side": "SELL",
                    "type": "LIMIT",
                    "status": "OPEN",
                    "limit_price": limit_price,
                    "base_size": base_size,
                    "created_time": datetime.now(timezone.utc).isoformat(),
                }
                self._paper_orders.append(order)
                logger.info(
                    f"📝 Paper Limit SELL resting: {quantity:.6f} {base_currency} "
                    f"@ {lim_price:,.2f} {quote_currency} (market={price:,.2f})"
                )
                return {"success": True, "order": order}

            fill_price = max(price, lim_price)
            quote_amount = quantity * fill_price
            maker_fee_pct = self._paper_fee_pct * 0.5
            fee = round(quote_amount * maker_fee_pct, 8)

            self._paper_balance[base_currency] = round(
                self._paper_balance[base_currency] - quantity, 8
            )
            self._paper_balance[quote_currency] = round(
                self._paper_balance.get(quote_currency, 0) + quote_amount - fee, 8
            )

            order_id = str(uuid.uuid4())
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "side": "SELL",
                "type": "LIMIT",
                "status": "FILLED",
                "filled_size": str(quantity),
                "filled_value": str(quote_amount),
                "average_filled_price": str(fill_price),
                "fee": str(fee),
                "created_time": datetime.now(timezone.utc).isoformat(),
            }
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper Limit SELL filled: {quantity:.6f} {base_currency} "
            f"@ {fill_price:,.2f} {quote_currency} (maker fee={fee:.2f})"
        )
        return {"success": True, "order": order}

    # =========================================================================
    # Mock Data (for paper trading without API keys)
    # =========================================================================

    def _mock_product(self, product_id: str) -> dict:
        """Generate mock product data."""
        mock_prices = {
            "BTC-USD": 97500.0,
            "ETH-USD": 2750.0,
            "SOL-USD": 195.0,
            "DOGE-USD": 0.25,
        }
        base_price = mock_prices.get(product_id, 100.0)
        price = base_price * (1 + random.uniform(-0.005, 0.005))
        self._last_prices[product_id] = price

        return {
            "product_id": product_id,
            "price": str(price),
            "price_percentage_change_24h": str(random.uniform(-5, 5)),
            "volume_24h": str(random.uniform(1000000, 50000000)),
            "volume_percentage_change_24h": str(random.uniform(-20, 20)),
            "base_currency_id": product_id.split("-")[0],
            "quote_currency_id": product_id.split("-")[1],
            "status": "online",
        }

    def _mock_candles(
        self,
        product_id: str,
        count: int = 200,
        seed: int | None = None,
    ) -> list[dict]:
        """Generate mock candle data for testing.

        Args:
            product_id: Trading pair.
            count: Number of candles to generate.
            seed: Optional RNG seed for reproducible test data.
        """
        rng = random.Random(seed)

        mock_prices = {
            "BTC-USD": 97500.0,
            "ETH-USD": 2750.0,
            "SOL-USD": 195.0,
        }
        base_price = mock_prices.get(product_id, 100.0)
        candles = []
        current_price = base_price

        now = int(time.time())

        for i in range(count):
            change = rng.gauss(0, base_price * 0.005)
            current_price += change
            current_price = max(current_price, base_price * 0.5)

            high = current_price * (1 + rng.uniform(0, 0.01))
            low = current_price * (1 - rng.uniform(0, 0.01))
            open_price = current_price + rng.gauss(0, base_price * 0.002)
            close_price = current_price
            volume = rng.uniform(100, 10000)

            candles.append({
                "start": str(now - (count - i) * 3600),
                "low": str(low),
                "high": str(high),
                "open": str(open_price),
                "close": str(close_price),
                "volume": str(volume),
            })

        return candles
