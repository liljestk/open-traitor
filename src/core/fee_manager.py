"""
Fee Manager — Fee-aware trading decisions.

Coinbase Advanced Trade fee tiers (based on 30-day volume):
  Tier 1 (<$1K):    Taker 0.60%, Maker 0.40%
  Tier 2 (<$10K):   Taker 0.40%, Maker 0.25%
  Tier 3 (<$50K):   Taker 0.25%, Maker 0.15%

A swap (sell A → buy B) costs TWO trades worth of fees.
We must ensure expected gain > total fees or we lose money.
"""

from __future__ import annotations

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


class FeeManager:
    """
    Manages fee calculations and determines if trades are worth executing.

    Core principle: NEVER lose money to fees.
    A trade is only worth it if:
        expected_gain > total_fees * fee_safety_margin

    For swaps (sell A → buy B):
        expected_relative_gain > 2 * trade_fee * safety_margin
    """

    # Coinbase fee tiers (simplification — uses conservative tier)
    TAKER_FEE_PCT = 0.006   # 0.60% (worst case, <$1K volume)
    MAKER_FEE_PCT = 0.004   # 0.40%

    # We use taker fees as default since market orders = taker
    DEFAULT_FEE_PCT = TAKER_FEE_PCT

    def __init__(self, config: dict):
        self.config = config.get("fees", {})

        # Override fee rates from config
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
        """Estimate fees for a single trade."""
        fee_pct = self.maker_fee_pct if is_maker else self.trade_fee_pct
        return quote_amount * fee_pct

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
        fee_pct = self.maker_fee_pct if is_maker else self.trade_fee_pct

        # Compound fees across N legs
        remaining = quote_amount
        total_fee = 0.0
        leg_fees: list[float] = []
        for _ in range(n_legs):
            leg_fee = remaining * fee_pct
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
            estimate = FeeEstimate(
                sell_fee_pct=self.trade_fee_pct,
                buy_fee_pct=0,
                total_fee_pct=self.trade_fee_pct,
                sell_fee_quote=fee_quote,
                buy_fee_quote=0,
                total_fee_quote=fee_quote,
                breakeven_move_pct=self.trade_fee_pct * self.fee_safety_margin,
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
