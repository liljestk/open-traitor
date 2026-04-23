"""
Portfolio optimizer (Phase 4).

Pure-numpy mean-variance optimizer with optional CVaR risk constraint.
We deliberately avoid CVXPY/SciPy.optimize as a hard dependency — the
problems we solve in the live loop are small (≤ ~50 assets, often
< 10), so a projected-gradient solver converges in a few hundred
iterations and stays robust under weird inputs (singular covariances,
short history, etc.).

Outputs are long-only weights summing to 1.0 by default. A
``max_weight`` cap and a ``min_weight`` floor (drop dust positions)
are enforced via projection.

This module is the *quantitative* portfolio sizer; capital
allocation across strategies (the meta-layer) lives in
``capital_allocator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class PortfolioWeights:
    """Result of a portfolio optimization."""
    assets: tuple[str, ...]
    weights: tuple[float, ...]
    expected_return: float
    expected_vol: float
    cvar_95: float

    def as_dict(self) -> dict[str, float]:
        return {a: w for a, w in zip(self.assets, self.weights)}


class PortfolioOptimizer:
    """Mean-variance optimizer with CVaR-aware option."""

    def __init__(
        self,
        *,
        risk_aversion: float = 5.0,
        max_weight: float = 0.30,
        min_weight: float = 0.005,
        long_only: bool = True,
        cvar_alpha: float = 0.05,
        max_iter: int = 800,
        lr: float = 0.05,
        tol: float = 1e-7,
    ) -> None:
        if risk_aversion <= 0:
            raise ValueError("risk_aversion must be > 0")
        if not (0 < max_weight <= 1.0):
            raise ValueError("max_weight must be in (0, 1]")
        if min_weight < 0 or min_weight >= max_weight:
            raise ValueError("0 <= min_weight < max_weight required")
        self.risk_aversion = float(risk_aversion)
        self.max_weight = float(max_weight)
        self.min_weight = float(min_weight)
        self.long_only = bool(long_only)
        self.cvar_alpha = float(cvar_alpha)
        self.max_iter = int(max_iter)
        self.lr = float(lr)
        self.tol = float(tol)

    # ------------------------------------------------------------------ #

    def optimize(
        self,
        returns: np.ndarray,             # shape (T, N) — historical returns
        assets: list[str],
        *,
        expected_returns: Optional[np.ndarray] = None,  # shape (N,) — overrides mean
    ) -> PortfolioWeights:
        """
        Mean-variance with projected gradient.

        Objective:  maximize  μᵀw − (λ/2) wᵀΣw
        s.t.        w ≥ 0, sum(w) = 1, w ≤ max_weight
        """
        R = np.asarray(returns, dtype=float)
        if R.ndim != 2:
            raise ValueError("returns must be (T, N)")
        T, N = R.shape
        if N != len(assets):
            raise ValueError("len(assets) must match returns columns")
        if N == 0:
            return PortfolioWeights((), (), 0.0, 0.0, 0.0)
        if T < 5:
            # Too little data → equal weights, capped.
            w = self._project(np.ones(N) / N)
            return self._wrap(assets, w, R, np.zeros(N))

        mu = np.asarray(expected_returns, dtype=float) if expected_returns is not None else R.mean(axis=0)
        if mu.shape != (N,):
            raise ValueError("expected_returns shape mismatch")
        # Covariance with shrinkage to identity for stability.
        sigma = np.cov(R, rowvar=False)
        if sigma.ndim == 0:
            sigma = sigma.reshape(1, 1)
        sigma = self._shrink(sigma)

        # Initial guess: equal weights, projected.
        w = self._project(np.ones(N) / N)

        prev_obj = -np.inf
        for _ in range(self.max_iter):
            grad = mu - self.risk_aversion * sigma @ w
            w_new = self._project(w + self.lr * grad)
            obj = float(mu @ w_new - 0.5 * self.risk_aversion * w_new @ sigma @ w_new)
            if abs(obj - prev_obj) < self.tol:
                w = w_new
                break
            prev_obj = obj
            w = w_new

        return self._wrap(assets, w, R, mu)

    # ------------------------------------------------------------------ #

    def _wrap(
        self, assets: list[str], w: np.ndarray, R: np.ndarray, mu: np.ndarray,
    ) -> PortfolioWeights:
        port_returns = R @ w
        exp_ret = float(mu @ w)
        exp_vol = float(np.std(port_returns, ddof=1)) if len(port_returns) > 1 else 0.0
        cvar = float(_cvar(port_returns, self.cvar_alpha))
        return PortfolioWeights(
            assets=tuple(assets),
            weights=tuple(float(x) for x in w),
            expected_return=round(exp_ret, 8),
            expected_vol=round(exp_vol, 8),
            cvar_95=round(cvar, 8),
        )

    def _project(self, w: np.ndarray) -> np.ndarray:
        """Project onto {w | sum=1, 0 ≤ w ≤ max_weight, drop dust}."""
        # Long-only: clip non-negative.
        if self.long_only:
            w = np.clip(w, 0.0, self.max_weight)
        # Drop dust positions below min_weight.
        w = np.where(w < self.min_weight, 0.0, w)
        s = w.sum()
        if s <= 0:
            # Fall back to equal allocation across all assets (cap-respecting).
            n = len(w)
            equal = min(self.max_weight, 1.0 / n)
            w = np.full(n, equal)
            s = w.sum()
        # Renormalise to sum=1, but if cap binds, redistribute to others.
        for _ in range(50):
            w = w / s
            over = w > self.max_weight
            if not over.any():
                break
            excess = (w[over] - self.max_weight).sum()
            w[over] = self.max_weight
            free = ~over & (w > 0)
            if free.any():
                w[free] += excess * (w[free] / w[free].sum())
            s = w.sum()
        return w

    @staticmethod
    def _shrink(sigma: np.ndarray, lam: float = 0.05) -> np.ndarray:
        n = sigma.shape[0]
        target = np.eye(n) * (np.trace(sigma) / max(1, n))
        return (1 - lam) * sigma + lam * target


# --------------------------------------------------------------------- #
# CVaR helper (pure numpy)
# --------------------------------------------------------------------- #

def _cvar(returns: np.ndarray, alpha: float) -> float:
    """Conditional VaR (expected shortfall) at level alpha. Returns a *negative*
    number for losses (lower is worse)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return 0.0
    cutoff = np.quantile(r, alpha)
    tail = r[r <= cutoff]
    if len(tail) == 0:
        return float(cutoff)
    return float(tail.mean())


__all__ = ["PortfolioOptimizer", "PortfolioWeights"]
