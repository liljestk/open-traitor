"""
Nordnet Exchange Client – trades OMX Stockholm equities via Nordnet.

Currently implements **paper mode only**.  Live trading will be added once
Nordnet External API access (OAuth2 / API-key) is available.

The paper engine uses the same balance / order-tracking pattern as
CoinbaseClient's paper mode but with equity-oriented defaults (SEK,
flat + percent fee schedule).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("nordnet_client")

# Try importing a real REST helper.  This will be implemented once we have
# Nordnet API credentials; for now only paper mode is functional.
try:
    import requests as _requests  # noqa: F401
except ImportError:  # pragma: no cover
    _requests = None  # type: ignore[assignment]

from src.core.exchange_client import ExchangeClient
from src.core.paper_trading import PaperTradingMixin
from src.core import equity_feed


class NordnetClient(PaperTradingMixin, ExchangeClient):
    """
    Exchange client for **Nordnet** (OMX Stockholm equities).

    In *paper mode* the client simulates order execution, tracking balances
    internally.  Real-mode support requires Nordnet API credentials and will
    be added later.
    """

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def exchange_id(self) -> str:
        return "nordnet"

    @property
    def asset_class(self) -> str:
        return "equity"

    # ── Lifecycle ────────────────────────────────────────────────────────

    def __init__(
        self,
        paper_mode: bool = True,
        paper_slippage_pct: float = 0.0005,
        initial_balance_sek: float = 100_000.0,
    ):
        self.paper_mode = paper_mode
        self._native_currency = "SEK"

        # Paper-mode state via mixin
        self._init_paper(
            initial_balances={self._native_currency: initial_balance_sek},
            slippage_pct=paper_slippage_pct,
        )
        self._paper_fee_flat: float = 39.0   # SEK minimum commission
        self._paper_fee_pct: float = 0.0015  # 0.15 %
        self._last_prices: dict[str, float] = {}

        if not paper_mode:
            # Future: initialise session / REST client against Nordnet API
            self._init_live_session()
        else:
            logger.info(
                f"NordnetClient initialised in 📝 PAPER mode "
                f"({self._native_currency} {initial_balance_sek:,.0f})"
            )

    # ------------------------------------------------------------------
    # Live-session placeholder
    # ------------------------------------------------------------------

    def _init_live_session(self) -> None:
        """Placeholder for OAuth2 / basic-auth session init against Nordnet."""
        raise NotImplementedError(
            "Live Nordnet trading is not yet implemented. Use paper_mode=True."
        )

    # ── Connection / account methods ─────────────────────────────────────

    def check_connection(self) -> dict[str, Any]:
        if self.paper_mode:
            return {
                "ok": True,
                "mode": "paper",
                "message": "Nordnet paper-mode active",
                "non_zero_accounts": sum(
                    1 for v in self._paper_balance.values() if v > 0
                ),
            }
        # Future: ping Nordnet API and verify token
        return {
            "ok": False,
            "mode": "live",
            "message": "Live mode not yet implemented",
            "error": "not_implemented",
        }

    def get_accounts(self) -> list[dict[str, Any]]:
        if self.paper_mode:
            return self.paper_get_accounts()
        raise NotImplementedError

    @property
    def balance(self) -> dict[str, float]:
        return self.paper_get_all_balances()

    def detect_native_currency(self) -> str:
        return self._native_currency

    # ── Market data ──────────────────────────────────────────────────────

    def get_current_price(self, pair: str) -> float:
        """
        In paper mode, fetch live price via Yahoo Finance (yfinance).
        In live mode, this will call the Nordnet price endpoint.
        """
        if self.paper_mode:
            # Try cached manual price first, then Yahoo Finance
            cached = self._last_prices.get(pair.upper())
            if cached and cached > 0:
                return cached
            price = equity_feed.get_current_price(pair)
            if price > 0:
                self._last_prices[pair.upper()] = price
            return price
        raise NotImplementedError

    def set_price(self, pair: str, price: float) -> None:
        """
        Helper for tests / paper mode: set the current price for a pair.
        """
        self._last_prices[pair.upper()] = price

    def get_candles(
        self, product_id: str, granularity: str = "ONE_DAY", limit: int = 200
    ) -> list[dict]:
        """
        Return OHLCV candles. In paper mode, returns an empty list.
        Live mode will query Nordnet historical data.
        """
        if self.paper_mode:
            candles = equity_feed.get_candles(product_id, granularity, limit)
            if not candles:
                logger.debug(
                    f"get_candles({product_id}) — paper mode: yfinance returned no data"
                )
            return candles
        raise NotImplementedError

    def get_market_trades(self, product_id: str, limit: int = 50) -> list[dict]:
        if self.paper_mode:
            return []
        raise NotImplementedError

    def get_product_book(self, product_id: str, limit: int = 10) -> dict:
        if self.paper_mode:
            return {"bids": [], "asks": []}
        raise NotImplementedError

    def get_product(self, product_id: str) -> Optional[dict]:
        """
        Return product metadata. For equities the base/quote split is the
        ticker itself vs SEK.  Paper mode returns sensible defaults.
        """
        if self.paper_mode:
            # For equity pairs like VOLV_B-SEK: base = VOLV_B, quote = SEK
            parts = product_id.upper().split("-")
            base = parts[0] if parts else product_id.upper()
            quote = parts[1] if len(parts) > 1 else self._native_currency
            return {
                "base_currency_id": base,
                "quote_currency_id": quote,
                "base_min_size": "1",      # equities: min 1 share
                "base_max_size": "100000",
                "base_increment": "1",     # whole shares only
                "quote_increment": "0.01",
            }
        raise NotImplementedError

    # ── Order execution ──────────────────────────────────────────────────

    def place_market_order(
        self,
        pair: str,
        side: str,
        amount: float,
        amount_is_base: bool = False,
        client_oid: str = "",
    ) -> dict:
        if self.paper_mode:
            if side.upper() == "BUY":
                return self._paper_market_buy(pair, amount, amount_is_base)
            else:
                return self._paper_market_sell(pair, amount, amount_is_base)
        raise NotImplementedError

    def place_limit_order(
        self,
        pair: str,
        side: str,
        price: float,
        size: float,
        client_oid: str = "",
    ) -> dict:
        # Paper mode doesn't simulate limit orders properly — treat as market
        if self.paper_mode:
            self._last_prices.setdefault(pair.upper(), price)
            return self.place_market_order(pair, side, size, amount_is_base=True, client_oid=client_oid)
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> dict:
        if self.paper_mode:
            return {"success": False, "error": "Paper orders are instant-fill"}
        raise NotImplementedError

    def get_order(self, order_id: str) -> Optional[dict]:
        if self.paper_mode:
            return self.paper_get_order(order_id)
        raise NotImplementedError

    def get_open_orders(self, pair: str | None = None) -> list[dict]:
        # Paper orders are all instant-fill → nothing is ever "open"
        return self.paper_get_open_orders()

    # ── Paper trading engine ─────────────────────────────────────────────

    def _compute_fee(self, trade_value_sek: float) -> float:
        """Nordnet-style fee: max(flat_min, value × pct)."""
        return max(self._paper_fee_flat, trade_value_sek * self._paper_fee_pct)

    def _paper_market_buy(
        self, pair: str, amount: float, amount_is_base: bool
    ) -> dict:
        pair = pair.upper()
        price = self.get_current_price(pair)
        if price <= 0:
            return {"success": False, "error": f"No price for {pair}"}

        # Apply slippage (buy → price goes up slightly)
        exec_price = price * (1 + self._paper_slippage_pct)

        parts = pair.split("-")
        base = parts[0] if parts else pair
        quote = parts[1] if len(parts) > 1 else self._native_currency

        if amount_is_base:
            shares = int(amount)
            if shares < 1:
                return {"success": False, "error": "Must buy at least 1 share"}
            cost = shares * exec_price
        else:
            cost = amount
            shares = int(cost / exec_price)
            if shares < 1:
                return {"success": False, "error": "Insufficient amount for 1 share"}
            cost = shares * exec_price

        fee = self._compute_fee(cost)
        total_cost = cost + fee

        try:
            self.paper_adjust_balance(quote, -total_cost)
            self.paper_adjust_balance(base, float(shares))
        except ValueError as e:
            return {"success": False, "error": str(e)}

        order = {
            "success": True,
            "order_id": self.paper_generate_order_id(),
            "status": "FILLED",
            "side": "BUY",
            "pair": pair,
            "filled_size": str(shares),
            "filled_value": str(cost),
            "average_filled_price": str(exec_price),
            "fee": str(fee),
            "ts": self.paper_now_iso(),
        }
        self.paper_record_order(order)

        logger.info(
            f"📝 Paper BUY {shares} × {pair} @ {exec_price:.2f} {quote} "
            f"(cost {cost:.2f}, fee {fee:.2f})"
        )
        return order

    def _paper_market_sell(
        self, pair: str, amount: float, amount_is_base: bool
    ) -> dict:
        pair = pair.upper()
        price = self.get_current_price(pair)
        if price <= 0:
            return {"success": False, "error": f"No price for {pair}"}

        exec_price = price * (1 - self._paper_slippage_pct)

        parts = pair.split("-")
        base = parts[0] if parts else pair
        quote = parts[1] if len(parts) > 1 else self._native_currency

        if amount_is_base:
            shares = int(amount)
        else:
            shares = int(amount / exec_price)

        if shares < 1:
            return {"success": False, "error": "Must sell at least 1 share"}

        try:
            self.paper_adjust_balance(base, -float(shares))
        except ValueError as e:
            return {"success": False, "error": str(e)}

        proceeds = shares * exec_price
        fee = self._compute_fee(proceeds)
        net = proceeds - fee
        self.paper_adjust_balance(quote, net)

        order = {
            "success": True,
            "order_id": self.paper_generate_order_id(),
            "status": "FILLED",
            "side": "SELL",
            "pair": pair,
            "filled_size": str(shares),
            "filled_value": str(proceeds),
            "average_filled_price": str(exec_price),
            "fee": str(fee),
            "ts": self.paper_now_iso(),
        }
        self.paper_record_order(order)

        logger.info(
            f"📝 Paper SELL {shares} × {pair} @ {exec_price:.2f} {quote} "
            f"(proceeds {proceeds:.2f}, fee {fee:.2f})"
        )
        return order

    # ── Portfolio helpers ────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """Compute total portfolio value in SEK (cash + held shares at last price)."""
        total = 0.0
        with self._paper_balance_lock:
            for asset, amount in self._paper_balance.items():
                if asset == self._native_currency:
                    total += amount
                else:
                    # Try to find a price for ASSET-SEK
                    pair = f"{asset}-{self._native_currency}"
                    px = self._last_prices.get(pair, 0.0)
                    total += amount * px
        return total

    def reconcile_positions(self, expected: dict[str, float]) -> dict:
        mismatches: list[dict] = []
        matched = 0
        with self._paper_balance_lock:
            for pair, expected_qty in expected.items():
                parts = pair.upper().split("-")
                base = parts[0]
                actual = self._paper_balance.get(base, 0.0)
                if abs(actual - expected_qty) > 0.5:  # equity tolerance: 0.5 shares
                    mismatches.append({
                        "pair": pair,
                        "expected": expected_qty,
                        "actual": actual,
                    })
                else:
                    matched += 1
        return {"mismatches": mismatches, "matched": matched, "total": len(expected)}

    # ── Pair discovery ───────────────────────────────────────────────────

    def discover_all_pairs(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: list[str] | None = None,
        only_trade: list[str] | None = None,
    ) -> list[str]:
        """
        In paper mode, return the ``only_trade`` list (if set) since we
        cannot query the Nordnet API for the full instrument list.
        Live mode will fetch instrument lists from Nordnet.
        """
        if only_trade:
            return list(only_trade)
        if self.paper_mode:
            pairs = equity_feed.discover_pairs(
                exchange_id=self.exchange_id,
                quote_currencies=quote_currencies,
                never_trade=list(never_trade) if never_trade else None,
            )
            logger.info(
                f"discover_all_pairs: paper mode discovered {len(pairs)} equity pairs via yfinance"
            )
            return pairs
        raise NotImplementedError

    def discover_all_pairs_detailed(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: list[str] | None = None,
        only_trade: list[str] | None = None,
        include_crypto_quotes: bool = False,
    ) -> list[dict]:
        """Return detailed pair metadata for the universe scanner.

        Paper mode uses Yahoo Finance; live mode will use Nordnet API.
        """
        if self.paper_mode:
            return equity_feed.discover_pairs_detailed(
                exchange_id=self.exchange_id,
                quote_currencies=quote_currencies,
                never_trade=list(never_trade) if never_trade else None,
                only_trade=list(only_trade) if only_trade else None,
            )
        raise NotImplementedError

    def adapt_pairs_to_account(self, pairs: list[str], native_currency: str) -> list[str]:
        """Nordnet pairs are already in SEK — no adaptation needed."""
        return pairs
