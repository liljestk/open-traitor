"""
Self-healing controller (Phase 7).

Watches the live trading system for failure modes and takes
*automatic* recovery actions:

  • Auto-disables a strategy whose rolling Sharpe drops below
    ``disable_sharpe_floor`` for ``disable_window`` consecutive
    samples (cool-down before re-enable).
  • Auto-requests a Walk-Forward Optimization rerun for a strategy
    whose realised PnL has *drifted* materially from its WFO-validated
    OOS expectation (relative tolerance gate).
  • Auto-rolls back the live parameter file (via WFOAutoPromoter)
    when a circuit-breaker trips and the most recent promotion is
    suspect.
  • Maintains heartbeat watchdogs per ``tier`` (microstructure /
    quant / LLM-advisory): if no heartbeat in ``heartbeat_timeout``
    seconds, marks the tier degraded and reports.

Pure controller. State is in-memory + best-effort JSONL audit. No
network, no DB. Designed to be invoked once per orchestrator cycle.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from src.utils.logger import get_logger

logger = get_logger("core.self_healing")


@dataclass
class StrategyHealth:
    name: str
    recent_sharpes: deque = field(default_factory=lambda: deque(maxlen=10))
    recent_pnls: deque = field(default_factory=lambda: deque(maxlen=50))
    disabled_until: float = 0.0
    last_event: str = ""
    last_event_at: float = 0.0


@dataclass
class TierHeartbeat:
    name: str
    last_beat_at: float = 0.0
    degraded: bool = False


class SelfHealingController:
    """Single-process controller; holds state across cycles."""

    def __init__(
        self,
        *,
        disable_sharpe_floor: float = -0.5,
        disable_window: int = 5,
        disable_cooldown: int = 6 * 3600,
        drift_relative_tol: float = 0.5,    # PnL diverges by >50% from expected
        drift_min_observations: int = 30,
        heartbeat_timeout: int = 120,
        rollback_on_breaker: bool = True,
        audit_path: Optional[str] = None,
    ) -> None:
        self.disable_sharpe_floor = float(disable_sharpe_floor)
        self.disable_window = int(disable_window)
        self.disable_cooldown = int(disable_cooldown)
        self.drift_relative_tol = float(drift_relative_tol)
        self.drift_min_observations = int(drift_min_observations)
        self.heartbeat_timeout = int(heartbeat_timeout)
        self.rollback_on_breaker = bool(rollback_on_breaker)
        self.audit_path = Path(audit_path) if audit_path else None

        self._strategies: dict[str, StrategyHealth] = {}
        self._tiers: dict[str, TierHeartbeat] = {}

    # ------------------------------------------------------------------ #
    # Strategy health
    # ------------------------------------------------------------------ #

    def record_sharpe(self, strategy: str, sharpe: float) -> None:
        h = self._get_strategy(strategy)
        h.recent_sharpes.append(float(sharpe))

    def record_pnl(self, strategy: str, pnl: float) -> None:
        h = self._get_strategy(strategy)
        h.recent_pnls.append(float(pnl))

    def evaluate_strategy(
        self,
        strategy: str,
        *,
        wfo_expected_oos_return: Optional[float] = None,
        wfo_rerun_callback: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Apply healing rules. Returns a dict describing actions taken.
        """
        actions: dict = {"strategy": strategy, "actions": []}
        h = self._get_strategy(strategy)
        now = time.time()

        # Re-enable expired cool-downs.
        if h.disabled_until and now >= h.disabled_until:
            h.disabled_until = 0.0
            # Reset rolling Sharpe so a single fresh good window is enough to
            # *not* immediately re-disable on the same stale data.
            h.recent_sharpes.clear()
            actions["actions"].append("re_enabled")
            self._audit({"event": "re_enabled", "strategy": strategy})

        # Auto-disable on persistent low Sharpe.
        if (
            h.disabled_until == 0.0
            and len(h.recent_sharpes) >= self.disable_window
            and all(s < self.disable_sharpe_floor for s in list(h.recent_sharpes)[-self.disable_window:])
        ):
            h.disabled_until = now + self.disable_cooldown
            h.last_event = "auto_disabled"
            h.last_event_at = now
            actions["actions"].append("disabled")
            actions["disabled_until"] = h.disabled_until
            self._audit({"event": "disabled", "strategy": strategy, "until": h.disabled_until})
            logger.warning(
                f"🛑 Self-healing disabled {strategy} until "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(h.disabled_until))}"
            )

        # Drift detection vs WFO expectation.
        if (
            wfo_expected_oos_return is not None
            and len(h.recent_pnls) >= self.drift_min_observations
        ):
            realised = sum(h.recent_pnls) / len(h.recent_pnls)
            expected = float(wfo_expected_oos_return)
            denom = max(1e-9, abs(expected))
            if abs(realised - expected) / denom >= self.drift_relative_tol:
                actions["actions"].append("drift_detected")
                actions["realised"] = realised
                actions["expected"] = expected
                self._audit({
                    "event": "drift_detected", "strategy": strategy,
                    "realised": realised, "expected": expected,
                })
                if wfo_rerun_callback:
                    try:
                        wfo_rerun_callback(strategy)
                        actions["actions"].append("wfo_rerun_requested")
                    except Exception as e:  # pragma: no cover
                        logger.error(f"WFO rerun callback failed: {e}")

        return actions

    def is_disabled(self, strategy: str) -> bool:
        h = self._strategies.get(strategy)
        if h is None:
            return False
        return h.disabled_until > time.time()

    def force_enable(self, strategy: str) -> None:
        h = self._get_strategy(strategy)
        h.disabled_until = 0.0
        h.last_event = "force_enabled"
        h.last_event_at = time.time()
        self._audit({"event": "force_enabled", "strategy": strategy})

    # ------------------------------------------------------------------ #
    # Circuit-breaker rollback
    # ------------------------------------------------------------------ #

    def on_circuit_breaker(
        self,
        strategy: str,
        *,
        rollback_callback: Optional[Callable[[str], bool]] = None,
        reason: str = "",
    ) -> dict:
        """Called when a circuit breaker trips. May roll back live params."""
        actions: dict = {"strategy": strategy, "reason": reason, "actions": []}
        h = self._get_strategy(strategy)
        h.last_event = f"breaker:{reason}"
        h.last_event_at = time.time()
        h.disabled_until = max(h.disabled_until, time.time() + self.disable_cooldown)
        actions["actions"].append("disabled_for_cooldown")

        if self.rollback_on_breaker and rollback_callback:
            try:
                ok = bool(rollback_callback(strategy))
                actions["actions"].append("rolled_back" if ok else "rollback_no_prev")
            except Exception as e:  # pragma: no cover
                logger.error(f"Rollback callback failed: {e}")
                actions["actions"].append("rollback_error")

        self._audit({"event": "circuit_breaker", "strategy": strategy, "reason": reason})
        logger.warning(f"⚡ Circuit breaker on {strategy}: {reason} → actions={actions['actions']}")
        return actions

    # ------------------------------------------------------------------ #
    # Heartbeats
    # ------------------------------------------------------------------ #

    def heartbeat(self, tier: str) -> None:
        t = self._tiers.setdefault(tier, TierHeartbeat(name=tier))
        t.last_beat_at = time.time()
        if t.degraded:
            t.degraded = False
            self._audit({"event": "tier_recovered", "tier": tier})

    def evaluate_heartbeats(self) -> list[str]:
        """Return tier names currently degraded (no heartbeat in window)."""
        now = time.time()
        degraded: list[str] = []
        for name, t in self._tiers.items():
            stale = (now - t.last_beat_at) > self.heartbeat_timeout
            if stale and not t.degraded:
                t.degraded = True
                self._audit({"event": "tier_degraded", "tier": name})
                logger.warning(f"💤 Tier degraded: {name} (no heartbeat for {self.heartbeat_timeout}s)")
            if stale:
                degraded.append(name)
        return degraded

    def tier_status(self) -> dict[str, dict]:
        now = time.time()
        return {
            name: {
                "degraded": t.degraded,
                "seconds_since_beat": round(now - t.last_beat_at, 1) if t.last_beat_at else None,
            }
            for name, t in self._tiers.items()
        }

    # ------------------------------------------------------------------ #

    def _get_strategy(self, name: str) -> StrategyHealth:
        if name not in self._strategies:
            self._strategies[name] = StrategyHealth(name=name)
        return self._strategies[name]

    def _audit(self, payload: dict) -> None:
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"ts": int(time.time()), **payload}
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception:
            pass


__all__ = ["SelfHealingController", "StrategyHealth", "TierHeartbeat"]
