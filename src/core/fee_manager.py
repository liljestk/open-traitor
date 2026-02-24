"""
Fee Manager — Fee-aware trading decisions.

Supports pluggable fee models:

  - ``crypto_percentage`` (default): Percentage-based taker/maker fees.
    Coinbase Advanced Trade fee tiers (based on 30-day volume):
      Tier 1 (<$1K):    Taker 0.60%, Maker 0.40%
      Tier 2 (<$10K):   Taker 0.40%, Maker 0.25%
      Tier 3 (<$50K):   Taker 0.25%, Maker 0.15%

  - ``equity_flat_plus_pct``: Flat minimum fee per trade plus a percentage.
    Typical for Scandinavian stock brokers (flat + percentage model).

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


class EquityPerShareFeeModel(BaseFeeModel):
    """
    Per-share fee model (typical for IBKR).

    Fee = max(min_fee, shares × per_share_fee)
    IBKR US equities: $0.0035/share, $0.35 minimum per order.
    """

    def __init__(
        self,
        per_share_fee: float = 0.0035,
        min_fee: float = 0.35,
        max_fee_pct: float = 0.01,  # 1% cap on fees
    ):
        self.per_share_fee = per_share_fee
        self.min_fee = min_fee
        self.max_fee_pct = max_fee_pct

    def estimate_trade_fee(self, quote_amount: float, is_maker: bool = False) -> float:
        # Approximate shares from quote amount assuming typical prices
        # The actual per-share calc is: shares × per_share_fee
        # We approximate: fee = max(min_fee, quote_amount × effective_rate)
        # With avg share price ~$50, 100 shares = $5000, fee = $0.35
        # effective rate ≈ 0.007% for large orders
        fee = max(self.min_fee, quote_amount * self.per_share_fee / 50.0)
        # Cap at max_fee_pct of trade value
        return min(fee, quote_amount * self.max_fee_pct)

    def estimate_trade_fee_shares(
        self, n_shares: float, price_per_share: float
    ) -> float:
        """Precise fee calculation when share count is known."""
        return max(self.min_fee, n_shares * self.per_share_fee)

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

    if model_type == "equity_per_share":
        return EquityPerShareFeeModel(
            per_share_fee=config.get("per_share_fee", 0.0035),
            min_fee=config.get("min_fee", 0.35),
            max_fee_pct=config.get("max_fee_pct", 0.01),
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
        # This is the ABSOLUTE floor — exchange minimum (Coinbase: ~1 EUR)
        self.min_trade_quote = self.config.get("min_trade_quote", self.config.get("min_trade_usd", 1.0))

        # Dynamic minimum: percentage of portfolio (scales with account size)
        # For a 6.80 EUR account with 1%, minimum = 0.068 EUR (we use the higher of floor or %)
        self.min_trade_pct = self.config.get("min_trade_pct", 0.01)  # 1% of portfolio

        # Cooldown period after a swap to prevent churn (seconds)
        self.swap_cooldown_seconds = self.config.get("swap_cooldown_seconds", 3600)

        logger.info(
            f"💰 Fee Manager: trade fee={self.trade_fee_pct*100:.2f}%, "
            f"safety margin={self.fee_safety_margin}x, "
            f"min gain after fees={self.min_gain_after_fees_pct*100:.2f}%, "
            f"min trade floor={self.min_trade_quote:.2f}, "
            f"min trade pct={self.min_trade_pct*100:.1f}%"
        )

    def get_dynamic_min_trade(self, portfolio_value: float = 0.0) -> float:
        """Compute the effective minimum trade size based on portfolio value.

        Returns the higher of:
          - Absolute floor (min_trade_quote from config, default 1.0 EUR)
          - Percentage of portfolio (min_trade_pct, default 1%)

        This allows micro-accounts (e.g. 6.80 EUR) to trade while still
        maintaining a sane floor for larger accounts.
        """
        pct_min = portfolio_value * self.min_trade_pct if portfolio_value > 0 else 0.0
        return max(self.min_trade_quote, pct_min)

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

        # C7 fix: compute per-leg fee percentage from the model
        sell_fee_pct = self._fee_model.effective_fee_pct(quote_amount, is_maker)
        buy_fee_pct = self._fee_model.effective_fee_pct(quote_amount, is_maker)

        return FeeEstimate(
            sell_fee_pct=sell_fee_pct,
            buy_fee_pct=buy_fee_pct,
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
        portfolio_value: float = 0.0,
    ) -> tuple[bool, FeeEstimate]:
        """
        Determine if a trade is worth executing after fees.

        Args:
            quote_amount: Trade size in quote currency
            expected_gain_pct: Expected price movement (0.05 = 5%)
            is_swap: Whether this is a swap (2x fees)
            n_legs: Override leg count for route-aware fee calc (None = auto)
            portfolio_value: Current portfolio value for dynamic min trade calc

        Returns:
            (is_worthwhile, fee_estimate)
        """
        # Too small to trade — use dynamic minimum based on portfolio size
        effective_min = self.get_dynamic_min_trade(portfolio_value)
        if quote_amount < effective_min:
            logger.debug(f"Trade too small: {quote_amount:.4f} < {effective_min:.4f} (portfolio={portfolio_value:.2f})")
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
            # Round-trip fee: a buy must eventually be sold.
            # Total cost = buy fee + sell fee = 2 × one-way fee.
            buy_fee_quote = self.estimate_trade_fees(quote_amount)
            sell_fee_quote = self.estimate_trade_fees(quote_amount)
            total_fee_quote = buy_fee_quote + sell_fee_quote
            buy_fee_pct = self._fee_model.effective_fee_pct(quote_amount)
            sell_fee_pct = self._fee_model.effective_fee_pct(quote_amount)
            total_fee_pct = buy_fee_pct + sell_fee_pct
            estimate = FeeEstimate(
                sell_fee_pct=sell_fee_pct,
                buy_fee_pct=buy_fee_pct,
                total_fee_pct=total_fee_pct,
                sell_fee_quote=sell_fee_quote,
                buy_fee_quote=buy_fee_quote,
                total_fee_quote=total_fee_quote,
                breakeven_move_pct=total_fee_pct * self.fee_safety_margin,
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
        portfolio_value: float = 0.0,
    ) -> float:
        """
        Calculate optimal trade size given fee constraints.
        Larger trades dilute the fixed cost, but increase risk.
        """
        if expected_gain_pct <= 0:
            return 0.0

        # Start from max and work down
        for pct in [1.0, 0.75, 0.5, 0.25, 0.1, 0.05]:
            amount = available_quote * pct
            worthwhile, _ = self.is_trade_worthwhile(
                amount, expected_gain_pct, is_swap,
                portfolio_value=portfolio_value,
            )
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
            f"  Min trade floor: {self.min_trade_quote:.2f}\n"
            f"  Min trade pct of portfolio: {self.min_trade_pct*100:.1f}%\n"
            f"  Swap breakeven: ~{swap_cost * self.fee_safety_margin:.2f}% predicted move"
        )
