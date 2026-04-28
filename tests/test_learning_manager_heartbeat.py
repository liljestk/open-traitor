"""Regression test: long-running ALE subsystems must keep the orchestrator
cycle watchdog heartbeat fresh.

Root cause this guards against (April 2026): weekly Auto-WFO runs ~1000+
backtests synchronously inside ``LearningManager.tick``, which is called
from the orchestrator main loop. While it ran, no ``bump_heartbeat()`` calls
happened, so the cycle watchdog (timeout = max(interval*8, 1800s)) tripped,
``os._exit(1)`` fired, and the container restart-looped. Fix: a daemon
thread inside ``_run_subsystem`` bumps the heartbeat every 30s while a
subsystem is running.
"""
from __future__ import annotations

import asyncio
import time
import threading
from unittest.mock import MagicMock

import pytest

from src.core.managers import learning_manager as lm_mod
from src.core.managers.learning_manager import LearningManager


class _FakeOrch:
    """Minimal Orchestrator stand-in for LearningManager construction."""

    def __init__(self) -> None:
        self.bump_count = 0
        self._lock = threading.Lock()
        self.stats_db = MagicMock()
        self.stats_db._get_conn.return_value.__enter__.return_value = MagicMock()
        self.config = {"trading": {"exchange": "coinbase"}}
        self.audit = MagicMock()
        self.exchange = MagicMock()
        self.llm = MagicMock()

    def bump_heartbeat(self) -> None:
        with self._lock:
            self.bump_count += 1


@pytest.fixture
def lm(monkeypatch):
    """Construct a LearningManager with all heavy init bypassed."""
    monkeypatch.setattr(LearningManager, "_ensure_tables", lambda self: None)
    monkeypatch.setattr(LearningManager, "_restore_last_runs", lambda self: None)
    monkeypatch.setattr(LearningManager, "_init_subsystems", lambda self: None)
    monkeypatch.setattr(LearningManager, "_persist_run",
                        lambda self, *a, **kw: None)
    return LearningManager(_FakeOrch())


def test_long_subsystem_pumps_heartbeat(lm, monkeypatch):
    """A subsystem that runs longer than the pump interval must
    cause multiple heartbeat bumps (initial + at least one timer tick)."""
    # Speed up the pump cadence so the test stays fast.
    monkeypatch.setattr(lm_mod, "_HEARTBEAT_PUMP_INTERVAL_S", 0.1)

    async def _slow_subsystem() -> dict:
        # Sleep long enough to cross several pump cadences.
        await asyncio.sleep(0.6)
        return {"ok": True}

    asyncio.run(lm._run_subsystem("auto_wfo", 1, _slow_subsystem))

    # Initial bump + ≥1 timer-driven bump + final bump in finally.
    assert lm.orch.bump_count >= 3, (
        f"Expected ≥3 heartbeat bumps for a long subsystem, got "
        f"{lm.orch.bump_count}"
    )


def test_short_subsystem_still_bumps_heartbeat(lm):
    """Even a fast subsystem must bump at least once (initial + final)."""

    async def _fast_subsystem() -> dict:
        return {"ok": True}

    asyncio.run(lm._run_subsystem("scorecard", 1, _fast_subsystem))

    assert lm.orch.bump_count >= 1, (
        f"Expected ≥1 heartbeat bump even for instant subsystem, got "
        f"{lm.orch.bump_count}"
    )


def test_failing_subsystem_still_bumps_heartbeat(lm):
    """A subsystem that raises must NOT leak the pump thread, and the
    finally-bump must still execute so the next watchdog window starts
    fresh."""

    async def _broken_subsystem() -> dict:
        raise RuntimeError("boom")

    result = asyncio.run(lm._run_subsystem("ensemble", 1, _broken_subsystem))

    assert result["status"] == "error"
    assert lm.orch.bump_count >= 1
    # Pump thread should not be alive after subsystem returns.
    pump_threads = [t for t in threading.enumerate()
                    if t.name.startswith("ale-heartbeat-")]
    # Allow a brief join window — should be empty almost immediately.
    deadline = time.monotonic() + 2.0
    while pump_threads and time.monotonic() < deadline:
        time.sleep(0.05)
        pump_threads = [t for t in threading.enumerate()
                        if t.name.startswith("ale-heartbeat-") and t.is_alive()]
    assert not pump_threads, f"Leaked pump threads: {pump_threads}"
