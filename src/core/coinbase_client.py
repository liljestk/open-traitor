"""
Coinbase Advanced Trade API client wrapper.
Handles both REST and WebSocket connections with paper trading support.

Composed from three mixins:
  - CoinbaseCurrencyMixin  (currency conversion + portfolio valuation)
  - CoinbaseDiscoveryMixin (pair discovery + account diagnostics)
  - CoinbasePaperMixin     (simulated orders + mock data)
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from src.core.exchange_client import ExchangeClient
from src.core.coinbase_currency import (
    CoinbaseCurrencyMixin,
    # Re-export constants for backward compatibility (used by holdings_manager etc.)
    _USD_EQUIVALENTS,
    _EUR_EQUIVALENTS,
    _ALL_STABLECOINS,
    _KNOWN_FIAT,
    _KNOWN_QUOTES,
    _get_fiat_rate_usd,
)
from src.core.coinbase_discovery import CoinbaseDiscoveryMixin
from src.core.coinbase_paper import CoinbasePaperMixin
from src.utils.logger import get_logger

logger = get_logger("core.coinbase")


def _extract_cb_error(result: dict) -> str:
    """Extract a human-readable error from a Coinbase CreateOrderResponse dict."""
    err_resp = result.get("error_response") or {}
    return (
        result.get("error")
        or err_resp.get("message")
        or err_resp.get("error")
        or err_resp.get("preview_failure_reason")
        or err_resp.get("new_order_failure_reason")
        or result.get("failure_reason")
        or "Unknown error"
    )


# Make re-exports visible to star-imports and static analysis
__all__ = [
    "CoinbaseClient",
    "_USD_EQUIVALENTS",
    "_EUR_EQUIVALENTS",
    "_ALL_STABLECOINS",
    "_KNOWN_FIAT",
    "_KNOWN_QUOTES",
    "_get_fiat_rate_usd",
]


class CoinbaseClient(
    CoinbaseCurrencyMixin,
    CoinbaseDiscoveryMixin,
    CoinbasePaperMixin,
    ExchangeClient,
):
    """Wrapper around the Coinbase Advanced Trade API with paper trading support."""

    # ── ExchangeClient identity ──────────────────────────────────────────

    @property
    def exchange_id(self) -> str:
        return "coinbase"

    @property
    def asset_class(self) -> str:
        return "crypto"

    # ─────────────────────────────────────────────────────────────────────

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
        self._paper_balance_lock = threading.Lock()
        self._paper_orders: list[dict] = []
        self._paper_fee_pct: float = 0.006  # Match Coinbase taker fee (0.6%)
        self._paper_slippage_pct: float = paper_slippage_pct
        self._max_paper_orders: int = 500
        self._last_prices: dict[str, float] = {}
        self._product_cache: list[dict] = []
        self._product_cache_ts: float = 0.0
        self._product_cache_lock = threading.RLock()
        self._valid_product_ids: set[str] = set()

        # ── Centralised REST API throttle + retry ─────────────────────────
        from src.utils.rate_limiter import get_rate_limiter
        self._rate_limiter = get_rate_limiter()
        self._throttle_lock = threading.Lock()
        self._backoff_until: float = 0.0
        self._consecutive_errors: int = 0
        self._MAX_RETRIES: int = 3
        self._BASE_BACKOFF: float = 2.0
        self._MAX_BACKOFF: float = 120.0

        if not paper_mode:
            self._init_real_client(api_key, api_secret, key_file)
        else:
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
    # Centralised REST throttle + automatic retry
    # =========================================================================

    def _throttled_request(self, method_name: str, *args, **kwargs) -> Any:
        """
        Call a method on ``_rest_client`` with rate-limit throttling and
        automatic retry on 429 / 403-rate-limit responses.
        """
        import time as _time

        cooldown_remaining = self._backoff_until - _time.monotonic()
        if cooldown_remaining > 0:
            logger.info(
                f"⏳ Coinbase cooldown active — sleeping {cooldown_remaining:.1f}s"
            )
            _time.sleep(cooldown_remaining)

        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            self._rate_limiter.wait("coinbase_rest", timeout=60.0)

            try:
                fn = getattr(self._rest_client, method_name)
                result = fn(*args, **kwargs)
                if self._consecutive_errors > 0:
                    self._consecutive_errors = 0
                return result
            except Exception as exc:
                exc_str = str(exc).lower()
                is_rate_limit = (
                    "429" in exc_str
                    or "too many" in exc_str
                    or ("403" in exc_str and "too many" in exc_str)
                    or "rate" in exc_str
                )
                if is_rate_limit and attempt < self._MAX_RETRIES:
                    self._consecutive_errors += 1
                    backoff = min(
                        self._BASE_BACKOFF * (2 ** (attempt - 1)),
                        self._MAX_BACKOFF,
                    )
                    if self._consecutive_errors >= 3:
                        backoff = min(
                            backoff * self._consecutive_errors, self._MAX_BACKOFF
                        )
                    self._backoff_until = _time.monotonic() + backoff
                    logger.warning(
                        f"⚠️ Coinbase rate-limited ({method_name}, attempt {attempt}/"
                        f"{self._MAX_RETRIES}) — backing off {backoff:.1f}s"
                    )
                    _time.sleep(backoff)
                    last_exc = exc
                    continue
                raise

        raise last_exc  # type: ignore[misc]

    # =========================================================================
    # Market Data
    # =========================================================================

    def get_product(self, product_id: str) -> dict[str, Any]:
        """Get product details (e.g., BTC-USD)."""
        if self._rest_client:
            try:
                product = self._throttled_request("get_product", product_id)
                result = (
                    product.to_dict()
                    if hasattr(product, "to_dict")
                    else dict(product)
                )
                self._last_prices[product_id] = float(result.get("price", 0))
                return result
            except Exception as e:
                logger.error(f"Error fetching product {product_id}: {e}")
                return {"product_id": product_id, "price": "0"}

        # Mock data for paper trading without API
        return self._mock_product(product_id)

    def get_current_price(self, pair: str) -> float:
        """Get the **live** price for a trading pair.

        Guards against 404 spam: only calls the individual ``get_product``
        endpoint when the pair is known to exist in the Coinbase product
        catalogue.  Delisted / invalid pairs return 0 instantly.
        """
        if not self._is_known_product(pair):
            return 0.0

        product = self.get_product(pair)
        price = float(product.get("price", 0))
        if price > 0:
            self._last_prices[pair] = price
        return price

    _CANDLE_PAGE_SIZE = 300  # Coinbase limit is 350; use 300 for safety

    def get_candles(
        self,
        product_id: str,
        granularity: str = "ONE_HOUR",
        limit: int = 200,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        """Get historical candles (OHLCV data).

        Automatically paginates when *limit* exceeds the Coinbase per-request
        cap of 350 candles.
        """
        if self._rest_client:
            try:
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
                end = end_time or int(time.time())
                start = start_time or (end - (limit * seconds))

                # If the range fits in one page, fetch directly
                if limit <= self._CANDLE_PAGE_SIZE:
                    return self._fetch_candle_page(product_id, start, end, granularity)

                # Paginate: split into non-overlapping windows from oldest to newest
                all_candles: list[dict] = []
                total_span = end - start
                page_span = self._CANDLE_PAGE_SIZE * seconds
                cursor = start
                while cursor < end:
                    page_end = min(cursor + page_span, end)
                    page = self._fetch_candle_page(product_id, cursor, page_end, granularity)
                    if not page:
                        break
                    all_candles.extend(page)
                    cursor = page_end
                # Return only the most recent *limit* candles
                return all_candles[-limit:] if len(all_candles) > limit else all_candles

            except Exception as e:
                logger.error(f"Error fetching candles for {product_id}: {e}")
                return []

        return self._mock_candles(product_id, limit)

    def _fetch_candle_page(
        self, product_id: str, start: int, end: int, granularity: str
    ) -> list[dict]:
        """Fetch a single page of candles (must be ≤ 350)."""
        candles = self._throttled_request(
            "get_candles",
            product_id=product_id,
            start=str(start),
            end=str(end),
            granularity=granularity,
        )
        result = (
            candles.to_dict()
            if hasattr(candles, "to_dict")
            else dict(candles)
        )
        return result.get("candles", [])

    def get_market_trades(self, product_id: str, limit: int = 50) -> list[dict]:
        """Get recent market trades."""
        if self._rest_client:
            try:
                trades = self._throttled_request(
                    "get_market_trades",
                    product_id=product_id,
                    limit=limit,
                )
                result = (
                    trades.to_dict()
                    if hasattr(trades, "to_dict")
                    else dict(trades)
                )
                return result.get("trades", [])
            except Exception as e:
                logger.error(f"Error fetching trades for {product_id}: {e}")

        return []

    def get_product_book(self, product_id: str, limit: int = 10) -> dict:
        """Get order book for a product."""
        if self._rest_client:
            try:
                book = self._throttled_request(
                    "get_product_book",
                    product_id=product_id,
                    limit=limit,
                )
                return (
                    book.to_dict() if hasattr(book, "to_dict") else dict(book)
                )
            except Exception as e:
                logger.error(f"Error fetching order book for {product_id}: {e}")

        return {"bids": [], "asks": []}

    # =========================================================================
    # Account & Portfolio
    # =========================================================================

    _ACCOUNTS_PAGE_SIZE: int = 250  # API max per page

    def get_accounts(self) -> list[dict]:
        """Get **all** accounts, paginating automatically.

        The Coinbase Advanced Trade API defaults to 49 accounts per page.
        We request 250 (the API maximum) and follow the cursor until no
        more pages remain — otherwise accounts beyond page-1 are invisible.
        """
        if self.paper_mode:
            return self._get_paper_accounts()

        if self._rest_client:
            try:
                all_accounts: list[dict] = []
                cursor: str | None = None
                page = 0

                while True:
                    page += 1
                    kwargs: dict[str, Any] = {"limit": self._ACCOUNTS_PAGE_SIZE}
                    if cursor:
                        kwargs["cursor"] = cursor

                    accounts = self._throttled_request("get_accounts", **kwargs)
                    result = (
                        accounts.to_dict()
                        if hasattr(accounts, "to_dict")
                        else dict(accounts)
                    )
                    batch = result.get("accounts", [])
                    all_accounts.extend(batch)

                    # Follow pagination cursor
                    cursor = result.get("cursor") or None
                    has_next = result.get("has_next", False)
                    if not cursor or not has_next or not batch:
                        break
                    if page >= 20:  # safety valve
                        logger.warning(
                            "⚠️ get_accounts: hit 20-page safety limit "
                            f"({len(all_accounts)} accounts so far)"
                        )
                        break

                if not all_accounts:
                    logger.warning(
                        "⚠️ get_accounts: Coinbase returned empty account list "
                        "(check API key permissions)"
                    )
                if page > 1:
                    logger.info(
                        f"📋 get_accounts: fetched {len(all_accounts)} accounts "
                        f"across {page} pages"
                    )
                return all_accounts
            except Exception as e:
                logger.error(f"Error fetching accounts: {e}")
                raise
        else:
            logger.warning("⚠️ get_accounts: No Coinbase REST client available")

        return []

    # =========================================================================
    # ExchangeClient abstract implementations
    # =========================================================================

    def _format_base_size(self, pair: str, amount: float) -> str:
        """Round *amount* down to the product's base_increment precision.

        Uses the already-cached product catalogue — no extra API call.
        Falls back to 8 decimal places if the product or increment is unknown.
        """
        import math

        increment_str = "0.00000001"  # safe default (8 dp)
        for prod in self._product_cache:
            if prod.get("product_id") == pair:
                increment_str = prod.get("base_increment", increment_str)
                break

        try:
            # Determine decimal places from the increment string (e.g. "0.001" → 3)
            if "." in increment_str:
                decimals = len(increment_str.rstrip("0").split(".")[1])
            else:
                decimals = 0
            factor = 10 ** decimals
            rounded = math.floor(amount * factor) / factor
            return f"{rounded:.{decimals}f}"
        except Exception:
            return f"{amount:.8f}"

    def place_market_order(
        self,
        pair: str,
        side: str,
        amount: float,
        amount_is_base: bool = False,
        client_oid: str = "",
    ) -> dict:
        """Place a market order (ExchangeClient abstract method implementation)."""
        if side.upper() == "BUY":
            if amount_is_base:
                return self.market_order_buy(pair, base_size=self._format_base_size(pair, amount))
            else:
                return self.market_order_buy(pair, quote_size=str(amount))
        elif side.upper() == "SELL":
            return self.market_order_sell(pair, base_size=self._format_base_size(pair, amount))
        return {"success": False, "error": f"Invalid side: {side}"}

    def place_limit_order(
        self,
        pair: str,
        side: str,
        price: float,
        size: float,
        client_oid: str = "",
    ) -> dict:
        """Place a limit order (ExchangeClient abstract method implementation)."""
        if side.upper() == "BUY":
            return self.limit_order_buy(
                pair, base_size=self._format_base_size(pair, size), limit_price=str(price)
            )
        elif side.upper() == "SELL":
            return self.limit_order_sell(
                pair, base_size=self._format_base_size(pair, size), limit_price=str(price)
            )
        return {"success": False, "error": f"Invalid side: {side}"}

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
                order = self._throttled_request(
                    "market_order_buy",
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    quote_size=quote_size,
                    base_size=base_size,
                )
                result = (
                    order.to_dict()
                    if hasattr(order, "to_dict")
                    else dict(order)
                )
                if result.get("success", True):
                    logger.info(
                        f"✅ Market BUY order placed: {product_id} | "
                        f"order_id={result.get('order_id', '?')}"
                    )
                else:
                    _err = _extract_cb_error(result)
                    logger.error(
                        f"❌ Market BUY rejected by Coinbase: {product_id} | {_err}"
                    )
                    result.setdefault("error", _err)
                logger.debug(f"BUY order detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place buy order: {e}")
                return {
                    "success": False,
                    "error": "Order failed — check logs for details",
                }

        return {"success": False, "error": "No client available"}

    def market_order_sell(self, product_id: str, base_size: str) -> dict:
        """Place a market sell order."""
        # Normalise precision to avoid "too many decimals" rejection from Coinbase
        base_size = self._format_base_size(product_id, float(base_size))

        if self.paper_mode:
            return self._paper_market_sell(product_id, base_size)

        if self._rest_client:
            try:
                order = self._throttled_request(
                    "market_order_sell",
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    base_size=base_size,
                )
                result = (
                    order.to_dict()
                    if hasattr(order, "to_dict")
                    else dict(order)
                )
                if result.get("success", True):
                    logger.info(
                        f"✅ Market SELL order placed: {product_id} | "
                        f"order_id={result.get('order_id', '?')}"
                    )
                else:
                    _err = _extract_cb_error(result)
                    logger.error(
                        f"❌ Market SELL rejected by Coinbase: {product_id} | {_err}"
                    )
                    result.setdefault("error", _err)
                logger.debug(f"SELL order detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place sell order: {e}")
                return {
                    "success": False,
                    "error": "Order failed — check logs for details",
                }

        return {"success": False, "error": "No client available"}

    def limit_order_buy(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
        post_only: bool = True,
    ) -> dict:
        """Place a limit buy order (maker order for lower fees)."""
        if self.paper_mode:
            return self._paper_limit_buy(product_id, base_size, limit_price)

        if self._rest_client:
            try:
                order = self._throttled_request(
                    "limit_order_gtc_buy",
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    base_size=base_size,
                    limit_price=limit_price,
                    post_only=post_only,
                )
                result = (
                    order.to_dict()
                    if hasattr(order, "to_dict")
                    else dict(order)
                )
                logger.info(
                    f"✅ Limit BUY order placed: {product_id} @ {limit_price} | "
                    f"order_id={result.get('order_id', '?')}"
                )
                logger.debug(f"Limit BUY detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place limit buy order: {e}")
                return {
                    "success": False,
                    "error": "Order failed — check logs for details",
                }

        return {"success": False, "error": "No client available"}

    def limit_order_sell(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
        post_only: bool = True,
    ) -> dict:
        """Place a limit sell order (maker order for lower fees)."""
        if self.paper_mode:
            return self._paper_limit_sell(product_id, base_size, limit_price)

        if self._rest_client:
            try:
                order = self._throttled_request(
                    "limit_order_gtc_sell",
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    base_size=base_size,
                    limit_price=limit_price,
                    post_only=post_only,
                )
                result = (
                    order.to_dict()
                    if hasattr(order, "to_dict")
                    else dict(order)
                )
                logger.info(
                    f"✅ Limit SELL order placed: {product_id} @ {limit_price} | "
                    f"order_id={result.get('order_id', '?')}"
                )
                logger.debug(f"Limit SELL detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place limit sell order: {e}")
                return {
                    "success": False,
                    "error": "Order failed — check logs for details",
                }

        return {"success": False, "error": "No client available"}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        if self.paper_mode:
            for order in self._paper_orders:
                if (
                    order.get("order_id") == order_id
                    and order.get("status") == "OPEN"
                ):
                    order["status"] = "CANCELLED"
                    logger.info(f"📝 Paper order cancelled: {order_id}")
                    return {"success": True, "order_id": order_id}
            return {"success": False, "error": "Order not found or not open"}

        if self._rest_client:
            try:
                result = self._throttled_request("cancel_orders", [order_id])
                res = (
                    result.to_dict()
                    if hasattr(result, "to_dict")
                    else dict(result)
                )
                logger.info(f"✅ Order cancelled: {order_id}")
                return {"success": True, "result": res}
            except Exception as e:
                logger.error(f"❌ Failed to cancel order {order_id}: {e}")
                return {
                    "success": False,
                    "error": "Order failed — check logs for details",
                }

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
                order = self._throttled_request("get_order", order_id)
                return (
                    order.to_dict()
                    if hasattr(order, "to_dict")
                    else dict(order)
                )
            except Exception as e:
                logger.error(f"Error fetching order {order_id}: {e}")

        return {}

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        if self.paper_mode:
            return [
                o for o in self._paper_orders if o.get("status") == "OPEN"
            ]

        if self._rest_client:
            try:
                orders = self._throttled_request(
                    "list_orders", order_status=["OPEN"]
                )
                result = (
                    orders.to_dict()
                    if hasattr(orders, "to_dict")
                    else dict(orders)
                )
                return result.get("orders", [])
            except Exception as e:
                logger.error(f"Error fetching open orders: {e}")

        return []

    # =========================================================================
    # Balance & Reconciliation
    # =========================================================================

    @property
    def balance(self) -> dict[str, float]:
        """Get current balance (paper or real).

        Returns available + held amounts so the balance reflects the
        full account value, not just what's immediately tradable.
        """
        if self.paper_mode:
            return self._paper_balance.copy()
        accounts = self.get_accounts()
        balances: dict[str, float] = {}
        for account in accounts:
            currency = account.get("available_balance", {}).get(
                "currency", account.get("currency", "")
            )
            try:
                avail = float(account.get("available_balance", {}).get("value", 0))
            except (ValueError, TypeError):
                avail = 0.0
            try:
                held = float(account.get("hold", {}).get("value", 0))
            except (ValueError, TypeError):
                held = 0.0
            total = avail + held
            if total > 0 and currency:
                balances[currency] = balances.get(currency, 0.0) + total
        return balances

    def reconcile_positions(self, expected: dict[str, float]) -> dict:
        """
        Reconcile expected positions against actual Coinbase balances.
        Returns discrepancies for logging and correction.
        """
        actual = self.balance
        discrepancies = []

        all_currencies = set(list(expected.keys()) + list(actual.keys()))
        fiat_currencies = {
            "USD", "EUR", "GBP", "CHF", "SEK", "NOK", "DKK",
            "CAD", "AUD", "JPY", "USDC", "USDT",
        }
        for currency in all_currencies:
            if currency in fiat_currencies:
                continue
            exp = expected.get(currency, 0.0)
            act = actual.get(currency, 0.0)
            if abs(exp - act) > max(1e-8, abs(exp) * 0.01):
                discrepancies.append({
                    "currency": currency,
                    "expected": exp,
                    "actual": act,
                    "diff": act - exp,
                    "diff_pct": (
                        ((act - exp) / exp * 100) if exp > 0 else float("inf")
                    ),
                })

        if discrepancies:
            logger.warning(
                f"⚠️ Position reconciliation found {len(discrepancies)} "
                f"discrepancies: "
                + ", ".join(
                    f"{d['currency']}: exp={d['expected']:.6f} "
                    f"act={d['actual']:.6f}"
                    for d in discrepancies
                )
            )

        return {
            "matched": len(discrepancies) == 0,
            "discrepancies": discrepancies,
            "actual": actual,
        }
