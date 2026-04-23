"""
Capital allocator across strategies (Phase 4).

Online Mirror Descent / Exponentiated-Gradient algorithm — assigns
weights to a *set of strategies* based on their realised PnL stream.
This is the meta-layer above PortfolioOptimizer (which sizes the
asset basket *inside* a strategy's universe).

Properties:
  • Multiplicative updates → automatic exploration (never zeros a
    strategy permanently; recoverable on regime change).
  • No-regret guarantee vs. best fixed weight in hindsight.
  • Online, single-pass, O(K) per update for K strategies.
  • Self-healing: a strategy that goes "dead" (no signals → 0 PnL
    samples) drifts toward the prior over time, so reactivating it
    later costs no warm-up.

Persistence is JSON via a tiny atomic write. Audit trail of weight
trajectories is append-only JSONL under
``data/<exchange>/audit/capital_allocator.jsonl``.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class AllocatorState:
    """Persisted state of the allocator."""
    weights: dict[str, float] = field(default_factory=dict)
    cumulative_pnl: dict[str, float] = field(default_factory=dict)
    sample_count: dict[str, int] = field(default_factory=dict)
    last_updated: float = 0.0

    def to_dict(self) -> dict:
        return {
            "weights": dict(self.weights),
            "cumulative_pnl": dict(self.cumulative_pnl),
            "sample_count": dict(self.sample_count),
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AllocatorState":
        return cls(
            weights=dict(d.get("weights") or {}),
            cumulative_pnl=dict(d.get("cumulative_pnl") or {}),
            sample_count=dict(d.get("sample_count") or {}),
            last_updated=float(d.get("last_updated") or 0.0),
        )


class CapitalAllocator:
    """Online EG (Hedge / OMD with negative-entropy regularizer)."""

    def __init__(
        self,
        *,
        eta: float = 0.10,
        min_weight: float = 0.02,
        max_weight: float = 0.50,
        prior_strength: float = 5.0,
        state_path: Optional[str] = None,
        audit_path: Optional[str] = None,
    ) -> None:
        if eta <= 0:
            raise ValueError("eta must be > 0")
        if not (0 <= min_weight < max_weight <= 1.0):
            raise ValueError("require 0 <= min_weight < max_weight <= 1")
        self.eta = float(eta)
        self.min_weight = float(min_weight)
        self.max_weight = float(max_weight)
        self.prior_strength = float(prior_strength)
        self.state_path = Path(state_path) if state_path else None
        self.audit_path = Path(audit_path) if audit_path else None
        self.state: AllocatorState = self._load() if self.state_path else AllocatorState()

    # ------------------------------------------------------------------ #
    # Update / query
    # ------------------------------------------------------------------ #

    def register(self, strategies: list[str]) -> None:
        """Ensure every strategy has a starting (uniform) weight."""
        if not strategies:
            return
        for s in strategies:
            self.state.weights.setdefault(s, 0.0)
            self.state.cumulative_pnl.setdefault(s, 0.0)
            self.state.sample_count.setdefault(s, 0)
        self._renorm_with_prior(strategies)

    def update(self, pnl_by_strategy: dict[str, float]) -> dict[str, float]:
        """
        Apply one EG step using the latest period PnL per strategy.

        ``pnl_by_strategy`` should be small log-return-like numbers
        (e.g. period return as decimal). Missing strategies get a
        zero update (and decay toward prior on next renorm).
        """
        if not pnl_by_strategy:
            return self.weights()
        # Ensure new strategies are registered.
        new_keys = [k for k in pnl_by_strategy if k not in self.state.weights]
        if new_keys:
            self.register(list(self.state.weights.keys()) + new_keys)

        names = sorted(self.state.weights.keys())
        w = np.array([self.state.weights[n] for n in names], dtype=float)
        pnl = np.array([pnl_by_strategy.get(n, 0.0) for n in names], dtype=float)

        # EG update: w_new ∝ w * exp(eta * pnl).
        # Clip exponent to avoid overflow on extreme PnL.
        exp_arg = np.clip(self.eta * pnl, -10.0, 10.0)
        w_new = w * np.exp(exp_arg)
        if w_new.sum() <= 0 or not np.all(np.isfinite(w_new)):
            w_new = np.ones_like(w)
        w_new = w_new / w_new.sum()

        # Cap and floor.
        w_new = self._project(w_new)

        for i, n in enumerate(names):
            self.state.weights[n] = float(w_new[i])
            self.state.cumulative_pnl[n] = self.state.cumulative_pnl.get(n, 0.0) + float(pnl[i])
            self.state.sample_count[n] = self.state.sample_count.get(n, 0) + 1

        self.state.last_updated = time.time()
        if self.state_path:
            self._save()
        if self.audit_path:
            self._audit(pnl_by_strategy)
        return self.weights()

    def weights(self) -> dict[str, float]:
        return dict(self.state.weights)

    def reset(self, strategies: Optional[list[str]] = None) -> None:
        """Wipe state. If ``strategies`` given, re-register them uniformly."""
        self.state = AllocatorState()
        if strategies:
            self.register(strategies)
        if self.state_path:
            self._save()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _renorm_with_prior(self, strategies: list[str]) -> None:
        """Pull current weights toward uniform prior — ensures floors."""
        n = len(self.state.weights)
        if n == 0:
            return
        prior = 1.0 / n
        # Mix existing weights with prior.
        s = sum(self.state.weights.values())
        if s <= 0:
            for k in self.state.weights:
                self.state.weights[k] = prior
        else:
            blend = self.prior_strength / (self.prior_strength + 1.0)
            for k in self.state.weights:
                self.state.weights[k] = blend * prior + (1 - blend) * (self.state.weights[k] / s)
        # Project to satisfy caps.
        names = sorted(self.state.weights.keys())
        w = np.array([self.state.weights[n] for n in names], dtype=float)
        w = self._project(w)
        for i, n in enumerate(names):
            self.state.weights[n] = float(w[i])

    def _project(self, w: np.ndarray) -> np.ndarray:
        """Sum=1, min_weight floor, max_weight cap via iterative projection."""
        n = len(w)
        if n == 0:
            return w
        max_total = self.max_weight * n
        if 1.0 > max_total:
            # Caps are binding even at uniform — relax cap silently.
            return np.full(n, 1.0 / n)
        if self.min_weight * n > 1.0:
            return np.full(n, 1.0 / n)
        # Apply floor.
        w = np.maximum(w, self.min_weight)
        s = w.sum()
        if s <= 0:
            return np.full(n, 1.0 / n)
        w = w / s
        # Iterative cap projection.
        for _ in range(50):
            over = w > self.max_weight
            if not over.any():
                break
            excess = (w[over] - self.max_weight).sum()
            w[over] = self.max_weight
            free = ~over
            if free.any() and w[free].sum() > 0:
                w[free] += excess * (w[free] / w[free].sum())
            else:
                break
        # Reapply floor (cap projection may push some below).
        w = np.maximum(w, self.min_weight)
        w = w / w.sum()
        return w

    # File I/O ---------------------------------------------------------- #

    def _load(self) -> AllocatorState:
        if not self.state_path or not self.state_path.exists():
            return AllocatorState()
        try:
            return AllocatorState.from_dict(json.loads(self.state_path.read_text()))
        except Exception:
            return AllocatorState()

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp, self.state_path)

    def _audit(self, pnl_by_strategy: dict[str, float]) -> None:
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = {
                "ts": int(time.time()),
                "weights": dict(self.state.weights),
                "pnl": dict(pnl_by_strategy),
            }
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(line) + "\n")
        except Exception:
            pass


__all__ = ["CapitalAllocator", "AllocatorState"]
