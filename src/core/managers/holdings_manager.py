"""
HoldingsManager — Live Coinbase snapshot, holdings refresh, and position reconciliation.

Extracted from Orchestrator for maintainability.  Takes an orchestrator reference
in its constructor (same pattern as PipelineManager / StateManager).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.core.coinbase_client import (
    _KNOWN_FIAT, _ALL_STABLECOINS, _EUR_EQUIVALENTS, _KNOWN_QUOTES, _get_fiat_rate_usd,
)
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.holdings_manager")


class HoldingsManager:
    """Live Coinbase account snapshot, holdings refresh, and position reconciliation."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator

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
            accounts = []

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
                currency = bal.get("currency", acct.get("currency", ""))
                val_str = bal.get("value", "0")
                try:
                    amount = float(val_str)
                except (ValueError, TypeError):
                    amount = 0.0

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
                            price = orch.exchange.get_current_price(tracked_pair)
                            if price > 0:
                                prices_by_pair[tracked_pair] = price
                                orch.state.update_price(tracked_pair, price)
                        except Exception:
                            pass
                    elif native != "USD":
                        # Try the native pair even if not tracked
                        try:
                            price = orch.exchange.get_current_price(native_pair)
                            if price > 0:
                                prices_by_pair[native_pair] = price
                        except Exception:
                            pass
                    if price == 0:
                        # Fallback: USD pair → convert to native
                        usd_pair = f"{currency}-USD"
                        try:
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
        """Refresh live Coinbase holdings if the TTL has elapsed.

        TTL-cached: only calls the API if enough time has passed since the
        last successful sync.  Graceful degradation: on failure, keeps the
        previous snapshot and does NOT update the timestamp, so the next
        cycle retries immediately.
        """
        import time
        orch = self.orchestrator
        if not orch._holdings_sync_enabled or getattr(orch.exchange, 'paper_mode', False):
            return
        now = time.time()
        if now - orch.state._live_snapshot_ts < orch._holdings_refresh_seconds:
            return  # still fresh
        try:
            snapshot = self.live_coinbase_snapshot()
            orch.state.sync_live_holdings(
                snapshot, dust_threshold=orch._holdings_dust_threshold
            )
        except Exception as e:
            logger.warning(f"⚠️ Holdings refresh failed (keeping stale data): {e}")

    # =========================================================================
    # Position Reconciliation
    # =========================================================================

    def reconcile_positions(self) -> None:
        """
        Reconcile TradingState.positions against actual Coinbase balances (live mode only).
        Corrects drift caused by partial fills, crashes, or external account changes.
        Runs every reconcile_every_cycles cycles (~20 min at default 120s interval).
        """
        orch = self.orchestrator
        try:
            # Reconcile fiat-quoted pairs (USD, EUR, GBP, etc.).
            base_to_pair: dict[str, str] = {}
            expected: dict[str, float] = {}
            for pair, qty in orch.state.open_positions.items():
                if qty <= 0 or "-" not in pair:
                    continue
                base, quote = pair.split("-", 1)
                if quote in _KNOWN_QUOTES:
                    expected[base] = qty
                    base_to_pair[base] = pair
            result = orch.exchange.reconcile_positions(expected)

            if not result["matched"]:
                for d in result["discrepancies"]:
                    currency = d["currency"]
                    actual_qty = d["actual"]
                    # Reconstruct the original pair (e.g. ATOM-EUR, not ATOM-USD)
                    pair = base_to_pair.get(currency, f"{currency}-USD")

                    # Correct state to match actual Coinbase balance
                    with orch.state._lock:
                        if actual_qty > 1e-8:
                            orch.state.positions[pair] = actual_qty
                        else:
                            orch.state.positions.pop(pair, None)

                    msg = (
                        f"⚠️ Position drift corrected: {currency} "
                        f"expected={d['expected']:.6f} actual={actual_qty:.6f} "
                        f"diff={d['diff']:+.6f}"
                    )
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
                    if orch.telegram:
                        orch.telegram.send_alert(msg)
            else:
                logger.debug("✅ Position reconciliation: no discrepancies")

        except Exception as e:
            logger.warning(f"Position reconciliation failed: {e}")
