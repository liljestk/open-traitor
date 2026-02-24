"""
Currency constants, fiat rate conversion, and portfolio valuation for Coinbase.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from src.utils.logger import get_logger

logger = get_logger("core.coinbase.currency")

# Currencies that are pegged ~1:1 to USD and should be counted at face value
_USD_EQUIVALENTS = {"USD", "USDC", "USDT", "FDUSD", "PYUSD", "DAI", "USDS"}

# Currencies that are pegged ~1:1 to EUR
_EUR_EQUIVALENTS = {"EURC"}

# All stablecoins / fiat-like currencies (for is_fiat checks)
_ALL_STABLECOINS = _USD_EQUIVALENTS | _EUR_EQUIVALENTS

# Known fiat currencies (used for native-currency detection)
_KNOWN_FIAT = {
    "USD", "EUR", "GBP", "CHF", "CAD", "AUD", "JPY",
    "SGD", "BRL", "MXN", "HKD", "NOK", "SEK", "DKK",
}

# Known quote currencies — fiat + stablecoins that can be the "right side" of a pair
_KNOWN_QUOTES = _KNOWN_FIAT | _ALL_STABLECOINS

# Live fiat-to-USD rate cache {currency: (rate, fetched_at_epoch)}
_FIAT_RATE_CACHE: dict[str, tuple[float, float]] = {}
_FIAT_RATE_LOCK = threading.Lock()
_FIAT_RATE_TTL = 6 * 3600  # 6 hours — fiat rates are stable intraday
_FIAT_RATE_URL = "https://api.frankfurter.app/latest?from=USD"  # ECB rates, no API key


def _get_fiat_rate_usd(currency: str) -> float:
    """
    Return the number of USD per 1 unit of *currency* (e.g. EUR → ~1.05).
    Fetches a single bulk request for all major fiats and caches for 6 hours.
    Returns 0 if the currency is unknown or the request fails.
    """
    now = time.time()
    with _FIAT_RATE_LOCK:
        cached = _FIAT_RATE_CACHE.get(currency)
        if cached and (now - cached[1]) < _FIAT_RATE_TTL:
            return cached[0]

    try:
        resp = requests.get(_FIAT_RATE_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        # Response: {"base": "USD", "rates": {"EUR": 0.952, "GBP": 0.789, ...}}
        # rates[X] = how many X per 1 USD  →  USD per X = 1 / rates[X]
        with _FIAT_RATE_LOCK:
            for code, per_usd in data.get("rates", {}).items():
                if per_usd and per_usd > 0:
                    _FIAT_RATE_CACHE[code] = (1.0 / per_usd, now)
        logger.debug(f"Fiat exchange rates refreshed ({len(data.get('rates', {}))} currencies)")
    except Exception as e:
        logger.warning(f"⚠️ Fiat rate fetch failed: {e} — using cached/zero rate for {currency}")

    with _FIAT_RATE_LOCK:
        result = _FIAT_RATE_CACHE.get(currency)
    return result[0] if result else 0.0


class CoinbaseCurrencyMixin:
    """Mixin providing currency conversion and portfolio valuation.

    Expects the host class to provide:
      - self.get_current_price(pair: str) -> float
      - self.paper_mode: bool
      - self._paper_balance: dict
      - self._paper_balance_lock: threading.Lock
      - self.get_accounts() -> list[dict]
    """

    def _currency_to_usd(self, currency: str, amount: float) -> float:
        """
        Convert a currency amount to its approximate USD value.
        Uses **live** prices via ``get_current_price`` (which is 404-safe).

        Order of preference:
          1. USD / known stablecoins → 1:1
          2. EUR-pegged stablecoins (EURC) → via EUR→USD rate
          3. Live price for {currency}-USD
          4. Live price for {currency}-EUR → EUR→USD conversion
          5. Live fiat exchange rate from Frankfurter (ECB)
          6. Return 0
        """
        if amount <= 0:
            return 0.0
        if currency in _USD_EQUIVALENTS:
            return amount
        # EURC and other EUR-pegged stablecoins: convert via EUR→USD rate
        if currency in _EUR_EQUIVALENTS:
            eur_to_usd = _get_fiat_rate_usd("EUR")
            return amount * eur_to_usd if eur_to_usd > 0 else amount

        # Try {currency}-USD directly (404-safe via catalogue guard)
        pair_usd = f"{currency}-USD"
        price = self.get_current_price(pair_usd)
        if price > 0:
            return amount * price

        # Try {currency}-EUR → convert EUR value to USD
        pair_eur = f"{currency}-EUR"
        price_eur = self.get_current_price(pair_eur)
        if price_eur > 0:
            eur_to_usd = _get_fiat_rate_usd("EUR")  # USD per EUR
            if eur_to_usd > 0:
                return amount * price_eur * eur_to_usd
            # If we can't get the EUR→USD rate, just use EUR price as rough estimate
            return amount * price_eur

        # Fiat fallback — live ECB rate via Frankfurter (EUR, GBP, CHF, etc.)
        if currency in _KNOWN_FIAT:
            fiat_rate = _get_fiat_rate_usd(currency)
            if fiat_rate > 0:
                logger.debug(f"Using live fiat rate for {currency}: {fiat_rate:.4f} USD")
                return amount * fiat_rate

        # Stablecoin bridge: try {currency}-USDT or {currency}-USDC
        for stable in ("USDT", "USDC"):
            pair_stable = f"{currency}-{stable}"
            price_stable = self.get_current_price(pair_stable)
            if price_stable > 0:
                return amount * price_stable  # stablecoins ≈ 1 USD

        logger.warning(f"⚠️ No USD price available for {currency} — excluding {amount:.6f} from portfolio value")
        return 0.0

    def _currency_to_native(self, currency: str, amount: float, native: str) -> float:
        """
        Convert a currency amount to *native* account currency (e.g. EUR).
        Uses **live** prices via ``get_current_price`` (which is 404-safe).
        """
        if amount <= 0:
            return 0.0
        if currency == native:
            return amount
        if currency in _USD_EQUIVALENTS and native == "USD":
            return amount
        # EUR-pegged stablecoins (EURC) → treat as ~1:1 EUR
        if currency in _EUR_EQUIVALENTS and native == "EUR":
            return amount
        # EURC → other native: go through EUR→native fiat conversion
        if currency in _EUR_EQUIVALENTS:
            rate_eur = _get_fiat_rate_usd("EUR")
            rate_nat = _get_fiat_rate_usd(native)
            if rate_eur > 0 and rate_nat > 0:
                return amount * (rate_eur / rate_nat)

        # Try direct pair: {currency}-{native}  (e.g. ATOM-EUR) — 404-safe
        pair = f"{currency}-{native}"
        price = self.get_current_price(pair)
        if price > 0:
            return amount * price

        # Fiat-to-fiat: use Frankfurter rates
        if currency in _KNOWN_FIAT and native in _KNOWN_FIAT:
            rate_cur = _get_fiat_rate_usd(currency)  # USD/unit of currency
            rate_nat = _get_fiat_rate_usd(native)     # USD/unit of native
            if rate_cur > 0 and rate_nat > 0:
                return amount * (rate_cur / rate_nat)

        # Fallback: try USD pair and convert USD→native
        pair_usd = f"{currency}-USD"
        price_usd = self.get_current_price(pair_usd)
        if price_usd > 0 and native in _KNOWN_FIAT:
            rate_nat = _get_fiat_rate_usd(native)
            if rate_nat > 0:
                return amount * price_usd / rate_nat

        # Stablecoin bridge: try {currency}-USDT or {currency}-USDC
        # Many altcoins only have stablecoin pairs on Coinbase.
        for stable in ("USDT", "USDC"):
            pair_stable = f"{currency}-{stable}"
            price_stable = self.get_current_price(pair_stable)
            if price_stable > 0:
                # stablecoins ≈ 1 USD → convert USD→native
                rate_nat = _get_fiat_rate_usd(native) if native in _KNOWN_FIAT else 0.0
                if rate_nat > 0:
                    return amount * price_stable / rate_nat
                # If native IS USD, stablecoin price ≈ USD price
                if native == "USD":
                    return amount * price_stable

        logger.warning(f"⚠️ No {native} price for {currency} — excluding {amount:.6f}")
        return 0.0

    def get_portfolio_value(self) -> float:
        """Get total portfolio value in USD."""
        if self.paper_mode:
            with self._paper_balance_lock:
                total = 0.0
                for currency, amount in self._paper_balance.items():
                    total += self._currency_to_usd(currency, amount)
                return total

        accounts = self.get_accounts()
        total = 0.0
        for account in accounts:
            balance = account.get("available_balance", {})
            hold = account.get("hold", {})
            currency = balance.get("currency", account.get("currency", ""))
            try:
                avail = float(balance.get("value", 0))
            except (ValueError, TypeError):
                avail = 0.0
            try:
                held = float(hold.get("value", 0))
            except (ValueError, TypeError):
                held = 0.0
            amount = avail + held
            if not currency or amount <= 0:
                continue
            total += self._currency_to_usd(currency, amount)
        return total
