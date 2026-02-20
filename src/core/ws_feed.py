"""
Coinbase WebSocket feed for real-time market data.
Low-latency price updates and order book data via WebSocket.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.utils.logger import get_logger
from src.utils.rate_limiter import get_rate_limiter

logger = get_logger("core.ws_feed")


class CoinbaseWebSocketFeed:
    """
    Real-time market data via Coinbase Advanced Trade WebSocket API.

    Channels:
      - ticker: Real-time price updates (low latency)
      - market_trades: Individual trade executions
      - candles: Candlestick updates

    Uses the free market_data WebSocket (no auth required for public data).
    """

    WS_URL = "wss://advanced-trade-ws.coinbase.com"

    def __init__(
        self,
        product_ids: list[str],
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        on_ticker: Optional[Callable[[dict], None]] = None,
        on_trade: Optional[Callable[[dict], None]] = None,
        on_candle: Optional[Callable[[dict], None]] = None,
    ):
        self.product_ids = product_ids
        self.api_key = api_key
        self.api_secret = api_secret
        self.on_ticker = on_ticker
        self.on_trade = on_trade
        self.on_candle = on_candle

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._rate_limiter = get_rate_limiter()

        # Additional ticker callbacks (registered post-construction)
        self._extra_ticker_callbacks: list[Callable[[dict], None]] = []

        # Latest prices (updated in real-time)
        self.prices: dict[str, float] = {}
        self._lock = threading.Lock()

        # Stats
        self.messages_received = 0
        self.last_message_time: Optional[datetime] = None

        logger.info(f"📡 WebSocket feed initialized | Products: {product_ids}")

    def _generate_signature(self, timestamp: str, channel: str) -> Optional[str]:
        """Generate HMAC signature for authenticated channels."""
        if not self.api_key or not self.api_secret:
            return None

        products_str = ",".join(self.product_ids)
        message = f"{timestamp}{channel}{products_str}"

        try:
            signature = hmac.new(
                self.api_secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return signature
        except Exception as e:
            logger.error(f"Signature generation failed: {e}")
            return None

    def _build_subscribe_message(self, channel: str) -> dict:
        """Build a subscription message."""
        timestamp = str(int(time.time()))

        msg = {
            "type": "subscribe",
            "product_ids": self.product_ids,
            "channel": channel,
        }

        # Add auth if available
        if self.api_key:
            signature = self._generate_signature(timestamp, channel)
            if signature:
                msg["api_key"] = self.api_key
                msg["timestamp"] = timestamp
                msg["signature"] = signature

        return msg

    def _on_message(self, ws, message: str) -> None:
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            channel = data.get("channel", "")

            self.messages_received += 1
            self.last_message_time = datetime.now(timezone.utc)

            if channel == "ticker":
                self._handle_ticker(data)
            elif channel == "market_trades":
                self._handle_trade(data)
            elif channel == "candles":
                self._handle_candle(data)
            elif channel == "subscriptions":
                logger.info(f"📡 Subscribed to channels: {data}")

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from WebSocket: {message[:100]}")
        except Exception as e:
            logger.error(f"WebSocket message handling error: {e}")

    def _handle_ticker(self, data: dict) -> None:
        """Handle ticker updates — real-time price."""
        events = data.get("events", [])
        for event in events:
            tickers = event.get("tickers", [])
            for ticker in tickers:
                product_id = ticker.get("product_id", "")
                price = float(ticker.get("price", 0))

                if product_id and price > 0:
                    with self._lock:
                        self.prices[product_id] = price

                    if self.on_ticker:
                        self.on_ticker({
                            "product_id": product_id,
                            "price": price,
                            "volume_24h": float(ticker.get("volume_24_h", 0)),
                            "price_pct_change_24h": float(ticker.get("price_percentage_change_24h", 0)),
                            "best_bid": float(ticker.get("best_bid", 0)),
                            "best_ask": float(ticker.get("best_ask", 0)),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                    for cb in self._extra_ticker_callbacks:
                        try:
                            cb({
                                "product_id": product_id,
                                "price": price,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                        except Exception as _cb_err:
                            logger.debug(f"Extra ticker callback error: {_cb_err}")

    def _handle_trade(self, data: dict) -> None:
        """Handle market trade events."""
        events = data.get("events", [])
        for event in events:
            trades = event.get("trades", [])
            for trade in trades:
                if self.on_trade:
                    self.on_trade({
                        "product_id": trade.get("product_id", ""),
                        "price": float(trade.get("price", 0)),
                        "size": float(trade.get("size", 0)),
                        "side": trade.get("side", ""),
                        "time": trade.get("time", ""),
                    })

    def _handle_candle(self, data: dict) -> None:
        """Handle candle updates."""
        events = data.get("events", [])
        for event in events:
            candles = event.get("candles", [])
            for candle in candles:
                if self.on_candle:
                    self.on_candle({
                        "product_id": candle.get("product_id", ""),
                        "start": candle.get("start", ""),
                        "open": float(candle.get("open", 0)),
                        "high": float(candle.get("high", 0)),
                        "low": float(candle.get("low", 0)),
                        "close": float(candle.get("close", 0)),
                        "volume": float(candle.get("volume", 0)),
                    })

    def _on_error(self, ws, error) -> None:
        """Handle WebSocket errors."""
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status, close_msg) -> None:
        """Handle WebSocket close — reconnection handled by _run_loop."""
        logger.warning(f"WebSocket closed: {close_status} {close_msg}")

    def _on_open(self, ws) -> None:
        """Handle WebSocket open — subscribe to channels."""
        logger.info("📡 WebSocket connected!")
        self._reconnect_delay = 1.0  # Reset backoff

        # Subscribe to ticker (real-time prices)
        ws.send(json.dumps(self._build_subscribe_message("ticker")))

        # Subscribe to market trades
        ws.send(json.dumps(self._build_subscribe_message("market_trades")))

        logger.info(f"📡 Subscribed to ticker + market_trades for {self.product_ids}")

    def _run_loop(self) -> None:
        """Connection loop with automatic reconnection and backoff."""
        try:
            import websocket
        except ImportError:
            logger.error("websocket-client not installed")
            return

        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                )
            except Exception as e:
                logger.error(f"WebSocket connection failed: {e}")

            # run_forever returned — reconnect if still running
            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    def start(self) -> None:
        """Start WebSocket feed in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("📡 WebSocket feed running in background")

    def stop(self) -> None:
        """Stop WebSocket feed."""
        self._running = False
        if self._ws:
            self._ws.close()
        logger.info("📡 WebSocket feed stopped")

    def get_price(self, product_id: str) -> float:
        """Get the latest real-time price for a product."""
        with self._lock:
            return self.prices.get(product_id, 0.0)

    def add_ticker_callback(self, fn: Callable[[dict], None]) -> None:
        """Register an additional ticker callback.

        Unlike *on_ticker* (set at construction), multiple callbacks may be
        added here and all will be called on every tick.  Safe to call after
        the feed has already started.
        """
        self._extra_ticker_callbacks.append(fn)

    def get_all_prices(self) -> dict[str, float]:
        """Get all latest prices."""
        with self._lock:
            return dict(self.prices)

    def get_stats(self) -> dict:
        """Get feed statistics."""
        return {
            "connected": self._running and self._ws is not None,
            "messages_received": self.messages_received,
            "last_message": self.last_message_time.isoformat() if self.last_message_time else None,
            "products": self.product_ids,
            "current_prices": self.get_all_prices(),
        }

    def update_subscriptions(self, new_product_ids: list[str]) -> None:
        """Dynamically update WebSocket subscriptions when active pairs change.

        Sends unsubscribe for removed products and subscribe for new ones.
        Thread-safe — can be called from any thread.
        """
        old_set = set(self.product_ids)
        new_set = set(new_product_ids)

        to_remove = old_set - new_set
        to_add = new_set - old_set

        if not to_remove and not to_add:
            return

        self.product_ids = list(new_set)

        ws = self._ws
        if not ws or not self._running:
            logger.debug("WS not connected — subscriptions updated for next reconnect")
            return

        try:
            if to_remove:
                timestamp = str(int(time.time()))
                unsub = {
                    "type": "unsubscribe",
                    "product_ids": list(to_remove),
                    "channel": "ticker",
                }
                if self.api_key:
                    products_str = ",".join(sorted(to_remove))
                    message = f"{timestamp}ticker{products_str}"
                    signature = hmac.new(
                        self.api_secret.encode("utf-8"),
                        message.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    unsub["api_key"] = self.api_key
                    unsub["timestamp"] = timestamp
                    unsub["signature"] = signature
                ws.send(json.dumps(unsub))
                logger.info(f"📡 WS unsubscribed from {sorted(to_remove)}")

            if to_add:
                timestamp = str(int(time.time()))
                sub = {
                    "type": "subscribe",
                    "product_ids": list(to_add),
                    "channel": "ticker",
                }
                if self.api_key:
                    products_str = ",".join(sorted(to_add))
                    message = f"{timestamp}ticker{products_str}"
                    signature = hmac.new(
                        self.api_secret.encode("utf-8"),
                        message.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    sub["api_key"] = self.api_key
                    sub["timestamp"] = timestamp
                    sub["signature"] = signature
                ws.send(json.dumps(sub))
                logger.info(f"📡 WS subscribed to {sorted(to_add)}")

        except Exception as e:
            logger.warning(f"⚠️ WS subscription update failed: {e}")
