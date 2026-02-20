"""
High-Stakes Mode — Time-limited elevated trading permissions.

Controlled exclusively by the owner via Telegram:
    /highstakes 4h        → Enable for 4 hours
    /highstakes 2d        → Enable for 2 days
    /highstakes 30m       → Enable for 30 minutes
    /highstakes off       → Disable immediately
    /highstakes status    → Show current status

When active:
    - Higher max single trade amount
    - Higher portfolio allocation for swaps
    - Lower confidence threshold for execution
    - Higher-impact trades allowed without per-trade approval

Still respects ABSOLUTE RULES (safety net never disabled).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger
from src.utils.audit import AuditLog

logger = get_logger("core.high_stakes")


@dataclass
class HighStakesConfig:
    """Configuration overrides during high-stakes mode."""
    # Multiplier for max_single_trade_usd
    trade_size_multiplier: float = 2.5
    # Multiplier for swap allocation percentage
    swap_allocation_multiplier: float = 2.0
    # Reduced confidence threshold (standard is typically 0.7)
    min_confidence: float = 0.5
    # Allow swaps with lower expected gain (still must beat fees)
    min_swap_gain_pct: float = 0.01
    # Skip per-trade Telegram approval up to this amount
    auto_approve_up_to: float = 500.0


class HighStakesManager:
    """
    Manages time-limited high-stakes trading mode.
    Only activatable by authorized owner via Telegram.
    Thread-safe with automatic expiration.
    """

    def __init__(self, config: dict, audit: Optional[AuditLog] = None):
        self.config = config.get("high_stakes", {})
        self.audit = audit

        # Default high-stakes overrides
        self.hs_config = HighStakesConfig(
            trade_size_multiplier=self.config.get("trade_size_multiplier", 2.5),
            swap_allocation_multiplier=self.config.get("swap_allocation_multiplier", 2.0),
            min_confidence=self.config.get("min_confidence", 0.5),
            min_swap_gain_pct=self.config.get("min_swap_gain_pct", 0.01),
            auto_approve_up_to=self.config.get("auto_approve_up_to", self.config.get("auto_approve_up_to_usd", 500.0)),
        )

        # State
        self._active = False
        self._expires_at: Optional[datetime] = None
        self._activated_by: str = ""
        self._activated_at: Optional[datetime] = None
        self._reason: str = ""
        self._lock = threading.Lock()

        logger.info("⚡ High-Stakes Manager initialized (inactive)")

    @property
    def is_active(self) -> bool:
        """Check if high-stakes mode is currently active (thread-safe)."""
        with self._lock:
            if not self._active:
                return False

            # Check expiration
            if self._expires_at and datetime.now(timezone.utc) >= self._expires_at:
                self._deactivate("expired")
                return False

            return True

    @property
    def time_remaining(self) -> Optional[timedelta]:
        """Get time remaining in high-stakes mode."""
        with self._lock:
            if not self._active or not self._expires_at:
                return None
            remaining = self._expires_at - datetime.now(timezone.utc)
            return remaining if remaining.total_seconds() > 0 else timedelta(0)

    def activate(
        self,
        duration_str: str,
        activated_by: str = "owner",
        reason: str = "",
    ) -> tuple[bool, str]:
        """
        Activate high-stakes mode for a duration.

        Args:
            duration_str: Duration like "4h", "2d", "30m", "1h30m"
            activated_by: User ID who activated
            reason: Optional reason

        Returns:
            (success, message)
        """
        duration = self._parse_duration(duration_str)
        if duration is None:
            return False, (
                f"Invalid duration: '{duration_str}'. "
                "Use format: 30m, 4h, 2d, 1h30m"
            )

        # Safety: cap at 7 days
        max_duration = timedelta(days=7)
        if duration > max_duration:
            return False, "Maximum high-stakes duration is 7 days."

        with self._lock:
            now = datetime.now(timezone.utc)
            self._active = True
            self._expires_at = now + duration
            self._activated_by = activated_by
            self._activated_at = now
            self._reason = reason

        # Audit log
        if self.audit:
            self.audit.log(
                "high_stakes_activated",
                {
                    "activated_by": activated_by,
                    "duration": str(duration),
                    "expires_at": self._expires_at.isoformat(),
                    "reason": reason,
                    "config": {
                        "trade_multiplier": self.hs_config.trade_size_multiplier,
                        "swap_multiplier": self.hs_config.swap_allocation_multiplier,
                        "min_confidence": self.hs_config.min_confidence,
                    },
                },
                severity="warning",
            )

        msg = (
            f"⚡ HIGH-STAKES MODE ACTIVATED\n\n"
            f"Duration: {self._format_duration(duration)}\n"
            f"Expires: {self._expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Activated by: {activated_by}\n\n"
            f"📊 Overrides active:\n"
            f"  Trade size: {self.hs_config.trade_size_multiplier}x normal\n"
            f"  Swap allocation: {self.hs_config.swap_allocation_multiplier}x normal\n"
            f"  Min confidence: {self.hs_config.min_confidence}\n"
            f"  Auto-approve up to: ${self.hs_config.auto_approve_up_to:.0f}\n\n"
            f"⚠️ Absolute rules still enforced.\n"
            f"Send /highstakes off to deactivate early."
        )

        logger.warning(f"⚡ High-stakes ACTIVATED for {duration} by {activated_by}")
        return True, msg

    def deactivate(self, deactivated_by: str = "owner") -> str:
        """Manually deactivate high-stakes mode."""
        with self._lock:
            if not self._active:
                return "High-stakes mode is not active."
            self._deactivate(f"manual:{deactivated_by}")

        logger.info(f"⚡ High-stakes DEACTIVATED by {deactivated_by}")
        return "⚡ High-stakes mode DEACTIVATED. Trading at normal levels."

    def _deactivate(self, reason: str) -> None:
        """Internal deactivation (must be called under lock)."""
        self._active = False

        if self.audit:
            self.audit.log(
                "high_stakes_deactivated",
                {
                    "reason": reason,
                    "was_activated_by": self._activated_by,
                    "was_activated_at": self._activated_at.isoformat() if self._activated_at else "",
                },
                severity="info",
            )

        self._expires_at = None
        self._activated_by = ""
        self._activated_at = None

        logger.info(f"⚡ High-stakes deactivated: {reason}")

    def get_effective_limits(self, base_limits: dict) -> dict:
        """
        Get the effective trading limits, adjusted for high-stakes if active.

        Args:
            base_limits: Normal trading limits dict

        Returns:
            Adjusted limits dict
        """
        if not self.is_active:
            return base_limits

        adjusted = dict(base_limits)

        # Scale up trade sizes
        if "max_single_trade" in adjusted:
            adjusted["max_single_trade"] = (
                adjusted["max_single_trade"] * self.hs_config.trade_size_multiplier
            )

        # Scale up swap allocation
        if "swap_allocation_pct" in adjusted:
            adjusted["swap_allocation_pct"] = min(
                adjusted["swap_allocation_pct"] * self.hs_config.swap_allocation_multiplier,
                0.50,  # Never more than 50% even in high-stakes
            )

        # Lower confidence threshold
        if "min_confidence" in adjusted:
            adjusted["min_confidence"] = self.hs_config.min_confidence

        # Raise auto-approve threshold
        if "require_approval_above" in adjusted:
            adjusted["require_approval_above"] = self.hs_config.auto_approve_up_to

        return adjusted

    def get_status(self) -> str:
        """Get formatted status string."""
        if not self.is_active:
            return "⚡ High-stakes mode: INACTIVE\nSend /highstakes <duration> to activate."

        remaining = self.time_remaining
        remaining_str = self._format_duration(remaining) if remaining else "expired"

        return (
            f"⚡ HIGH-STAKES MODE: ACTIVE\n\n"
            f"⏰ Time remaining: {remaining_str}\n"
            f"📅 Expires: {self._expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"👤 Activated by: {self._activated_by}\n\n"
            f"📊 Active overrides:\n"
            f"  Trade size: {self.hs_config.trade_size_multiplier}x\n"
            f"  Swap allocation: {self.hs_config.swap_allocation_multiplier}x\n"
            f"  Min confidence: {self.hs_config.min_confidence}\n"
            f"  Auto-approve: up to ${self.hs_config.auto_approve_up_to_usd:.0f}"
        )

    def _parse_duration(self, s: str) -> Optional[timedelta]:
        """Parse a human duration string like '4h', '2d', '30m', '1h30m'."""
        s = s.strip().lower()
        if not s:
            return None

        total_seconds = 0
        current_num = ""

        for char in s:
            if char.isdigit() or char == ".":
                current_num += char
            elif char in ("m", "h", "d", "w"):
                if not current_num:
                    return None
                val = float(current_num)
                if char == "m":
                    total_seconds += val * 60
                elif char == "h":
                    total_seconds += val * 3600
                elif char == "d":
                    total_seconds += val * 86400
                elif char == "w":
                    total_seconds += val * 604800
                current_num = ""
            else:
                return None

        return timedelta(seconds=total_seconds) if total_seconds > 0 else None

    def _format_duration(self, td: Optional[timedelta]) -> str:
        """Format a timedelta into human-readable string."""
        if td is None:
            return "N/A"
        total = int(td.total_seconds())
        if total <= 0:
            return "expired"

        days = total // 86400
        hours = (total % 86400) // 3600
        minutes = (total % 3600) // 60

        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")

        return " ".join(parts) if parts else "<1m"
