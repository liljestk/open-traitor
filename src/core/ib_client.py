"""
Interactive Brokers Exchange Client – trades US/EU equities & options via IBKR.

Currently implements **paper mode only**.  Live trading will be added once
the IB Gateway / TWS API connection is configured.

The paper engine uses the same balance / order-tracking pattern as
NordnetClient's paper mode but with USD-denominated defaults and
IBKR's tiered commission schedule.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("ib_client")

# Try importing ib_insync for real IB API connectivity.
# Paper mode works without it.
try:
    from ib_insync import IB as _IB  # noqa: F401
    _HAS_IB_INSYNC = True
except ImportError:
    _HAS_IB_INSYNC = False

from src.core.exchange_client import ExchangeClient
from src.core.paper_trading import PaperTradingMixin


class IBClient(PaperTradingMixin, ExchangeClient):
    """
    Exchange client for **Interactive Brokers** (US/EU equities, options, futures).

    In *paper mode* the client simulates order execution, tracking balances
    internally.  Real-mode support requires IB Gateway / TWS and the
    ``ib_insync`` library.
    """

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def exchange_id(self) -> str:
        return "ibkr"

    @property
    def asset_class(self) -> str:
        return "equity"

    # ── Lifecycle ────────────────────────────────────────────────────────

    def __init__(
        self,
        paper_mode: bool = True,
        paper_slippage_pct: float = 0.0003,
        initial_balance: float = 100_000.0,
        ib_host: str = "127.0.0.1",
        ib_port: int = 4002,        # 4001 = live TWS, 4002 = paper TWS / IB Gateway
        ib_client_id: int = 1,
    ):
        self.paper_mode = paper_mode
        self._native_currency = os.environ.get("IBKR_CURRENCY", "USD")

        # IB Gateway / TWS connection parameters
        self._ib_host = ib_host
        self._ib_port = ib_port
        self._ib_client_id = ib_client_id

        # Paper-mode state via mixin
        self._init_paper(
            initial_balances={self._native_currency: initial_balance},
            slippage_pct=paper_slippage_pct,
        )
        # IBKR US tiered commission: ~$0.0035/share, min $0.35, max 1% of trade
        self._paper_fee_per_share: float = 0.0035
        self._paper_fee_min: float = 0.35
        self._paper_fee_max_pct: float = 0.01
        self._last_prices: dict[str, float] = {}

        if not paper_mode:
            self._init_live_session()
        else:
            logger.info(
                f"IBClient initialised in 📝 PAPER mode "
                f"({self._native_currency} {initial_balance:,.0f})"
            )

    # ------------------------------------------------------------------
    # Live-session placeholder
    # ------------------------------------------------------------------

    def _init_live_session(self) -> None:
        """Connect to IB Gateway / TWS via ib_insync."""
        if not _HAS_IB_INSYNC:
            raise ImportError(
                "Live IB trading requires the 'ib_insync' package. "
                "Install with: pip install ib_insync"
            )
        raise NotImplementedError(
            "Live IB trading is not yet implemented. Use paper_mode=True."
        )

    # ── Connection / account methods ─────────────────────────────────────

    def check_connection(self) -> dict[str, Any]:
        if self.paper_mode:
            return {
                "ok": True,
                "mode": "paper",
                "message": "Interactive Brokers paper-mode active",
                "non_zero_accounts": sum(
                    1 for v in self._paper_balance.values() if v > 0
                ),
            }
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
        In paper mode, return last recorded price or 0.
        In live mode, this will query IB market data.
        """
        if self.paper_mode:
            return self._last_prices.get(pair.upper(), 0.0)
        raise NotImplementedError

    def set_price(self, pair: str, price: float) -> None:
        """Helper for tests / paper mode: set the current price for a pair."""
        self._last_prices[pair.upper()] = price

    def get_candles(
        self, product_id: str, granularity: str = "ONE_DAY", limit: int = 200
    ) -> list[dict]:
        """
        Return OHLCV candles. In paper mode, returns an empty list.
        Live mode will query IB historical data.
        """
        if self.paper_mode:
            logger.warning(
                f"get_candles({product_id}) — paper mode returns no candle data; "
                f"analysis pipeline will have no signals until a live data source is configured"
            )
            return []
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
        Return product metadata.  For equities the base/quote split is
        the ticker itself vs the native currency.  Paper mode returns
        sensible defaults.
        """
        if self.paper_mode:
            parts = product_id.upper().split("-")
            base = parts[0] if parts else product_id.upper()
            quote = parts[1] if len(parts) > 1 else self._native_currency
            return {
                "base_currency_id": base,
                "quote_currency_id": quote,
                "base_min_size": "1",       # equities: min 1 share
                "base_max_size": "100000",
                "base_increment": "1",      # whole shares only
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
        if self.paper_mode:
            self._last_prices.setdefault(pair.upper(), price)
            return self.place_market_order(
                pair, side, size, amount_is_base=True, client_oid=client_oid
            )
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
        return self.paper_get_open_orders()

    # ── Paper trading engine ─────────────────────────────────────────────

    def _compute_fee(self, shares: int, trade_value: float) -> float:
        """
        IBKR US tiered commission:
          $0.0035 per share, min $0.35, max 1% of trade value.
        """
        raw = shares * self._paper_fee_per_share
        fee = max(self._paper_fee_min, raw)
        fee = min(fee, trade_value * self._paper_fee_max_pct)
        return fee

    def _paper_market_buy(
        self, pair: str, amount: float, amount_is_base: bool
    ) -> dict:
        pair = pair.upper()
        price = self.get_current_price(pair)
        if price <= 0:
            return {"success": False, "error": f"No price for {pair}"}

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

        fee = self._compute_fee(shares, cost)
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
            f"(cost {cost:.2f}, fee {fee:.4f})"
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
        fee = self._compute_fee(shares, proceeds)
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
            f"(proceeds {proceeds:.2f}, fee {fee:.4f})"
        )
        return order

    # ── Portfolio helpers ────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """Compute total portfolio value in native currency."""
        total = 0.0
        with self._paper_balance_lock:
            for asset, amount in self._paper_balance.items():
                if asset == self._native_currency:
                    total += amount
                else:
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
                if abs(actual - expected_qty) > 0.5:
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
        cannot query the IB API for the full instrument list without a
        live connection.
        """
        if only_trade:
            return list(only_trade)
        if self.paper_mode:
            logger.warning(
                "discover_all_pairs: paper mode without only_trade — "
                "returning empty list. Configure trading.only_trade in ibkr.yaml."
            )
            return []
        raise NotImplementedError

    def adapt_pairs_to_account(
        self, pairs: list[str], native_currency: str
    ) -> list[str]:
        """IB pairs use the configured currency — no adaptation needed."""
        return pairs
