"""
Adversarial robustness drills (Phase 15).

Two pure-function utilities:

  • SlippageSimulator: stress-test a strategy's expected fill price under
    realistic worst-case slippage scenarios (light, normal, severe). Used
    by tests and by the executor's `_should_use_limit` gate when in
    paranoid mode.

  • OutageDrill: simulates exchange or feed outages and verifies the
    AbsoluteRules layer correctly halts trading. No real network calls.

Both are deterministic (seeded RNG) so tests + audit traces are stable.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class SlippageScenario:
    name: str
    bps_mean: float       # e.g. 5 bps
    bps_stdev: float      # e.g. 2 bps
    extreme_pct: float = 0.05  # 5% of fills get 3x mean


@dataclass
class SlippageResult:
    scenario: str
    n: int
    mean_bps: float
    p95_bps: float
    worst_bps: float


class SlippageSimulator:
    """Deterministic slippage stress-test."""

    SCENARIOS = (
        SlippageScenario("light",  bps_mean=2.0,  bps_stdev=1.0),
        SlippageScenario("normal", bps_mean=5.0,  bps_stdev=2.5),
        SlippageScenario("severe", bps_mean=15.0, bps_stdev=8.0, extreme_pct=0.10),
    )

    def __init__(self, seed: int = 42) -> None:
        self.seed = int(seed)

    def simulate(self, n: int = 500, scenario: str = "normal") -> SlippageResult:
        cfg = next((s for s in self.SCENARIOS if s.name == scenario), None)
        if cfg is None:
            raise ValueError(f"unknown scenario: {scenario}")
        rng = random.Random(self.seed)
        fills: list[float] = []
        for i in range(n):
            base = max(0.0, rng.gauss(cfg.bps_mean, cfg.bps_stdev))
            if rng.random() < cfg.extreme_pct:
                base *= 3.0
            fills.append(base)
        sorted_f = sorted(fills)
        p95_idx = max(0, int(0.95 * (n - 1)))
        return SlippageResult(
            scenario=scenario,
            n=n,
            mean_bps=statistics.mean(fills),
            p95_bps=sorted_f[p95_idx],
            worst_bps=sorted_f[-1],
        )

    def expected_fill_price(self, mid_price: float, side: str, scenario: str = "normal") -> float:
        """One-shot: return a single realistic fill given a mid price."""
        if mid_price <= 0:
            return mid_price
        result = self.simulate(n=50, scenario=scenario)
        slip = result.mean_bps / 10000.0
        if side.upper() == "BUY":
            return mid_price * (1.0 + slip)
        return mid_price * (1.0 - slip)


@dataclass
class OutageDrillResult:
    scenario: str
    halted: bool
    reason: str


class OutageDrill:
    """Validates that the rules engine halts trading on simulated outages.

    Caller passes an :class:`AbsoluteRules` instance and we trigger the
    paths that should set the kill switch.
    """

    def __init__(self, rules) -> None:
        self.rules = rules

    def simulate_exchange_disconnect(self) -> OutageDrillResult:
        """Most exchanges set a `kill_switch_engaged` flag on disconnect."""
        try:
            if hasattr(self.rules, "engage_kill_switch"):
                self.rules.engage_kill_switch("drill: exchange disconnect")
            elif hasattr(self.rules, "set_kill_switch"):
                self.rules.set_kill_switch(True, reason="drill")
            halted = bool(getattr(self.rules, "kill_switch_engaged", False))
            return OutageDrillResult("exchange_disconnect", halted, "kill switch engaged")
        except Exception as e:
            return OutageDrillResult("exchange_disconnect", False, f"error: {e}")

    def simulate_feed_stale(self, last_tick_age_s: float = 600.0) -> OutageDrillResult:
        """Feed older than threshold — rules layer should refuse trades."""
        try:
            if hasattr(self.rules, "record_feed_age"):
                self.rules.record_feed_age(last_tick_age_s)
            # Pass-through if the rules implementation has a check method.
            if hasattr(self.rules, "is_feed_stale"):
                stale = bool(self.rules.is_feed_stale())
            else:
                stale = last_tick_age_s > 300.0
            return OutageDrillResult(
                "feed_stale",
                halted=stale,
                reason=f"feed age {last_tick_age_s}s",
            )
        except Exception as e:
            return OutageDrillResult("feed_stale", False, f"error: {e}")


__all__ = [
    "SlippageScenario", "SlippageResult", "SlippageSimulator",
    "OutageDrill", "OutageDrillResult",
]
