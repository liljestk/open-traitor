"""Tests for CoinbaseCurrencyMixin — currency conversion chains and portfolio valuation."""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.core.coinbase_currency import (
    _USD_EQUIVALENTS,
    _EUR_EQUIVALENTS,
    _ALL_STABLECOINS,
    _KNOWN_FIAT,
    _KNOWN_QUOTES,
    _get_fiat_rate_usd,
    _should_warn_no_price,
    _NO_PRICE_WARN_COUNTS,
    _NO_PRICE_SUPPRESSED_UNTIL,
    CoinbaseCurrencyMixin,
)


# ── Constants ────────────────────────────────────────────────────────────

class TestCurrencyConstants:
    def test_usd_equivalents(self):
        for coin in ("USD", "USDC", "USDT", "DAI"):
            assert coin in _USD_EQUIVALENTS

    def test_eur_equivalents(self):
        assert "EURC" in _EUR_EQUIVALENTS

    def test_all_stablecoins(self):
        assert _ALL_STABLECOINS == _USD_EQUIVALENTS | _EUR_EQUIVALENTS

    def test_known_fiat(self):
        for cur in ("USD", "EUR", "GBP", "JPY", "CHF"):
            assert cur in _KNOWN_FIAT

    def test_known_quotes_superset(self):
        assert _KNOWN_FIAT.issubset(_KNOWN_QUOTES)
        assert _ALL_STABLECOINS.issubset(_KNOWN_QUOTES)


# ── _should_warn_no_price suppression logic ──────────────────────────────

class TestShouldWarnNoPrice:
    def setup_method(self):
        # Clean state
        _NO_PRICE_WARN_COUNTS.clear()
        _NO_PRICE_SUPPRESSED_UNTIL.clear()

    def test_first_call_warns(self):
        assert _should_warn_no_price("TESTCOIN") is True
        assert _NO_PRICE_WARN_COUNTS["TESTCOIN"] == 1

    def test_second_call_warns(self):
        _should_warn_no_price("TESTCOIN")
        assert _should_warn_no_price("TESTCOIN") is True
        assert _NO_PRICE_WARN_COUNTS["TESTCOIN"] == 2

    def test_threshold_suppresses(self):
        # After 3 warnings, suppression kicks in
        _should_warn_no_price("X1")
        _should_warn_no_price("X1")
        result = _should_warn_no_price("X1")  # 3rd = threshold
        assert result is False  # suppressed
        assert _NO_PRICE_WARN_COUNTS["X1"] == 0  # reset

    def test_suppressed_window(self):
        # After suppression, future calls return False
        for _ in range(3):
            _should_warn_no_price("X2")
        # Now in suppression window
        assert _should_warn_no_price("X2") is False

    def test_separate_currencies_independent(self):
        _should_warn_no_price("A")
        _should_warn_no_price("B")
        assert _NO_PRICE_WARN_COUNTS["A"] == 1
        assert _NO_PRICE_WARN_COUNTS["B"] == 1


# ── _get_fiat_rate_usd ──────────────────────────────────────────────────

class TestGetFiatRateUsd:
    @patch("src.core.coinbase_currency.requests.get")
    def test_successful_fetch(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "base": "USD",
            "rates": {"EUR": 0.92, "GBP": 0.79},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # Clear cache
        from src.core.coinbase_currency import _FIAT_RATE_CACHE, _FIAT_RATE_LOCK
        with _FIAT_RATE_LOCK:
            _FIAT_RATE_CACHE.clear()

        rate = _get_fiat_rate_usd("EUR")
        # rates["EUR"] = 0.92 means 0.92 EUR per 1 USD → 1/0.92 ≈ 1.087 USD per EUR
        assert rate == pytest.approx(1.0 / 0.92, rel=0.01)

    @patch("src.core.coinbase_currency.requests.get")
    def test_unknown_currency(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"base": "USD", "rates": {"EUR": 0.92}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from src.core.coinbase_currency import _FIAT_RATE_CACHE, _FIAT_RATE_LOCK
        with _FIAT_RATE_LOCK:
            _FIAT_RATE_CACHE.clear()

        rate = _get_fiat_rate_usd("ZZZ")
        assert rate == 0.0

    @patch("src.core.coinbase_currency.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = Exception("Network error")

        from src.core.coinbase_currency import _FIAT_RATE_CACHE, _FIAT_RATE_LOCK
        with _FIAT_RATE_LOCK:
            _FIAT_RATE_CACHE.clear()

        rate = _get_fiat_rate_usd("EUR")
        assert rate == 0.0

    @patch("src.core.coinbase_currency.requests.get")
    def test_cache_hit(self, mock_get):
        from src.core.coinbase_currency import _FIAT_RATE_CACHE, _FIAT_RATE_LOCK
        with _FIAT_RATE_LOCK:
            _FIAT_RATE_CACHE["GBP"] = (1.27, time.time())

        rate = _get_fiat_rate_usd("GBP")
        assert rate == 1.27
        mock_get.assert_not_called()


# ── CoinbaseCurrencyMixin ───────────────────────────────────────────────

class FakeCoinbase(CoinbaseCurrencyMixin):
    """Minimal host class for testing the mixin."""
    def __init__(self):
        self.paper_mode = True
        self._paper_balance = {"USD": 1000.0, "BTC": 0.5, "ETH": 2.0}
        self._paper_balance_lock = threading.Lock()
        self._price_map = {}  # pair → price

    def get_current_price(self, pair: str) -> float:
        return self._price_map.get(pair, 0.0)

    def get_accounts(self) -> list:
        return []


class TestCurrencyToUsd:
    def setup_method(self):
        self.client = FakeCoinbase()

    def test_usd_equivalents(self):
        for coin in ("USD", "USDC", "USDT", "DAI"):
            assert self.client._currency_to_usd(coin, 100.0) == 100.0

    def test_zero_amount(self):
        assert self.client._currency_to_usd("BTC", 0) == 0.0

    def test_negative_amount(self):
        assert self.client._currency_to_usd("BTC", -1) == 0.0

    @patch("src.core.coinbase_currency._get_fiat_rate_usd", return_value=1.08)
    def test_eur_equivalent(self, mock_rate):
        # EURC → EUR → USD
        result = self.client._currency_to_usd("EURC", 100.0)
        assert result == pytest.approx(108.0)

    @patch("src.core.coinbase_currency._get_fiat_rate_usd", return_value=0.0)
    def test_eur_equivalent_no_rate(self, mock_rate):
        # No EUR→USD rate → fallback to face value
        result = self.client._currency_to_usd("EURC", 100.0)
        assert result == 100.0

    def test_direct_usd_pair(self):
        self.client._price_map["BTC-USD"] = 50000.0
        assert self.client._currency_to_usd("BTC", 0.5) == 25000.0

    @patch("src.core.coinbase_currency._get_fiat_rate_usd", return_value=1.08)
    def test_eur_pair_fallback(self, mock_rate):
        # No USD pair, but EUR pair exists
        self.client._price_map["ATOM-EUR"] = 10.0
        result = self.client._currency_to_usd("ATOM", 5.0)
        # 5 * 10 EUR * 1.08 USD/EUR = 54
        assert result == pytest.approx(54.0)

    @patch("src.core.coinbase_currency._get_fiat_rate_usd", return_value=1.27)
    def test_fiat_fallback(self, mock_rate):
        result = self.client._currency_to_usd("GBP", 100.0)
        assert result == pytest.approx(127.0)

    def test_stablecoin_bridge(self):
        # No USD pair, no EUR pair, but USDT pair exists
        self.client._price_map["SHIB-USDT"] = 0.00001
        result = self.client._currency_to_usd("SHIB", 1000000.0)
        assert result == pytest.approx(10.0)

    def test_no_price_returns_zero(self):
        result = self.client._currency_to_usd("FAKECOIN", 100.0)
        assert result == 0.0


class TestCurrencyToNative:
    def setup_method(self):
        self.client = FakeCoinbase()

    def test_same_currency(self):
        assert self.client._currency_to_native("EUR", 100.0, "EUR") == 100.0

    def test_usd_equivalent_to_usd(self):
        assert self.client._currency_to_native("USDC", 100.0, "USD") == 100.0

    def test_eur_equivalent_to_eur(self):
        assert self.client._currency_to_native("EURC", 100.0, "EUR") == 100.0

    def test_direct_pair(self):
        self.client._price_map["BTC-EUR"] = 45000.0
        result = self.client._currency_to_native("BTC", 0.5, "EUR")
        assert result == pytest.approx(22500.0)

    @patch("src.core.coinbase_currency._get_fiat_rate_usd")
    def test_fiat_to_fiat(self, mock_rate):
        # EUR→USD=1.08, GBP→USD=1.27  →  EUR→GBP = 1.08/1.27
        mock_rate.side_effect = lambda c: {"EUR": 1.08, "GBP": 1.27}.get(c, 0)
        result = self.client._currency_to_native("EUR", 100.0, "GBP")
        expected = 100.0 * (1.08 / 1.27)
        assert result == pytest.approx(expected, rel=0.01)

    @patch("src.core.coinbase_currency._get_fiat_rate_usd", return_value=1.08)
    def test_eur_equivalent_to_other_native(self, mock_rate):
        # EURC → EUR fiat rate → GBP
        mock_rate.side_effect = lambda c: {"EUR": 1.08, "GBP": 1.27}.get(c, 0)
        result = self.client._currency_to_native("EURC", 100.0, "GBP")
        expected = 100.0 * (1.08 / 1.27)
        assert result == pytest.approx(expected, rel=0.01)

    @patch("src.core.coinbase_currency._get_fiat_rate_usd", return_value=0.92)
    def test_usd_pair_fallback_to_native(self, mock_rate):
        self.client._price_map["BTC-USD"] = 50000.0
        # BTC→USD→EUR: 50000 * 0.5 / rate(EUR)
        result = self.client._currency_to_native("BTC", 0.5, "EUR")
        expected = 0.5 * 50000.0 / 0.92
        assert result == pytest.approx(expected, rel=0.01)

    def test_zero_amount(self):
        assert self.client._currency_to_native("BTC", 0, "EUR") == 0.0


class TestGetPortfolioValue:
    def setup_method(self):
        self.client = FakeCoinbase()

    def test_paper_mode(self):
        self.client._price_map["BTC-USD"] = 50000.0
        self.client._price_map["ETH-USD"] = 3000.0
        value = self.client.get_portfolio_value()
        # USD=1000 + BTC=0.5*50000=25000 + ETH=2*3000=6000 = 32000
        assert value == pytest.approx(32000.0)

    def test_paper_mode_no_prices(self):
        # BTC and ETH have no prices → only USD counted
        value = self.client.get_portfolio_value()
        assert value == pytest.approx(1000.0)

    def test_live_mode(self):
        self.client.paper_mode = False
        self.client._price_map["BTC-USD"] = 50000.0

        def fake_accounts():
            return [
                {
                    "available_balance": {"currency": "USD", "value": "500.0"},
                    "hold": {"value": "0"},
                },
                {
                    "available_balance": {"currency": "BTC", "value": "0.1"},
                    "hold": {"value": "0.02"},
                },
            ]
        self.client.get_accounts = fake_accounts

        value = self.client.get_portfolio_value()
        # USD=500 + BTC=(0.1+0.02)*50000=6000 = 6500
        assert value == pytest.approx(6500.0)

    def test_live_mode_empty_accounts(self):
        self.client.paper_mode = False
        self.client.get_accounts = lambda: []
        assert self.client.get_portfolio_value() == 0.0

    def test_live_mode_bad_values(self):
        self.client.paper_mode = False
        self.client.get_accounts = lambda: [
            {
                "available_balance": {"currency": "USD", "value": "not_a_number"},
                "hold": {"value": None},
            },
        ]
        value = self.client.get_portfolio_value()
        assert value == 0.0
