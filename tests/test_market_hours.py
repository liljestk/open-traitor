"""Tests for market_hours module — exchange suffix parsing & gating."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.market_hours import _exchange_suffix, is_market_open, market_status_label


# ═══════════════════════════════════════════════════════════════════════
# Exchange Suffix Parsing
# ═══════════════════════════════════════════════════════════════════════

class TestExchangeSuffix:
    def test_us_plain_ticker(self):
        assert _exchange_suffix("AAPL-USD") == ""
        assert _exchange_suffix("GE-USD") == ""

    def test_stockholm(self):
        assert _exchange_suffix("VOLV-B.ST-SEK") == "ST"

    def test_helsinki(self):
        assert _exchange_suffix("NESTE.HE-EUR") == "HE"

    def test_swiss(self):
        assert _exchange_suffix("ABB.SW-CHF") == "SW"

    def test_london(self):
        assert _exchange_suffix("BP.L-GBP") == "L"

    def test_tokyo(self):
        assert _exchange_suffix("SONY.T-JPY") == "T"

    def test_unknown_suffix(self):
        assert _exchange_suffix("FOO.ZZ-EUR") is None

    def test_no_currency_suffix(self):
        assert _exchange_suffix("AAPL") is None

    def test_empty_string(self):
        assert _exchange_suffix("") is None

    def test_case_insensitive(self):
        assert _exchange_suffix("volv-b.st-sek") == "ST"


# ═══════════════════════════════════════════════════════════════════════
# is_market_open
# ═══════════════════════════════════════════════════════════════════════

class TestIsMarketOpen:
    def test_crypto_always_open(self):
        assert is_market_open("BTC-USD", "crypto") is True

    def test_default_asset_class_is_crypto(self):
        assert is_market_open("ETH-EUR") is True

    def test_unknown_equity_pair_fails_open(self):
        """Unknown pair format should fail-safe to open."""
        assert is_market_open("NONSENSE", "equity") is True

    def test_unknown_suffix_fails_open(self):
        assert is_market_open("FOO.ZZ-EUR", "equity") is True


# ═══════════════════════════════════════════════════════════════════════
# market_status_label
# ═══════════════════════════════════════════════════════════════════════

class TestMarketStatusLabel:
    def test_crypto_label(self):
        assert market_status_label("BTC-USD", "crypto") == "open (crypto)"

    def test_unknown_exchange_label(self):
        assert market_status_label("FOO.ZZ-EUR", "equity") == "open (unknown exchange)"

    def test_equity_contains_mic(self):
        label = market_status_label("AAPL-USD", "equity")
        # Should contain XNYS since AAPL is US
        assert "XNYS" in label
