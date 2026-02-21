"""
Fee Manager — Fee-aware trading decisions.

Supports pluggable fee models:

  - ``crypto_percentage`` (default): Percentage-based taker/maker fees.
    Coinbase Advanced Trade fee tiers (based on 30-day volume):
      Tier 1 (<$1K):    Taker 0.60%, Maker 0.40%
      Tier 2 (<$10K):   Taker 0.40%, Maker 0.25%
      Tier 3 (<$50K):   Taker 0.25%, Maker 0.15%

  - ``equity_flat_plus_pct``: Flat minimum fee per trade plus a percentage.
    Typical for Scandinavian stock brokers (e.g. Nordnet Courtage Mini).

A swap (sell A → buy B) costs TWO trades worth of fees.
We must ensure expected gain > total fees or we lose money.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("core.fee_manager")


@dataclass
class FeeEstimate:
    """Fee breakdown for a proposed trade or swap."""
    sell_fee_pct: float
    buy_fee_pct: float
    total_fee_pct: float
    sell_fee_quote: float
    buy_fee_quote: float
    total_fee_quote: float
    breakeven_move_pct: float  # Min price move needed to break even
    is_profitable: bool        # True if expected gain > fees


# ═══════════════════════════════════════════════════════════════════════════
# Fee model ABCs
# ═══════════════════════════════════════════════════════════════════════════

class BaseFeeModel(abc.ABC):
    """Interface for pluggable fee calculation strategies."""

    @abc.abstractmethod
    def estimate_trade_fee(self, quote_amount: float, is_maker: bool = False) -> float:
        """Return the estimated fee in quote currency for a single trade."""

    @abc.abstractmethod
    def effective_fee_pct(self, quote_amount: float, is_maker: bool = False) -> float:
        """Return the effective fee as a fraction of ``quote_amount``."""


class CryptoPercentageFeeModel(BaseFeeModel):
    """
    Percentage-based fee model (typical for crypto exchanges).

    Both maker and taker pay a flat percentage of the trade value.
    """

    def __init__(self, taker_pct: float = 0.006, maker_pct: float = 0.004):
        self.taker_pct = taker_pct
        self.maker_pct = maker_pct

    def estimate_trade_fee(self, quote_amount: float, is_maker: bool = False) -> float:
        pct = self.maker_pct if is_maker else self.taker_pct
        return quote_amount * pct

    def effective_fee_pct(self, quote_amount: float, is_maker: bool = False) -> float:
        return self.maker_pct if is_maker else self.taker_pct


class EquityFlatPlusPctFeeModel(BaseFeeModel):
    """
    Flat minimum + percentage fee model (typical for Nordic stock brokers).

    The fee is ``max(flat_fee_min, quote_amount * percent_fee)``, optionally
    plus a currency-conversion surcharge for foreign-listed instruments.
    """

    def __init__(
        self,
        flat_fee_min: float = 39.0,       # SEK
        percent_fee: float = 0.0015,       # 0.15 %
        currency_conversion_pct: float = 0.0025,
    ):
        self.flat_fee_min = flat_fee_min
        self.percent_fee = percent_fee
        self.currency_conversion_pct = currency_conversion_pct

    def estimate_trade_fee(self, quote_amount: float, is_maker: bool = False) -> float:
        return max(self.flat_fee_min, quote_amount * self.percent_fee)

    def effective_fee_pct(self, quote_amount: float, is_maker: bool = False) -> float:
        if quote_amount <= 0:
            return 0.0
        return self.estimate_trade_fee(quote_amount) / quote_amount


def _build_fee_model(config: dict) -> BaseFeeModel:
    """Instantiate the correct fee model from the ``fees`` config block."""
    model_type = config.get("model_type", "crypto_percentage")

    if model_type == "equity_flat_plus_pct":
        return EquityFlatPlusPctFeeModel(
            flat_fee_min=config.get("flat_fee_min", 39.0),
            percent_fee=config.get("percent_fee", 0.0015),
            currency_conversion_pct=config.get("currency_conversion_pct", 0.0025),
        )

    # Default / "crypto_percentage"
    return CryptoPercentageFeeModel(
        taker_pct=config.get("trade_fee_pct", 0.006),
        maker_pct=config.get("maker_fee_pct", 0.004),
    )


class FeeManager:
    """
    Manages fee calculations and determines if trades are worth executing.

    Core principle: NEVER lose money to fees.
    A trade is only worth it if:
        expected_gain > total_fees * fee_safety_margin

    For swaps (sell A → buy B):
        expected_relative_gain > 2 * trade_fee * safety_margin
    """

    # Legacy class-level constants kept for backward compatibility
    TAKER_FEE_PCT = 0.006
    MAKER_FEE_PCT = 0.004
    DEFAULT_FEE_PCT = TAKER_FEE_PCT

    def __init__(self, config: dict):
        self.config = config.get("fees", {})

        # Build the pluggable fee model
        self._fee_model: BaseFeeModel = _build_fee_model(self.config)

        # Override fee rates from config (used as fallback / summary display)
        self.trade_fee_pct = self.config.get("trade_fee_pct", self.DEFAULT_FEE_PCT)
        self.maker_fee_pct = self.config.get("maker_fee_pct", self.MAKER_FEE_PCT)

        # Safety margin: require gains to be this multiple of fees
        # e.g., 1.5 = need 1.5x the fee amount in expected gains
        self.fee_safety_margin = self.config.get("safety_margin", 1.5)

        # Minimum expected gain after fees to justify a trade
        self.min_gain_after_fees_pct = self.config.get("min_gain_after_fees_pct", 0.005)

        # Below this amount, don't even bother (fees eat everything)
        self.min_trade_quote = self.config.get("min_trade_quote", self.config.get("min_trade_usd", 50.0))

        # Cooldown period after a swap to prevent churn (seconds)
        self.swap_cooldown_seconds = self.config.get("swap_cooldown_seconds", 3600)

        logger.info(
            f"💰 Fee Manager: trade fee={self.trade_fee_pct*100:.2f}%, "
            f"safety margin={self.fee_safety_margin}x, "
            f"min gain after fees={self.min_gain_after_fees_pct*100:.2f}%"
        )

    def estimate_trade_fees(
        self,
        quote_amount: float,
        is_maker: bool = False,
    ) -> float:
        """Estimate fees for a single trade (delegates to the active fee model)."""
        return self._fee_model.estimate_trade_fee(quote_amount, is_maker)

    def estimate_swap_fees(
        self,
        quote_amount: float,
        is_maker: bool = False,
        n_legs: int = 2,
    ) -> FeeEstimate:
        """
        Estimate total fees for a swap across N legs.

        Args:
            quote_amount: Amount in quote currency
            is_maker: Whether using maker (limit) orders
            n_legs: Number of trade legs (1=direct, 2=fiat-routed, 3+=bridged)
        """
        # Compound fees across N legs using the pluggable model
        remaining = quote_amount
        total_fee = 0.0
        leg_fees: list[float] = []
        for _ in range(n_legs):
            leg_fee = self._fee_model.estimate_trade_fee(remaining, is_maker)
            leg_fees.append(leg_fee)
            total_fee += leg_fee
            remaining -= leg_fee

        total_fee_pct = total_fee / quote_amount if quote_amount > 0 else 0

        # Breakeven: price of target needs to move this much to cover fees
        breakeven = total_fee_pct * self.fee_safety_margin

        return FeeEstimate(
            sell_fee_pct=fee_pct,
            buy_fee_pct=fee_pct,
            total_fee_pct=total_fee_pct,
            sell_fee_quote=leg_fees[0] if leg_fees else 0,
            buy_fee_quote=leg_fees[-1] if leg_fees else 0,
            total_fee_quote=total_fee,
            breakeven_move_pct=breakeven,
            is_profitable=False,  # Caller sets after comparing to expected gain
        )

    def is_trade_worthwhile(
        self,
        quote_amount: float,
        expected_gain_pct: float,
        is_swap: bool = False,
        n_legs: int | None = None,
    ) -> tuple[bool, FeeEstimate]:
        """
        Determine if a trade is worth executing after fees.

        Args:
            quote_amount: Trade size in quote currency
            expected_gain_pct: Expected price movement (0.05 = 5%)
            is_swap: Whether this is a swap (2x fees)
            n_legs: Override leg count for route-aware fee calc (None = auto)

        Returns:
            (is_worthwhile, fee_estimate)
        """
        # Too small to trade
        if quote_amount < self.min_trade_quote:
            logger.debug(f"Trade too small: {quote_amount:.2f} < {self.min_trade_quote:.2f}")
            estimate = FeeEstimate(
                sell_fee_pct=0, buy_fee_pct=0, total_fee_pct=0,
                sell_fee_quote=0, buy_fee_quote=0, total_fee_quote=0,
                breakeven_move_pct=0, is_profitable=False,
            )
            return False, estimate

        if is_swap:
            swap_legs = n_legs if n_legs is not None else 2
            estimate = self.estimate_swap_fees(quote_amount, n_legs=swap_legs)
        else:
            fee_quote = self.estimate_trade_fees(quote_amount)
            eff_pct = self._fee_model.effective_fee_pct(quote_amount)
            estimate = FeeEstimate(
                sell_fee_pct=eff_pct,
                buy_fee_pct=0,
                total_fee_pct=eff_pct,
                sell_fee_quote=fee_quote,
                buy_fee_quote=0,
                total_fee_quote=fee_quote,
                breakeven_move_pct=eff_pct * self.fee_safety_margin,
                is_profitable=False,
            )

        # Check if expected gain exceeds fees with safety margin
        min_required = estimate.breakeven_move_pct
        gain_after_fees = expected_gain_pct - estimate.total_fee_pct

        is_worthwhile = (
            expected_gain_pct >= min_required
            and gain_after_fees >= self.min_gain_after_fees_pct
        )

        estimate.is_profitable = is_worthwhile

        if is_worthwhile:
            logger.info(
                f"✅ Trade profitable: expected {expected_gain_pct*100:.2f}% "
                f"> breakeven {min_required*100:.2f}% "
                f"(net gain: {gain_after_fees*100:.2f}%, "
                f"fee: {estimate.total_fee_quote:.2f})"
            )
        else:
            logger.info(
                f"❌ Trade NOT worthwhile: expected {expected_gain_pct*100:.2f}% "
                f"< breakeven {min_required*100:.2f}% "
                f"(would lose {estimate.total_fee_quote - (quote_amount * expected_gain_pct):.2f})"
            )

        return is_worthwhile, estimate

    def get_optimal_trade_size(
        self,
        available_quote: float,
        expected_gain_pct: float,
        is_swap: bool = False,
    ) -> float:
        """
        Calculate optimal trade size given fee constraints.
        Larger trades dilute the fixed cost, but increase risk.
        """
        if expected_gain_pct <= 0:
            return 0.0

        # Start from max and work down
        for pct in [1.0, 0.75, 0.5, 0.25, 0.1]:
            amount = available_quote * pct
            worthwhile, _ = self.is_trade_worthwhile(amount, expected_gain_pct, is_swap)
            if worthwhile:
                return amount

        return 0.0

    def get_fee_summary(self) -> str:
        """Get a human-readable fee configuration summary."""
        swap_cost = self.trade_fee_pct * 2 * 100
        return (
            f"📊 Fee Config:\n"
            f"  Trade fee: {self.trade_fee_pct*100:.2f}%\n"
            f"  Swap cost (sell+buy): ~{swap_cost:.2f}%\n"
            f"  Safety margin: {self.fee_safety_margin}x\n"
            f"  Min gain after fees: {self.min_gain_after_fees_pct*100:.2f}%\n"
            f"  Min trade size: {self.min_trade_quote:.0f}\n"
            f"  Swap breakeven: ~{swap_cost * self.fee_safety_margin:.2f}% predicted move"
        )
