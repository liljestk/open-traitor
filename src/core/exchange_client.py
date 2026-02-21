"""
Abstract Base Class for Exchange Clients.

Provides a unified interface for both Crypto (e.g. Coinbase) and Shares (e.g. Nordnet)
trading clients, allowing the orchestrator and agents to operate agnostically.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any


class ExchangeClient(abc.ABC):
    """
    Abstract interface for an exchange or broker client.
    """

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
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.
        Returns True if successful.
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
