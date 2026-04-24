"""
Quant Substrate (Phase 9) — single integration point that wires the
per-profile CapitalAllocator, SelfHealingController, and SignalEdgeLibrary
into the live orchestrator.

The dashboard's lazy factories in ``src/dashboard/deps.py`` point at the
same on-disk paths under ``data/<profile>/...`` so anything the
orchestrator writes here is visible to ``/api/quant/*`` on next request.

No IPC required — file/JSONL persistence is the contract.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("core.quant_substrate")


@dataclass
class _CycleSnapshot:
    cycle_id: int
    timestamp: float
    pnl_by_strategy: dict[str, float]
    regime: str


class QuantSubstrate:
    """One per orchestrator. Holds the per-profile quant artefacts and
    exposes recording hooks the orchestrator / agents can call.
    """

    def __init__(self, profile: str, *, data_root: str = "data") -> None:
        self.profile = (profile or "default").lower()
        self.data_root = os.path.join(data_root, self.profile)
        os.makedirs(os.path.join(self.data_root, "audit"), exist_ok=True)

        # Lazily import to avoid hard dependencies during partial test runs.
        self.allocator = None
        self.healing = None
        self.edges = None
        self._lock = threading.Lock()

        try:
            from src.core.capital_allocator import CapitalAllocator
            self.allocator = CapitalAllocator(
                state_path=os.path.join(self.data_root, "allocator_state.json"),
                audit_path=os.path.join(self.data_root, "audit", "capital_allocator.jsonl"),
            )
        except Exception as e:
            logger.warning("substrate.allocator_unavailable err=%s", e)

        try:
            from src.core.self_healing import SelfHealingController
            self.healing = SelfHealingController(
                audit_path=os.path.join(self.data_root, "audit", "self_healing.jsonl"),
            )
        except Exception as e:
            logger.warning("substrate.healing_unavailable err=%s", e)

        try:
            from src.analysis.signal_edge_library import (
                SignalEdgeLibrary, InMemorySignalEdgeStore,
            )
            self.edges = SignalEdgeLibrary(
                store=InMemorySignalEdgeStore(),
                exchange=self.profile,
            )
        except Exception as e:
            logger.warning("substrate.edges_unavailable err=%s", e)

    # ------------------------------------------------------------------ #
    # Recording hooks
    # ------------------------------------------------------------------ #

    def register_strategies(self, strategies: list[str]) -> None:
        if not strategies or self.allocator is None:
            return
        with self._lock:
            try:
                self.allocator.register(strategies)
            except Exception as e:
                logger.warning("substrate.register_failed err=%s", e)

    def record_strategy_pnl(self, strategy: str, pnl: float) -> None:
        """Log a single PnL observation. Uses bounded log-return-like input."""
        if not math.isfinite(pnl):
            return
        with self._lock:
            if self.healing is not None:
                try:
                    self.healing.record_pnl(strategy, pnl)
                except Exception as e:
                    logger.warning("substrate.record_pnl_failed err=%s", e)

    def record_sharpe(self, strategy: str, sharpe: float) -> None:
        if not math.isfinite(sharpe) or self.healing is None:
            return
        with self._lock:
            try:
                self.healing.record_sharpe(strategy, sharpe)
            except Exception as e:
                logger.warning("substrate.record_sharpe_failed err=%s", e)

    def heartbeat(self, tier: str) -> None:
        if self.healing is None:
            return
        with self._lock:
            try:
                if hasattr(self.healing, "heartbeat"):
                    self.healing.heartbeat(tier)
            except Exception as e:
                logger.warning("substrate.heartbeat_failed err=%s", e)

    def record_signal_sample(
        self,
        *,
        signal_name: str,
        regime: str,
        score: float,
        forward_return: float,
        pair: str,
    ) -> None:
        if self.edges is None:
            return
        if not (math.isfinite(score) and math.isfinite(forward_return)):
            return
        with self._lock:
            try:
                self.edges.record_sample(
                    signal_name=signal_name,
                    regime=regime,
                    score=score,
                    forward_return=forward_return,
                    pair=pair,
                )
            except Exception as e:
                logger.warning("substrate.record_sample_failed err=%s", e)

    def update_allocator(self, pnl_by_strategy: dict[str, float]) -> dict[str, float]:
        """Apply one EG step. Returns current weights."""
        if self.allocator is None or not pnl_by_strategy:
            return {}
        with self._lock:
            try:
                return self.allocator.update(pnl_by_strategy)
            except Exception as e:
                logger.warning("substrate.update_allocator_failed err=%s", e)
                return {}

    def evaluate_health(self, strategies: list[str]) -> list[dict]:
        """Run the healing controller's per-strategy evaluation. Returns
        a list of action dicts."""
        if self.healing is None:
            return []
        out: list[dict] = []
        with self._lock:
            for s in strategies:
                try:
                    out.append(self.healing.evaluate_strategy(s))
                except Exception as e:
                    logger.warning("substrate.evaluate_failed strat=%s err=%s", s, e)
        return out

    # ------------------------------------------------------------------ #
    # Convenience snapshot for logging
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        out: dict = {"profile": self.profile}
        if self.allocator is not None:
            try:
                out["weights"] = self.allocator.weights()
            except Exception:
                pass
        if self.healing is not None and getattr(self.healing, "_strategies", None):
            try:
                out["strategies_tracked"] = sorted(self.healing._strategies.keys())
            except Exception:
                pass
        return out


__all__ = ["QuantSubstrate"]
