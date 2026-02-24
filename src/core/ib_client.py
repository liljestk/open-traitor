"""
Interactive Brokers Exchange Client – trades US/EU equities & options via IBKR.

Supports both **paper mode** (simulated execution via Yahoo Finance prices)
and **live mode** (real execution via IB Gateway / TWS + ib_insync).

The paper engine uses the same balance / order-tracking pattern as
CoinbaseClient's paper mode but with USD-denominated defaults and
IBKR's tiered commission schedule.
"""

from __future__ import annotations

import math
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
from src.core import equity_feed


def _safe_float(val) -> float:
    """Convert IB ticker value to float, returning 0.0 for NaN/None."""
    if val is None:
        return 0.0
    try:
        f = float(val)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


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
        ib_port: int = 4001,        # 4001 = live IB Gateway, 4002 = paper TWS
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
        self._known_pairs: set[str] = set()

        if not paper_mode:
            self._init_live_session()
        else:
            logger.info(
                f"IBClient initialised in 📝 PAPER mode "
                f"({self._native_currency} {initial_balance:,.0f})"
            )

    # ------------------------------------------------------------------
    # Known-pairs bookkeeping
    # ------------------------------------------------------------------

    def seed_known_pairs(self, pairs: list[str]) -> None:
        """Seed the known-pairs set with configured / discovered pairs.

        Called by main.py after boot-time pair resolution so that
        ``discover_all_pairs_detailed()`` always has a baseline universe
        even if the IB Scanner is unavailable.
        """
        self._known_pairs.update(p.upper() for p in pairs)
        logger.debug(f"Seeded {len(self._known_pairs)} known pairs")

    # ------------------------------------------------------------------
    # Live-session setup
    # ------------------------------------------------------------------

    def _init_live_session(self) -> None:
        """Connect to IB Gateway / TWS via ib_insync."""
        if not _HAS_IB_INSYNC:
            raise ImportError(
                "Live IB trading requires the 'ib_insync' package. "
                "Install with: pip install ib_insync"
            )
        # ib_insync requires an asyncio event loop in the current thread.
        # When called from non-main threads (e.g. FastAPI/AnyIO workers),
        # no event loop exists — create one so ib_insync can function.
        import asyncio
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        self.ib = _IB()
        try:
            self.ib.connect(self._ib_host, self._ib_port, clientId=self._ib_client_id)
            logger.info(f"✅ IBClient LIVE connected to {self._ib_host}:{self._ib_port} (Client ID: {self._ib_client_id})")
        except Exception as e:
            logger.error(f"❌ IBClient LIVE connection failed: {e}")
            raise

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
        
        is_connected = getattr(self, "ib", None) and self.ib.isConnected()
        if not is_connected:
            return {
                "ok": False,
                "mode": "live",
                "message": "IB Gateway not connected",
                "error": "disconnected",
            }
        
        accounts = self.ib.managedAccounts()
        return {
            "ok": True,
            "mode": "live",
            "message": f"IB Gateway live connected. Accounts: {', '.join(accounts)}",
            "non_zero_accounts": len(accounts),
            "total_accounts": len(accounts),
        }

    def get_accounts(self) -> list[dict[str, Any]]:
        if self.paper_mode:
            return self.paper_get_accounts()
        
        accounts_data = []
        for acc in self.ib.managedAccounts():
            vals = self.ib.accountValues(acc)
            acc_info = {"id": acc, "currency": self._native_currency, "balances": {}}
            for v in vals:
                if v.tag == "NetLiquidationByCurrency" and v.currency == self._native_currency:
                    acc_info["balances"][self._native_currency] = float(v.value)
                elif v.tag == "CashBalance" and v.currency == self._native_currency:
                    acc_info["available_cash"] = float(v.value)
            accounts_data.append(acc_info)
        return accounts_data

    @property
    def balance(self) -> dict[str, float]:
        if self.paper_mode:
            return self.paper_get_all_balances()
        
        # Return portfolio positions and cash
        balances = {"USD": 0.0, "EUR": 0.0}
        
        vals = self.ib.accountValues()
        for v in vals:
            if v.tag == "CashBalance":
                balances[v.currency] = float(v.value)

        for pos in self.ib.positions():
            # Ticker symbol in live mode is position.contract.symbol
            sym = pos.contract.symbol
            balances[sym] = float(pos.position)
        
        # Make sure native currency is present
        balances.setdefault(self._native_currency, 0.0)
        return balances

    def detect_native_currency(self) -> str:
        return self._native_currency

    # ── Market data ──────────────────────────────────────────────────────

    def _get_contract(self, pair: str):
        """
        Helper to create and qualify an ib_insync Stock contract.
        
        Pair format: 'AAPL-USD', 'MSFT-EUR', or just 'AAPL'.
        For US stocks, currency should be USD even if the account holds EUR
        (IBKR handles FX conversion automatically on trade execution).
        """
        from ib_insync import Stock
        parts = pair.upper().split("-")
        symbol = parts[0]
        currency = parts[1] if len(parts) > 1 else "USD"
        
        # US equities always trade in USD on SMART routing
        contract = Stock(symbol, 'SMART', currency)
        qualified = self.ib.qualifyContracts(contract)
        
        if not qualified or not contract.conId:
            # Retry with USD if alternate currency failed
            if currency != "USD":
                contract = Stock(symbol, 'SMART', 'USD')
                qualified = self.ib.qualifyContracts(contract)
                if qualified and contract.conId:
                    logger.debug(f"Contract {symbol} not available in {currency}, using USD")
            
            # Try specific exchanges for European stocks
            if not qualified or not contract.conId:
                for exch_currency in ["EUR", "GBP", "CHF"]:
                    if exch_currency == currency:
                        continue
                    contract = Stock(symbol, 'SMART', exch_currency)
                    qualified = self.ib.qualifyContracts(contract)
                    if qualified and contract.conId:
                        logger.debug(f"Contract {symbol} found with currency {exch_currency}")
                        break
        
        return contract

    def get_current_price(self, pair: str) -> float:
        """
        In paper mode, fetch live price via Yahoo Finance (yfinance).
        In live mode, queries IB market data.
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
        
        try:
            contract = self._get_contract(pair)
            tickers = self.ib.reqTickers(contract)
            if tickers:
                t = tickers[0]
                price = t.last if t.last == t.last and t.last > 0 else t.close
                if price == price and price > 0:  # Check for NaN and > 0
                    return float(price)
        except Exception as e:
            logger.warning(f"Failed to fetch live price for {pair}: {e}")
        return 0.0

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
            candles = equity_feed.get_candles(product_id, granularity, limit)
            if not candles:
                logger.debug(
                    f"get_candles({product_id}) — paper mode: yfinance returned no data"
                )
            return candles
        
        try:
            contract = self._get_contract(product_id)
            barSize = "1 day"
            duration = f"{min(limit, 365)} D"
            
            if granularity == "ONE_HOUR":
                barSize = "1 hour"
                duration = f"{max(1, limit // 8)} D"
            elif granularity == "ONE_MINUTE":
                barSize = "1 min"
                duration = f"{max(1, limit * 60)} S"

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr=duration,
                barSizeSetting=barSize,
                whatToShow='TRADES',
                useRTH=True
            )
            
            return [
                {
                    "time": bar.date.isoformat() if hasattr(bar.date, "isoformat") else str(bar.date),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
                for bar in bars
            ]
        except Exception as e:
            logger.warning(f"Failed to fetch candles for {product_id}: {e}")
            return []

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
        
        from ib_insync import MarketOrder
        try:
            contract = self._get_contract(pair)
            
            if not amount_is_base:
                # IBKR requires quantity in shares (base asset)
                price = self.get_current_price(pair)
                if price <= 0:
                    return {"success": False, "error": f"Invalid price for {pair}"}
                shares = int(amount / price)
            else:
                shares = int(amount)
                
            if shares < 1:
                return {"success": False, "error": "Order size must be at least 1 share"}

            order = MarketOrder(side.upper(), shares)
            if client_oid:
                order.orderRef = client_oid
                
            trade = self.ib.placeOrder(contract, order)
            # We don't wait for execution here, just return the order details
            return {
                "success": True,
                "order_id": str(trade.order.orderId),
                "status": "OPEN",  # It might execute soon, but returning OPEN
                "side": side.upper(),
                "pair": pair,
                "filled_size": "0",
                "filled_value": "0",
                "average_filled_price": "0",
                "fee": "0",
                "ts": self.paper_now_iso(),
            }
        except Exception as e:
            logger.error(f"Live market order failed: {e}")
            return {"success": False, "error": str(e)}

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

        from ib_insync import LimitOrder
        try:
            contract = self._get_contract(pair)
            shares = int(size)
            if shares < 1:
                return {"success": False, "error": "Order size must be at least 1 share"}

            order = LimitOrder(side.upper(), shares, round(price, 2))
            if client_oid:
                order.orderRef = client_oid
                
            trade = self.ib.placeOrder(contract, order)
            return {
                "success": True,
                "order_id": str(trade.order.orderId),
                "status": "OPEN",
                "side": side.upper(),
                "pair": pair,
                "filled_size": "0",
                "filled_value": "0",
                "average_filled_price": "0",
                "fee": "0",
                "ts": self.paper_now_iso(),
            }
        except Exception as e:
            logger.error(f"Live limit order failed: {e}")
            return {"success": False, "error": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        if self.paper_mode:
            return {"success": False, "error": "Paper orders are instant-fill"}
        try:
            for trade in self.ib.openTrades():
                if str(trade.order.orderId) == order_id:
                    self.ib.cancelOrder(trade.order)
                    return {"success": True, "order_id": order_id}
            return {"success": False, "error": "Order not found or not active"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_order(self, order_id: str) -> Optional[dict]:
        if self.paper_mode:
            return self.paper_get_order(order_id)
        # Scan through trades
        for trade in self.ib.trades():
            if str(trade.order.orderId) == order_id:
                return {
                    "order_id": str(trade.order.orderId),
                    "status": trade.orderStatus.status.upper(),
                    "side": trade.order.action,
                    "filled_size": str(trade.orderStatus.filled),
                    "average_filled_price": str(trade.orderStatus.avgFillPrice),
                }
        return None

    def get_open_orders(self, pair: str | None = None) -> list[dict]:
        if self.paper_mode:
            return self.paper_get_open_orders()
        open_orders = []
        for trade in self.ib.openTrades():
            sym = trade.contract.symbol
            if pair and sym not in pair:
                continue
            open_orders.append({
                "order_id": str(trade.order.orderId),
                "pair": f"{sym}-{self._native_currency}",
                "side": trade.order.action,
                "size": str(trade.order.totalQuantity),
                "price": str(getattr(trade.order, 'lmtPrice', 0)),
                "status": trade.orderStatus.status.upper()
            })
        return open_orders

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
        if self.paper_mode:
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
        else:
            vals = self.ib.accountValues()
            for v in vals:
                if v.tag == "NetLiquidationByCurrency" and v.currency == self._native_currency:
                    return float(v.value)
            
            # Fallback if specific currency is not found:
            for v in vals:
                if v.tag == "NetLiquidation":
                    return float(v.value)
            return 0.0

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
        In live mode, use IB Scanner (Top % Gainers) to find active tickers.
        Returns pairs with their actual trading currency (usually USD for US stocks).
        """
        if only_trade:
            result = list(only_trade)
            self._known_pairs.update(p.upper() for p in result)
            return result
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

        # ── Live mode: IB Scanner + known-pairs fallback ─────────────
        never_set = set(never_trade) if never_trade else set()
        found: set[str] = set()

        # 1) Always include previously-known pairs (seeded from YAML config)
        for p in self._known_pairs:
            if p not in never_set:
                found.add(p)

        # 2) Augment with IB Scanner results (top % gainers)
        try:
            from ib_insync import ScannerSubscription
            sub = ScannerSubscription(
                instrument='STK',
                locationCode='STK.US.MAJOR',
                scanCode='TOP_PERC_GAIN',
            )
            scan_data = self.ib.reqScannerData(sub)
            for item in scan_data:
                contract = item.contractDetails.contract
                sym = contract.symbol
                cur = contract.currency or "USD"
                pair = f"{sym}-{cur}"
                if pair not in never_set:
                    found.add(pair)
            logger.info(f"IB Scanner found {len(scan_data)} tickers; merged total = {len(found)}")
        except Exception as e:
            logger.warning(f"IB Scanner discovery failed (using known pairs only): {e}")

        result = sorted(found)
        self._known_pairs.update(result)
        return result

    def get_news(self, pair: str, limit: int = 5) -> list[dict]:
        """Fetch news for a specific pair via IBKR News API."""
        if self.paper_mode:
            return []

        try:
            contract = self._get_contract(pair)
            self.ib.qualifyContracts(contract)

            # Discover available news providers if not cached
            if not hasattr(self, '_news_providers_str') or not self._news_providers_str:
                try:
                    providers = self.ib.reqNewsProviders()
                    if providers:
                        self._news_providers_str = '+'.join(p.code for p in providers)
                        logger.info(f"IBKR news providers: {self._news_providers_str}")
                    else:
                        self._news_providers_str = 'BRF+DJNL+BST'  # Common defaults
                except Exception:
                    self._news_providers_str = 'BRF+DJNL+BST'

            news = self.ib.reqHistoricalNews(
                conId=contract.conId,
                providerCodes=self._news_providers_str,
                startDateTime='',
                endDateTime='',
                totalResults=limit
            )

            results = []
            for n in news:
                results.append({
                    "time": n.time.isoformat() if hasattr(n.time, "isoformat") else str(n.time),
                    "headline": n.headline,
                    "provider": n.providerCode,
                    "article_id": n.articleId
                })
            return results
        except Exception as e:
            logger.error(f"Failed to fetch IBKR news for {pair}: {e}")
            return []

    def get_news_providers(self) -> list[str]:
        """Return available news provider codes from IBKR."""
        if self.paper_mode:
            return []
        try:
            providers = self.ib.reqNewsProviders()
            return [p.code for p in providers]
        except Exception as e:
            logger.error(f"Failed to fetch IBKR news providers: {e}")
            return []

    def get_news_article_body(self, provider_code: str, article_id: str) -> str:
        """Fetch the full text body of a news article by ID."""
        if self.paper_mode:
            return ""
        try:
            article = self.ib.reqNewsArticle(provider_code, article_id)
            return article.articleText if article else ""
        except Exception as e:
            logger.error(f"Failed to fetch article body {article_id}: {e}")
            return ""

    def discover_all_pairs_detailed(
        self,
        quote_currencies: list[str] | None = None,
        never_trade: list[str] | None = None,
        only_trade: list[str] | None = None,
        include_crypto_quotes: bool = False,
    ) -> list[dict]:
        """Return detailed pair metadata for the universe scanner.

        Paper mode uses Yahoo Finance; live mode will use IB Scanner.
        """
        if self.paper_mode:
            return equity_feed.discover_pairs_detailed(
                exchange_id=self.exchange_id,
                quote_currencies=quote_currencies,
                never_trade=list(never_trade) if never_trade else None,
                only_trade=list(only_trade) if only_trade else None,
            )

        # ── Live mode: enrich pairs with IB market-data snapshots ────
        pairs = self.discover_all_pairs(
            quote_currencies=quote_currencies,
            never_trade=never_trade,
            only_trade=only_trade,
        )
        if not pairs:
            logger.warning("discover_all_pairs_detailed: no pairs to enrich")
            return []

        # Qualify contracts (skip any that fail)
        contracts_map: dict[str, Any] = {}
        for pair in pairs:
            try:
                contract = self._get_contract(pair)
                if contract.conId:
                    contracts_map[pair] = contract
                else:
                    logger.debug(f"Skipping {pair}: contract has no conId")
            except Exception as e:
                logger.debug(f"Skipping {pair}: contract qualification failed: {e}")

        if not contracts_map:
            logger.warning(
                "discover_all_pairs_detailed: no contracts qualified — "
                "returning pairs with zero metadata"
            )
            return [
                self._empty_pair_meta(pair) for pair in pairs
            ]

        # Request market-data snapshots for all qualified contracts
        ticker_by_conid: dict[int, Any] = {}
        try:
            self.ib.reqMarketDataType(3)  # 3 = delayed (free, 15-min lag)
            tickers = self.ib.reqTickers(*contracts_map.values())
            ticker_by_conid = {
                t.contract.conId: t for t in tickers if t.contract
            }
        except Exception as e:
            logger.error(f"reqTickers batch failed: {e}")

        results: list[dict] = []
        for pair, contract in contracts_map.items():
            t = ticker_by_conid.get(contract.conId)

            last_price = 0.0
            prev_close = 0.0
            volume = 0.0

            if t:
                last_price = (
                    _safe_float(t.last)
                    or _safe_float(t.close)
                    or _safe_float(t.prevClose)
                )
                prev_close = _safe_float(t.prevClose)
                volume = _safe_float(t.volume)

            pct_change = 0.0
            if prev_close > 0 and last_price > 0:
                pct_change = ((last_price - prev_close) / prev_close) * 100.0

            notional_vol = (
                volume * last_price if (last_price > 0 and volume > 0) else 0.0
            )

            parts = pair.upper().split("-")
            base = parts[0]
            quote = parts[1] if len(parts) > 1 else "USD"

            results.append({
                "product_id": pair,
                "base_currency_id": base,
                "quote_currency_id": quote,
                "base_min_size": "1",
                "quote_min_size": "1.00",
                "volume_24h": str(round(notional_vol, 2)),
                "price_percentage_change_24h": str(round(pct_change, 4)),
            })

        # Also include unqualified pairs so they appear in the universe
        qualified_set = set(contracts_map.keys())
        for pair in pairs:
            if pair not in qualified_set:
                results.append(self._empty_pair_meta(pair))

        logger.info(
            f"discover_all_pairs_detailed: enriched {len(results)} pairs "
            f"with IB market data ({len(ticker_by_conid)} had snapshots)"
        )
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_pair_meta(pair: str) -> dict:
        """Return a universe-scanner dict with zero metadata for *pair*."""
        parts = pair.upper().split("-")
        base = parts[0]
        quote = parts[1] if len(parts) > 1 else "USD"
        return {
            "product_id": pair,
            "base_currency_id": base,
            "quote_currency_id": quote,
            "base_min_size": "1",
            "quote_min_size": "1.00",
            "volume_24h": "0",
            "price_percentage_change_24h": "0",
        }

    def adapt_pairs_to_account(
        self, pairs: list[str], native_currency: str
    ) -> list[str]:
        """
        IB pairs trade in the stock's native currency (e.g. AAPL trades in USD).
        IBKR handles FX conversion automatically when the account is in EUR.
        Do NOT rewrite USD→EUR for stock tickers.
        """
        return pairs
