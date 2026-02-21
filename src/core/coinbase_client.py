"""
Coinbase Advanced Trade API client wrapper.
Handles both REST and WebSocket connections with paper trading support.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.utils.logger import get_logger

logger = get_logger("core.coinbase")

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


from src.core.exchange_client import ExchangeClient

class CoinbaseClient(ExchangeClient):
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
                # Return empty dict — do NOT fall through to mock data
                # when we have a real API client. Mock is only for paper mode
                # without API access.
                return {"product_id": product_id, "price": "0"}

        # Mock data for paper trading without API
        return self._mock_product(product_id)

    def get_current_price(self, pair: str) -> float:
        """Get the current price for a trading pair."""
        product = self.get_product(pair)
        price = float(product.get("price", 0))
        if price > 0:
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

    # =========================================================================
    # Account Diagnostics & Currency Discovery
    # =========================================================================

    def check_connection(self) -> dict[str, Any]:
        """
        Verify the Coinbase API connection and key permissions.

        Returns a dict with keys:
          ok          – bool, True if the API is reachable
          mode        – 'live' | 'paper'
          message     – human-readable status line
          total_accounts / non_zero_accounts / currencies  (on success)
          error       – error string (on failure)
        """
        if not self._rest_client:
            if self.paper_mode:
                return {
                    "ok": True,
                    "mode": "paper",
                    "message": "Paper mode — Coinbase API not required",
                }
            return {
                "ok": False,
                "mode": "live",
                "error": "REST client not initialized (missing API credentials)",
            }

        try:
            accounts = self._rest_client.get_accounts()
            result = accounts.to_dict() if hasattr(accounts, "to_dict") else dict(accounts)
            account_list = result.get("accounts", [])
            currencies = [
                a.get("available_balance", {}).get("currency", a.get("currency", "?"))
                for a in account_list
            ]
            non_zero = [
                a for a in account_list
                if float(a.get("available_balance", {}).get("value", 0)) > 0
            ]
            return {
                "ok": True,
                "mode": "live" if not self.paper_mode else "paper",
                "total_accounts": len(account_list),
                "non_zero_accounts": len(non_zero),
                "currencies": currencies,
                "message": (
                    f"Connected — {len(account_list)} accounts, "
                    f"{len(non_zero)} with balance"
                ),
            }
        except Exception as e:
            logger.error(f"Account validation error: {e}")
            return {
                "ok": False,
                "mode": "live" if not self.paper_mode else "paper",
                "error": "Account validation failed — check logs for details",
            }

    def detect_native_currency(self) -> str:
        """
        Detect the account's native fiat currency.

        Logic: find the fiat account with the largest USD-equivalent balance.
        If no non-USD fiat balance is found but a non-USD fiat account exists,
        that currency is still preferred over the USD default so pair adaption
        fires correctly for EUR/GBP accounts.
        Falls back to 'USD' if no API client or detection fails.
        """
        if not self._rest_client:
            return "USD"

        try:
            accounts = self._rest_client.get_accounts()
            result = accounts.to_dict() if hasattr(accounts, "to_dict") else dict(accounts)
            account_list = result.get("accounts", [])

            best_currency = "USD"
            best_value_usd = -1.0  # -1 so even a zero EUR balance beats the USD default

            for account in account_list:
                balance = account.get("available_balance", {})
                currency = balance.get("currency", "")
                value = float(balance.get("value", 0))

                if currency not in _KNOWN_FIAT or currency == "USD":
                    continue

                # USD-equivalent value of this fiat pocket
                value_usd = self._currency_to_usd(currency, value) if value > 0 else 0.0

                if value_usd >= best_value_usd:
                    best_value_usd = value_usd
                    best_currency = currency

            logger.info(f"\U0001f30d Detected native account currency: {best_currency}")
            return best_currency

        except Exception as e:
            logger.warning(f"\u26a0\ufe0f Could not detect native currency: {e} — defaulting to USD")
            return "USD"

    def adapt_pairs_to_account(self, pairs: list[str], native_currency: str) -> list[str]:
        """
        Dynamically expands the configured pairs to include all valid, 
        tradeable pairs between the extracted assets on Coinbase.
        If the API is unavailable, falls back to basic adaptation.
        """
        # Step 1: Extract all allowed assets
        allowed_assets = {native_currency}
        for pair in pairs:
            if "-" in pair:
                base, quote = pair.split("-", 1)
                allowed_assets.add(base)
                allowed_assets.add(quote)
            else:
                allowed_assets.add(pair)
                
        # Step 2: Fetch all products
        if self._rest_client:
            try:
                products_resp = self._rest_client.get_products()
                pdata = products_resp.to_dict() if hasattr(products_resp, "to_dict") else dict(products_resp)
                all_products = pdata.get("products", [])
                
                dynamic_pairs: set[str] = set()
                for prod in all_products:
                    base = prod.get("base_currency_id")
                    quote = prod.get("quote_currency_id")
                    product_id = prod.get("product_id")
                    
                    if base in allowed_assets and quote in allowed_assets:
                        if (not prod.get("trading_disabled", True) and 
                            not prod.get("is_disabled", False) and 
                            str(prod.get("status", "")).lower() == "online"):
                            dynamic_pairs.add(product_id)
                            
                if dynamic_pairs:
                    rewritten = sorted(list(dynamic_pairs))
                    logger.info(f"  ✓ Dynamic pairs generated from assets {sorted(list(allowed_assets))}")
                    return rewritten
            except Exception as e:
                logger.warning(f"  ⚠ Failed to generate dynamic pairs: {e} — falling back")

        # Fallback to standard adaptation (e.g. BTC-USD -> BTC-EUR)
        if native_currency == "USD":
            return list(pairs)

        rewritten_fallback: list[str] = []
        for pair in pairs:
            if "-" not in pair:
                rewritten_fallback.append(pair)
                continue

            base, quote = pair.rsplit("-", 1)
            if quote != "USD":
                rewritten_fallback.append(pair)
                continue

            candidate = f"{base}-{native_currency}"
            if self._rest_client:
                try:
                    product = self._rest_client.get_product(candidate)
                    pdata = product.to_dict() if hasattr(product, "to_dict") else dict(product)
                    if (
                        pdata.get("product_id") == candidate
                        and not pdata.get("trading_disabled", True)
                        and not pdata.get("is_disabled", False)
                    ):
                        logger.info(f"  ✓ Pair adapted: {pair} → {candidate}")
                        rewritten_fallback.append(candidate)
                        continue
                except Exception:
                    pass

            logger.warning(f"  ⚠ {candidate} not available on Coinbase — keeping {pair}")
            rewritten_fallback.append(pair)

        return rewritten_fallback

    def discover_all_pairs(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: set[str] | None = None,
        only_trade: set[str] | None = None,
    ) -> list[str]:
        """
        Discover ALL tradable pairs on Coinbase for the given quote currencies.

        Returns a sorted list of product IDs like ["ATOM-EUR", "BTC-EUR", "BTC-EURC", ...].
        Respects never_trade / only_trade filters from AbsoluteRules.
        """
        if not self._rest_client:
            logger.warning("discover_all_pairs: no REST client — returning empty list")
            return []

        if quote_currencies is None:
            quote_currencies = ["EUR"]
        quote_set = {q.upper() for q in quote_currencies}
        never_trade = never_trade or set()
        only_trade = only_trade or set()

        try:
            products_resp = self._rest_client.get_products()
            pdata = products_resp.to_dict() if hasattr(products_resp, "to_dict") else dict(products_resp)
            all_products = pdata.get("products", [])

            discovered: set[str] = set()
            for prod in all_products:
                product_id = prod.get("product_id", "")
                base = prod.get("base_currency_id", "")
                quote = prod.get("quote_currency_id", "")

                # Must be one of our target quote currencies
                if quote not in quote_set:
                    continue

                # Must be online and tradable
                if prod.get("trading_disabled", True):
                    continue
                if prod.get("is_disabled", False):
                    continue
                if str(prod.get("status", "")).lower() != "online":
                    continue

                # Apply AbsoluteRules filters
                if product_id in never_trade or base in never_trade:
                    continue
                if only_trade and product_id not in only_trade and base not in only_trade:
                    continue

                discovered.add(product_id)

            result = sorted(discovered)
            logger.info(
                f"🔍 Discovered {len(result)} tradable pairs for "
                f"quote currencies {sorted(quote_set)}"
            )
            return result

        except Exception as e:
            logger.warning(f"⚠️ discover_all_pairs failed: {e}")
            return []

    # ─── Universe Discovery (detailed metadata) ───────────────────────────

    # Cached product list for find_direct_pair and universe discovery
    _PRODUCT_CACHE_TTL: float = 600.0  # 10 min

    def _refresh_product_cache(self) -> list[dict]:
        """Refresh the full product list from Coinbase (cached 10 min)."""
        import time as _time
        now = _time.time()
        with self._product_cache_lock:
            if self._product_cache and (now - self._product_cache_ts) < self._PRODUCT_CACHE_TTL:
                return self._product_cache
            if not self._rest_client:
                return self._product_cache or []
            try:
                resp = self._rest_client.get_products()
                pdata = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
                self._product_cache = pdata.get("products", [])
                self._product_cache_ts = now
            except Exception as e:
                logger.warning(f"⚠️ Product cache refresh failed: {e}")
            return self._product_cache

    def discover_all_pairs_detailed(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: set[str] | None = None,
        only_trade: set[str] | None = None,
        include_crypto_quotes: bool = False,
    ) -> list[dict]:
        """
        Discover ALL tradable pairs on Coinbase with detailed metadata.

        Returns list[dict] with: product_id, base_currency_id, quote_currency_id,
        price, volume_24h, price_percentage_change_24h.
        When include_crypto_quotes=True, also returns crypto-to-crypto pairs (e.g. ETH-BTC).
        """
        all_products = self._refresh_product_cache()
        if not all_products:
            return []

        if quote_currencies is None:
            quote_currencies = ["EUR"]
        quote_set = {q.upper() for q in quote_currencies}
        never_trade = never_trade or set()
        only_trade = only_trade or set()

        discovered: list[dict] = []
        for prod in all_products:
            product_id = prod.get("product_id", "")
            base = prod.get("base_currency_id", "")
            quote = prod.get("quote_currency_id", "")

            # Must be online and tradable
            if prod.get("trading_disabled", True):
                continue
            if prod.get("is_disabled", False):
                continue
            if str(prod.get("status", "")).lower() != "online":
                continue

            # Quote currency filter
            is_target_quote = quote in quote_set
            is_crypto_quote = include_crypto_quotes and quote not in _KNOWN_QUOTES
            if not is_target_quote and not is_crypto_quote:
                continue

            # Apply AbsoluteRules filters
            if product_id in never_trade or base in never_trade:
                continue
            if only_trade and product_id not in only_trade and base not in only_trade:
                continue

            # Extract price/volume metadata
            try:
                price = float(prod.get("price", 0))
            except (ValueError, TypeError):
                price = 0.0
            try:
                volume_24h = float(prod.get("volume_24h", 0))
            except (ValueError, TypeError):
                volume_24h = 0.0
            try:
                pct_change = float(prod.get("price_percentage_change_24h", 0))
            except (ValueError, TypeError):
                pct_change = 0.0

            discovered.append({
                "product_id": product_id,
                "base_currency_id": base,
                "quote_currency_id": quote,
                "price": price,
                "volume_24h": volume_24h,
                "price_percentage_change_24h": pct_change,
            })

        crypto_count = sum(1 for d in discovered if d["quote_currency_id"] not in _KNOWN_QUOTES)
        logger.info(
            f"🔍 Universe discovery: {len(discovered)} pairs "
            f"({crypto_count} crypto-to-crypto)"
        )
        return discovered

    def find_direct_pair(self, base: str, quote: str) -> tuple[str, str] | None:
        """
        Check if a direct trading pair exists between two assets.

        Returns (product_id, "buy"/"sell") or None.
        Example: find_direct_pair("ETH", "BTC") → ("ETH-BTC", "sell") if selling ETH for BTC
                 or ("BTC-ETH", "buy") if buying ETH with BTC
        """
        all_products = self._refresh_product_cache()
        product_map = {
            p.get("product_id", ""): p for p in all_products
            if not p.get("trading_disabled", True)
            and not p.get("is_disabled", False)
            and str(p.get("status", "")).lower() == "online"
        }

        # Check base-quote directly (e.g. ETH-BTC)
        direct = f"{base}-{quote}"
        if direct in product_map:
            return (direct, "sell")  # sell base for quote

        # Check reverse (e.g. BTC-ETH → buy base using quote)
        reverse = f"{quote}-{base}"
        if reverse in product_map:
            return (reverse, "buy")  # buy base from quote side

        return None

    def _currency_to_usd(self, currency: str, amount: float) -> float:
        """
        Convert a currency amount to its approximate USD value.
        Order of preference:
          1. USD / known stablecoins → 1:1
          2. EUR-pegged stablecoins (EURC) → via EUR→USD rate
          3. Cached price for {currency}-USD or {currency}-EUR→USD
          4. Live fetch from Coinbase (try -USD then -EUR→USD)
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

        # Try {currency}-USD directly
        pair_usd = f"{currency}-USD"
        price = self._last_prices.get(pair_usd, 0)
        if price == 0:
            price = self.get_current_price(pair_usd)
        if price > 0:
            return amount * price

        # Try {currency}-EUR → convert EUR value to USD
        pair_eur = f"{currency}-EUR"
        price_eur = self._last_prices.get(pair_eur, 0)
        if price_eur == 0:
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

        logger.warning(f"⚠️ No USD price available for {currency} — excluding {amount:.6f} from portfolio value")
        return 0.0

    def _currency_to_native(self, currency: str, amount: float, native: str) -> float:
        """
        Convert a currency amount to *native* account currency (e.g. EUR).
        For display purposes — avoids the double-conversion error.
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

        # Try direct pair: {currency}-{native}  (e.g. ATOM-EUR)
        pair = f"{currency}-{native}"
        price = self._last_prices.get(pair, 0)
        if price == 0:
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
        price_usd = self._last_prices.get(pair_usd, 0)
        if price_usd == 0:
            price_usd = self.get_current_price(pair_usd)
        if price_usd > 0 and native in _KNOWN_FIAT:
            rate_nat = _get_fiat_rate_usd(native)
            if rate_nat > 0:
                return amount * price_usd / rate_nat

        logger.warning(f"⚠️ No {native} price for {currency} — excluding {amount:.6f}")
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
    # ExchangeClient Implementations
    # =========================================================================

    def place_market_order(self, pair: str, side: str, amount: float, amount_is_base: bool = False, client_oid: str = "") -> dict:
        """Place a market order (ExchangeClient abstract method implementation)."""
        if side.upper() == "BUY":
            if amount_is_base:
                return self.market_order_buy(pair, base_size=str(amount))
            else:
                return self.market_order_buy(pair, quote_size=str(amount))
        elif side.upper() == "SELL":
            return self.market_order_sell(pair, base_size=str(amount))
        return {"success": False, "error": f"Invalid side: {side}"}

    def place_limit_order(self, pair: str, side: str, price: float, size: float, client_oid: str = "") -> dict:
        """Place a limit order (ExchangeClient abstract method implementation)."""
        if side.upper() == "BUY":
            return self.limit_order_buy(pair, base_size=str(size), limit_price=str(price))
        elif side.upper() == "SELL":
            return self.limit_order_sell(pair, base_size=str(size), limit_price=str(price))
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
                return {"success": False, "error": "Order failed — check logs for details"}

        return {"success": False, "error": "No client available"}

    def limit_order_buy(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
        post_only: bool = True,
    ) -> dict:
        """
        Place a limit buy order (maker order for lower fees).

        Args:
            product_id: Trading pair (e.g. BTC-USD)
            base_size: Amount of base currency to buy
            limit_price: Maximum price willing to pay
            post_only: If True, order is rejected if it would fill immediately
                       (ensures maker fee). Default True.
        """
        if self.paper_mode:
            return self._paper_limit_buy(product_id, base_size, limit_price)

        if self._rest_client:
            try:
                import uuid

                order = self._rest_client.limit_order_gtc_buy(
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    base_size=base_size,
                    limit_price=limit_price,
                    post_only=post_only,
                )
                result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
                logger.info(
                    f"✅ Limit BUY order placed: {product_id} @ {limit_price} | "
                    f"order_id={result.get('order_id', '?')}"
                )
                logger.debug(f"Limit BUY detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place limit buy order: {e}")
                return {"success": False, "error": "Order failed — check logs for details"}

        return {"success": False, "error": "No client available"}

    def limit_order_sell(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
        post_only: bool = True,
    ) -> dict:
        """
        Place a limit sell order (maker order for lower fees).

        Args:
            product_id: Trading pair (e.g. BTC-USD)
            base_size: Amount of base currency to sell
            limit_price: Minimum price willing to accept
            post_only: If True, order is rejected if it would fill immediately
                       (ensures maker fee). Default True.
        """
        if self.paper_mode:
            return self._paper_limit_sell(product_id, base_size, limit_price)

        if self._rest_client:
            try:
                import uuid

                order = self._rest_client.limit_order_gtc_sell(
                    client_order_id=str(uuid.uuid4()),
                    product_id=product_id,
                    base_size=base_size,
                    limit_price=limit_price,
                    post_only=post_only,
                )
                result = order.to_dict() if hasattr(order, "to_dict") else dict(order)
                logger.info(
                    f"✅ Limit SELL order placed: {product_id} @ {limit_price} | "
                    f"order_id={result.get('order_id', '?')}"
                )
                logger.debug(f"Limit SELL detail: {result}")
                return result
            except Exception as e:
                logger.error(f"❌ Failed to place limit sell order: {e}")
                return {"success": False, "error": "Order failed — check logs for details"}

        return {"success": False, "error": "No client available"}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        if self.paper_mode:
            for order in self._paper_orders:
                if order.get("order_id") == order_id and order.get("status") == "OPEN":
                    order["status"] = "CANCELLED"
                    logger.info(f"📝 Paper order cancelled: {order_id}")
                    return {"success": True, "order_id": order_id}
            return {"success": False, "error": "Order not found or not open"}

        if self._rest_client:
            try:
                result = self._rest_client.cancel_orders([order_id])
                res = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                logger.info(f"✅ Order cancelled: {order_id}")
                return {"success": True, "result": res}
            except Exception as e:
                logger.error(f"❌ Failed to cancel order {order_id}: {e}")
                return {"success": False, "error": "Order failed — check logs for details"}

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
                return {"success": False, "error": "Order failed — check logs for details"}

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
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        if not quote_size and not base_size:
            return {"success": False, "error": "Must specify quote_size or base_size"}

        # Apply slippage: buys fill slightly above mid-price
        fill_price = price * (1.0 + self._paper_slippage_pct)
        if quote_size:
            quantity = float(quote_size) / fill_price
            quote_amount = float(quote_size)
        elif base_size:
            quantity = float(base_size)
            quote_amount = quantity * fill_price

        fee = round(quote_amount * self._paper_fee_pct, 8)
        total_cost = round(quote_amount + fee, 8)
        with self._paper_balance_lock:
            quote_bal = self._paper_balance.get(quote_currency, 0)
            if quote_bal < total_cost:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient balance. "
                        f"Have: {quote_bal:,.2f} {quote_currency}, "
                        f"Need: {total_cost:,.2f} {quote_currency} (incl. fee)"
                    ),
                }

            self._paper_balance[quote_currency] = round(quote_bal - total_cost, 8)
            self._paper_balance[base_currency] = round(
                self._paper_balance.get(base_currency, 0) + quantity, 8
            )

        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id,
            "product_id": product_id,
            "side": "BUY",
            "type": "MARKET",
            "status": "FILLED",
            "filled_size": str(quantity),
            "filled_value": str(quote_amount),
            "average_filled_price": str(fill_price),
            "fee": str(fee),
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        with self._paper_balance_lock:
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper BUY: {quantity:.6f} {base_currency} @ {fill_price:,.2f} {quote_currency} "
            f"(mid={price:,.2f}, slippage={self._paper_slippage_pct:.2%}, "
            f"{quote_amount:,.2f} + {fee:.2f} fee {quote_currency})"
        )
        return {"success": True, "order": order}

    def _paper_market_sell(self, product_id: str, base_size: str) -> dict:
        """Execute a paper trading market sell."""
        import uuid

        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"
        quantity = float(base_size)

        # Apply slippage: sells fill slightly below mid-price
        fill_price = price * (1.0 - self._paper_slippage_pct)
        quote_amount = quantity * fill_price
        fee = quote_amount * self._paper_fee_pct

        with self._paper_balance_lock:
            base_bal = self._paper_balance.get(base_currency, 0)
            if base_bal < quantity:
                return {
                    "success": False,
                    "error": f"Insufficient {base_currency} balance. Have: {base_bal:.6f}, Need: {quantity:.6f}",
                }

            self._paper_balance[base_currency] = base_bal - quantity
            self._paper_balance[quote_currency] = self._paper_balance.get(quote_currency, 0) + quote_amount
            self._paper_balance[quote_currency] -= fee

        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id,
            "product_id": product_id,
            "side": "SELL",
            "type": "MARKET",
            "status": "FILLED",
            "filled_size": str(quantity),
            "filled_value": str(quote_amount),
            "average_filled_price": str(fill_price),
            "fee": str(fee),
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        with self._paper_balance_lock:
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper SELL: {quantity:.6f} {base_currency} @ {fill_price:,.2f} {quote_currency} "
            f"(mid={price:,.2f}, slippage={self._paper_slippage_pct:.2%}, "
            f"{quote_amount:,.2f} - {fee:.2f} fee {quote_currency})"
        )
        return {"success": True, "order": order}

    def _paper_limit_buy(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
    ) -> dict:
        """Simulate a paper limit buy (fills immediately at limit or better)."""
        import uuid

        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        lim_price = float(limit_price)
        quantity = float(base_size)

        # Limit buy only fills if market price <= limit price
        if price > lim_price:
            # Place as resting order (OPEN) — will need to be checked later
            order_id = str(uuid.uuid4())
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "side": "BUY",
                "type": "LIMIT",
                "status": "OPEN",
                "limit_price": limit_price,
                "base_size": base_size,
                "created_time": datetime.now(timezone.utc).isoformat(),
            }
            self._paper_orders.append(order)
            logger.info(
                f"📝 Paper Limit BUY resting: {quantity:.6f} {base_currency} "
                f"@ {lim_price:,.2f} {quote_currency} (market={price:,.2f})"
            )
            return {"success": True, "order": order}

        # Fills at limit price (or market if better)
        fill_price = min(price, lim_price)
        quote_amount = quantity * fill_price

        # Use lower maker fee for limit orders
        maker_fee_pct = self._paper_fee_pct * 0.5  # ~50% of taker fee
        fee = quote_amount * maker_fee_pct

        quote_bal = self._paper_balance.get(quote_currency, 0)
        if quote_bal < quote_amount + fee:
            return {
                "success": False,
                "error": f"Insufficient {quote_currency} balance for limit buy",
            }

        self._paper_balance[quote_currency] = quote_bal - quote_amount - fee
        self._paper_balance[base_currency] = self._paper_balance.get(base_currency, 0) + quantity

        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id,
            "product_id": product_id,
            "side": "BUY",
            "type": "LIMIT",
            "status": "FILLED",
            "filled_size": str(quantity),
            "filled_value": str(quote_amount),
            "average_filled_price": str(fill_price),
            "fee": str(fee),
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        self._paper_orders.append(order)
        if len(self._paper_orders) > self._max_paper_orders:
            self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper Limit BUY filled: {quantity:.6f} {base_currency} "
            f"@ {fill_price:,.2f} {quote_currency} (maker fee={fee:.2f})"
        )
        return {"success": True, "order": order}

    def _paper_limit_sell(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
    ) -> dict:
        """Simulate a paper limit sell (fills immediately at limit or better)."""
        import uuid

        price = self.get_current_price(product_id)
        parts = product_id.split("-")
        base_currency = parts[0]
        quote_currency = parts[1] if len(parts) > 1 else "USD"

        lim_price = float(limit_price)
        quantity = float(base_size)

        if self._paper_balance.get(base_currency, 0) < quantity:
            return {
                "success": False,
                "error": f"Insufficient {base_currency} balance for limit sell",
            }

        # Limit sell only fills if market price >= limit price
        if price < lim_price:
            order_id = str(uuid.uuid4())
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "side": "SELL",
                "type": "LIMIT",
                "status": "OPEN",
                "limit_price": limit_price,
                "base_size": base_size,
                "created_time": datetime.now(timezone.utc).isoformat(),
            }
            self._paper_orders.append(order)
            logger.info(
                f"📝 Paper Limit SELL resting: {quantity:.6f} {base_currency} "
                f"@ {lim_price:,.2f} {quote_currency} (market={price:,.2f})"
            )
            return {"success": True, "order": order}

        fill_price = max(price, lim_price)
        quote_amount = quantity * fill_price
        maker_fee_pct = self._paper_fee_pct * 0.5
        fee = quote_amount * maker_fee_pct

        self._paper_balance[base_currency] -= quantity
        self._paper_balance[quote_currency] = self._paper_balance.get(quote_currency, 0) + quote_amount - fee

        order_id = str(uuid.uuid4())
        order = {
            "order_id": order_id,
            "product_id": product_id,
            "side": "SELL",
            "type": "LIMIT",
            "status": "FILLED",
            "filled_size": str(quantity),
            "filled_value": str(quote_amount),
            "average_filled_price": str(fill_price),
            "fee": str(fee),
            "created_time": datetime.now(timezone.utc).isoformat(),
        }
        self._paper_orders.append(order)
        if len(self._paper_orders) > self._max_paper_orders:
            self._paper_orders = self._paper_orders[-self._max_paper_orders:]

        logger.info(
            f"📝 Paper Limit SELL filled: {quantity:.6f} {base_currency} "
            f"@ {fill_price:,.2f} {quote_currency} (maker fee={fee:.2f})"
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
