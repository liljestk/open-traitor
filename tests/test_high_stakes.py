"""Tests for HighStakesManager — time-limited elevated trading."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.core.high_stakes import HighStakesManager, HighStakesConfig


@pytest.fixture
def hs():
    return HighStakesManager(config={})


@pytest.fixture
def hs_custom():
    return HighStakesManager(config={
        "high_stakes": {
            "trade_size_multiplier": 3.0,
            "swap_allocation_multiplier": 2.5,
            "min_confidence": 0.4,
            "auto_approve_up_to": 1000.0,
        }
    })


# ═══════════════════════════════════════════════════════════════════════
# Activation / Deactivation
# ═══════════════════════════════════════════════════════════════════════

class TestActivation:
    def test_inactive_by_default(self, hs):
        assert hs.is_active is False

    def test_activate_valid_duration(self, hs):
        ok, msg = hs.activate("4h")
        assert ok
        assert hs.is_active is True
        assert "ACTIVATED" in msg

    def test_activate_minutes(self, hs):
        ok, _ = hs.activate("30m")
        assert ok
        assert hs.time_remaining.total_seconds() > 0

    def test_activate_days(self, hs):
        ok, _ = hs.activate("2d")
        assert ok
        r = hs.time_remaining
        assert r.total_seconds() > 86400  # > 1 day

    def test_activate_combined(self, hs):
        ok, _ = hs.activate("1h30m")
        assert ok
        r = hs.time_remaining
        assert r.total_seconds() > 5000

    def test_max_7_days(self, hs):
        ok, msg = hs.activate("30d")
        assert not ok
        assert "7 days" in msg
        assert hs.is_active is False

    def test_invalid_duration(self, hs):
        ok, msg = hs.activate("xyz")
        assert not ok
        assert hs.is_active is False

    def test_empty_duration(self, hs):
        ok, msg = hs.activate("")
        assert not ok

    def test_deactivate(self, hs):
        hs.activate("4h")
        msg = hs.deactivate()
        assert "DEACTIVATED" in msg
        assert hs.is_active is False

    def test_deactivate_when_inactive(self, hs):
        msg = hs.deactivate()
        assert "not active" in msg


# ═══════════════════════════════════════════════════════════════════════
# Expiration
# ═══════════════════════════════════════════════════════════════════════

class TestExpiration:
    def test_auto_expires(self, hs):
        hs.activate("1m")
        # Manually set expiration in the past
        hs._expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert hs.is_active is False

    def test_time_remaining_when_inactive(self, hs):
        assert hs.time_remaining is None


# ═══════════════════════════════════════════════════════════════════════
# Effective Limits
# ═══════════════════════════════════════════════════════════════════════

class TestEffectiveLimits:
    def test_no_change_when_inactive(self, hs):
        base = {"max_single_trade": 100, "min_confidence": 0.7}
        result = hs.get_effective_limits(base)
        assert result == base

    def test_scales_trade_size(self, hs):
        hs.activate("4h")
        base = {"max_single_trade": 100}
        result = hs.get_effective_limits(base)
        assert result["max_single_trade"] == 250.0  # 100 * 2.5

    def test_lowers_confidence(self, hs):
        hs.activate("4h")
        base = {"min_confidence": 0.7}
        result = hs.get_effective_limits(base)
        assert result["min_confidence"] == 0.5

    def test_swap_allocation_capped_at_50pct(self, hs):
        hs.activate("4h")
        base = {"swap_allocation_pct": 0.40}
        result = hs.get_effective_limits(base)
        assert result["swap_allocation_pct"] <= 0.50

    def test_approval_threshold_raised(self, hs):
        hs.activate("4h")
        base = {"require_approval_above": 100}
        result = hs.get_effective_limits(base)
        assert result["require_approval_above"] >= 500  # auto_approve_up_to

    def test_approval_never_lowered(self, hs):
        """M29 fix: high-stakes should never lower the base ceiling."""
        hs.hs_config.auto_approve_up_to = 50  # lower than base
        hs.activate("4h")
        base = {"require_approval_above": 200}
        result = hs.get_effective_limits(base)
        assert result["require_approval_above"] >= 200

    def test_custom_config(self, hs_custom):
        hs_custom.activate("4h")
        base = {"max_single_trade": 100}
        result = hs_custom.get_effective_limits(base)
        assert result["max_single_trade"] == 300.0  # 100 * 3.0

    def test_expired_returns_base(self, hs):
        hs.activate("1m")
        hs._expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        base = {"max_single_trade": 100}
        result = hs.get_effective_limits(base)
        assert result["max_single_trade"] == 100


# ═══════════════════════════════════════════════════════════════════════
# Duration Parsing
# ═══════════════════════════════════════════════════════════════════════

class TestDurationParsing:
    def test_parse_hours(self, hs):
        td = hs._parse_duration("4h")
        assert td == timedelta(hours=4)

    def test_parse_minutes(self, hs):
        td = hs._parse_duration("30m")
        assert td == timedelta(minutes=30)

    def test_parse_days(self, hs):
        td = hs._parse_duration("2d")
        assert td == timedelta(days=2)

    def test_parse_weeks(self, hs):
        td = hs._parse_duration("1w")
        assert td == timedelta(weeks=1)

    def test_parse_combined(self, hs):
        td = hs._parse_duration("1h30m")
        assert td == timedelta(hours=1, minutes=30)

    def test_parse_invalid(self, hs):
        assert hs._parse_duration("abc") is None
        assert hs._parse_duration("") is None
        assert hs._parse_duration("x4h") is None


# ═══════════════════════════════════════════════════════════════════════
# Status
# ═══════════════════════════════════════════════════════════════════════

class TestStatus:
    def test_inactive_status(self, hs):
        s = hs.get_status()
        assert "INACTIVE" in s

    def test_active_status(self, hs):
        hs.activate("4h")
        s = hs.get_status()
        assert "ACTIVE" in s
        assert "2.5x" in s  # trade multiplier

    def test_expired_status(self, hs):
        hs.activate("1m")
        hs._expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        s = hs.get_status()
        assert "INACTIVE" in s


# ═══════════════════════════════════════════════════════════════════════
# Duration Formatting
# ═══════════════════════════════════════════════════════════════════════

class TestFormatDuration:
    def test_format_hours(self, hs):
        assert "4h" in hs._format_duration(timedelta(hours=4))

    def test_format_minutes(self, hs):
        assert "30m" in hs._format_duration(timedelta(minutes=30))

    def test_format_days_hours(self, hs):
        result = hs._format_duration(timedelta(days=1, hours=3))
        assert "1d" in result
        assert "3h" in result

    def test_format_none(self, hs):
        assert hs._format_duration(None) == "N/A"

    def test_format_expired(self, hs):
        assert hs._format_duration(timedelta(seconds=-5)) == "expired"


# ═══════════════════════════════════════════════════════════════════════
# Audit Logging
# ═══════════════════════════════════════════════════════════════════════

class TestAuditLogging:
    def test_activation_logs_audit(self):
        audit = MagicMock()
        hs = HighStakesManager(config={}, audit=audit)
        hs.activate("4h", activated_by="testuser")
        audit.log.assert_called_once()
        args = audit.log.call_args
        assert args[0][0] == "high_stakes_activated"
        assert args[0][1]["activated_by"] == "testuser"
