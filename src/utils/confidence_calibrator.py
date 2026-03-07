"""
Confidence Calibrator — Adjusts raw LLM confidence to reflect actual accuracy.

Uses isotonic regression (monotone, non-parametric) trained on historical
(raw_confidence → was_correct) pairs from ``signal_scores``.  Per-pair
calibrators are trained when sample size ≥ 100, otherwise a global calibrator
is used.

New DB table: ``calibration_models``
"""

from __future__ import annotations

import json
import pickle
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("utils.confidence_calibrator")

# Minimum samples to train any calibrator
_MIN_GLOBAL_SAMPLES = 50
# Minimum samples for a per-pair calibrator
_MIN_PAIR_SAMPLES = 100
# Clamp calibrated confidence to this range
_CLAMP_MIN = 0.05
_CLAMP_MAX = 0.95


class ConfidenceCalibrator:
    """Maps raw LLM confidence → calibrated probability of correctness.

    Lifecycle:
        calibrator = ConfidenceCalibrator(stats_db, scorecard)
        calibrator.retrain()          # weekly
        adjusted = calibrator.calibrate(0.72, pair="BTC-USD")
    """

    def __init__(self, stats_db, scorecard):
        self._db = stats_db
        self._scorecard = scorecard
        # In-memory model cache: {None: global_model, "BTC-USD": pair_model, ...}
        self._models: dict[str | None, Any] = {}
        self._brier_before: float | None = None
        self._brier_after: float | None = None
        self._last_retrain: float = 0.0

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS calibration_models (
            id SERIAL PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'global',
            trained_at TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            sample_count INTEGER NOT NULL DEFAULT 0,
            brier_before REAL DEFAULT NULL,
            brier_after REAL DEFAULT NULL,
            model_bytes BYTEA DEFAULT NULL,
            horizon_hours INTEGER NOT NULL DEFAULT 24,
            is_active BOOLEAN DEFAULT TRUE
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_calibration_scope ON calibration_models(scope, is_active)",
        ]

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def retrain(self, window_days: int = 90, horizon_hours: int = 24) -> dict[str, Any]:
        """Retrain calibration models from signal_scores data.

        Returns summary of training results.
        """
        try:
            from sklearn.isotonic import IsotonicRegression
        except ImportError:
            logger.warning("scikit-learn not installed — calibration disabled")
            return {"error": "scikit-learn not installed"}

        data = self._scorecard.get_calibration_data(window_days=window_days, horizon_hours=horizon_hours)
        if len(data) < _MIN_GLOBAL_SAMPLES:
            logger.info(
                f"Calibration: insufficient data ({len(data)}/{_MIN_GLOBAL_SAMPLES} samples)"
            )
            return {"skipped": True, "reason": "insufficient_data", "samples": len(data)}

        # Train global model
        confs = [d[0] for d in data]
        corrects = [float(d[1]) for d in data]

        brier_before = sum((c - y) ** 2 for c, y in zip(confs, corrects)) / len(data)

        iso = IsotonicRegression(y_min=_CLAMP_MIN, y_max=_CLAMP_MAX, out_of_bounds="clip")
        iso.fit(confs, corrects)

        calibrated = iso.predict(confs)
        brier_after = sum((c - y) ** 2 for c, y in zip(calibrated, corrects)) / len(data)

        self._models[None] = iso
        self._brier_before = brier_before
        self._brier_after = brier_after
        self._last_retrain = time.monotonic()

        # Persist global model
        self._persist_model("global", iso, len(data), brier_before, brier_after, horizon_hours)

        results = {
            "global": {
                "samples": len(data),
                "brier_before": round(brier_before, 4),
                "brier_after": round(brier_after, 4),
                "improvement_pct": round((1 - brier_after / brier_before) * 100, 1) if brier_before > 0 else 0,
            },
            "pair_models": {},
        }

        # Train per-pair models
        pair_data = self._get_pair_calibration_data(window_days, horizon_hours)
        for pair, pdata in pair_data.items():
            if len(pdata) < _MIN_PAIR_SAMPLES:
                continue
            p_confs = [d[0] for d in pdata]
            p_corrects = [float(d[1]) for d in pdata]

            p_brier_before = sum((c - y) ** 2 for c, y in zip(p_confs, p_corrects)) / len(pdata)

            p_iso = IsotonicRegression(y_min=_CLAMP_MIN, y_max=_CLAMP_MAX, out_of_bounds="clip")
            p_iso.fit(p_confs, p_corrects)

            p_calibrated = p_iso.predict(p_confs)
            p_brier_after = sum((c - y) ** 2 for c, y in zip(p_calibrated, p_corrects)) / len(pdata)

            # Only use pair model if it improves on global
            if p_brier_after < brier_after:
                self._models[pair] = p_iso
                self._persist_model(f"pair:{pair}", p_iso, len(pdata), p_brier_before, p_brier_after, horizon_hours)
                results["pair_models"][pair] = {
                    "samples": len(pdata),
                    "brier_before": round(p_brier_before, 4),
                    "brier_after": round(p_brier_after, 4),
                }

        logger.info(
            f"🎯 Calibration retrained: global Brier {brier_before:.4f}→{brier_after:.4f} "
            f"({len(results['pair_models'])} pair models)"
        )
        return results

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def calibrate(self, raw_confidence: float, pair: str | None = None) -> float:
        """Map raw confidence to calibrated probability.

        Falls back to raw confidence if no model is loaded.
        """
        # Try pair-specific model first
        if pair and pair in self._models:
            model = self._models[pair]
        elif None in self._models:
            model = self._models[None]
        else:
            return raw_confidence

        try:
            calibrated = float(model.predict([raw_confidence])[0])
            return max(_CLAMP_MIN, min(_CLAMP_MAX, calibrated))
        except Exception:
            return raw_confidence

    def load_from_db(self) -> bool:
        """Load the latest active models from the database."""
        loaded = False
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT scope, model_bytes
                    FROM calibration_models
                    WHERE is_active = TRUE
                    ORDER BY trained_at DESC
                    """,
                ).fetchall()

            for row in rows:
                scope = row["scope"]
                model_bytes = row["model_bytes"]
                if not model_bytes:
                    continue
                try:
                    # model_bytes is already bytes from psycopg2
                    raw = bytes(model_bytes) if not isinstance(model_bytes, bytes) else model_bytes
                    model = pickle.loads(raw)
                    if scope == "global":
                        self._models[None] = model
                    elif scope.startswith("pair:"):
                        pair = scope[5:]
                        self._models[pair] = model
                    loaded = True
                except Exception as e:
                    logger.debug(f"Failed to load calibration model {scope}: {e}")

            if loaded:
                logger.info(f"🎯 Loaded {len(self._models)} calibration models from DB")
        except Exception as e:
            logger.warning(f"Failed to load calibration models: {e}")

        return loaded

    def get_calibration_curve(self, n_bins: int = 10) -> list[dict]:
        """Compute calibration curve data (for dashboard visualization).

        Returns list of {bin_center, predicted_avg, actual_avg, count}.
        """
        data = self._scorecard.get_calibration_data(window_days=90, horizon_hours=24)
        if not data:
            return []

        bin_width = 1.0 / n_bins
        bins: dict[int, dict] = {i: {"sum_conf": 0, "sum_correct": 0, "count": 0} for i in range(n_bins)}

        for conf, correct in data:
            bin_idx = min(int(conf / bin_width), n_bins - 1)
            bins[bin_idx]["sum_conf"] += conf
            bins[bin_idx]["sum_correct"] += float(correct)
            bins[bin_idx]["count"] += 1

        result = []
        for i in range(n_bins):
            b = bins[i]
            if b["count"] == 0:
                continue
            result.append({
                "bin_center": round((i + 0.5) * bin_width, 2),
                "predicted_avg": round(b["sum_conf"] / b["count"], 3),
                "actual_avg": round(b["sum_correct"] / b["count"], 3),
                "count": b["count"],
            })
        return result

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _persist_model(
        self, scope: str, model: Any, sample_count: int,
        brier_before: float, brier_after: float, horizon_hours: int
    ) -> None:
        """Save a trained model to the database."""
        try:
            model_bytes = pickle.dumps(model)
            with self._db._get_conn() as conn:
                # Deactivate previous model for this scope
                conn.execute(
                    "UPDATE calibration_models SET is_active = FALSE WHERE scope = %s",
                    (scope,),
                )
                conn.execute(
                    """
                    INSERT INTO calibration_models
                        (scope, sample_count, brier_before, brier_after, model_bytes, horizon_hours)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (scope, sample_count, brier_before, brier_after, model_bytes, horizon_hours),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to persist calibration model {scope}: {e}")

    def _get_pair_calibration_data(
        self, window_days: int, horizon_hours: int
    ) -> dict[str, list[tuple[float, bool]]]:
        """Get calibration data grouped by pair."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        with self._db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT pair, raw_confidence, is_correct
                FROM signal_scores
                WHERE horizon_hours = %s
                  AND scored_at >= %s
                  AND is_correct IS NOT NULL
                  AND raw_confidence > 0
                """,
                (horizon_hours, cutoff),
            ).fetchall()

        pair_data: dict[str, list[tuple[float, bool]]] = {}
        for r in rows:
            pair = r["pair"]
            if pair not in pair_data:
                pair_data[pair] = []
            pair_data[pair].append((float(r["raw_confidence"]), bool(r["is_correct"])))
        return pair_data
