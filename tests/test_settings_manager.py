"""Tests for SettingsManager — validation, presets, autonomous guards, I/O."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest
import yaml

from src.utils import settings_manager as sm


@pytest.fixture
def settings_file(tmp_path):
    """Create a temporary settings file with base config."""
    cfg = {
        "trading": {
            "mode": "paper",
            "exchange": "coinbase",
            "pairs": ["BTC-EUR"],
            "min_confidence": 0.7,
            "max_open_positions": 5,
            "interval": 120,
        },
        "absolute_rules": {
            "max_single_trade": 200,
            "max_daily_spend": 1000,
            "max_daily_loss": 200,
            "max_portfolio_risk_pct": 0.10,
            "require_approval_above": 100,
            "max_trades_per_day": 20,
            "max_cash_per_trade_pct": 0.15,
            "always_use_stop_loss": True,
            "max_stop_loss_pct": 0.05,
        },
        "risk": {
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.05,
            "trailing_stop_pct": 0.02,
            "max_position_pct": 0.10,
            "max_total_exposure_pct": 0.50,
            "max_drawdown_pct": 0.10,
            "max_trades_per_hour": 5,
        },
        "rotation": {
            "enabled": True,
            "autonomous_allocation_pct": 0.1,
        },
        "fees": {
            "trade_fee_pct": 0.006,
            "maker_fee_pct": 0.004,
        },
    }
    path = str(tmp_path / "settings.yaml")
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


# ═══════════════════════════════════════════════════════════════════════
# Field Validation
# ═══════════════════════════════════════════════════════════════════════

class TestValidateField:
    def test_valid_float(self):
        ok, err, val = sm.validate_field("absolute_rules", "max_single_trade", 100.0)
        assert ok
        assert val == 100.0

    def test_min_bound(self):
        ok, err, val = sm.validate_field("absolute_rules", "max_single_trade", 0.5)
        assert not ok
        assert "must be >=" in err

    def test_max_bound(self):
        ok, err, val = sm.validate_field("absolute_rules", "max_single_trade", 2_000_000)
        assert not ok
        assert "must be <=" in err

    def test_bool_field(self):
        ok, _, val = sm.validate_field("absolute_rules", "always_use_stop_loss", True)
        assert ok and val is True

    def test_bool_from_string(self):
        ok, _, val = sm.validate_field("absolute_rules", "always_use_stop_loss", "true")
        assert ok and val is True

    def test_enum_valid(self):
        ok, _, val = sm.validate_field("trading", "mode", "paper")
        assert ok and val == "paper"

    def test_enum_invalid(self):
        ok, err, _ = sm.validate_field("trading", "mode", "yolo")
        assert not ok
        assert "must be one of" in err

    def test_list_field(self):
        ok, _, val = sm.validate_field("trading", "pairs", ["BTC-EUR"])
        assert ok

    def test_list_field_invalid(self):
        ok, err, _ = sm.validate_field("trading", "pairs", "BTC-EUR")
        assert not ok

    def test_unknown_field_passes_through(self):
        ok, _, val = sm.validate_field("trading", "nonexistent_xyz", 42)
        assert ok
        assert val == 42

    def test_int_field(self):
        ok, _, val = sm.validate_field("trading", "interval", 60)
        assert ok and val == 60

    def test_int_below_min(self):
        ok, err, _ = sm.validate_field("trading", "interval", 5)
        assert not ok

    def test_nested_section_validation(self):
        ok, _, val = sm.validate_field("analysis.technical", "rsi_period", 14)
        assert ok and val == 14


# ═══════════════════════════════════════════════════════════════════════
# Section Validation
# ═══════════════════════════════════════════════════════════════════════

class TestValidateSection:
    def test_all_valid(self):
        ok, errs, cast = sm.validate_section("absolute_rules", {
            "max_single_trade": 300,
            "max_daily_spend": 1500,
        })
        assert ok
        assert cast["max_single_trade"] == 300

    def test_partial_failure(self):
        ok, errs, cast = sm.validate_section("absolute_rules", {
            "max_single_trade": 300,
            "max_daily_spend": -1,  # below min
        })
        assert not ok
        assert len(errs) == 1
        assert "max_daily_spend" in errs[0]


# ═══════════════════════════════════════════════════════════════════════
# Load / Save
# ═══════════════════════════════════════════════════════════════════════

class TestLoadSave:
    def test_load_settings(self, settings_file):
        cfg = sm.load_settings(settings_file)
        assert cfg["trading"]["mode"] == "paper"
        assert cfg["absolute_rules"]["max_single_trade"] == 200

    def test_save_settings_atomic(self, settings_file):
        cfg = sm.load_settings(settings_file)
        cfg["trading"]["interval"] = 300
        sm.save_settings(cfg, settings_file)
        reloaded = sm.load_settings(settings_file)
        assert reloaded["trading"]["interval"] == 300

    def test_get_full_settings(self, settings_file):
        result = sm.get_full_settings(settings_file)
        assert "settings" in result
        assert "trading_enabled" in result
        assert "sections" in result

    def test_get_section(self, settings_file):
        trading = sm.get_section("trading", settings_file)
        assert trading["mode"] == "paper"


# ═══════════════════════════════════════════════════════════════════════
# Update Section
# ═══════════════════════════════════════════════════════════════════════

class TestUpdateSection:
    def test_update_persists(self, settings_file):
        ok, msg, changes = sm.update_section(
            "trading", {"min_confidence": 0.8}, settings_file
        )
        assert ok
        assert changes["min_confidence"] == 0.8
        reloaded = sm.load_settings(settings_file)
        assert reloaded["trading"]["min_confidence"] == 0.8

    def test_update_rejects_invalid(self, settings_file):
        ok, msg, changes = sm.update_section(
            "trading", {"interval": 5}, settings_file  # below min 10
        )
        assert not ok
        assert changes == {}

    def test_update_rules(self, settings_file):
        ok, _, changes = sm.update_absolute_rules(
            {"max_single_trade": 350}, settings_file
        )
        assert ok
        assert changes["max_single_trade"] == 350

    def test_update_trading_params(self, settings_file):
        ok, _, changes = sm.update_trading_params(
            {"min_confidence": 0.6}, settings_file
        )
        assert ok


# ═══════════════════════════════════════════════════════════════════════
# Presets
# ═══════════════════════════════════════════════════════════════════════

class TestPresets:
    def test_apply_disabled(self, settings_file):
        ok, _, changes = sm.apply_preset("disabled", settings_file)
        assert ok
        cfg = sm.load_settings(settings_file)
        assert cfg["absolute_rules"]["max_single_trade"] == 0
        assert cfg["trading"]["min_confidence"] == 1.1

    def test_apply_conservative(self, settings_file):
        ok, _, changes = sm.apply_preset("conservative", settings_file)
        assert ok
        cfg = sm.load_settings(settings_file)
        assert cfg["absolute_rules"]["max_single_trade"] == 50

    def test_apply_moderate(self, settings_file):
        ok, _, changes = sm.apply_preset("moderate", settings_file)
        assert ok
        cfg = sm.load_settings(settings_file)
        assert cfg["absolute_rules"]["max_single_trade"] == 150

    def test_apply_aggressive(self, settings_file):
        ok, _, changes = sm.apply_preset("aggressive", settings_file)
        assert ok
        cfg = sm.load_settings(settings_file)
        assert cfg["absolute_rules"]["max_single_trade"] == 500

    def test_apply_unknown_preset(self, settings_file):
        ok, err, _ = sm.apply_preset("godmode", settings_file)
        assert not ok
        assert "Unknown preset" in err

    def test_trading_enabled_after_aggressive(self, settings_file):
        sm.apply_preset("aggressive", settings_file)
        assert sm.is_trading_enabled(settings_file) is True

    def test_trading_disabled_after_disabled(self, settings_file):
        sm.apply_preset("disabled", settings_file)
        assert sm.is_trading_enabled(settings_file) is False


# ═══════════════════════════════════════════════════════════════════════
# Telegram Safety Tiers
# ═══════════════════════════════════════════════════════════════════════

class TestTelegramTiers:
    def test_safe_sections(self):
        assert sm.is_telegram_allowed("absolute_rules") == "safe"
        assert sm.is_telegram_allowed("risk") == "safe"
        assert sm.is_telegram_allowed("rotation") == "safe"

    def test_semi_safe_sections(self):
        assert sm.is_telegram_allowed("telegram") == "semi_safe"
        assert sm.is_telegram_allowed("news") == "semi_safe"

    def test_blocked_sections(self):
        assert sm.is_telegram_allowed("llm") == "blocked"
        assert sm.is_telegram_allowed("dashboard") == "blocked"
        assert sm.is_telegram_allowed("health") == "blocked"


# ═══════════════════════════════════════════════════════════════════════
# Autonomous Update Validation
# ═══════════════════════════════════════════════════════════════════════

class TestAutonomousUpdates:
    def test_allowed_section(self):
        ok, errs, clamped = sm.validate_autonomous_update(
            "risk", {"stop_loss_pct": 0.04}
        )
        assert ok
        assert clamped["stop_loss_pct"] == 0.04

    def test_blocked_section(self):
        ok, errs, _ = sm.validate_autonomous_update(
            "llm", {"model": "gpt-4"}
        )
        assert not ok
        assert "not allowed" in errs[0]

    def test_blocked_field(self):
        ok, errs, _ = sm.validate_autonomous_update(
            "trading", {"mode": "live"}
        )
        assert not ok
        assert "blocked" in errs[0]

    def test_clamping_min(self):
        ok, errs, clamped = sm.validate_autonomous_update(
            "trading", {"min_confidence": 0.1}
        )
        assert ok
        assert clamped["min_confidence"] >= 0.3  # autonomous floor

    def test_clamping_max(self):
        ok, errs, clamped = sm.validate_autonomous_update(
            "trading", {"min_confidence": 0.99}
        )
        assert ok
        assert clamped["min_confidence"] <= 0.95  # autonomous ceiling

    def test_unknown_field_rejected(self):
        ok, errs, _ = sm.validate_autonomous_update(
            "trading", {"nonexistent_field": 42}
        )
        assert not ok
        assert "not in the autonomous-allowed field list" in errs[0]

    def test_always_use_stop_loss_blocked(self):
        ok, errs, _ = sm.validate_autonomous_update(
            "absolute_rules", {"always_use_stop_loss": False}
        )
        assert not ok

    def test_risk_stop_loss_clamped(self):
        ok, _, clamped = sm.validate_autonomous_update(
            "risk", {"stop_loss_pct": 0.50}
        )
        assert ok
        assert clamped["stop_loss_pct"] <= 0.20

    def test_interval_min_guard(self):
        ok, _, clamped = sm.validate_autonomous_update(
            "trading", {"interval": 10}
        )
        assert ok
        assert clamped["interval"] >= 30

    def test_append_only_lists(self, settings_file):
        """Autonomous updates cannot remove items from safety-critical lists."""
        with patch.object(sm, "load_settings", return_value={
            "absolute_rules": {"never_trade_pairs": ["SCAM-USD"]},
        }):
            ok, _, clamped = sm.validate_autonomous_update(
                "absolute_rules", {"never_trade_pairs": []}
            )
            assert ok
            assert "SCAM-USD" in clamped["never_trade_pairs"]


# ═══════════════════════════════════════════════════════════════════════
# Runtime Push
# ═══════════════════════════════════════════════════════════════════════

class TestRuntimePush:
    def test_push_to_rules(self):
        rules = MagicMock()
        rules._lock = MagicMock()
        rules.max_single_trade = 100
        config = {}
        sm.push_to_runtime(rules, config, {"absolute_rules.max_single_trade": 300})
        assert rules.max_single_trade == 300

    def test_push_to_config(self):
        config = {"risk": {"stop_loss_pct": 0.03}}
        sm.push_to_runtime(None, config, {"risk.stop_loss_pct": 0.05})
        assert config["risk"]["stop_loss_pct"] == 0.05

    def test_push_blocked_attribute(self):
        rules = MagicMock()
        rules._lock = MagicMock()
        config = {}
        # Trying to set a non-allowlisted attribute
        sm.push_to_runtime(rules, config, {"absolute_rules.dangerous_attr": True})
        assert not hasattr(rules, "dangerous_attr") or rules.dangerous_attr != True


# ═══════════════════════════════════════════════════════════════════════
# Schema Metadata
# ═══════════════════════════════════════════════════════════════════════

class TestSchemaMetadata:
    def test_get_schema_metadata_has_sections(self):
        meta = sm.get_schema_metadata()
        assert "absolute_rules" in meta
        assert "trading" in meta
        assert "risk" in meta
        assert "analysis" in meta

    def test_schema_metadata_field_types(self):
        meta = sm.get_schema_metadata()
        fields = meta["absolute_rules"]["fields"]
        assert fields["max_single_trade"]["type"] == "float"
        assert fields["always_use_stop_loss"]["type"] == "bool"

    def test_nested_schema(self):
        meta = sm.get_schema_metadata()
        assert "nested" in meta["analysis"]
        assert "technical" in meta["analysis"]["nested"]


# ═══════════════════════════════════════════════════════════════════════
# Preset Summary
# ═══════════════════════════════════════════════════════════════════════

class TestPresetSummary:
    def test_disabled_summary(self):
        s = sm.get_preset_summary("disabled")
        assert "Disabled" in s

    def test_moderate_summary(self):
        s = sm.get_preset_summary("moderate")
        assert "150" in s  # max_single_trade

    def test_unknown_preset_summary(self):
        s = sm.get_preset_summary("godmode")
        assert "Unknown" in s


# ═══════════════════════════════════════════════════════════════════════
# Autonomous Schema Summary
# ═══════════════════════════════════════════════════════════════════════

class TestAutonomousSchemaSummary:
    def test_has_allowed_sections(self):
        summary = sm.get_autonomous_schema_summary()
        assert "risk" in summary
        assert "trading" in summary
        assert "absolute_rules" in summary

    def test_does_not_have_blocked_sections(self):
        summary = sm.get_autonomous_schema_summary()
        assert "llm" not in summary
        assert "dashboard" not in summary
