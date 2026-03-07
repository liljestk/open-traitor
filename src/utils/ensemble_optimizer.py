"""
Ensemble Optimizer — Adaptive strategy weights based on rolling performance.

Replaces hardcoded ``_STRATEGY_WEIGHTS`` in ``PipelineManager`` with dynamic
weights that adapt to which strategies perform best under current market
conditions.  Uses Bayesian weight updating with regime awareness.

New DB table: ``ensemble_weights``
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.utils.logger import get_logger

logger = get_logger("utils.ensemble_optimizer")

# Guardrails
_MIN_WEIGHT = 0.10
_MAX_WEIGHT = 0.90
_MAX_SHIFT_PER_UPDATE = 0.05   # max delta per weekly update
_MIN_SIGNALS_TO_ADAPT = 30     # per strategy
_DEFAULT_WEIGHTS = {
    "ema_crossover": 0.55,
    "bollinger_reversion": 0.45,
}

# Market regimes (mapped from multi_timeframe / market_condition outputs)
_REGIME_MAP = {
    "strong_uptrend": "trending",
    "uptrend": "trending",
    "strong_downtrend": "trending",
    "downtrend": "trending",
    "bullish": "trending",
    "bearish": "trending",
    "ranging": "ranging",
    "sideways": "ranging",
    "consolidation": "ranging",
    "choppy": "volatile",
    "volatile": "volatile",
    "high_volatility": "volatile",
}

REGIMES = ("trending", "ranging", "volatile", "unknown")


class EnsembleOptimizer:
    """Optimizes strategy ensemble weights based on rolling performance.

    Lifecycle:
        optimizer = EnsembleOptimizer(stats_db, scorecard)
        optimizer.load_from_db()                              # on startup
        weights = optimizer.get_weights(market_regime="trending")
        optimizer.update_weights()                            # weekly
    """

    def __init__(self, stats_db, scorecard, audit=None):
        self._db = stats_db
        self._scorecard = scorecard
        self._audit = audit
        # In-memory: {regime: {strategy: weight}}
        self._weights: dict[str, dict[str, float]] = {
            r: dict(_DEFAULT_WEIGHTS) for r in REGIMES
        }
        self._last_update: float = 0.0

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS ensemble_weights (
            id SERIAL PRIMARY KEY,
            regime TEXT NOT NULL,
            strategy TEXT NOT NULL,
            weight REAL NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            sample_size INTEGER NOT NULL DEFAULT 0,
            accuracy_at_update REAL DEFAULT NULL,
            is_active BOOLEAN DEFAULT TRUE
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_ensemble_regime ON ensemble_weights(regime, is_active)",
        ]

    # ------------------------------------------------------------------
    # Weight queries
    # ------------------------------------------------------------------

    def get_weights(self, market_regime: str | None = None) -> dict[str, float]:
        """Get current strategy weights for the given regime.

        If no regime-specific weights exist, returns global weights.
        """
        regime = self._normalize_regime(market_regime)
        weights = self._weights.get(regime)
        if not weights:
            weights = self._weights.get("unknown", dict(_DEFAULT_WEIGHTS))
        return dict(weights)

    def get_all_weights(self) -> dict[str, dict[str, float]]:
        """Get weights for all regimes (for dashboard display)."""
        return {r: dict(w) for r, w in self._weights.items()}

    # ------------------------------------------------------------------
    # Weight update (weekly)
    # ------------------------------------------------------------------

    def update_weights(self, window_days: int = 14) -> dict[str, Any]:
        """Recalculate weights from recent strategy performance.

        Uses Bayesian updating:
          posterior_weight ∝ prior_weight × likelihood(accuracy)

        Returns summary of changes.
        """
        strat_accuracy = self._scorecard.get_strategy_accuracy(
            window_days=window_days, horizon_hours=4
        )

        if not strat_accuracy:
            return {"skipped": True, "reason": "no_strategy_data"}

        # Check minimum samples
        insufficient = {
            s: d for s, d in strat_accuracy.items()
            if d["total"] < _MIN_SIGNALS_TO_ADAPT
        }
        if len(insufficient) == len(strat_accuracy):
            return {
                "skipped": True,
                "reason": "insufficient_samples",
                "strategy_counts": {s: d["total"] for s, d in strat_accuracy.items()},
            }

        # Get regime-specific accuracy
        regime_accuracy = self._get_regime_strategy_accuracy(window_days)

        changes = {}
        for regime in REGIMES:
            old_weights = dict(self._weights.get(regime, _DEFAULT_WEIGHTS))
            new_weights = self._bayesian_update(
                old_weights,
                regime_accuracy.get(regime, strat_accuracy),
            )
            # Enforce max shift
            clamped_weights = self._clamp_shift(old_weights, new_weights)
            # Normalize to sum=1
            total = sum(clamped_weights.values())
            if total > 0:
                clamped_weights = {k: v / total for k, v in clamped_weights.items()}

            self._weights[regime] = clamped_weights

            # Track changes
            for strat in clamped_weights:
                old_w = old_weights.get(strat, 0.3)
                new_w = clamped_weights[strat]
                if abs(new_w - old_w) > 0.001:
                    changes[f"{regime}/{strat}"] = {
                        "old": round(old_w, 3),
                        "new": round(new_w, 3),
                        "delta": round(new_w - old_w, 3),
                    }

        # Persist
        self._persist_weights()

        # Audit log
        if changes and self._audit:
            self._audit.log(
                "ensemble_weight_update",
                {"changes": changes, "window_days": window_days},
            )

        self._last_update = datetime.now(timezone.utc).timestamp()

        logger.info(
            f"⚖️ Ensemble weights updated: {len(changes)} changes across {len(REGIMES)} regimes"
        )
        return {"changes": changes, "current_weights": self.get_all_weights()}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_from_db(self) -> bool:
        """Load the latest active weights from the database."""
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT regime, strategy, weight
                    FROM ensemble_weights
                    WHERE is_active = TRUE
                    ORDER BY updated_at DESC
                    """,
                ).fetchall()

            if not rows:
                return False

            loaded: dict[str, dict[str, float]] = {}
            for row in rows:
                regime = row["regime"]
                if regime not in loaded:
                    loaded[regime] = {}
                loaded[regime][row["strategy"]] = row["weight"]

            for regime, weights in loaded.items():
                self._weights[regime] = weights

            logger.info(f"⚖️ Loaded ensemble weights for {len(loaded)} regimes from DB")
            return True
        except Exception as e:
            logger.warning(f"Failed to load ensemble weights: {e}")
            return False

    def _persist_weights(self) -> None:
        """Save current weights to the database."""
        try:
            with self._db._get_conn() as conn:
                # Deactivate all previous weights
                conn.execute("UPDATE ensemble_weights SET is_active = FALSE")

                for regime, weights in self._weights.items():
                    for strat, weight in weights.items():
                        conn.execute(
                            """
                            INSERT INTO ensemble_weights
                                (regime, strategy, weight, sample_size)
                            VALUES (%s, %s, %s, 0)
                            """,
                            (regime, strat, round(weight, 4)),
                        )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to persist ensemble weights: {e}")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_regime(raw: str | None) -> str:
        """Map a market_condition string to a canonical regime."""
        if not raw:
            return "unknown"
        return _REGIME_MAP.get(raw.lower().strip(), "unknown")

    @staticmethod
    def _bayesian_update(
        prior_weights: dict[str, float],
        accuracy_data: dict[str, dict],
    ) -> dict[str, float]:
        """Compute posterior weights using Bayesian update.

        prior_weight × exp(accuracy × log_scale) → unnormalized posterior
        """
        posteriors = {}
        for strat, prior in prior_weights.items():
            acc_info = accuracy_data.get(strat)
            if not acc_info or acc_info.get("total", 0) < 5:
                posteriors[strat] = prior
                continue

            # Use win_rate as likelihood (with smoothing)
            win_rate = acc_info.get("win_rate", 50.0) / 100.0
            # Log-odds transformation for stable update
            likelihood = math.exp(2.0 * (win_rate - 0.5))  # centered at 50%
            posterior = prior * likelihood
            posteriors[strat] = max(_MIN_WEIGHT, min(_MAX_WEIGHT, posterior))

        # Normalize
        total = sum(posteriors.values())
        if total > 0:
            posteriors = {k: v / total for k, v in posteriors.items()}

        return posteriors

    @staticmethod
    def _clamp_shift(
        old: dict[str, float], new: dict[str, float]
    ) -> dict[str, float]:
        """Limit weight changes to _MAX_SHIFT_PER_UPDATE per strategy."""
        result = {}
        for strat in set(old) | set(new):
            old_w = old.get(strat, 0.3)
            new_w = new.get(strat, 0.3)
            delta = new_w - old_w
            if abs(delta) > _MAX_SHIFT_PER_UPDATE:
                delta = _MAX_SHIFT_PER_UPDATE if delta > 0 else -_MAX_SHIFT_PER_UPDATE
            result[strat] = max(_MIN_WEIGHT, min(_MAX_WEIGHT, old_w + delta))
        return result

    def _get_regime_strategy_accuracy(
        self, window_days: int
    ) -> dict[str, dict[str, dict]]:
        """Get strategy accuracy broken down by market regime.

        Returns {regime: {strategy: {total, correct, win_rate}}}.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        with self._db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT ss.market_condition, t.action, t.pnl, ar.reasoning_json
                FROM signal_scores ss
                JOIN agent_reasoning ar ON ar.id = ss.reasoning_id
                LEFT JOIN trades t ON t.id = ar.trade_id
                WHERE ss.scored_at >= %s
                  AND ss.horizon_hours = 4
                  AND t.pnl IS NOT NULL
                """,
                (cutoff,),
            ).fetchall()

        regime_strat: dict[str, dict[str, dict]] = {}
        for row in rows:
            regime = self._normalize_regime(row.get("market_condition"))
            if regime not in regime_strat:
                regime_strat[regime] = defaultdict(lambda: {"total": 0, "correct": 0})

            pnl = row.get("pnl") or 0
            is_win = pnl > 0

            try:
                reasoning = json.loads(row.get("reasoning_json") or "{}")
                strat_signals = reasoning.get("strategy_signals", {})
            except (json.JSONDecodeError, TypeError):
                continue

            for strat_name in strat_signals:
                if strat_name.startswith("_"):
                    continue
                regime_strat[regime][strat_name]["total"] += 1
                if is_win:
                    regime_strat[regime][strat_name]["correct"] += 1

        # Compute win_rate
        for regime, strats in regime_strat.items():
            for strat, data in strats.items():
                data["win_rate"] = (
                    round(data["correct"] / data["total"] * 100, 1)
                    if data["total"] > 0 else 50.0
                )

        return regime_strat

    def get_weight_history(self, limit: int = 50) -> list[dict]:
        """Get historical weight changes for dashboard visualization."""
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT regime, strategy, weight, updated_at, sample_size, accuracy_at_update
                    FROM ensemble_weights
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
