"""
HoldingsManager — Live Coinbase snapshot, holdings refresh, and position reconciliation.

Extracted from Orchestrator for maintainability.  Takes an orchestrator reference
in its constructor (same pattern as PipelineManager / StateManager).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.core.coinbase_client import (
    _KNOWN_FIAT, _ALL_STABLECOINS, _EUR_EQUIVALENTS, _KNOWN_QUOTES, _get_fiat_rate_usd,
)
from src.utils.logger import get_logger
from src.utils.rate_limiter import get_rate_limiter as _get_rate_limiter

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.holdings_manager")


class HoldingsManager:
    """Live Coinbase account snapshot, holdings refresh, and position reconciliation."""

    # Suppress repeated drift alerts for the same currency (1-hour cooldown)
    _DRIFT_ALERT_COOLDOWN: float = 3600.0  # seconds

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        # H6 fix: Prevent concurrent maybe_refresh_holdings calls
        self._refresh_lock = threading.Lock()
        # Track last drift alert time per currency to avoid Telegram spam
        self._drift_alert_times: dict[str, float] = {}

    # =========================================================================
    # Live Coinbase Snapshot
    # =========================================================================

    def live_coinbase_snapshot(self) -> dict:
        """
        Fetch actual live account data directly from the Coinbase API.

        Returns a structured dict with:
          native_currency    – detected account currency (e.g. "EUR")
          total_portfolio    – total portfolio value in native currency
          fiat_cash          – fiat/cash balance in native currency
          currency_symbol    – display symbol (e.g. "€" or "$")
          holdings           – list of {currency, amount, native_value, is_fiat, pair, price}
          prices_by_pair     – {pair: price} for all tracked + held pairs
          fetch_ts           – UTC timestamp of this fetch
        """
        orch = self.orchestrator
        fetch_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Detect the native (quote) currency from config or fallback to pairs
        native = orch.config.get("trading", {}).get("quote_currency", "auto").upper()
        if native == "AUTO":
            native = "USD"
            for pair in orch.pairs:
                if "-" in pair:
                    _, quote = pair.rsplit("-", 1)
                    if quote in _KNOWN_FIAT:
                        native = quote
                        break
                    if quote in _EUR_EQUIVALENTS:
                        native = "EUR"
                        break
        currency_symbols = {"EUR": "€", "GBP": "£", "CHF": "CHF ", "USD": "$", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
        symbol = currency_symbols.get(native, native + " ")

        try:
            accounts = orch.exchange.get_accounts()
        except Exception as e:
            logger.warning(f"live_coinbase_snapshot: get_accounts failed: {e}")
            raise

        holdings = []
        prices_by_pair: dict[str, float] = {}
        total_value = 0.0
        fiat_cash = 0.0

        # Build a mapping of all tracked pairs to native currency pairs
        native_tracked = {}
        for pair in orch.pairs:
            base = pair.split("-")[0] if "-" in pair else pair
            native_tracked[base] = pair

        if accounts:
            # M30 fix: handle list input (from ABC), SDK response objects, or dicts
            if isinstance(accounts, list):
                account_list = accounts
            elif hasattr(accounts, "to_dict"):
                raw = accounts.to_dict()
                account_list = raw.get("accounts", [])
            elif isinstance(accounts, dict):
                account_list = accounts.get("accounts", [])
            else:
                account_list = []

            for acct in account_list:
                bal = acct.get("available_balance", {})
                hold = acct.get("hold", {})
                currency = bal.get("currency", acct.get("currency", ""))
                try:
                    avail = float(bal.get("value", "0"))
                except (ValueError, TypeError):
                    avail = 0.0
                try:
                    held = float(hold.get("value", "0"))
                except (ValueError, TypeError):
                    held = 0.0

                # Total = available + on-hold (pending orders)
                amount = avail + held

                if amount <= 0 or not currency:
                    continue

                is_fiat = currency in _KNOWN_FIAT or currency in _ALL_STABLECOINS

                # Convert to native account currency (e.g. EUR)
                if hasattr(orch.exchange, "_currency_to_native"):
                    native_val = orch.exchange._currency_to_native(currency, amount, native)
                else:
                    native_val = amount  # Fallback

                # Determine best price pair to quote for this asset
                tracked_pair = native_tracked.get(currency)
                price = 0.0
                if not is_fiat:
                    # Try native pair first (e.g. ATOM-EUR)
                    native_pair = f"{currency}-{native}"
                    if tracked_pair:
                        try:
                            _get_rate_limiter().wait("coinbase_rest")  # M8
                            price = orch.exchange.get_current_price(tracked_pair)
                            if price > 0:
                                prices_by_pair[tracked_pair] = price
                                orch.state.update_price(tracked_pair, price)
                        except Exception:
                            pass
                    elif native != "USD":
                        # Try the native pair even if not tracked
                        try:
                            _get_rate_limiter().wait("coinbase_rest")  # M8
                            price = orch.exchange.get_current_price(native_pair)
                            if price > 0:
                                prices_by_pair[native_pair] = price
                        except Exception:
                            pass
                    if price == 0:
                        # Fallback: USD pair → convert to native
                        usd_pair = f"{currency}-USD"
                        try:
                            _get_rate_limiter().wait("coinbase_rest")  # M8
                            price_usd = orch.exchange.get_current_price(usd_pair)
                            if price_usd > 0:
                                prices_by_pair[usd_pair] = price_usd
                                if native != "USD":
                                    rate_nat = _get_fiat_rate_usd(native)
                                    price = price_usd / rate_nat if rate_nat > 0 else price_usd
                                else:
                                    price = price_usd
                        except Exception:
                            pass

                holding = {
                    "currency": currency,
                    "amount": amount,
                    "native_value": round(native_val, 4),
                    "is_fiat": is_fiat,
                    "pair": tracked_pair or (f"{currency}-{native}" if not is_fiat else None),
                    "price": price,
                }
                holdings.append(holding)
                total_value += native_val
                if is_fiat:
                    fiat_cash += native_val

        # Also fetch prices for tracked pairs that aren't in the account
        for pair in orch.pairs:
            if pair not in prices_by_pair:
                try:
                    _get_rate_limiter().wait("coinbase_rest")  # M8
                    p = orch.exchange.get_current_price(pair)
                    if p > 0:
                        prices_by_pair[pair] = p
                        orch.state.update_price(pair, p)
                except Exception:
                    pass

        # Sort holdings by value descending
        holdings.sort(key=lambda h: h["native_value"], reverse=True)

        return {
            "fetch_ts": fetch_ts,
            "native_currency": native,
            "currency_symbol": symbol,
            "total_portfolio": round(total_value, 2),
            "fiat_cash": round(fiat_cash, 2),
            # Legacy keys for backward compat
            "total_portfolio_usd": round(total_value, 2),
            "fiat_cash_usd": round(fiat_cash, 2),
            "holdings": holdings,
            "prices_by_pair": prices_by_pair,
            "tracked_pairs": orch.pairs,
            "bot_pnl": orch.state.total_pnl,
            "bot_trades": orch.state.total_trades,
            "is_paused": orch.state.is_paused,
            "circuit_breaker": orch.state.circuit_breaker_triggered,
        }

    # =========================================================================
    # TTL-Cached Holdings Refresh
    # =========================================================================

    def maybe_refresh_holdings(self) -> None:
        """Refresh live holdings if the TTL has elapsed.

        Supports both Coinbase and IBKR exchanges.
        TTL-cached: only calls the API if enough time has passed since the
        last successful sync.  Graceful degradation: on failure, keeps the
        previous snapshot and does NOT update the timestamp, so the next
        cycle retries immediately.
        
        H6 fix: Uses a lock to prevent concurrent API calls from multiple paths.
        """
        import time
        
        # H6 fix: Use non-blocking acquire to skip if another refresh is in progress
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            orch = self.orchestrator
            if not orch._holdings_sync_enabled or getattr(orch.exchange, 'paper_mode', False):
                return
            now = time.time()
            if now - orch.state._live_snapshot_ts < orch._holdings_refresh_seconds:
                return  # still fresh

            _is_ibkr = orch.exchange.__class__.__name__ == "IBClient"
            if _is_ibkr:
                self._refresh_ibkr_holdings()
            else:
                try:
                    snapshot = self.live_coinbase_snapshot()
                    new_externals = orch.state.sync_live_holdings(
                        snapshot, dust_threshold=orch._holdings_dust_threshold
                    )
                    if new_externals:
                        self._register_external_holdings(new_externals)
                except Exception as e:
                    logger.warning(f"⚠️ Holdings refresh failed (keeping stale data): {e}")
        finally:
            self._refresh_lock.release()

    def _refresh_ibkr_holdings(self) -> None:
        """Refresh portfolio value and cash balance from IBKR API."""
        import time

        orch = self.orchestrator
        try:
            pv = orch.exchange.get_portfolio_value()
            accs = orch.exchange.get_accounts()
            cash = 0.0
            for acc in accs:
                cash += acc.get("available_cash", 0.0)

            orch.state.live_portfolio_value = pv
            orch.state.cash_balance = cash
            orch.state.live_cash_balances = {orch.state.native_currency: cash}
            orch.state._live_snapshot_ts = time.time()
            # Correct initial_balance on first successful refresh (mirrors Coinbase logic)
            if not orch.state._initial_balance_synced and pv > 0:
                orch.state.initial_balance = pv
                orch.state.peak_portfolio_value = pv
                orch.state._initial_balance_synced = True
                orch.state.max_drawdown = 0.0
                orch.state.circuit_breaker_triggered = False
                logger.info(
                    f"📊 Initial balance corrected to live IBKR portfolio: "
                    f"{orch.state.currency_symbol}{pv:,.2f} (drawdown reset)"
                )
            logger.debug(
                f"📡 IBKR refresh: portfolio {orch.state.currency_symbol}{pv:,.2f}, "
                f"cash {orch.state.currency_symbol}{cash:,.2f}"
            )
        except Exception as e:
            logger.warning(f"⚠️ IBKR holdings refresh failed (keeping stale data): {e}")

    def _register_external_holdings(self, new_externals: dict[str, float]) -> None:
        """Register newly discovered external holdings in FIFO tracker and trailing stops.

        For assets the bot didn't buy (pre-existing on the exchange), we create
        a synthetic cost-basis lot at the discovery price.  This means:
          - PNL is tracked from the moment the bot first sees the holding
          - Trailing stops have a reference price to work from
          - The actual purchase price (before the bot was running) remains unknown

        Args:
            new_externals: {pair: discovery_price} returned by sync_live_holdings.
        """
        orch = self.orchestrator
        for pair, discovery_price in new_externals.items():
            qty = orch.state.positions.get(pair, 0)
            if qty <= 0 or discovery_price <= 0:
                continue
            base_asset = pair.split("-")[0] if "-" in pair else pair

            # Synthetic FIFO lot at discovery price so realized PNL is meaningful
            try:
                orch.fifo_tracker.record_buy(
                    asset=base_asset,
                    quantity=qty,
                    cost_per_unit=discovery_price,
                    fees=0.0,
                )
                logger.info(
                    f"📎 External holding {pair}: synthetic cost basis registered "
                    f"at discovery price {discovery_price:.6f} (qty={qty:.6f})"
                )
            except Exception as e:
                logger.debug(f"FIFO registration for external {pair} failed: {e}")

            # Trailing stop at discovery price — protects against further downside
            try:
                orch.trailing_stops.add_stop(
                    pair=pair,
                    entry_price=discovery_price,
                    initial_stop=None,
                    total_quantity=float(qty),
                )
                logger.info(
                    f"📎 External holding {pair}: trailing stop added "
                    f"at discovery price {discovery_price:.6f}"
                )
            except Exception as e:
                logger.debug(f"Trailing stop for external {pair} failed: {e}")

    # =========================================================================
    # Reconciliation Helpers
    # =========================================================================

    @staticmethod
    def _sync_trade_filled_qty(orch: "Orchestrator", currency: str, actual_qty: float) -> None:
        """Update open buy-trade filled_quantity values to match exchange reality.

        Distributes the actual quantity proportionally across all open buy
        trades for *currency* so subsequent reconciliation cycles won't
        re-flag the same drift.
        """
        buy_trades = [
            t for t in orch.state.get_open_trades()
            if t.action.value == "buy"
            and "-" in t.pair
            and t.pair.split("-", 1)[0] == currency
        ]
        if not buy_trades:
            return

        total_expected = sum((t.filled_quantity or t.quantity) for t in buy_trades)
        if total_expected <= 0:
            return

        for t in buy_trades:
            share = (t.filled_quantity or t.quantity) / total_expected
            t.filled_quantity = actual_qty * share

    @staticmethod
    def _auto_close_zero_balance_trades(
        orch: "Orchestrator", currency: str, pair: str, d: dict
    ) -> None:
        """Close all open buy trades for a currency whose actual balance is zero.

        The position was likely sold or transferred externally.
        """
        buy_trades = [
            t for t in orch.state.get_open_trades()
            if t.action.value == "buy"
            and "-" in t.pair
            and t.pair.split("-", 1)[0] == currency
        ]
        for t in buy_trades:
            try:
                orch.state.close_trade(t.id, close_price=0.0, fees=0.0)
            except Exception:
                pass

        # Clean up position tracking
        orch.state.reconcile_position(
            pair=pair, actual_qty=0.0, current_price=0.0,
            reason="zero_balance_auto_close",
        )

        msg = (
            f"🔄 Auto-closed {currency}: balance is 0 on exchange "
            f"(expected={d['expected']:.6f}). "
            f"{len(buy_trades)} trade(s) closed."
        )
        logger.warning(msg)
        orch.audit.log_rule_check(
            "position_reconciliation", passed=False, details=msg,
        )
        orch.stats_db.record_event(
            event_type="reconciliation", message=msg,
            severity="warning", pair=pair, data=d,
        )
        if orch.telegram:
            orch.telegram.send_alert(msg)

    # =========================================================================
    # Position Reconciliation
    # =========================================================================

    def reconcile_positions(self) -> None:
        """
        Reconcile TradingState.positions against actual Coinbase balances (live mode only).
        Corrects drift caused by partial fills, crashes, or external account changes.
        Also discovers NEW holdings on Coinbase that aren't tracked yet.
        Runs every reconcile_every_cycles cycles (~20 min at default 120s interval).
        """
        orch = self.orchestrator

        # Read configured quote currency for fallback pair construction
        trading_cfg = orch.config.get("trading", {})
        default_quote = (
            trading_cfg.get("quote_currency", "USD") or "USD"
        ).upper()

        try:
            # Build expected from BOT-OPENED trades only (not state.positions, which also
            # includes external pre-existing holdings).  External holdings are discovered
            # by the holdings-sync path below and should NOT fire drift warnings.
            base_to_pair: dict[str, str] = {}
            expected: dict[str, float] = {}
            for trade in orch.state.get_open_trades():
                if trade.action.value != "buy":
                    continue
                pair = trade.pair
                if "-" not in pair:
                    continue
                base, quote = pair.split("-", 1)
                if quote not in _KNOWN_QUOTES:
                    continue
                qty = trade.filled_quantity or trade.quantity
                if qty and qty > 0:
                    expected[base] = expected.get(base, 0.0) + qty
                    base_to_pair[base] = pair
            result = orch.exchange.reconcile_positions(expected)

            if not result["matched"]:
                for d in result["discrepancies"]:
                    currency = d["currency"]
                    actual_qty = d["actual"]
                    # Skip currencies the bot never opened a trade for — those are
                    # pre-existing external holdings and are handled by the discovery
                    # section below (not genuine drift).
                    if d["expected"] == 0:
                        continue
                    # Reconstruct the original pair using config quote currency
                    pair = base_to_pair.get(currency, f"{currency}-{default_quote}")

                    # ── Auto-close trades when actual balance is zero ──
                    if actual_qty < 1e-8:
                        self._auto_close_zero_balance_trades(
                            orch, currency, pair, d
                        )
                        continue

                    # C2 fix: Use state.reconcile_position() instead of direct _lock access
                    # to maintain atomic cash/position accounting
                    try:
                        current_price = orch.exchange.get_current_price(pair)
                    except Exception:
                        current_price = 0.0
                    recon_result = orch.state.reconcile_position(
                        pair=pair,
                        actual_qty=actual_qty,
                        current_price=current_price,
                        reason="position_drift",
                    )

                    # ── Update trade filled_quantity so drift doesn't recur ──
                    self._sync_trade_filled_qty(orch, currency, actual_qty)

                    msg = (
                        f"⚠️ Position drift corrected: {currency} "
                        f"expected={d['expected']:.6f} actual={actual_qty:.6f} "
                        f"diff={d['diff']:+.6f}"
                    )
                    if recon_result.get("cash_adj", 0) != 0:
                        msg += f" (cash adj: {recon_result['cash_adj']:+.2f})"
                    logger.warning(msg)
                    orch.audit.log_rule_check(
                        "position_reconciliation",
                        passed=False,
                        details=msg,
                    )
                    orch.stats_db.record_event(
                        event_type="reconciliation",
                        message=msg,
                        severity="warning",
                        pair=pair,
                        data=d,
                    )
                    # ── Suppress repeated Telegram alerts (1h cooldown) ──
                    if orch.telegram:
                        now = time.monotonic()
                        last = self._drift_alert_times.get(currency, 0.0)
                        if now - last >= self._DRIFT_ALERT_COOLDOWN:
                            self._drift_alert_times[currency] = now
                            orch.telegram.send_alert(msg)
            else:
                logger.debug("✅ Position reconciliation: no discrepancies")

            # --- Discover new holdings not currently tracked ---
            actual_balances: dict[str, float] = result.get("actual", {})
            fiat_skip = _KNOWN_FIAT | _ALL_STABLECOINS
            dust_threshold = orch._holdings_dust_threshold if hasattr(orch, "_holdings_dust_threshold") else 0.50

            # Build a set of already-tracked base currencies (from state.positions)
            # so we don't re-emit discovery logs for holdings synced by holdings-manager.
            tracked_bases: set[str] = set()
            for pair in orch.state.open_positions:
                if "-" in pair:
                    tracked_bases.add(pair.split("-", 1)[0])

            for currency, amount in actual_balances.items():
                if currency in fiat_skip:
                    continue
                if currency in base_to_pair:
                    continue  # already tracked via open trade
                if currency in tracked_bases:
                    continue  # already in state.positions (synced externally)
                if amount <= 1e-8:
                    continue

                # Build pair from configured quote currency
                new_pair = f"{currency}-{default_quote}"

                # Check if the balance is above dust threshold (value-wise)
                try:
                    _get_rate_limiter().wait("coinbase_rest")
                    price = orch.exchange.get_current_price(new_pair)
                except Exception:
                    price = 0.0

                value = amount * price if price > 0 else 0.0
                if value < dust_threshold:
                    continue  # too small to track

                with orch.state._lock:
                    max_pos = (
                        orch.portfolio_scaler.tier.max_open_positions
                        if hasattr(orch, "portfolio_scaler") else 999
                    )
                    current_count = len(orch.state.positions)
                    if current_count >= max_pos:
                        logger.warning(
                            f"⚠️ Position limit reached ({current_count}/{max_pos}) — "
                            f"still tracking discovered {new_pair} for portfolio accounting"
                        )
                    orch.state.positions[new_pair] = amount

                msg = (
                    f"🆕 Discovered new holding: {new_pair} "
                    f"qty={amount:.6f} value≈{value:.2f} {default_quote}"
                )
                logger.info(msg)
                orch.stats_db.record_event(
                    event_type="reconciliation_discovery",
                    message=msg,
                    severity="info",
                    pair=new_pair,
                    data={"currency": currency, "amount": amount, "value": value},
                )

        except Exception as e:
            logger.warning(f"Position reconciliation failed: {e}")
