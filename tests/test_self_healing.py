"""Tests for SelfHealingController (Phase 7)."""

from __future__ import annotations

import time

import pytest

from src.core.self_healing import SelfHealingController


@pytest.fixture
def ctrl(tmp_path):
    return SelfHealingController(
        disable_sharpe_floor=-0.5,
        disable_window=3,
        disable_cooldown=10,  # 10s for fast testing
        drift_relative_tol=0.4,
        drift_min_observations=5,
        heartbeat_timeout=2,
        audit_path=str(tmp_path / "audit.jsonl"),
    )


# --------------------------------------------------------------------- #
# Auto-disable / re-enable
# --------------------------------------------------------------------- #

class TestAutoDisable:
    def test_no_disable_under_threshold_count(self, ctrl):
        for _ in range(2):
            ctrl.record_sharpe("z", -1.0)
        out = ctrl.evaluate_strategy("z")
        assert "disabled" not in out["actions"]
        assert not ctrl.is_disabled("z")

    def test_disables_on_persistent_low_sharpe(self, ctrl):
        for _ in range(3):
            ctrl.record_sharpe("z", -1.0)
        out = ctrl.evaluate_strategy("z")
        assert "disabled" in out["actions"]
        assert ctrl.is_disabled("z")

    def test_no_disable_on_mixed(self, ctrl):
        ctrl.record_sharpe("z", -1.0)
        ctrl.record_sharpe("z", 0.5)
        ctrl.record_sharpe("z", -1.0)
        out = ctrl.evaluate_strategy("z")
        assert "disabled" not in out["actions"]

    def test_re_enables_after_cooldown(self, ctrl):
        ctrl._strategies.clear()
        for _ in range(3):
            ctrl.record_sharpe("z", -1.0)
        ctrl.evaluate_strategy("z")
        assert ctrl.is_disabled("z")
        # Force the cool-down to be in the past.
        ctrl._strategies["z"].disabled_until = time.time() - 1
        out = ctrl.evaluate_strategy("z")
        assert "re_enabled" in out["actions"]
        assert not ctrl.is_disabled("z")

    def test_force_enable(self, ctrl):
        for _ in range(3):
            ctrl.record_sharpe("z", -1.0)
        ctrl.evaluate_strategy("z")
        ctrl.force_enable("z")
        assert not ctrl.is_disabled("z")


# --------------------------------------------------------------------- #
# Drift detection
# --------------------------------------------------------------------- #

class TestDriftDetection:
    def test_no_drift_when_realised_matches(self, ctrl):
        for _ in range(10):
            ctrl.record_pnl("z", 0.005)
        out = ctrl.evaluate_strategy("z", wfo_expected_oos_return=0.005)
        assert "drift_detected" not in out["actions"]

    def test_drift_detected_when_diverges(self, ctrl):
        for _ in range(10):
            ctrl.record_pnl("z", -0.005)  # opposite of expected
        out = ctrl.evaluate_strategy("z", wfo_expected_oos_return=0.01)
        assert "drift_detected" in out["actions"]

    def test_drift_triggers_wfo_callback(self, ctrl):
        called = []
        for _ in range(10):
            ctrl.record_pnl("z", -0.01)
        out = ctrl.evaluate_strategy(
            "z",
            wfo_expected_oos_return=0.01,
            wfo_rerun_callback=lambda s: called.append(s),
        )
        assert called == ["z"]
        assert "wfo_rerun_requested" in out["actions"]

    def test_drift_needs_min_observations(self, ctrl):
        for _ in range(2):
            ctrl.record_pnl("z", -0.05)
        out = ctrl.evaluate_strategy("z", wfo_expected_oos_return=0.01)
        assert "drift_detected" not in out["actions"]


# --------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------- #

class TestCircuitBreaker:
    def test_breaker_disables_and_rolls_back(self, ctrl):
        called = []
        out = ctrl.on_circuit_breaker(
            "z",
            rollback_callback=lambda s: called.append(s) or True,
            reason="loss_streak",
        )
        assert "disabled_for_cooldown" in out["actions"]
        assert "rolled_back" in out["actions"]
        assert called == ["z"]
        assert ctrl.is_disabled("z")

    def test_breaker_rollback_no_prev(self, ctrl):
        out = ctrl.on_circuit_breaker(
            "z", rollback_callback=lambda s: False, reason="x",
        )
        assert "rollback_no_prev" in out["actions"]

    def test_breaker_without_rollback_disabled_in_init(self, tmp_path):
        c = SelfHealingController(
            rollback_on_breaker=False,
            disable_cooldown=5,
            audit_path=str(tmp_path / "a.jsonl"),
        )
        called = []
        out = c.on_circuit_breaker("z", rollback_callback=lambda s: called.append(s) or True)
        assert "rolled_back" not in out["actions"]
        assert called == []


# --------------------------------------------------------------------- #
# Heartbeats
# --------------------------------------------------------------------- #

class TestHeartbeats:
    def test_no_tiers(self, ctrl):
        assert ctrl.evaluate_heartbeats() == []
        assert ctrl.tier_status() == {}

    def test_fresh_heartbeat_not_degraded(self, ctrl):
        ctrl.heartbeat("microstructure")
        assert ctrl.evaluate_heartbeats() == []

    def test_stale_tier_marked_degraded(self, ctrl):
        ctrl.heartbeat("quant")
        # Force last_beat_at into the past.
        ctrl._tiers["quant"].last_beat_at = time.time() - 10
        deg = ctrl.evaluate_heartbeats()
        assert "quant" in deg
        assert ctrl.tier_status()["quant"]["degraded"] is True

    def test_recovery_clears_degraded(self, ctrl):
        ctrl.heartbeat("llm")
        ctrl._tiers["llm"].last_beat_at = time.time() - 10
        ctrl.evaluate_heartbeats()
        assert ctrl._tiers["llm"].degraded is True
        ctrl.heartbeat("llm")
        assert ctrl._tiers["llm"].degraded is False


# --------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------- #

class TestAudit:
    def test_audit_jsonl_written(self, ctrl, tmp_path):
        for _ in range(3):
            ctrl.record_sharpe("z", -1.0)
        ctrl.evaluate_strategy("z")
        path = tmp_path / "audit.jsonl"
        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert any('"event": "disabled"' in line for line in lines)
