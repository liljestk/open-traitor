from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.dashboard import deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.planning")

router = APIRouter(tags=["Planning"])


# ---------------------------------------------------------------------------
# REST — Strategic context (planning)
# ---------------------------------------------------------------------------

@router.get("/api/strategic", summary="Recent strategic plans from Temporal workflows")
def get_strategic(
    horizon: Optional[str] = Query(None, description="daily | weekly | monthly"),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(deps.get_profile_db),
):
    """Returns the most recent planning workflow outputs with Temporal + Langfuse IDs."""
    conn = deps.open_conn(db)
    try:
        if horizon:
            rows = conn.execute(
                """SELECT id, horizon, plan_json, summary_text, ts,
                          langfuse_trace_id, temporal_workflow_id, temporal_run_id
                   FROM strategic_context
                   WHERE horizon = ?
                   ORDER BY ts DESC LIMIT ?""",
                (horizon, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, horizon, plan_json, summary_text, ts,
                          langfuse_trace_id, temporal_workflow_id, temporal_run_id
                   FROM strategic_context
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        result = []
        for r in rows:
            row = dict(r)
            try:
                row["plan_json"] = json.loads(row["plan_json"] or "{}")
            except Exception:
                pass
            row["langfuse_url"] = deps.langfuse_url(row.get("langfuse_trace_id"))
            result.append(row)
        return {"plans": result, "count": len(result)}
    except Exception as exc:
        logger.exception("strategic error")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST — Temporal workflow replay
# ---------------------------------------------------------------------------

@router.get("/api/temporal/runs", summary="List recent Temporal planning workflow runs")
async def list_temporal_runs(
    limit: int = Query(50, ge=1, le=200),
    workflow_type: Optional[str] = Query(None, description="DailyPlanWorkflow | WeeklyReviewWorkflow | MonthlyReviewWorkflow"),
):
    """Returns recent workflow executions from Temporal with their status."""
    if deps.temporal_client is None:
        return {"runs": [], "error": "Temporal client not available"}
    try:
        query = " OR ".join(
            f"WorkflowType = '{wt}'"
            for wt in ("DailyPlanWorkflow", "WeeklyReviewWorkflow", "MonthlyReviewWorkflow")
        )
        _ALLOWED_WORKFLOW_TYPES = {"DailyPlanWorkflow", "WeeklyReviewWorkflow", "MonthlyReviewWorkflow"}
        if workflow_type:
            if workflow_type not in _ALLOWED_WORKFLOW_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid workflow_type. Allowed: {sorted(_ALLOWED_WORKFLOW_TYPES)}",
                )
            query = f"WorkflowType = '{workflow_type}'"

        runs = []
        async for wf in deps.temporal_client.list_workflows(query=query):
            runs.append({
                "workflow_id": wf.id,
                "run_id": wf.run_id,
                "workflow_type": wf.workflow_type,
                "status": str(wf.status),
                "start_time": wf.start_time.isoformat() if wf.start_time else None,
                "close_time": wf.close_time.isoformat() if wf.close_time else None,
            })
            if len(runs) >= limit:
                break
        return {"runs": runs, "count": len(runs)}
    except Exception as exc:
        logger.exception("temporal/runs error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/temporal/replay/{workflow_id}/{run_id}", summary="Full Temporal workflow event history")
async def get_temporal_replay(workflow_id: str, run_id: str):
    """
    Fetches the complete event history for a Temporal workflow run.
    Each event records input, LLM call, output, timing — enabling full step-by-step replay.
    """
    if deps.temporal_client is None:
        raise HTTPException(status_code=503, detail="Temporal client not available")
    try:
        handle = deps.temporal_client.get_workflow_handle(workflow_id, run_id=run_id)
        history = await handle.fetch_history()
        events = []
        for event in history.events:
            events.append({
                "event_id": event.event_id,
                "event_type": str(event.event_type),
                "event_time": event.event_time.isoformat() if event.event_time else None,
                "attributes": deps.serialize_event_attrs(event),
            })

        # Cross-link with Langfuse trace ID from StatsDB
        langfuse_trace_id = None
        if deps.stats_db:
            conn = deps.fresh_conn()
            try:
                row = conn.execute(
                    """SELECT langfuse_trace_id FROM strategic_context
                       WHERE temporal_workflow_id = ? AND temporal_run_id = ?
                       LIMIT 1""",
                    (workflow_id, run_id),
                ).fetchone()
                if row:
                    langfuse_trace_id = row[0]
            finally:
                conn.close()

        return {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "event_count": len(events),
            "langfuse_trace_id": langfuse_trace_id,
            "langfuse_url": deps.langfuse_url(langfuse_trace_id),
            "events": events,
        }
    except Exception as exc:
        logger.exception("temporal/replay error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/temporal/rerun/{workflow_id}/{run_id}", summary="Trigger a fresh planning workflow run")
async def rerun_temporal_workflow(workflow_id: str, run_id: str):
    """
    Starts a new execution of the same workflow type with a fresh run ID.
    Useful for debugging or forcing an out-of-schedule planning run.
    """
    if deps.temporal_client is None:
        raise HTTPException(status_code=503, detail="Temporal client not available")

    # Determine workflow class from the original run
    try:
        handle = deps.temporal_client.get_workflow_handle(workflow_id, run_id=run_id)
        desc = await handle.describe()
        workflow_type = desc.workflow_type
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Cannot find workflow: {exc}")

    from src.planning.workflows import DailyPlanWorkflow, WeeklyReviewWorkflow, MonthlyReviewWorkflow
    _wf_map = {
        "DailyPlanWorkflow": DailyPlanWorkflow,
        "WeeklyReviewWorkflow": WeeklyReviewWorkflow,
        "MonthlyReviewWorkflow": MonthlyReviewWorkflow,
    }
    wf_cls = _wf_map.get(workflow_type)
    if not wf_cls:
        raise HTTPException(status_code=400, detail=f"Unknown workflow type: {workflow_type!r}")

    import uuid
    new_wf_id = f"manual-rerun-{workflow_type}-{uuid.uuid4().hex[:8]}"
    try:
        new_handle = await deps.temporal_client.start_workflow(
            wf_cls.run,
            id=new_wf_id,
            task_queue="planning",
        )
        return {
            "status": "started",
            "new_workflow_id": new_wf_id,
            "new_run_id": new_handle.first_execution_run_id,
            "original_workflow_id": workflow_id,
            "original_run_id": run_id,
        }
    except Exception as exc:
        logger.exception("temporal/rerun error")
        raise HTTPException(status_code=500, detail="Internal server error")
