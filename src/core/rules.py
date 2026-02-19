"""
Absolute Rules Engine — Hard limits that the agent can NEVER break.

These rules are checked before every trade execution and are not
overridable by any LLM reasoning, market condition, or user task.
The only way to change them is by editing the config file and restarting.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.models.trade import Trade, TradeAction
from src.utils.logger import get_logger

logger = get_logger("core.rules")


class RuleViolation:
    """Represents a violated absolute rule."""

    def __init__(self, rule_name: str, description: str, details: str = ""):
        self.rule_name = rule_name
        self.description = description
        self.details = details
        self.timestamp = datetime.now(timezone.utc)

    def __str__(self) -> str:
        return f"🚫 RULE VIOLATION [{self.rule_name}]: {self.description} — {self.details}"


class AbsoluteRules:
    """
    Enforces absolute rules that can never be broken.
    This is the final gatekeeper before any trade is executed.
    """

    def __init__(self, config: dict):
        self.max_single_trade_usd = config.get("max_single_trade_usd", 500)
        self.max_daily_spend_usd = config.get("max_daily_spend_usd", 2000)
        self.max_daily_loss_usd = config.get("max_daily_loss_usd", 300)
        self.max_portfolio_risk_pct = config.get("max_portfolio_risk_pct", 0.20)
        self.require_approval_above_usd = config.get("require_approval_above_usd", 200)
        self.never_trade_pairs = set(config.get("never_trade_pairs", []))
        self.only_trade_pairs = set(config.get("only_trade_pairs", []))
        self.min_trade_interval_seconds = config.get("min_trade_interval_seconds", 60)
        self.max_trades_per_day = config.get("max_trades_per_day", 20)
        self.max_cash_per_trade_pct = config.get("max_cash_per_trade_pct", 0.25)
        self.emergency_stop_portfolio_usd = config.get("emergency_stop_portfolio_usd", 5000)
        self.always_use_stop_loss = config.get("always_use_stop_loss", True)
        self.max_stop_loss_pct = config.get("max_stop_loss_pct", 0.05)

        # Thread safety — protects all daily counter reads/writes
        self._lock = threading.RLock()

        # Track daily stats
        self._daily_spend = 0.0
        self._daily_loss = 0.0
        self._daily_trade_count = 0
        self._last_reset_date: Optional[datetime] = None
        self._last_trade_time: Optional[datetime] = None

        logger.info("🔒 Absolute Rules Engine initialized")
        self._log_rules()

    def seed_daily_counters(self, db_path: str = "data/stats.db") -> None:
        """Seed daily counters from persisted trades to survive restarts.

        Should be called once during startup after the DB is known to exist.
        Safe to call even when the DB does not yet exist — logs a warning and
        continues with zero counters in that case.
        """
        try:
            today_start = (
                datetime.now(timezone.utc)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            )
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                # Only sum BUY trades for spend — sell proceeds are not an expense.
                row = conn.execute(
                    """SELECT COUNT(*) as cnt, COALESCE(SUM(usd_amount), 0) as spend
                       FROM trades WHERE ts >= ?""",
                    (today_start,),
                ).fetchone()
                spend_row = conn.execute(
                    """SELECT COALESCE(SUM(usd_amount), 0) as spend
                       FROM trades WHERE ts >= ? AND action = 'buy'""",
                    (today_start,),
                ).fetchone()
                if row:
                    self._daily_trade_count = int(row[0])
                if spend_row:
                    self._daily_spend = float(spend_row[0])

                loss_row = conn.execute(
                    """SELECT COALESCE(SUM(ABS(pnl)), 0) as loss
                       FROM trades WHERE ts >= ? AND pnl < 0""",
                    (today_start,),
                ).fetchone()
                if loss_row:
                    self._daily_loss = float(loss_row[0])

                self._last_reset_date = datetime.now(timezone.utc)
                logger.info(
                    f"📅 Daily counters seeded from DB — "
                    f"spend=${self._daily_spend:.2f}, loss=${self._daily_loss:.2f}, "
                    f"trades={self._daily_trade_count}"
                )
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"⚠️ Could not seed daily counters from DB (using zeros): {e}")

    def _log_rules(self) -> None:
        """Log all active rules."""
        logger.info("═══════════════════════════════════════════")
        logger.info("🔒 ABSOLUTE RULES (cannot be overridden):")
        logger.info(f"   Max single trade:     ${self.max_single_trade_usd:,.0f}")
        logger.info(f"   Max daily spend:      ${self.max_daily_spend_usd:,.0f}")
        logger.info(f"   Max daily loss:       ${self.max_daily_loss_usd:,.0f}")
        logger.info(f"   Max portfolio risk:   {self.max_portfolio_risk_pct:.0%}")
        logger.info(f"   Approval required >   ${self.require_approval_above_usd:,.0f}")
        logger.info(f"   Max trades/day:       {self.max_trades_per_day}")
        logger.info(f"   Min trade interval:   {self.min_trade_interval_seconds}s")
        logger.info(f"   Emergency stop below: ${self.emergency_stop_portfolio_usd:,.0f}")
        logger.info(f"   Always stop-loss:     {self.always_use_stop_loss}")
        if self.never_trade_pairs:
            logger.info(f"   Blacklisted pairs:    {self.never_trade_pairs}")
        if self.only_trade_pairs:
            logger.info(f"   Whitelisted pairs:    {self.only_trade_pairs}")
        logger.info("═══════════════════════════════════════════")

    def _reset_daily_if_needed(self) -> None:
        """Reset daily counters at midnight UTC."""
        now = datetime.now(timezone.utc)
        if self._last_reset_date is None or self._last_reset_date.date() < now.date():
            self._daily_spend = 0.0
            self._daily_loss = 0.0
            self._daily_trade_count = 0
            self._last_reset_date = now
            logger.info("📅 Daily counters reset")

    def check_trade(
        self,
        pair: str,
        action: TradeAction,
        usd_value: float,
        portfolio_value: float,
        cash_balance: float,
        has_stop_loss: bool = False,
    ) -> tuple[bool, list[RuleViolation], bool]:
        """
        Check if a proposed trade violates any absolute rules.

        Returns:
            (is_allowed, violations, needs_approval)

        Acquires self._lock for the entire evaluation so two concurrent callers
        (main loop + Telegram-approved trade) cannot both pass the same daily
        spend / trade-count limits simultaneously.
        """
        with self._lock:
            return self._check_trade_impl(
                pair, action, usd_value, portfolio_value, cash_balance, has_stop_loss
            )

    def _check_trade_impl(
        self,
        pair: str,
        action: TradeAction,
        usd_value: float,
        portfolio_value: float,
        cash_balance: float,
        has_stop_loss: bool = False,
    ) -> tuple[bool, list[RuleViolation], bool]:
        """Inner implementation — caller must hold self._lock."""
        self._reset_daily_if_needed()

        violations: list[RuleViolation] = []
        needs_approval = False
        now = datetime.now(timezone.utc)

        # --- Rule: Blacklisted pairs ---
        if pair in self.never_trade_pairs:
            violations.append(RuleViolation(
                "never_trade_pair",
                f"Pair {pair} is blacklisted",
                f"The pair {pair} is in the never_trade_pairs list",
            ))

        # --- Rule: Whitelist (if set) ---
        if self.only_trade_pairs and pair not in self.only_trade_pairs:
            violations.append(RuleViolation(
                "only_trade_pairs",
                f"Pair {pair} is not whitelisted",
                f"Only these pairs are allowed: {self.only_trade_pairs}",
            ))

        # --- Rule: Max single trade ---
        if usd_value > self.max_single_trade_usd:
            violations.append(RuleViolation(
                "max_single_trade",
                f"Trade value ${usd_value:,.2f} exceeds max ${self.max_single_trade_usd:,.0f}",
                "Reduce position size",
            ))

        # --- Rule: Max daily spend (BUY only — sells are returns, not expenses) ---
        if action == TradeAction.BUY and self._daily_spend + usd_value > self.max_daily_spend_usd:
            violations.append(RuleViolation(
                "max_daily_spend",
                f"Daily spend would be ${self._daily_spend + usd_value:,.2f}, max is ${self.max_daily_spend_usd:,.0f}",
                f"Already spent today: ${self._daily_spend:,.2f}",
            ))

        # --- Rule: Max daily loss ---
        if self._daily_loss >= self.max_daily_loss_usd:
            violations.append(RuleViolation(
                "max_daily_loss",
                f"Daily loss limit reached: ${self._daily_loss:,.2f}",
                "Trading suspended until tomorrow",
            ))

        # --- Rule: Max trades per day ---
        if self._daily_trade_count >= self.max_trades_per_day:
            violations.append(RuleViolation(
                "max_trades_per_day",
                f"Max {self.max_trades_per_day} trades/day reached",
                f"Trades today: {self._daily_trade_count}",
            ))

        # --- Rule: Min trade interval ---
        if self._last_trade_time:
            elapsed = (now - self._last_trade_time).total_seconds()
            if elapsed < self.min_trade_interval_seconds:
                violations.append(RuleViolation(
                    "min_trade_interval",
                    f"Only {elapsed:.0f}s since last trade, minimum is {self.min_trade_interval_seconds}s",
                    "Wait before trading again",
                ))

        # --- Rule: Max cash per trade (BUY only — not meaningful when selling an asset) ---
        if action == TradeAction.BUY and cash_balance > 0:
            cash_pct = usd_value / cash_balance
            if cash_pct > self.max_cash_per_trade_pct:
                violations.append(RuleViolation(
                    "max_cash_per_trade",
                    f"Trade uses {cash_pct:.0%} of cash, max is {self.max_cash_per_trade_pct:.0%}",
                    f"Cash: ${cash_balance:,.2f}, Trade: ${usd_value:,.2f}",
                ))

        # --- Rule: Emergency portfolio stop ---
        if portfolio_value < self.emergency_stop_portfolio_usd:
            violations.append(RuleViolation(
                "emergency_stop",
                f"Portfolio ${portfolio_value:,.2f} below emergency stop ${self.emergency_stop_portfolio_usd:,.0f}",
                "ALL TRADING HALTED",
            ))

        # --- Rule: Portfolio risk ---
        if portfolio_value > 0:
            risk_pct = usd_value / portfolio_value
            if risk_pct > self.max_portfolio_risk_pct:
                violations.append(RuleViolation(
                    "max_portfolio_risk",
                    f"Trade risks {risk_pct:.0%} of portfolio, max is {self.max_portfolio_risk_pct:.0%}",
                    "Reduce position size",
                ))

        # --- Rule: Stop-loss required ---
        if self.always_use_stop_loss and action == TradeAction.BUY and not has_stop_loss:
            violations.append(RuleViolation(
                "always_use_stop_loss",
                "Stop-loss is required for all buy orders",
                "Set a stop-loss before opening a position",
            ))

        # --- Check: Needs approval ---
        if usd_value > self.require_approval_above_usd and not violations:
            needs_approval = True
            logger.info(
                f"⚠️ Trade ${usd_value:,.2f} exceeds approval threshold "
                f"${self.require_approval_above_usd:,.0f} — requesting approval"
            )

        # Log violations
        for v in violations:
            logger.warning(str(v))

        is_allowed = len(violations) == 0
        return is_allowed, violations, needs_approval

    def record_trade(self, usd_value: float, action: str = "buy") -> None:
        """Record a trade for daily tracking.

        Only BUY trades count against ``_daily_spend``; SELL trades still
        increment the trade-count (rate-limiting) but do not consume spend budget.
        """
        with self._lock:
            self._reset_daily_if_needed()
            if action == "buy":
                self._daily_spend += usd_value
            self._daily_trade_count += 1
            self._last_trade_time = datetime.now(timezone.utc)

    def record_loss(self, loss_usd: float) -> None:
        """Record a loss for daily tracking."""
        with self._lock:
            self._reset_daily_if_needed()
            self._daily_loss += abs(loss_usd)

    def get_status(self) -> dict:
        """Get current rules status."""
        with self._lock:
            self._reset_daily_if_needed()
            return {
                "daily_spend": self._daily_spend,
                "daily_spend_remaining": max(0, self.max_daily_spend_usd - self._daily_spend),
                "daily_loss": self._daily_loss,
                "daily_loss_remaining": max(0, self.max_daily_loss_usd - self._daily_loss),
                "trades_today": self._daily_trade_count,
                "trades_remaining": max(0, self.max_trades_per_day - self._daily_trade_count),
                "max_single_trade": self.max_single_trade_usd,
                "approval_threshold": self.require_approval_above_usd,
            }

    def get_rules_text(self) -> str:
        """Get a human-readable summary of all rules."""
        return (
            "🔒 **Absolute Rules**\n"
            f"• Max single trade: ${self.max_single_trade_usd:,.0f}\n"
            f"• Max daily spend: ${self.max_daily_spend_usd:,.0f}\n"
            f"• Max daily loss: ${self.max_daily_loss_usd:,.0f}\n"
            f"• Max portfolio risk: {self.max_portfolio_risk_pct:.0%}\n"
            f"• Approval required above: ${self.require_approval_above_usd:,.0f}\n"
            f"• Max trades/day: {self.max_trades_per_day}\n"
            f"• Min trade interval: {self.min_trade_interval_seconds}s\n"
            f"• Emergency stop below: ${self.emergency_stop_portfolio_usd:,.0f}\n"
            f"• Stop-loss required: {'Yes' if self.always_use_stop_loss else 'No'}\n"
        )
