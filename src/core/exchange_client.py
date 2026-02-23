"""
Abstract Base Class for Exchange Clients.

Provides a unified interface for both Crypto (e.g. Coinbase) and Shares (e.g. Nordnet)
trading clients, allowing the orchestrator and agents to operate agnostically.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Optional


class ExchangeClient(abc.ABC):
    """
    Abstract interface for an exchange or broker client.
    """

    # ── Identity properties ──────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def exchange_id(self) -> str:
        """
        Unique short identifier for this exchange (e.g. ``"coinbase"``, ``"nordnet"``).
        Used as a key in per-exchange state, stats, config, and logging.
        """

    @property
    @abc.abstractmethod
    def asset_class(self) -> str:
        """
        The asset class traded on this exchange.
        Returns ``"crypto"`` or ``"equity"``.
        Allows generic branching (fee models, routing, pair parsing) without
        exchange-specific ``if`` checks.
        """

    # ── Connection & account methods ─────────────────────────────────────

    @abc.abstractmethod
    def check_connection(self) -> dict[str, Any]:
        """
        Verify the API connection and key permissions.

        Returns a dict with keys:
          ok          – bool, True if the API is reachable
          mode        – 'live' | 'paper'
          message     – human-readable status line
          error       – error string (on failure)
          (plus exchange-specific metadata)
        """
        pass

    @abc.abstractmethod
    def get_accounts(self) -> list[dict[str, Any]]:
        """
        Get all accounts (wallets/portfolios) for the user.
        
        Returns a list of dicts, each containing:
          currency    - string identifier (e.g., 'BTC', 'USD', 'NOK')
          balance     - float (total amount)
          hold        - float (amount locked in orders)
          available   - float (amount tradeable)
          account_id  - string (exchange-specific ID)
        """
        pass

    @abc.abstractmethod
    def get_current_price(self, pair: str) -> float:
        """
        Get the current price for a trading pair (e.g., 'BTC-USD' or 'NOKIA').
        Returns 0.0 on failure.
        """
        pass

    @abc.abstractmethod
    def get_candles(self, product_id: str, granularity: str = "ONE_HOUR", limit: int = 200) -> list[dict]:
        """
        Get historical candles (OHLCV data).
        
        Args:
            product_id: The trading pair or symbol.
            granularity: Usually 'ONE_MINUTE', 'ONE_HOUR', 'ONE_DAY' etc.
            limit: Maximum number of candles to return.
            
        Returns a list of dicts: [ {start: int timestamp, open: float, high: float, low: float, close: float, volume: float}, ... ]
        """
        pass

    @abc.abstractmethod
    def get_market_trades(self, product_id: str, limit: int = 50) -> list[dict]:
        """
        Get recent market trades for a product.
        
        Returns a list of dicts: [ {time: str ISO8601, trade_id: str, price: str, size: str, side: 'BUY'|'SELL'}, ... ]
        """
        pass

    @abc.abstractmethod
    def get_product_book(self, product_id: str, limit: int = 10) -> dict:
        """
        Get the current order book.
        
        Returns: { 'bids': [[price: str, size: str], ...], 'asks': [[price, size], ...] }
        """
        pass

    @abc.abstractmethod
    def place_market_order(self, pair: str, side: str, amount: float, amount_is_base: bool = False, client_oid: str = "") -> dict:
        """
        Place a market order.
        
        Args:
            pair: The trading pair (e.g., 'BTC-USD' or 'NOKIA')
            side: 'BUY' or 'SELL'
            amount: The quantity to trade
            amount_is_base: If True, `amount` is in the base asset (e.g., amount of BTC).
                            If False, `amount` is in the quote asset (e.g., amount of USD).
            client_oid: Optional idempotency key.
            
        Returns a dict containing 'success' (bool), 'order_id', 'status', 'filled_size', 'filled_value', 'fee', 'error' (if failed).
        """
        pass

    @abc.abstractmethod
    def place_limit_order(self, pair: str, side: str, price: float, size: float, client_oid: str = "") -> dict:
        """
        Place a limit order.
        
        Args:
            pair: The trading pair
            side: 'BUY' or 'SELL'
            price: The limit price
            size: The exact amount of the base asset to trade
            client_oid: Optional idempotency key.
            
        Returns a result dict similar to place_market_order.
        """
        pass

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel a pending order.
        Returns a dict with 'success' (bool) and optionally 'error'.
        """
        pass

    @abc.abstractmethod
    def get_order(self, order_id: str) -> Optional[dict]:
        """
        Get the status of an order.
        
        Returns a dict with 'status' (e.g., 'FILLED', 'OPEN', 'CANCELLED'), 'filled_size', 'filled_value', 'fee'.
        """
        pass

    @abc.abstractmethod
    def get_product(self, product_id: str) -> Optional[dict]:
        """
        Get product details (e.g., min valid sizes).
        
        Returns a dict with 'base_currency_id', 'quote_currency_id', 'base_min_size', 'base_max_size', 'base_increment', 'quote_increment'
        """
        pass

    # =========================================================================
    # Methods with default implementations (override in subclasses as needed)
    # =========================================================================

    @property
    def balance(self) -> dict[str, float]:
        """
        Get aggregated account balances as {currency: available_amount}.

        Default implementation calls get_accounts() and aggregates.
        Subclasses may override for cached/optimized access.
        """
        accounts = self.get_accounts()
        result: dict[str, float] = {}
        for acc in accounts:
            currency = acc.get("currency", "")
            available = float(acc.get("available", 0))
            if currency and available > 0:
                result[currency] = result.get(currency, 0) + available
        return result

    def detect_native_currency(self) -> str:
        """
        Detect the native/home currency of the account (e.g. 'EUR', 'USD', 'SEK').

        Default: returns 'USD'. Subclasses should override based on account data.
        """
        return "USD"

    def adapt_pairs_to_account(self, pairs: list[str], native_currency: str) -> list[str]:
        """
        Adapt trading pairs to the account's native currency.
        
        E.g. if native is EUR and pairs contain BTC-USD, convert to BTC-EUR if available.
        Default: returns pairs unchanged.
        """
        return pairs

    def discover_all_pairs(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: list[str] | None = None,
        only_trade: list[str] | None = None,
    ) -> list[str]:
        """
        Discover all available trading pairs, filtered by quote currency and exclusions.

        Returns a list of pair strings (e.g. ['BTC-EUR', 'ETH-EUR']).
        Default: returns empty list.
        """
        return []

    def discover_all_pairs_detailed(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: list[str] | None = None,
        only_trade: list[str] | None = None,
        include_crypto_quotes: bool = False,
    ) -> list[dict]:
        """
        Discover all available trading pairs with detailed metadata.

        Returns a list of dicts with keys: product_id, base_currency_id,
        quote_currency_id, base_min_size, quote_min_size, volume_24h,
        price_percentage_change_24h.
        Default: returns empty list.
        """
        return []

    def get_portfolio_value(self) -> float:
        """
        Calculate total portfolio value in the native currency.

        Default: sums all account balances at face value (no conversion).
        Subclasses should override with proper currency conversion.
        """
        accounts = self.get_accounts()
        total = 0.0
        for acc in accounts:
            total += float(acc.get("balance", 0))
        return total

    def reconcile_positions(self, expected: dict[str, float]) -> dict:
        """
        Compare expected positions with actual exchange positions.

        Args:
            expected: Dict of {pair: expected_quantity}

        Returns a dict with 'mismatches' (list), 'matched' (int), 'total' (int).
        Default: returns empty reconciliation.
        """
        return {"mismatches": [], "matched": 0, "total": 0}

    def get_open_orders(self, pair: str | None = None) -> list[dict]:
        """
        Get all open/pending orders, optionally filtered by pair.

        Returns a list of order dicts.
        Default: returns empty list.
        """
        return []

    def get_news(self, pair: str, limit: int = 5) -> list[dict]:
        """
        Fetch news articles for a specific pair from the exchange's news feed.

        Args:
            pair: The trading pair (e.g. 'AAPL-EUR', 'BTC-USD').
            limit: Maximum number of articles to return.

        Returns a list of dicts with keys: time, headline, provider, article_id.
        Default: returns empty list.  Exchanges with built-in news (e.g. IBKR)
        should override this.
        """
        return []

    def get_news_providers(self) -> list[str]:
        """
        Return a list of available news provider codes on this exchange.

        Default: returns empty list.  IBKR overrides with reqNewsProviders().
        """
        return []
