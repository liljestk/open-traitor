"""Tests for CoinbasePaperMixin — paper trading simulation."""
from __future__ import annotations

import threading
import pytest


class PaperHost:
    """Minimal host object with the attributes CoinbasePaperMixin expects."""

    def __init__(self, balances: dict[str, float] | None = None, prices: dict[str, float] | None = None):
        self._paper_balance = balances or {"EUR": 1000.0, "BTC": 0.5}
        self._paper_balance_lock = threading.Lock()
        self._paper_orders: list[dict] = []
        self._paper_fee_pct = 0.01  # 1%
        self._paper_slippage_pct = 0.001  # 0.1%
        self._max_paper_orders = 100
        self._last_prices: dict[str, float] = prices or {}
        self._prices = prices or {"BTC-EUR": 50000.0, "ETH-EUR": 3000.0, "SOL-EUR": 100.0}

    def get_current_price(self, pair: str) -> float:
        return self._prices.get(pair, 1.0)


# Dynamically mix the mixin onto PaperHost
from src.core.coinbase_paper import CoinbasePaperMixin


class PaperClient(CoinbasePaperMixin, PaperHost):
    def __init__(self, **kwargs):
        PaperHost.__init__(self, **kwargs)


@pytest.fixture
def pc():
    return PaperClient(
        balances={"EUR": 10_000.0, "BTC": 1.0, "ETH": 5.0},
        prices={"BTC-EUR": 50000.0, "ETH-EUR": 3000.0, "SOL-EUR": 100.0},
    )


# ═══════════════════════════════════════════════════════════════════════
# Paper Accounts
# ═══════════════════════════════════════════════════════════════════════

class TestPaperAccounts:
    def test_get_accounts(self, pc):
        accs = pc._get_paper_accounts()
        currencies = {a["currency"] for a in accs}
        assert "EUR" in currencies
        assert "BTC" in currencies
        for a in accs:
            assert "available_balance" in a
            assert "uuid" in a


# ═══════════════════════════════════════════════════════════════════════
# Market Buy
# ═══════════════════════════════════════════════════════════════════════

class TestMarketBuy:
    def test_buy_with_quote_size(self, pc):
        result = pc._paper_market_buy("BTC-EUR", quote_size="500")
        assert result["success"] is True
        order = result["order"]
        assert order["side"] == "BUY"
        assert order["status"] == "FILLED"
        assert float(order["filled_size"]) > 0
        # Balance should decrease
        assert pc._paper_balance["EUR"] < 10_000.0
        assert pc._paper_balance["BTC"] > 1.0

    def test_buy_with_base_size(self, pc):
        result = pc._paper_market_buy("ETH-EUR", base_size="1.0")
        assert result["success"] is True
        assert pc._paper_balance["ETH"] > 5.0

    def test_buy_insufficient_balance(self, pc):
        result = pc._paper_market_buy("BTC-EUR", quote_size="999999")
        assert result["success"] is False
        assert "Insufficient" in result["error"]

    def test_buy_no_size_specified(self, pc):
        result = pc._paper_market_buy("BTC-EUR")
        assert result["success"] is False

    def test_buy_applies_slippage_upward(self, pc):
        result = pc._paper_market_buy("BTC-EUR", quote_size="1000")
        assert result["success"] is True
        fill_price = float(result["order"]["average_filled_price"])
        # Buy slippage: price goes up
        assert fill_price > 50000.0

    def test_buy_charges_fee(self, pc):
        result = pc._paper_market_buy("BTC-EUR", quote_size="1000")
        fee = float(result["order"]["fee"])
        assert fee > 0
        assert fee == pytest.approx(1000 * 0.01, abs=1)  # 1% of quote


# ═══════════════════════════════════════════════════════════════════════
# Market Sell
# ═══════════════════════════════════════════════════════════════════════

class TestMarketSell:
    def test_sell_basic(self, pc):
        result = pc._paper_market_sell("BTC-EUR", base_size="0.1")
        assert result["success"] is True
        assert pc._paper_balance["BTC"] < 1.0
        assert pc._paper_balance["EUR"] > 10_000.0

    def test_sell_insufficient_balance(self, pc):
        result = pc._paper_market_sell("BTC-EUR", base_size="100")
        assert result["success"] is False

    def test_sell_applies_slippage_downward(self, pc):
        result = pc._paper_market_sell("BTC-EUR", base_size="0.1")
        fill_price = float(result["order"]["average_filled_price"])
        # Sell slippage: price goes down
        assert fill_price < 50000.0

    def test_sell_deducts_fee(self, pc):
        result = pc._paper_market_sell("ETH-EUR", base_size="1.0")
        fee = float(result["order"]["fee"])
        assert fee > 0


# ═══════════════════════════════════════════════════════════════════════
# Limit Buy
# ═══════════════════════════════════════════════════════════════════════

class TestLimitBuy:
    def test_limit_buy_fills_when_price_at_or_below_limit(self, pc):
        # Market price is 3000, limit at 3100 → fills immediately
        result = pc._paper_limit_buy("ETH-EUR", base_size="1.0", limit_price="3100")
        assert result["success"] is True
        order = result["order"]
        assert order["status"] == "FILLED"
        assert pc._paper_balance["ETH"] > 5.0

    def test_limit_buy_rests_when_price_above_limit(self, pc):
        # Market price is 50000, limit at 40000 → resting
        result = pc._paper_limit_buy("BTC-EUR", base_size="0.01", limit_price="40000")
        assert result["success"] is True
        order = result["order"]
        assert order["status"] == "OPEN"

    def test_limit_buy_insufficient_balance(self, pc):
        result = pc._paper_limit_buy("BTC-EUR", base_size="100", limit_price="60000")
        assert result["success"] is False


# ═══════════════════════════════════════════════════════════════════════
# Order History
# ═══════════════════════════════════════════════════════════════════════

class TestOrderHistory:
    def test_orders_appended(self, pc):
        pc._paper_market_buy("ETH-EUR", quote_size="100")
        pc._paper_market_sell("ETH-EUR", base_size="0.01")
        assert len(pc._paper_orders) == 2

    def test_order_cap_enforced(self, pc):
        pc._max_paper_orders = 3
        for i in range(5):
            pc._paper_market_buy("ETH-EUR", quote_size="10")
        assert len(pc._paper_orders) <= 3
