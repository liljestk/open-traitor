"""
Coinbase Advanced Trade API client wrapper.
Handles both REST and WebSocket connections with paper trading support.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("core.coinbase")

# Currencies that are pegged ~1:1 to USD and should be counted at face value
_USD_EQUIVALENTS = {"USD", "USDC", "USDT", "FDUSD", "PYUSD", "DAI", "EURC", "USDS"}

# Live fiat-to-USD rate cache {currency: (rate, fetched_at_epoch)}
_FIAT_RATE_CACHE: dict[str, tuple[float, float]] = {}
_FIAT_RATE_TTL = 6 * 3600  # 6 hours — fiat rates are stable intraday
_FIAT_RATE_URL = "https://api.frankfurter.app/latest?from=USD"  # ECB rates, no API key


def _get_fiat_rate_usd(currency: str) -> float:
    """
    Return the number of USD per 1 unit of *currency* (e.g. EUR → ~1.05).
    Fetches a single bulk request for all major fiats and caches for 6 hours.
    Returns 0 if the currency is unknown or the request fails.
    """
    now = time.time()
    cached = _FIAT_RATE_CACHE.get(currency)
    if cached and (now - cached[1]) < _FIAT_RATE_TTL:
        return cached[0]

    try:
        resp = requests.get(_FIAT_RATE_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        # Response: {"base": "USD", "rates": {"EUR": 0.952, "GBP": 0.789, ...}}
        # rates[X] = how many X per 1 USD  →  USD per X = 1 / rates[X]
        for code, per_usd in data.get("rates", {}).items():
            if per_usd and per_usd > 0:
                _FIAT_RATE_CACHE[code] = (1.0 / per_usd, now)
        logger.debug(f"Fiat exchange rates refreshed ({len(data.get('rates', {}))} currencies)")
    except Exception as e:
        logger.warning(f"⚠️ Fiat rate fetch failed: {e} — using cached/zero rate for {currency}")

    result = _FIAT_RATE_CACHE.get(currency)
    return result[0] if result else 0.0


class CoinbaseClient:
    """Wrapper around the Coinbase Advanced Trade API with paper trading support."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        key_file: Optional[str] = None,
        paper_mode: bool = True,
        paper_slippage_pct: float = 0.0005,
    ):
        self.paper_mode = paper_mode
        self._rest_client = None
        self._ws_client = None
        self._ws_callbacks: dict[str, list] = {}
        self._paper_balance: dict[str, float] = {
            "USD": 10000.0,  # Start with $10,000 in paper mode
        }
        self._paper_orders: list[dict] = []
        self._paper_fee_pct: float = 0.006  # Match Coinbase taker fee (0.6%)
        self._paper_slippage_pct: float = paper_slippage_pct
        self._max_paper_orders: int = 500
        self._last_prices: dict[str, float] = {}

        if not paper_mode:
            self._init_real_client(api_key, api_secret, key_file)
        else:
            # Still initialize for market data even in paper mode
            self._try_init_client(api_key, api_secret, key_file)

        logger.info(
            f"CoinbaseClient initialized in {'📝 PAPER' if paper_mode else '💰 LIVE'} mode"
        )

    def _init_real_client(
        self,
        api_key: Optional[str],
        api_secret: Optional[str],
        key_file: Optional[str],
    ) -> None:
        """Initialize the real Coinbase REST client."""
        try:
            from coinbase.rest import RESTClient

            if key_file:
                self._rest_client = RESTClient(key_file=key_file)
            elif api_key and api_secret:
                self._rest_client = RESTClient(
                    api_key=api_key, api_secret=api_secret
                )
            else:
                # Try environment variables
                self._rest_client = RESTClient()
            logger.info("✅ Coinbase REST client connected")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Coinbase client: {e}")
            raise

    def _try_init_client(
        self,
        api_key: Optional[str],
        api_secret: Optional[str],
        key_file: Optional[str],
    ) -> None:
        """Try to initialize the client for market data, but don't fail in paper mode."""
        try:
            self._init_real_client(api_key, api_secret, key_file)
        except Exception as e:
            logger.warning(
                f"⚠️ Coinbase client not initialized (paper mode will use mock data): {e}"
            )

    # =========================================================================
    # Market Data
    # =========================================================================

    def get_product(self, product_id: str) -> dict[str, Any]:
        """Get product details (e.g., BTC-USD)."""
        if self._rest_client:
            try:
                product = self._rest_client.get_product(product_id)
                result = product.to_dict() if hasattr(product, "to_dict") else dict(product)
                self._last_prices[product_id] = float(result.get("price", 0))
                return result
            except Exception as e:
                logger.error(f"Error fetching product {product_id}: {e}")

        # Mock data for paper trading without API
        return self._mock_product(product_id)

    def get_current_price(self, pair: str) -> float:
        """Get the current price for a trading pair."""
        product = self.get_product(pair)
        price = float(product.get("price", 0))
        self._last_prices[pair] = price
        return price

    def get_candles(
        self,
        product_id: str,
        granularity: str = "ONE_HOUR",
        limit: int = 200,
    ) -> list[dict]:
        """Get historical candles (OHLCV data)."""
        if self._rest_client:
            try:
                end = int(time.time())
                # Map granularity to seconds
                granularity_seconds = {
                    "ONE_MINUTE": 60,
                    "FIVE_MINUTE": 300,
                    "FIFTEEN_MINUTE": 900,
                    "THIRTY_MINUTE": 1800,
                    "ONE_HOUR": 3600,
                    "TWO_HOUR": 7200,
                    "SIX_HOUR": 21600,
                    "ONE_DAY": 86400,
                }
                seconds = granularity_seconds.get(granularity, 3600)
                start = end - (limit * seconds)

                candles = self._rest_client.get_candles(
                    product_id=product_id,
                    start=str(start),
                    end=str(end),
                    granularity=granularity,
                )
                result = candles.to_dict() if hasattr(candles, "to_dict") else dict(candles)
                candle_list = result.get("candles", [])
                return candle_list
            except Exception as e:
                logger.error(f"Error fetching candles for {product_id}: {e}")

        return self._mock_candles(product_id, limit)

    def get_market_trades(self, product_id: str, limit: int = 50) -> list[dict]:
        """Get recent market trades."""
        if self._rest_client:
            try:
                trades = self._rest_client.get_market_trades(
                    product_id=product_id, limit=limit
                )
                result = trades.to_dict() if hasattr(trades, "to_dict") else dict(trades)
                return result.get("trades", [])
            except Exception as e:
                logger.error(f"Error fetching trades for {product_id}: {e}")

        return []

    def get_product_book(self, product_id: str, limit: int = 10) -> dict:
        """Get order book for a product."""
        if self._rest_client:
            try:
                book = self._rest_client.get_product_book(
                    product_id=product_id, limit=limit
                )
                return book.to_dict() if hasattr(book, "to_dict") else dict(book)
            except Exception as e:
                logger.error(f"Error fetching order book for {product_id}: {e}")

        return {"bids": [], "asks": []}

    # =========================================================================
    # Account & Portfolio
    # =========================================================================

    def get_accounts(self) -> list[dict]:
        """Get all accounts."""
        if self.paper_mode:
            return self._get_paper_accounts()

        if self._rest_client:
            try:
                accounts = self._rest_client.get_accounts()
                result = accounts.to_dict() if hasattr(accounts, "to_dict") else dict(accounts)
                account_list = result.get("accounts", [])
                if not account_list:
                    logger.warning("⚠️ get_accounts: Coinbase returned empty account list (check API key permissions)")
                return account_list
            except Exception as e:
                logger.error(f"Error fetching accounts: {e}")
        else:
            logger.warning("⚠️ get_accounts: No Coinbase REST client available")

        return []

    def _currency_to_usd(self, currency: str, amount: float) -> float:
        """
        Convert a currency amount to its approximate USD value.
        Order of preference:
          1. USD / known stablecoins → 1:1
          2. Cached price for {currency}-USD (Coinbase crypto price)
          3. Live fetch of {currency}-USD from Coinbase
          4. Live fiat exchange rate from Frankfurter (ECB), cached 6 h
          5. Log a warning and return 0
        """
        if amount <= 0:
            return 0.0
        if currency in _USD_EQUIVALENTS:
            return amount
        pair = f"{currency}-USD"
        price = self._last_prices.get(pair, 0)
        if price == 0:
            price = self.get_current_price(pair)
        if price > 0:
            return amount * price
        # Fiat fallback — live ECB rate via Frankfurter (EUR, GBP, CHF, etc.)
        fiat_rate = _get_fiat_rate_usd(currency)
        if fiat_rate > 0:
            logger.debug(f"Using live fiat rate for {currency}: {fiat_rate:.4f} USD")
            return amount * fiat_rate
        logger.warning(f"⚠️ No USD price available for {currency} — excluding {amount:.6f} from portfolio value")
        return 0.0

    def get_portfolio_value(self) -> float:
        """Get total portfolio value in USD."""
        if self.paper_mode:
            total = 0.0
            for currency, amount in self._paper_balance.items():
                total += self._currency_to_usd(currency, amount)
            return total

        accounts = self.get_accounts()
        total = 0.0
        for account in accounts:
            balance = account.get("available_balance", {})
            value = float(balance.get("value", 0))
            currency = balance.get("currency", "")
            if not currency or value == 0:
                continue
            total += self._currency_to_usd(currency, value)
        return total

    # =========================================================================
    # Order Execution
    # =========================================================================

    def market_order_buy(
        self,
        product_id: str,
        quote_size: Optional[str] = None,
        base_size: Optional[str] = None,
    ) -> dict:
        """Place a market buy order."""
        if self.paper_mode:
            return self._paper_market_buy(product_id, quote_size, base_size)

        if self._rest_client:
            try:
                import uuid

                order = self._rest_client.market_order_buy(
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    quote_size=quote_size,
                    base_size=base_size,
                )
                result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
                logger.info(f"✅ Market BUY order placed: {product_id} | order_id={result.get('order_id', '?')}")
                logger.debug(f"BUY order detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place buy order: {e}")
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "No client available"}

    def market_order_sell(
        self,
        product_id: str,
        base_size: str,
    ) -> dict:
        """Place a market sell order."""
        if self.paper_mode:
            return self._paper_market_sell(product_id, base_size)

        if self._rest_client:
            try:
                import uuid

                order = self._rest_client.market_order_sell(
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    base_size=base_size,
                )
                result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
                logger.info(f"✅ Market SELL order placed: {product_id} | order_id={result.get('order_id', '?')}")
                logger.debug(f"SELL order detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place sell order: {e}")
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "No client available"}

    def get_order(self, order_id: str) -> dict:
        """Get order details."""
        if self.paper_mode:
            for order in self._paper_orders:
                if order.get("order_id") == order_id:
                    return order
            return {}

        if self._rest_client:
            try:
                order = self._rest_client.get_order(order_id)
                return order.to_dict() if hasattr(order, "to_dict") else dict(order)
            except Exception as e:
                logger.error(f"Error fetching order {order_id}: {e}")

        return {}

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        if self.paper_mode:
            return [o for o in self._paper_orders if o.get("status") == "OPEN"]

        if self._rest_client:
            try:
                orders = self._rest_client.list_orders(order_status=["OPEN"])
                result = orders.to_dict() if hasattr(orders, "to_dict") else dict(orders)
                return result.get("orders", [])
            except Exception as e:
                logger.error(f"Error fetching open orders: {e}")

        return []

    # =========================================================================
    # Paper Trading Internals
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

    def _paper_market_buy(
        self,
        product_id: str,
        quote_size: Optional[str] = None,
        base_size: Optional[str] = None,
    ) -> dict:
        """Execute a paper trading market buy."""
        import uuid

        price = self.get_current_price(product_id)
        base_currency = product_id.split("-")[0]

        if not quote_size and not base_size:
            return {"success": False, "error": "Must specify quote_size or base_size"}

        # Apply slippage: buys fill slightly above mid-price
        fill_price = price * (1.0 + self._paper_slippage_pct)
        if quote_size:
            quantity = float(quote_size) / fill_price
            usd_amount = float(quote_size)
        elif base_size:
            quantity = float(base_size)
            usd_amount = quantity * fill_price

        # Check balance (include estimated fee so post-fee deduction cannot go negative)
        fee_estimate = usd_amount * self._paper_fee_pct
        if self._paper_balance.get("USD", 0) < usd_amount + fee_estimate:
            return {
                "success": False,
                "error": (
                    f"Insufficient balance. "
                    f"Have: ${self._paper_balance.get('USD', 0):.2f}, "
                    f"Need: ${usd_amount + fee_estimate:.2f} (incl. fee)"
                ),
            }

        # Execute paper trade
        self._paper_balance["USD"] -= usd_amount
        self._paper_balance[base_currency] = self._paper_balance.get(base_currency, 0) + quantity

        fee = usd_amount * self._paper_fee_pct
        self._paper_balance["USD"] -= fee

        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id,
            "product_id": product_id,
            "side": "BUY",
            "type": "MARKET",
            "status": "FILLED",
            "filled_size": str(quantity),
            "filled_value": str(usd_amount),
            "average_filled_price": str(fill_price),
            "fee": str(fee),
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        self._paper_orders.append(order)
        if len(self._paper_orders) > self._max_paper_orders:
            self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper BUY: {quantity:.6f} {base_currency} @ ${fill_price:,.2f} "
            f"(mid=${price:,.2f}, slippage={self._paper_slippage_pct:.2%}, "
            f"${usd_amount:,.2f} + ${fee:.2f} fee)"
        )
        return {"success": True, "order": order}

    def _paper_market_sell(self, product_id: str, base_size: str) -> dict:
        """Execute a paper trading market sell."""
        import uuid

        price = self.get_current_price(product_id)
        base_currency = product_id.split("-")[0]
        quantity = float(base_size)

        # Check balance
        if self._paper_balance.get(base_currency, 0) < quantity:
            return {
                "success": False,
                "error": f"Insufficient {base_currency} balance. Have: {self._paper_balance.get(base_currency, 0):.6f}, Need: {quantity:.6f}",
            }

        # Apply slippage: sells fill slightly below mid-price
        fill_price = price * (1.0 - self._paper_slippage_pct)
        usd_amount = quantity * fill_price

        # Execute paper trade
        self._paper_balance[base_currency] -= quantity
        self._paper_balance["USD"] += usd_amount

        fee = usd_amount * self._paper_fee_pct
        self._paper_balance["USD"] -= fee

        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id,
            "product_id": product_id,
            "side": "SELL",
            "type": "MARKET",
            "status": "FILLED",
            "filled_size": str(quantity),
            "filled_value": str(usd_amount),
            "average_filled_price": str(fill_price),
            "fee": str(fee),
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        self._paper_orders.append(order)
        if len(self._paper_orders) > self._max_paper_orders:
            self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper SELL: {quantity:.6f} {base_currency} @ ${fill_price:,.2f} "
            f"(mid=${price:,.2f}, slippage={self._paper_slippage_pct:.2%}, "
            f"${usd_amount:,.2f} - ${fee:.2f} fee)"
        )
        return {"success": True, "order": order}

    # =========================================================================
    # Mock Data (for paper trading without API keys)
    # =========================================================================

    def _mock_product(self, product_id: str) -> dict:
        """Generate mock product data."""
        import random

        mock_prices = {
            "BTC-USD": 97500.0,
            "ETH-USD": 2750.0,
            "SOL-USD": 195.0,
            "DOGE-USD": 0.25,
        }
        base_price = mock_prices.get(product_id, 100.0)
        # Add some randomness
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

    def _mock_candles(self, product_id: str, count: int = 200) -> list[dict]:
        """Generate mock candle data for testing."""
        import random

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
            # Random walk
            change = random.gauss(0, base_price * 0.005)
            current_price += change
            current_price = max(current_price, base_price * 0.5)

            high = current_price * (1 + random.uniform(0, 0.01))
            low = current_price * (1 - random.uniform(0, 0.01))
            open_price = current_price + random.gauss(0, base_price * 0.002)
            close_price = current_price
            volume = random.uniform(100, 10000)

            candles.append({
                "start": str(now - (count - i) * 3600),
                "low": str(low),
                "high": str(high),
                "open": str(open_price),
                "close": str(close_price),
                "volume": str(volume),
            })

        return candles

    @property
    def balance(self) -> dict[str, float]:
        """Get current balance (paper or real)."""
        if self.paper_mode:
            return self._paper_balance.copy()
        # For real mode, fetch from API
        accounts = self.get_accounts()
        balances = {}
        for account in accounts:
            bal = account.get("available_balance", {})
            currency = bal.get("currency", "")
            value = float(bal.get("value", 0))
            if value > 0:
                balances[currency] = value
        return balances

    def reconcile_positions(self, expected: dict[str, float]) -> dict:
        """
        Reconcile expected positions against actual Coinbase balances.
        Returns discrepancies for logging and correction.

        Args:
            expected: dict of currency -> expected quantity from TradingState

        Returns:
            {"matched": bool, "discrepancies": [...], "actual": {...}}
        """
        actual = self.balance
        discrepancies = []

        all_currencies = set(list(expected.keys()) + list(actual.keys()))
        for currency in all_currencies:
            if currency == "USD":
                continue
            exp = expected.get(currency, 0.0)
            act = actual.get(currency, 0.0)
            # Allow small floating-point tolerance
            if abs(exp - act) > max(1e-8, abs(exp) * 0.01):
                discrepancies.append({
                    "currency": currency,
                    "expected": exp,
                    "actual": act,
                    "diff": act - exp,
                    "diff_pct": ((act - exp) / exp * 100) if exp > 0 else float("inf"),
                })

        if discrepancies:
            logger.warning(
                f"⚠️ Position reconciliation found {len(discrepancies)} discrepancies: "
                + ", ".join(f"{d['currency']}: exp={d['expected']:.6f} act={d['actual']:.6f}" for d in discrepancies)
            )

        return {
            "matched": len(discrepancies) == 0,
            "discrepancies": discrepancies,
            "actual": actual,
        }
