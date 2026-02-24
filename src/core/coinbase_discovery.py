"""
Account diagnostics, currency detection, pair adaptation, and universe discovery
for the Coinbase client.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from src.core.coinbase_currency import _KNOWN_FIAT, _KNOWN_QUOTES
from src.utils.logger import get_logger

logger = get_logger("core.coinbase.discovery")


class CoinbaseDiscoveryMixin:
    """Mixin providing account diagnostics and pair/universe discovery.

    Expects the host class to provide:
      - self._rest_client
      - self._throttled_request(method_name, *args, **kwargs)
      - self._currency_to_usd(currency, amount) -> float
      - self.paper_mode: bool
      - self._product_cache: list[dict]
      - self._product_cache_ts: float
      - self._product_cache_lock: threading.RLock
      - self._valid_product_ids: set[str]
      - self.get_current_price(pair) -> float
    """

    # Product catalogue cache TTL — 24 hours
    _PRODUCT_CACHE_TTL: float = 86_400.0

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
            accounts = self._throttled_request("get_accounts")
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
            accounts = self._throttled_request("get_accounts")
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
                products_resp = self._throttled_request("get_products")
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
                    product = self._throttled_request("get_product", candidate)
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
            products_resp = self._throttled_request("get_products")
            pdata = products_resp.to_dict() if hasattr(products_resp, "to_dict") else dict(products_resp)
            all_products = pdata.get("products", [])

            discovered: set[str] = set()
            for prod in all_products:
                product_id = prod.get("product_id", "")
                base = prod.get("base_currency_id", "")
                quote = prod.get("quote_currency_id", "")

                if quote not in quote_set:
                    continue
                if prod.get("trading_disabled", True):
                    continue
                if prod.get("is_disabled", False):
                    continue
                if str(prod.get("status", "")).lower() != "online":
                    continue
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

    # ─── Product catalogue cache ─────────────────────────────────────────

    def _refresh_product_cache(self) -> list[dict]:
        """Refresh the full product catalogue from Coinbase (single bulk call).

        Cached for 24 hours.  Only rebuilds ``_valid_product_ids`` — the set
        of product IDs that actually exist on Coinbase.
        """
        import time as _time
        now = _time.time()
        with self._product_cache_lock:
            if self._product_cache and (now - self._product_cache_ts) < self._PRODUCT_CACHE_TTL:
                return self._product_cache
            if not self._rest_client:
                return self._product_cache or []
            try:
                resp = self._throttled_request("get_products")
                pdata = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
                products = pdata.get("products", [])
                self._product_cache = products
                self._product_cache_ts = now

                valid_ids: set[str] = set()
                for prod in products:
                    pid = prod.get("product_id", "")
                    if pid:
                        valid_ids.add(pid)
                self._valid_product_ids = valid_ids
                logger.info(
                    f"📦 Product catalogue refreshed: {len(valid_ids)} products"
                )
            except Exception as e:
                logger.warning(f"⚠️ Product catalogue refresh failed: {e}")
            return self._product_cache

    def _is_known_product(self, pair: str) -> bool:
        """Return True if *pair* exists in the Coinbase product catalogue."""
        self._refresh_product_cache()
        return pair in self._valid_product_ids

    def discover_all_pairs_detailed(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: set[str] | None = None,
        only_trade: set[str] | None = None,
        include_crypto_quotes: bool = False,
    ) -> list[dict]:
        """
        Discover ALL tradable pairs on Coinbase with detailed metadata.
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

            if prod.get("trading_disabled", True):
                continue
            if prod.get("is_disabled", False):
                continue
            if str(prod.get("status", "")).lower() != "online":
                continue

            is_target_quote = quote in quote_set
            is_crypto_quote = include_crypto_quotes and quote not in _KNOWN_QUOTES
            if not is_target_quote and not is_crypto_quote:
                continue

            if product_id in never_trade or base in never_trade:
                continue
            if only_trade and product_id not in only_trade and base not in only_trade:
                continue

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

        # Check reverse (e.g. BTC-ETH → buy base from quote side)
        reverse = f"{quote}-{base}"
        if reverse in product_map:
            return (reverse, "buy")  # buy base from quote side

        return None
