"""Tests for CoinbaseClient — market data, order routing, throttle, error extraction."""
import math
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.core.coinbase_client import CoinbaseClient, _extract_cb_error


# ── _extract_cb_error ────────────────────────────────────────────────────

class TestExtractCbError:
    def test_top_level_error(self):
        assert _extract_cb_error({"error": "bad request"}) == "bad request"

    def test_error_response_message(self):
        assert _extract_cb_error({"error_response": {"message": "Insufficient funds"}}) == "Insufficient funds"

    def test_error_response_error(self):
        assert _extract_cb_error({"error_response": {"error": "INVALID"}}) == "INVALID"

    def test_preview_failure_reason(self):
        assert _extract_cb_error({"error_response": {"preview_failure_reason": "MIN_SIZE"}}) == "MIN_SIZE"

    def test_new_order_failure_reason(self):
        assert _extract_cb_error({"error_response": {"new_order_failure_reason": "REJECT"}}) == "REJECT"

    def test_failure_reason(self):
        assert _extract_cb_error({"failure_reason": "timeout"}) == "timeout"

    def test_unknown(self):
        assert _extract_cb_error({}) == "Unknown error"

    def test_priority_order(self):
        # "error" takes priority over error_response
        r = {"error": "top", "error_response": {"message": "inner"}, "failure_reason": "f"}
        assert _extract_cb_error(r) == "top"


# ── CoinbaseClient paper mode construction ──────────────────────────────

class TestCoinbaseClientInit:
    def test_paper_mode_defaults(self):
        client = CoinbaseClient(paper_mode=True)
        assert client.paper_mode is True
        assert client.exchange_id == "coinbase"
        assert client.asset_class == "crypto"
        assert client._paper_balance["USD"] == 10000.0

    def test_properties(self):
        client = CoinbaseClient(paper_mode=True)
        assert client.exchange_id == "coinbase"
        assert client.asset_class == "crypto"


# ── _format_base_size ────────────────────────────────────────────────────

class TestFormatBaseSize:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)

    def test_default_8_decimals(self):
        # No product cache entry → falls back to 8 decimals
        result = self.client._format_base_size("XYZ-USD", 1.123456789)
        assert result == "1.12345678"  # floor to 8 dp

    def test_product_cache_3_decimals(self):
        self.client._product_cache = [
            {"product_id": "BTC-USD", "base_increment": "0.001"},
        ]
        result = self.client._format_base_size("BTC-USD", 0.12567)
        assert result == "0.125"  # floor to 3 dp

    def test_product_cache_integer(self):
        self.client._product_cache = [
            {"product_id": "DOGE-USD", "base_increment": "1"},
        ]
        result = self.client._format_base_size("DOGE-USD", 42.9)
        assert result == "42"

    def test_floor_not_round(self):
        self.client._product_cache = [
            {"product_id": "ETH-USD", "base_increment": "0.01"},
        ]
        # 1.999 should floor to 1.99, not round to 2.00
        result = self.client._format_base_size("ETH-USD", 1.999)
        assert result == "1.99"


# ── Market data (paper mode mock data) ──────────────────────────────────

class TestMarketDataPaper:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = None  # Force mock-data path

    def test_get_product_paper(self):
        product = self.client.get_product("BTC-USD")
        assert product["product_id"] == "BTC-USD"
        assert float(product.get("price", 0)) > 0

    def test_get_current_price_unknown_product(self):
        # Not in catalogue → returns 0 instantly
        self.client._valid_product_ids = {"BTC-USD"}
        price = self.client.get_current_price("FAKE-USD")
        assert price == 0.0

    def test_get_current_price_known_product(self):
        self.client._valid_product_ids = {"BTC-USD"}
        price = self.client.get_current_price("BTC-USD")
        assert price > 0

    def test_get_candles_paper(self):
        candles = self.client.get_candles("BTC-USD", limit=10)
        assert isinstance(candles, list)
        assert len(candles) > 0

    def test_get_accounts_paper(self):
        accounts = self.client.get_accounts()
        assert isinstance(accounts, list)
        # Should have at least USD account
        currencies = [a.get("currency") or a.get("available_balance", {}).get("currency", "") for a in accounts]
        assert any("USD" in str(c) for c in currencies)


# ── Order routing ────────────────────────────────────────────────────────

class TestPlaceOrderRouting:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = None  # Force paper-only path
        # Seed a price for BTC-USD
        self.client._last_prices["BTC-USD"] = 50000.0
        self.client._valid_product_ids = {"BTC-USD"}

    def test_place_market_order_buy_quote(self):
        result = self.client.place_market_order("BTC-USD", "BUY", 100.0, amount_is_base=False)
        assert result.get("success") is True or "order_id" in result

    def test_place_market_order_buy_base(self):
        result = self.client.place_market_order("BTC-USD", "BUY", 0.001, amount_is_base=True)
        assert result.get("success") is True or "order_id" in result

    def test_place_market_order_sell(self):
        # Give paper balance some BTC
        self.client._paper_balance["BTC"] = 1.0
        result = self.client.place_market_order("BTC-USD", "SELL", 0.001)
        assert result.get("success") is True or "order_id" in result

    def test_place_market_order_invalid_side(self):
        result = self.client.place_market_order("BTC-USD", "HOLD", 100.0)
        assert result.get("success") is False
        assert "Invalid side" in result.get("error", "")

    def test_place_limit_order_buy(self):
        result = self.client.place_limit_order("BTC-USD", "BUY", 49000.0, 0.001)
        assert "order_id" in result or result.get("success") is not None

    def test_place_limit_order_sell(self):
        self.client._paper_balance["BTC"] = 1.0
        result = self.client.place_limit_order("BTC-USD", "SELL", 51000.0, 0.001)
        assert "order_id" in result or result.get("success") is not None

    def test_place_limit_order_invalid_side(self):
        result = self.client.place_limit_order("BTC-USD", "HOLD", 50000.0, 0.001)
        assert result.get("success") is False


# ── Cancel order ─────────────────────────────────────────────────────────

class TestCancelOrder:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)

    def test_cancel_open_order(self):
        self.client._paper_orders.append({
            "order_id": "order-1",
            "status": "OPEN",
        })
        result = self.client.cancel_order("order-1")
        assert result["success"] is True
        assert self.client._paper_orders[0]["status"] == "CANCELLED"

    def test_cancel_nonexistent(self):
        result = self.client.cancel_order("no-such-order")
        assert result["success"] is False


# ── Throttled requests (mocked REST client) ─────────────────────────────

class TestThrottledRequest:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = MagicMock()
        self.client._backoff_until = 0.0

    def test_success_resets_consecutive_errors(self):
        self.client._consecutive_errors = 3
        self.client._rest_client.get_product.return_value = {"product_id": "BTC-USD", "price": "50000"}
        result = self.client._throttled_request("get_product", "BTC-USD")
        assert result == {"product_id": "BTC-USD", "price": "50000"}
        assert self.client._consecutive_errors == 0

    def test_non_rate_limit_error_raises(self):
        self.client._rest_client.get_product.side_effect = ValueError("bad")
        with pytest.raises(ValueError, match="bad"):
            self.client._throttled_request("get_product", "BTC-USD")

    def test_rate_limit_retries(self):
        # First call raises 429, second succeeds
        self.client._rest_client.get_product.side_effect = [
            Exception("429 Too Many Requests"),
            {"product_id": "BTC-USD", "price": "50000"},
        ]
        result = self.client._throttled_request("get_product", "BTC-USD")
        assert result["price"] == "50000"
        assert self.client._rest_client.get_product.call_count == 2

    def test_rate_limit_exhausted(self):
        self.client._MAX_RETRIES = 2
        self.client._BASE_BACKOFF = 0.01  # speed up test
        self.client._rest_client.get_product.side_effect = Exception("429 rate limit")
        with pytest.raises(Exception, match="429"):
            self.client._throttled_request("get_product", "BTC-USD")


# ── get_product with real client ────────────────────────────────────────

class TestGetProductRealClient:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = MagicMock()
        self.client.paper_mode = False  # simulate non-paper but with mocked client

    def test_get_product_success(self):
        self.client._rest_client = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"product_id": "ETH-USD", "price": "3500.50"}
        self.client._rest_client.get_product.return_value = mock_result
        self.client._backoff_until = 0.0
        product = self.client.get_product("ETH-USD")
        assert product["price"] == "3500.50"
        assert self.client._last_prices["ETH-USD"] == 3500.50

    def test_get_product_error(self):
        self.client._rest_client.get_product.side_effect = Exception("Network error")
        self.client._backoff_until = 0.0
        product = self.client.get_product("ETH-USD")
        assert product["price"] == "0"


# ── get_candles with real client ────────────────────────────────────────

class TestGetCandlesRealClient:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = MagicMock()
        self.client.paper_mode = False
        self.client._backoff_until = 0.0

    def test_get_candles_success(self):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"candles": [{"open": "100", "close": "105"}]}
        self.client._rest_client.get_candles.return_value = mock_result
        candles = self.client.get_candles("BTC-USD", limit=5)
        assert len(candles) == 1
        assert candles[0]["open"] == "100"

    def test_get_candles_error(self):
        self.client._rest_client.get_candles.side_effect = Exception("API error")
        candles = self.client.get_candles("BTC-USD")
        assert candles == []


# ── get_accounts pagination ─────────────────────────────────────────────

class TestGetAccountsPagination:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = MagicMock()
        self.client.paper_mode = False
        self.client._backoff_until = 0.0

    def test_single_page(self):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "accounts": [{"currency": "BTC"}, {"currency": "ETH"}],
            "cursor": None,
            "has_next": False,
        }
        self.client._rest_client.get_accounts.return_value = mock_result
        accounts = self.client.get_accounts()
        assert len(accounts) == 2

    def test_multi_page(self):
        page1 = MagicMock()
        page1.to_dict.return_value = {
            "accounts": [{"currency": f"TOKEN{i}"} for i in range(250)],
            "cursor": "page2cursor",
            "has_next": True,
        }
        page2 = MagicMock()
        page2.to_dict.return_value = {
            "accounts": [{"currency": "LAST"}],
            "cursor": None,
            "has_next": False,
        }
        self.client._rest_client.get_accounts.side_effect = [page1, page2]
        accounts = self.client.get_accounts()
        assert len(accounts) == 251

    def test_empty_accounts_warning(self):
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "accounts": [],
            "cursor": None,
            "has_next": False,
        }
        self.client._rest_client.get_accounts.return_value = mock_result
        accounts = self.client.get_accounts()
        assert accounts == []

    def test_no_client(self):
        self.client._rest_client = None
        accounts = self.client.get_accounts()
        assert accounts == []


# ── Order execution with real client ────────────────────────────────────

class TestOrderExecutionRealClient:
    def setup_method(self):
        self.client = CoinbaseClient(paper_mode=True)
        self.client._rest_client = MagicMock()
        self.client.paper_mode = False
        self.client._backoff_until = 0.0

    def test_market_buy_success(self):
        mock_order = MagicMock()
        mock_order.to_dict.return_value = {"success": True, "order_id": "o1"}
        self.client._rest_client.market_order_buy.return_value = mock_order
        result = self.client.market_order_buy("BTC-USD", quote_size="100")
        assert result["success"] is True

    def test_market_buy_rejected(self):
        mock_order = MagicMock()
        mock_order.to_dict.return_value = {
            "success": False,
            "error_response": {"message": "Insufficient funds"},
        }
        self.client._rest_client.market_order_buy.return_value = mock_order
        result = self.client.market_order_buy("BTC-USD", quote_size="100")
        assert result["success"] is False

    def test_market_buy_exception(self):
        self.client._rest_client.market_order_buy.side_effect = Exception("network")
        result = self.client.market_order_buy("BTC-USD", quote_size="100")
        assert result["success"] is False

    def test_market_sell_success(self):
        mock_order = MagicMock()
        mock_order.to_dict.return_value = {"success": True, "order_id": "o2"}
        self.client._rest_client.market_order_sell.return_value = mock_order
        result = self.client.market_order_sell("BTC-USD", base_size="0.001")
        assert result["success"] is True

    def test_limit_buy_success(self):
        mock_order = MagicMock()
        mock_order.to_dict.return_value = {"order_id": "lb1"}
        self.client._rest_client.limit_order_gtc_buy.return_value = mock_order
        result = self.client.limit_order_buy("BTC-USD", base_size="0.001", limit_price="49000")
        assert result["order_id"] == "lb1"

    def test_limit_sell_success(self):
        mock_order = MagicMock()
        mock_order.to_dict.return_value = {"order_id": "ls1"}
        self.client._rest_client.limit_order_gtc_sell.return_value = mock_order
        result = self.client.limit_order_sell("BTC-USD", base_size="0.001", limit_price="51000")
        assert result["order_id"] == "ls1"

    def test_no_client_buy(self):
        self.client._rest_client = None
        self.client.paper_mode = False
        result = self.client.market_order_buy("BTC-USD", quote_size="100")
        assert result["success"] is False
