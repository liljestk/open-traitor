"""
Thompson-sampling bandit for online strategy weighting.

Each (exchange, regime, strategy) cell maintains a Beta(alpha, beta) posterior
over the probability that picking this strategy yields a win in this regime.

Update rule (per closed trade):
    alpha += win_score      # win_score ∈ [0, 1]
    beta  += 1 - win_score

Sampling:
    sample θ_s ~ Beta(α_s, β_s) per strategy
    weight_s = θ_s / Σ θ
    Returned weights are dict[str, float] summing to 1.0.

Falls back to uniform weights if no posterior data.

Domain isolation: every read/write goes through ``StatsDB`` with explicit
``exchange`` so coinbase/ibkr never share state.
"""

from __future__ import annotations

import random
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("bandit")


def _sample_beta(alpha: float, beta: float, rng: Optional[random.Random] = None) -> float:
    """Stdlib-only Beta sample via gamma ratio (avoids numpy dep at import time)."""
    rng = rng or random
    a = max(float(alpha), 1e-6)
    b = max(float(beta), 1e-6)
    x = rng.gammavariate(a, 1.0)
    y = rng.gammavariate(b, 1.0)
    s = x + y
    return x / s if s > 0 else 0.5


class StrategyBandit:
    """Thompson-sampling allocator over strategy weights, per-regime.

    Usage::

        bandit = StrategyBandit(stats_db, exchange="coinbase")
        weights = bandit.sample_weights("trending_up", strategies=["ema", "bbands", "pattern"])
        # ... after trade closes ...
        bandit.update("trending_up", "ema", win_score=1.0 if pnl > 0 else 0.0)
    """

    def __init__(
        self,
        stats_db,
        *,
        exchange: str,
        rng: Optional[random.Random] = None,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ):
        if not exchange:
            raise ValueError("StrategyBandit requires explicit exchange")
        self.db = stats_db
        self.exchange = exchange
        self._rng = rng or random.Random()
        self._prior = (float(prior_alpha), float(prior_beta))

    # ── core ──────────────────────────────────────────────────────────────

    def sample_weights(
        self, regime: str, strategies: list[str]
    ) -> dict[str, float]:
        """Sample per-strategy weights from current posteriors.

        Strategies with no recorded outcomes get uniform-prior Beta(1, 1)
        which exposes them to exploration (mean 0.5).
        """
        if not strategies:
            return {}
        regime_key = (regime or "unknown").lower()
        try:
            state = self.db.get_bandit_state(self.exchange, regime_key)
        except Exception as e:
            logger.warning(f"bandit: state read failed ({e}); uniform fallback")
            n = len(strategies)
            return {s: 1.0 / n for s in strategies}

        samples: dict[str, float] = {}
        for s in strategies:
            row = state.get(s) or {}
            a = float(row.get("alpha") or self._prior[0])
            b = float(row.get("beta") or self._prior[1])
            samples[s] = _sample_beta(a, b, self._rng)

        total = sum(samples.values())
        if total <= 0:
            n = len(strategies)
            return {s: 1.0 / n for s in strategies}
        return {s: v / total for s, v in samples.items()}

    def update(
        self, regime: str, strategy: str, *, win_score: float
    ) -> None:
        """Apply a single observation. ``win_score`` ∈ [0, 1]."""
        regime_key = (regime or "unknown").lower()
        ws = max(0.0, min(1.0, float(win_score)))
        try:
            state = self.db.get_bandit_state(self.exchange, regime_key)
        except Exception as e:
            logger.warning(f"bandit: update state read failed ({e})")
            return
        row = state.get(strategy) or {}
        a = float(row.get("alpha") or self._prior[0]) + ws
        b = float(row.get("beta") or self._prior[1]) + (1.0 - ws)
        n = int(row.get("n_pulls") or 0) + 1
        try:
            self.db.upsert_bandit(
                exchange=self.exchange,
                regime=regime_key,
                strategy=strategy,
                alpha=a,
                beta=b,
                n_pulls=n,
            )
        except Exception as e:
            logger.warning(f"bandit: upsert failed ({e})")

    def expected_weight(
        self, regime: str, strategies: list[str]
    ) -> dict[str, float]:
        """Posterior-mean weights (deterministic; useful for dashboards)."""
        if not strategies:
            return {}
        regime_key = (regime or "unknown").lower()
        try:
            state = self.db.get_bandit_state(self.exchange, regime_key)
        except Exception:
            state = {}
        means: dict[str, float] = {}
        for s in strategies:
            row = state.get(s) or {}
            a = float(row.get("alpha") or self._prior[0])
            b = float(row.get("beta") or self._prior[1])
            means[s] = a / (a + b) if (a + b) > 0 else 0.5
        total = sum(means.values())
        if total <= 0:
            n = len(strategies)
            return {s: 1.0 / n for s in strategies}
        return {s: v / total for s, v in means.items()}
