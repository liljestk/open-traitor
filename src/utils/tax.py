"""
Tax Module — FIFO Cost-Basis Tracking and CARF Transaction Export.

Implements:
  - FIFO (First-In-First-Out) lot tracking for cost-basis calculation
  - Per-asset lot queue with realized/unrealized P&L
  - CARF (Crypto-Asset Reporting Framework) compatible CSV/JSON export
  - Supports crypto-to-crypto swaps (treated as sell + buy for tax purposes)

CARF: OECD standard for reporting crypto transactions to tax authorities.
Most jurisdictions (EU, US, etc.) use FIFO for cost-basis calculation.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("utils.tax")


@dataclass
class TaxLot:
    """A single purchase lot for FIFO tracking."""
    asset: str
    quantity: float
    cost_per_unit: float       # In quote currency (e.g. USD/EUR)
    quote_currency: str
    acquired_date: str         # ISO 8601
    order_id: str = ""
    remaining_quantity: float = 0.0  # Reduced as lots are sold
    fees: float = 0.0

    def __post_init__(self):
        if self.remaining_quantity == 0:
            self.remaining_quantity = self.quantity

    @property
    def total_cost(self) -> float:
        return self.remaining_quantity * self.cost_per_unit + self.fees

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "quantity": self.quantity,
            "remaining_quantity": self.remaining_quantity,
            "cost_per_unit": self.cost_per_unit,
            "quote_currency": self.quote_currency,
            "acquired_date": self.acquired_date,
            "order_id": self.order_id,
            "fees": self.fees,
        }


@dataclass
class TaxDisposal:
    """Record of a disposal (sale) event with cost-basis and P&L."""
    asset: str
    quantity_sold: float
    sale_price_per_unit: float
    cost_basis_per_unit: float
    quote_currency: str
    sale_date: str
    acquired_date: str         # Of the lot being disposed
    realized_pnl: float
    holding_period_days: int
    is_short_term: bool        # < 1 year
    order_id: str = ""
    fees: float = 0.0

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "quantity_sold": self.quantity_sold,
            "sale_price_per_unit": self.sale_price_per_unit,
            "cost_basis_per_unit": self.cost_basis_per_unit,
            "proceeds": self.quantity_sold * self.sale_price_per_unit,
            "cost_basis_total": self.quantity_sold * self.cost_basis_per_unit,
            "realized_pnl": self.realized_pnl,
            "fees": self.fees,
            "quote_currency": self.quote_currency,
            "sale_date": self.sale_date,
            "acquired_date": self.acquired_date,
            "holding_period_days": self.holding_period_days,
            "is_short_term": self.is_short_term,
            "order_id": self.order_id,
        }


class FIFOTracker:
    """
    FIFO cost-basis tracker.

    Maintains per-asset queues of tax lots. When assets are sold,
    the oldest lots are consumed first (FIFO principle).
    """

    def __init__(self):
        self._lots: dict[str, list[TaxLot]] = {}  # asset → [lots in FIFO order]
        self._disposals: list[TaxDisposal] = []

    def record_buy(
        self,
        asset: str,
        quantity: float,
        price_per_unit: float,
        quote_currency: str = "USD",
        date: str = "",
        order_id: str = "",
        fees: float = 0.0,
    ) -> TaxLot:
        """Record a buy (acquisition) event."""
        if not date:
            date = datetime.now(timezone.utc).isoformat()

        lot = TaxLot(
            asset=asset,
            quantity=quantity,
            cost_per_unit=price_per_unit,
            quote_currency=quote_currency,
            acquired_date=date,
            order_id=order_id,
            fees=fees,
        )

        if asset not in self._lots:
            self._lots[asset] = []
        self._lots[asset].append(lot)

        logger.debug(
            f"Tax lot added: {quantity:.8f} {asset} @ {price_per_unit:,.2f} {quote_currency}"
        )
        return lot

    def record_sell(
        self,
        asset: str,
        quantity: float,
        price_per_unit: float,
        quote_currency: str = "USD",
        date: str = "",
        order_id: str = "",
        fees: float = 0.0,
    ) -> list[TaxDisposal]:
        """
        Record a sell (disposal) event using FIFO.

        Returns list of TaxDisposal records (one per lot consumed).
        """
        if not date:
            date = datetime.now(timezone.utc).isoformat()

        lots = self._lots.get(asset, [])
        if not lots:
            logger.warning(
                f"FIFO sell: no lots found for {asset} — recording with zero cost basis"
            )
            disposal = TaxDisposal(
                asset=asset,
                quantity_sold=quantity,
                sale_price_per_unit=price_per_unit,
                cost_basis_per_unit=0.0,
                quote_currency=quote_currency,
                sale_date=date,
                acquired_date="unknown",
                realized_pnl=quantity * price_per_unit - fees,
                holding_period_days=0,
                is_short_term=True,
                order_id=order_id,
                fees=fees,
            )
            self._disposals.append(disposal)
            return [disposal]

        remaining = quantity
        disposals = []
        fee_per_unit = fees / quantity if quantity > 0 else 0

        while remaining > 0 and lots:
            lot = lots[0]

            consume = min(remaining, lot.remaining_quantity)
            lot.remaining_quantity -= consume
            remaining -= consume

            # Calculate holding period
            try:
                acquired = datetime.fromisoformat(lot.acquired_date.replace("Z", "+00:00"))
                sold = datetime.fromisoformat(date.replace("Z", "+00:00"))
                holding_days = (sold - acquired).days
            except (ValueError, TypeError):
                holding_days = 0

            realized_pnl = consume * (price_per_unit - lot.cost_per_unit) - (fee_per_unit * consume)

            disposal = TaxDisposal(
                asset=asset,
                quantity_sold=consume,
                sale_price_per_unit=price_per_unit,
                cost_basis_per_unit=lot.cost_per_unit,
                quote_currency=quote_currency,
                sale_date=date,
                acquired_date=lot.acquired_date,
                realized_pnl=realized_pnl,
                holding_period_days=holding_days,
                is_short_term=holding_days < 365,
                order_id=order_id,
                fees=fee_per_unit * consume,
            )
            disposals.append(disposal)
            self._disposals.append(disposal)

            # Remove fully consumed lots
            if lot.remaining_quantity <= 1e-12:
                lots.pop(0)

        if remaining > 0:
            logger.warning(
                f"FIFO sell: {remaining:.8f} {asset} sold without matching lots "
                "(quantity exceeds tracked inventory)"
            )

        total_pnl = sum(d.realized_pnl for d in disposals)
        logger.info(
            f"FIFO disposal: {quantity:.8f} {asset} → "
            f"realized P&L: {total_pnl:+,.2f} {quote_currency} "
            f"({len(disposals)} lots consumed)"
        )

        return disposals

    def get_unrealized_pnl(
        self,
        asset: str,
        current_price: float,
    ) -> dict:
        """Calculate unrealized P&L for a given asset at current market price."""
        lots = self._lots.get(asset, [])
        if not lots:
            return {"asset": asset, "unrealized_pnl": 0, "quantity": 0, "avg_cost": 0}

        total_qty = sum(lot.remaining_quantity for lot in lots)
        total_cost = sum(lot.remaining_quantity * lot.cost_per_unit for lot in lots)
        avg_cost = total_cost / total_qty if total_qty > 0 else 0

        market_value = total_qty * current_price
        unrealized = market_value - total_cost

        return {
            "asset": asset,
            "quantity": total_qty,
            "avg_cost_basis": avg_cost,
            "current_price": current_price,
            "market_value": market_value,
            "cost_basis_total": total_cost,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": (unrealized / total_cost * 100) if total_cost > 0 else 0,
        }

    def get_all_lots(self) -> dict[str, list[dict]]:
        """Get all active lots (with remaining quantity > 0)."""
        result = {}
        for asset, lots in self._lots.items():
            active = [lot.to_dict() for lot in lots if lot.remaining_quantity > 1e-12]
            if active:
                result[asset] = active
        return result

    def get_all_disposals(self) -> list[dict]:
        """Get all disposal records."""
        return [d.to_dict() for d in self._disposals]

    def get_tax_summary(self, year: Optional[int] = None) -> dict:
        """
        Get a P&L tax summary, optionally filtered by year.

        Returns:
          {
            "total_realized_pnl": float,
            "short_term_pnl": float,
            "long_term_pnl": float,
            "total_fees": float,
            "total_disposals": int,
            "by_asset": {asset: {"realized_pnl": float, "disposals": int}},
          }
        """
        disposals = self._disposals
        if year:
            disposals = [
                d for d in disposals
                if d.sale_date.startswith(str(year))
            ]

        short_term = sum(d.realized_pnl for d in disposals if d.is_short_term)
        long_term = sum(d.realized_pnl for d in disposals if not d.is_short_term)
        total_fees = sum(d.fees for d in disposals)

        by_asset: dict[str, dict] = {}
        for d in disposals:
            if d.asset not in by_asset:
                by_asset[d.asset] = {"realized_pnl": 0, "disposals": 0}
            by_asset[d.asset]["realized_pnl"] += d.realized_pnl
            by_asset[d.asset]["disposals"] += 1

        return {
            "year": year or "all",
            "total_realized_pnl": short_term + long_term,
            "short_term_pnl": short_term,
            "long_term_pnl": long_term,
            "total_fees": total_fees,
            "total_disposals": len(disposals),
            "by_asset": by_asset,
        }

    # ═════════════════════════════════════════════════════════════════════════
    # CARF Export
    # ═════════════════════════════════════════════════════════════════════════

    def export_carf_csv(self, year: Optional[int] = None) -> str:
        """
        Export transactions in CARF-compatible CSV format.

        The Crypto-Asset Reporting Framework (CARF) by OECD requires
        reporting of transaction type, asset, quantity, value, dates,
        and counterparty info (here N/A for self-custody/exchange).
        """
        disposals = self._disposals
        if year:
            disposals = [d for d in disposals if d.sale_date.startswith(str(year))]

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Transaction_Type",
            "Asset",
            "Quantity",
            "Proceeds",
            "Cost_Basis",
            "Realized_PnL",
            "Fees",
            "Quote_Currency",
            "Sale_Date",
            "Acquisition_Date",
            "Holding_Period_Days",
            "Short_Term",
            "Order_ID",
        ])

        for d in disposals:
            writer.writerow([
                "DISPOSAL",
                d.asset,
                f"{d.quantity_sold:.8f}",
                f"{d.quantity_sold * d.sale_price_per_unit:.2f}",
                f"{d.quantity_sold * d.cost_basis_per_unit:.2f}",
                f"{d.realized_pnl:.2f}",
                f"{d.fees:.2f}",
                d.quote_currency,
                d.sale_date,
                d.acquired_date,
                str(d.holding_period_days),
                "YES" if d.is_short_term else "NO",
                d.order_id,
            ])

        return output.getvalue()

    def export_carf_json(self, year: Optional[int] = None) -> str:
        """Export transactions in CARF-compatible JSON format."""
        disposals = self._disposals
        if year:
            disposals = [d for d in disposals if d.sale_date.startswith(str(year))]

        report = {
            "report_type": "CARF_Crypto_Asset_Reporting",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "year": year or "all",
            "summary": self.get_tax_summary(year),
            "transactions": [d.to_dict() for d in disposals],
        }

        return json.dumps(report, indent=2)
