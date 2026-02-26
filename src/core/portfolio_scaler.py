"""
Portfolio Scaler — Dynamic parameter scaling based on account size.

Adapts position sizing, risk limits, pair counts, and fee thresholds
so the system works from micro (€5) to institutional (€100K+) accounts.

Config values in coinbase.yaml / settings.yaml are treated as the BASE
values for the MEDIUM tier (€500–€5K).  The scaler multiplies them by
tier-specific factors so a €5 account concentrates capital into fewer,
larger-relative trades while a €50K account stays conservative.

Tiers
─────
  MICRO   < €50       → aggressive concentration, 1–2 pairs
  SMALL   €50–€500    → moderate, 2–3 pairs
  MEDIUM  €500–€5K    → standard (config values used as-is)
  LARGE   €5K–€50K    → conservative, config values as-is
  WHALE   > €50K      → extra conservative

The scaler never INCREASES risk for large accounts — it only loosens
constraints for small ones where the config defaults would make trading
mechanically impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("core.portfolio_scaler")


# ══════════════════════════════════════════════════════════════════════════
# Tier definitions
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Tier:
    name: str
    max_position_pct: float       # Risk manager cap per trade
    max_cash_per_trade_pct: float  # AbsoluteRules % of cash per trade
    max_portfolio_risk_pct: float  # AbsoluteRules % of portfolio per trade
    max_active_pairs: int          # Orchestrator cap
    max_open_positions: int        # Risk manager cap
    min_gain_after_fees_pct: float # FeeManager minimum net gain
    take_profit_pct: float         # Risk manager TP
    stop_loss_pct: float           # Risk manager SL


# Tier table — values are absolute, NOT multipliers
_TIERS: list[tuple[float, Tier]] = [
    # (upper_bound_eur, Tier)
    (50, Tier(
        name="MICRO",
        max_position_pct=0.40,
        max_cash_per_trade_pct=0.50,
        max_portfolio_risk_pct=0.50,
        max_active_pairs=6,     # scan 6 candidates; risk_manager gates on max_open_positions
        max_open_positions=2,
        min_gain_after_fees_pct=0.015,
        take_profit_pct=0.05,   # 5% — more achievable on crypto; captures quick moves
        stop_loss_pct=0.03,     # 3% — tighter SL gives better risk-reward (5:3 = 1.67:1)
    )),
    (500, Tier(
        name="SMALL",
        max_position_pct=0.25,
        max_cash_per_trade_pct=0.35,
        max_portfolio_risk_pct=0.35,
        max_active_pairs=8,     # scan 8 candidates; hold at most 3
        max_open_positions=3,
        min_gain_after_fees_pct=0.010,
        take_profit_pct=0.07,
        stop_loss_pct=0.05,
    )),
    (5_000, Tier(
        name="MEDIUM",
        max_position_pct=0.15,
        max_cash_per_trade_pct=0.25,
        max_portfolio_risk_pct=0.20,
        max_active_pairs=5,
        max_open_positions=5,
        min_gain_after_fees_pct=0.005,
        take_profit_pct=0.06,
        stop_loss_pct=0.045,
    )),
    (50_000, Tier(
        name="LARGE",
        max_position_pct=0.08,
        max_cash_per_trade_pct=0.25,
        max_portfolio_risk_pct=0.20,
        max_active_pairs=8,
        max_open_positions=8,
        min_gain_after_fees_pct=0.005,
        take_profit_pct=0.06,
        stop_loss_pct=0.045,
    )),
    (float("inf"), Tier(
        name="WHALE",
        max_position_pct=0.03,
        max_cash_per_trade_pct=0.15,
        max_portfolio_risk_pct=0.15,
        max_active_pairs=10,
        max_open_positions=10,
        min_gain_after_fees_pct=0.003,
        take_profit_pct=0.05,
        stop_loss_pct=0.04,
    )),
]


def get_tier(portfolio_value: float) -> Tier:
    """Return the tier for the given portfolio value."""
    for upper, tier in _TIERS:
        if portfolio_value < upper:
            return tier
    return _TIERS[-1][1]  # fallback to WHALE


class PortfolioScaler:
    """
    Singleton-ish scaler that components call to get portfolio-aware limits.

    Usage:
        scaler = PortfolioScaler(config)
        scaler.update(portfolio_value)
        tier = scaler.tier  # current Tier dataclass
    """

    def __init__(self, config: dict):
        self._config = config
        self._portfolio_value: float = 0.0
        self._tier: Tier = _TIERS[2][1]  # default MEDIUM
        self._scaling_enabled: bool = config.get(
            "trading", {}
        ).get("portfolio_scaling", True)

    @property
    def tier(self) -> Tier:
        return self._tier

    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value

    def update(self, portfolio_value: float) -> Tier:
        """
        Recompute tier based on current portfolio value.
        Called once per cycle by the orchestrator.
        Returns the new tier.
        """
        if not self._scaling_enabled:
            return self._tier

        old_tier = self._tier
        self._portfolio_value = portfolio_value
        self._tier = get_tier(portfolio_value)

        if self._tier.name != old_tier.name:
            logger.info(
                f"📊 Portfolio tier changed: {old_tier.name} → {self._tier.name} "
                f"(portfolio: €{portfolio_value:,.2f})"
            )

        return self._tier

    def get_max_position_pct(self) -> float:
        """Max position size as fraction of portfolio."""
        return self._tier.max_position_pct

    def get_max_cash_per_trade_pct(self) -> float:
        """Max fraction of cash for a single trade."""
        return self._tier.max_cash_per_trade_pct

    def get_max_portfolio_risk_pct(self) -> float:
        """Max fraction of portfolio at risk per trade."""
        return self._tier.max_portfolio_risk_pct

    def get_max_active_pairs(self) -> int:
        """Max pairs the screener should select."""
        return self._tier.max_active_pairs

    def get_max_open_positions(self) -> int:
        """Max concurrent open positions."""
        return self._tier.max_open_positions

    def get_min_gain_after_fees_pct(self) -> float:
        """Minimum net gain after fees to justify a trade."""
        return self._tier.min_gain_after_fees_pct

    def get_take_profit_pct(self) -> float:
        """Default take-profit percentage."""
        return self._tier.take_profit_pct

    def get_stop_loss_pct(self) -> float:
        """Default stop-loss percentage."""
        return self._tier.stop_loss_pct

    def summary(self) -> str:
        """Human-readable summary for logs/dashboard."""
        t = self._tier
        return (
            f"📊 Portfolio Scaler [{t.name}] (€{self._portfolio_value:,.2f}):\n"
            f"  Position cap: {t.max_position_pct:.0%} | "
            f"Cash/trade: {t.max_cash_per_trade_pct:.0%} | "
            f"Portfolio risk: {t.max_portfolio_risk_pct:.0%}\n"
            f"  Pairs: {t.max_active_pairs} | "
            f"Positions: {t.max_open_positions} | "
            f"Min gain: {t.min_gain_after_fees_pct:.1%}\n"
            f"  TP: {t.take_profit_pct:.1%} | "
            f"SL: {t.stop_loss_pct:.1%}"
        )
