"""
Dashboard routes for Adaptive Learning Engine (ALE) status and analytics.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.dashboard import deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.routes.learning")

router = APIRouter(tags=["Learning"])


# ---------------------------------------------------------------------------
# ALE Status Overview
# ---------------------------------------------------------------------------

@router.get("/api/learning/status", summary="ALE subsystem status")
def get_learning_status(db=Depends(deps.get_profile_db)):
    """Return status of all learning subsystems including last run times."""
    try:
        with db._get_conn() as conn:
            # Get latest run for each subsystem
            rows = conn.execute(
                """
                SELECT DISTINCT ON (subsystem)
                    subsystem, run_ts, status, duration_ms, result_json
                FROM learning_runs
                ORDER BY subsystem, run_ts DESC
                """
            ).fetchall()

        subsystems = {}
        for r in rows:
            try:
                result = json.loads(r["result_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                result = {}
            subsystems[r["subsystem"]] = {
                "last_run": r["run_ts"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "result": result,
            }

        # Get aggregate stats
        with db._get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM learning_runs"
            ).fetchone()
            errors = conn.execute(
                "SELECT COUNT(*) as cnt FROM learning_runs WHERE status = 'error'"
            ).fetchone()

        from src.utils import llm_optimizer
        settings = llm_optimizer.get_settings()

        return {
            "enabled": settings.get("learning_enabled", True),
            "subsystems": subsystems,
            "total_runs": total["cnt"] if total else 0,
            "total_errors": errors["cnt"] if errors else 0,
        }
    except Exception as e:
        return {"enabled": False, "subsystems": {}, "error": str(e)}


# ---------------------------------------------------------------------------
# Accuracy Trends
# ---------------------------------------------------------------------------

@router.get("/api/learning/accuracy-trends", summary="Signal accuracy over time")
def get_accuracy_trends(
    days: int = Query(30, ge=1, le=365),
    db=Depends(deps.get_profile_db),
):
    """Return rolling accuracy data for chart visualization."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    DATE(scored_at) as day,
                    COUNT(*) as total,
                    SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct,
                    AVG(CASE WHEN is_correct THEN 1.0 ELSE 0.0 END) as accuracy,
                    AVG(raw_confidence) as avg_raw_confidence,
                    AVG(calibrated_confidence) as avg_calibrated_confidence
                FROM signal_scores
                WHERE scored_at >= %s
                GROUP BY DATE(scored_at)
                ORDER BY day ASC
                """,
                (cutoff,),
            ).fetchall()

        return {
            "days": days,
            "data": [
                {
                    "date": str(r["day"]),
                    "total": r["total"],
                    "correct": r["correct"],
                    "accuracy": round(float(r["accuracy"] or 0), 4),
                    "avg_raw_confidence": round(float(r["avg_raw_confidence"] or 0), 4),
                    "avg_calibrated_confidence": round(float(r["avg_calibrated_confidence"] or 0), 4),
                }
                for r in rows
            ],
        }
    except Exception as e:
        return {"days": days, "data": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Ensemble Weights
# ---------------------------------------------------------------------------

@router.get("/api/learning/ensemble-weights", summary="Current and historical strategy weights")
def get_ensemble_weights(db=Depends(deps.get_profile_db)):
    """Return current active weights and recent history."""
    try:
        with db._get_conn() as conn:
            # Current active weights
            active = conn.execute(
                """
                SELECT regime, strategy, weight, updated_at, sample_size, accuracy_at_update
                FROM ensemble_weights
                WHERE is_active = TRUE
                ORDER BY regime, strategy
                """
            ).fetchall()

            # Weight history (last 20 changes)
            history = conn.execute(
                """
                SELECT regime, strategy, weight, updated_at, accuracy_at_update
                FROM ensemble_weights
                ORDER BY updated_at DESC
                LIMIT 20
                """
            ).fetchall()

        return {
            "active": [dict(r) for r in active],
            "history": [dict(r) for r in history],
        }
    except Exception as e:
        return {"active": [], "history": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Calibration Curve
# ---------------------------------------------------------------------------

@router.get("/api/learning/calibration", summary="Confidence calibration curve data")
def get_calibration_data(db=Depends(deps.get_profile_db)):
    """Return calibration curve data (predicted vs actual) for visualization."""
    try:
        with db._get_conn() as conn:
            # Bucket raw confidence into deciles and compute actual accuracy
            rows = conn.execute(
                """
                SELECT
                    FLOOR(raw_confidence * 10) / 10 as bucket,
                    COUNT(*) as count,
                    AVG(CASE WHEN is_correct THEN 1.0 ELSE 0.0 END) as actual_accuracy,
                    AVG(raw_confidence) as avg_raw,
                    AVG(calibrated_confidence) as avg_calibrated
                FROM signal_scores
                GROUP BY FLOOR(raw_confidence * 10) / 10
                ORDER BY bucket ASC
                """
            ).fetchall()

            # Calibration model metadata
            models = conn.execute(
                """
                SELECT scope, trained_at, sample_count, brier_before, brier_after
                FROM calibration_models
                WHERE is_active = TRUE
                ORDER BY scope
                """
            ).fetchall()

        return {
            "curve": [
                {
                    "predicted": round(float(r["bucket"]), 1),
                    "actual": round(float(r["actual_accuracy"] or 0), 4),
                    "count": r["count"],
                    "avg_raw": round(float(r["avg_raw"] or 0), 4),
                    "avg_calibrated": round(float(r["avg_calibrated"] or 0), 4),
                }
                for r in rows
            ],
            "models": [dict(r) for r in models],
        }
    except Exception as e:
        return {"curve": [], "models": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Learned Lessons (Prompt Supplements)
# ---------------------------------------------------------------------------

@router.get("/api/learning/lessons", summary="Active prompt supplements / learned lessons")
def get_lessons(db=Depends(deps.get_profile_db)):
    """Return active prompt supplements generated by the Prompt Evolver."""
    try:
        with db._get_conn() as conn:
            active = conn.execute(
                """
                SELECT version, created_at, agent_name, supplement_text,
                       source_factor, source_accuracy_pct, priority
                FROM prompt_supplements
                WHERE is_active = TRUE
                ORDER BY agent_name, priority DESC
                """
            ).fetchall()

            # Deactivated recently
            deactivated = conn.execute(
                """
                SELECT version, created_at, agent_name, supplement_text,
                       source_factor, deactivated_at
                FROM prompt_supplements
                WHERE is_active = FALSE AND deactivated_at IS NOT NULL
                ORDER BY deactivated_at DESC
                LIMIT 10
                """
            ).fetchall()

        return {
            "active": [dict(r) for r in active],
            "deactivated": [dict(r) for r in deactivated],
        }
    except Exception as e:
        return {"active": [], "deactivated": [], "error": str(e)}


# ---------------------------------------------------------------------------
# WFO Promotions
# ---------------------------------------------------------------------------

@router.get("/api/learning/wfo-promotions", summary="Parameter optimization history")
def get_wfo_promotions(
    limit: int = Query(20, ge=1, le=100),
    db=Depends(deps.get_profile_db),
):
    """Return WFO parameter promotion/rollback history."""
    try:
        with db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT run_ts, pair, param_name, old_value, new_value,
                       wfe, oos_sharpe, promoted, rolled_back,
                       rollback_ts, rollback_reason,
                       pre_promotion_accuracy, post_promotion_accuracy
                FROM parameter_promotions
                ORDER BY run_ts DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

        return {"promotions": [dict(r) for r in rows]}
    except Exception as e:
        return {"promotions": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Fine-Tuning Exports
# ---------------------------------------------------------------------------

@router.get("/api/learning/finetune-exports", summary="Fine-tuning dataset export history")
def get_finetune_exports(
    limit: int = Query(10, ge=1, le=50),
    db=Depends(deps.get_profile_db),
):
    """Return history of fine-tuning dataset exports."""
    try:
        with db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT export_ts, example_count, win_count, loss_count,
                       file_path, model_target, window_days, status,
                       avg_win_pnl_pct, avg_loss_pnl_pct
                FROM finetune_exports
                ORDER BY export_ts DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

        return {"exports": [dict(r) for r in rows]}
    except Exception as e:
        return {"exports": [], "error": str(e)}
