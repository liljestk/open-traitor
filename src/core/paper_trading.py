"""
PaperTradingMixin — shared paper-trading infrastructure for all exchange clients.

Provides thread-safe balance tracking, order storage, and account listing.
Each exchange client subclass implements the actual buy/sell execution logic
using ``_paper_execute_buy`` / ``_paper_execute_sell`` (which call the helpers here).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from src.utils.logger import get_logger

logger = get_logger("paper_trading")


class PaperTradingMixin:
    """
    Mixin that provides common paper-trading state management.

    Subclasses should call ``_init_paper(...)`` from their ``__init__``.
    """

    def _init_paper(
        self,
        *,
        initial_balances: dict[str, float],
        slippage_pct: float = 0.0005,
        max_orders: int = 500,
    ) -> None:
        """Initialise paper-trading state.  Call from exchange client ``__init__``."""
        self._paper_balance: dict[str, float] = dict(initial_balances)
        self._paper_balance_lock = threading.Lock()
        self._paper_orders: list[dict[str, Any]] = []
        self._paper_slippage_pct = slippage_pct
        self._max_paper_orders = max_orders

    # ── Balance helpers ──────────────────────────────────────────────────

    def paper_get_balance(self, currency: str) -> float:
        """Thread-safe read of a single currency balance."""
        with self._paper_balance_lock:
            return self._paper_balance.get(currency, 0.0)

    def paper_get_all_balances(self) -> dict[str, float]:
        """Thread-safe snapshot of all non-zero balances."""
        with self._paper_balance_lock:
            return {k: v for k, v in self._paper_balance.items() if v > 0}

    def paper_adjust_balance(self, currency: str, delta: float) -> float:
        """
        Atomically adjust a currency balance by *delta* (can be negative).
        Returns the new balance.  Raises ``ValueError`` if the result would go negative.
        """
        with self._paper_balance_lock:
            current = self._paper_balance.get(currency, 0.0)
            new_val = current + delta
            if new_val < -1e-9:  # small float tolerance
                raise ValueError(
                    f"Insufficient {currency}: have {current:.6f}, need {abs(delta):.6f}"
                )
            self._paper_balance[currency] = max(0.0, new_val)
            return self._paper_balance[currency]

    def paper_set_balance(self, currency: str, amount: float) -> None:
        """Set an absolute balance (useful for tests)."""
        with self._paper_balance_lock:
            self._paper_balance[currency] = amount

    # ── Order storage ────────────────────────────────────────────────────

    def paper_record_order(self, order: dict[str, Any]) -> None:
        """Append an order to the paper log and trim if needed."""
        with self._paper_balance_lock:
            self._paper_orders.append(order)
            if len(self._paper_orders) > self._max_paper_orders:
                self._paper_orders = self._paper_orders[-self._max_paper_orders:]

    def paper_get_order(self, order_id: str) -> dict[str, Any] | None:
        """Look up a paper order by ID."""
        for o in reversed(self._paper_orders):
            if o.get("order_id") == order_id:
                return o
        return None

    def paper_get_open_orders(self) -> list[dict[str, Any]]:
        """Paper orders are instant-fill, so this always returns []."""
        return []

    # ── Account listing ──────────────────────────────────────────────────

    def paper_get_accounts(self) -> list[dict[str, Any]]:
        """
        Return paper balances in the standard ``get_accounts()`` format.

        The caller (exchange client) may want to re-format to match their
        exchange's specific schema.
        """
        accounts: list[dict[str, Any]] = []
        with self._paper_balance_lock:
            for currency, amount in self._paper_balance.items():
                accounts.append({
                    "currency": currency,
                    "balance": amount,
                    "hold": 0.0,
                    "available": amount,
                    "account_id": f"paper-{currency.lower()}",
                })
        return accounts

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def paper_generate_order_id() -> str:
        return f"paper-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def paper_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
