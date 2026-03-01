"""
LLM Analytics API — usage stats for all LLM calls made by the trading system.

Queries the agent_reasoning table for historical call data and augments with
live runtime stats from the LLMClient when available.

Also exposes optimizer endpoints to read/write tunable LLM cost parameters.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.dashboard import deps
from src.utils import llm_optimizer
from src.utils.logger import get_logger

logger = get_logger("dashboard.routes.llm_analytics")

router = APIRouter(tags=["LLM Analytics"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bucket_format(hours: int) -> str:
    """Return strftime format for time-bucket grouping based on range."""
    if hours <= 48:
        return "%Y-%m-%d %H:00"   # hourly
    if hours <= 720:
        return "%Y-%m-%d"          # daily
    return "%Y-%W"                 # weekly


# ---------------------------------------------------------------------------
# Main analytics endpoint
# ---------------------------------------------------------------------------

@router.get("/api/llm-analytics", summary="LLM call usage analytics")
def get_llm_analytics(
    hours: int = Query(168, ge=1, le=8760, description="Lookback window in hours"),
    profile: str = Query("", description="Exchange profile"),
    db=Depends(deps.get_profile_db),
):
    """
    Aggregated LLM call statistics for the given time window.

    Returns:
      - summary: total calls, token counts, latency stats
      - time_series: calls + tokens bucketed over time
      - by_agent: per-agent breakdown
      - by_exchange: per-exchange breakdown
      - top_pairs: pairs with most LLM calls
      - providers: live runtime provider stats (if LLMClient is available)
    """
    conn = deps.open_conn(db)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    bucket_fmt = _bucket_format(hours)

    try:
        # ── Summary ──────────────────────────────────────────────────────────
        summary_row = conn.execute("""
            SELECT
                COUNT(*) AS total_calls,
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens,
                ROUND(AVG(latency_ms), 0) AS avg_latency_ms,
                ROUND(AVG(prompt_tokens), 0) AS avg_prompt_tokens,
                ROUND(AVG(completion_tokens), 0) AS avg_completion_tokens,
                ROUND(AVG(prompt_tokens + completion_tokens), 0) AS avg_total_tokens,
                MAX(latency_ms) AS max_latency_ms,
                MIN(latency_ms) AS min_latency_ms,
                COUNT(DISTINCT pair) AS unique_pairs,
                COUNT(DISTINCT cycle_id) AS total_cycles
            FROM agent_reasoning
            WHERE ts >= ?
        """, (cutoff,)).fetchone()
        summary = dict(summary_row) if summary_row else {}

        # ── Time series ───────────────────────────────────────────────────────
        ts_rows = conn.execute(f"""
            SELECT
                STRFTIME('{bucket_fmt}', ts) AS bucket,
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens,
                ROUND(AVG(latency_ms), 0) AS avg_latency_ms
            FROM agent_reasoning
            WHERE ts >= ?
            GROUP BY bucket
            ORDER BY bucket ASC
        """, (cutoff,)).fetchall()
        time_series = [dict(r) for r in ts_rows]

        # ── By agent ──────────────────────────────────────────────────────────
        agent_rows = conn.execute("""
            SELECT
                agent_name,
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens,
                ROUND(AVG(latency_ms), 0) AS avg_latency_ms,
                ROUND(AVG(prompt_tokens), 0) AS avg_prompt_tokens,
                ROUND(AVG(completion_tokens), 0) AS avg_completion_tokens
            FROM agent_reasoning
            WHERE ts >= ?
            GROUP BY agent_name
            ORDER BY calls DESC
        """, (cutoff,)).fetchall()
        by_agent = [dict(r) for r in agent_rows]

        # ── By exchange ───────────────────────────────────────────────────────
        exch_rows = conn.execute("""
            SELECT
                COALESCE(exchange, 'unknown') AS exchange,
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens
            FROM agent_reasoning
            WHERE ts >= ?
            GROUP BY exchange
            ORDER BY calls DESC
        """, (cutoff,)).fetchall()
        by_exchange = [dict(r) for r in exch_rows]

        # ── Top pairs by call volume ──────────────────────────────────────────
        pair_rows = conn.execute("""
            SELECT
                pair,
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens,
                ROUND(AVG(latency_ms), 0) AS avg_latency_ms
            FROM agent_reasoning
            WHERE ts >= ?
            GROUP BY pair
            ORDER BY calls DESC
            LIMIT 20
        """, (cutoff,)).fetchall()
        top_pairs = [dict(r) for r in pair_rows]

        # ── Latency percentiles ───────────────────────────────────────────────
        latency_rows = conn.execute("""
            SELECT latency_ms
            FROM agent_reasoning
            WHERE ts >= ? AND latency_ms IS NOT NULL
            ORDER BY latency_ms ASC
        """, (cutoff,)).fetchall()
        latencies = [r["latency_ms"] for r in latency_rows]
        p50 = p90 = p99 = None
        if latencies:
            n = len(latencies)
            p50 = latencies[int(n * 0.50)]
            p90 = latencies[int(n * 0.90)]
            p99 = latencies[min(int(n * 0.99), n - 1)]
        summary["p50_latency_ms"] = p50
        summary["p90_latency_ms"] = p90
        summary["p99_latency_ms"] = p99

        # ── Runtime provider stats (from LLMClient in memory) ─────────────────
        providers: list[dict] = []
        llm = deps.llm_client
        if llm is not None:
            try:
                chain = getattr(llm, "_providers", None) or getattr(llm, "providers", [])
                for p in chain:
                    entry: dict = {
                        "name": getattr(p, "name", str(p)),
                        "enabled": getattr(p, "enabled", True),
                        "model": getattr(p, "model", None),
                        "daily_tokens_used": getattr(p, "daily_tokens", 0),
                        "daily_tokens_budget": getattr(p, "daily_token_budget", None),
                        "daily_requests_used": getattr(p, "daily_requests", 0),
                        "daily_requests_budget": getattr(p, "daily_request_budget", None),
                        "rpm_limit": getattr(p, "rpm_limit", None),
                        "in_cooldown": False,
                        "credits_remaining": None,
                    }
                    # Cooldown check
                    cooldown_until = getattr(p, "_cooldown_until", None)
                    if cooldown_until:
                        import time as _time
                        entry["in_cooldown"] = _time.time() < cooldown_until
                        entry["cooldown_until"] = cooldown_until
                    # OpenRouter credits
                    credits = getattr(p, "_credits_remaining", None)
                    if credits is not None:
                        entry["credits_remaining"] = credits
                    providers.append(entry)

                # Global LLMClient counters
                summary["runtime_total_calls"] = getattr(llm, "_call_count", None)
                summary["runtime_total_tokens"] = getattr(llm, "_total_tokens", None)
            except Exception as e:
                logger.debug(f"Could not read LLMClient runtime stats: {e}")

        return deps.sanitize_floats({
            "summary": summary,
            "time_series": time_series,
            "by_agent": by_agent,
            "by_exchange": by_exchange,
            "top_pairs": top_pairs,
            "providers": providers,
            "hours": hours,
            "bucket": "hourly" if hours <= 48 else ("daily" if hours <= 720 else "weekly"),
        })

    except Exception as exc:
        logger.exception("llm-analytics error")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Optimizer endpoints
# ---------------------------------------------------------------------------

class OptimizerApplyRequest(BaseModel):
    settings: dict[str, Any]


@router.get("/api/llm-analytics/optimizer", summary="Get LLM optimizer settings + context")
def get_optimizer(
    hours: int = Query(168, ge=1, le=8760),
    db=Depends(deps.get_profile_db),
):
    """
    Returns the current optimizer settings, parameter metadata, history,
    and real usage context from the DB (to drive simulation calculations).
    """
    current_settings = llm_optimizer.get_settings()
    history = llm_optimizer.get_history(limit=100)

    # ── Usage context from DB (last `hours`) ─────────────────────────────────
    conn = deps.open_conn(db)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    context: dict[str, Any] = {}
    try:
        # Per-agent avg prompt tokens and call count
        agent_rows = conn.execute("""
            SELECT
                agent_name,
                COUNT(*) AS calls,
                ROUND(AVG(prompt_tokens), 0) AS avg_prompt_tokens,
                ROUND(AVG(completion_tokens), 0) AS avg_completion_tokens,
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens
            FROM agent_reasoning
            WHERE ts >= ?
            GROUP BY agent_name
        """, (cutoff,)).fetchall()
        context["by_agent"] = [dict(r) for r in agent_rows]

        # Signal type distribution (for skip impact sim)
        signal_rows = conn.execute("""
            SELECT
                signal_type,
                COUNT(*) AS count,
                ROUND(AVG(confidence), 3) AS avg_confidence
            FROM agent_reasoning
            WHERE ts >= ? AND agent_name = 'strategist' AND signal_type IS NOT NULL
            GROUP BY signal_type
            ORDER BY count DESC
        """, (cutoff,)).fetchall()
        context["signal_distribution"] = [dict(r) for r in signal_rows]

        # Fraction of strategist calls that were below confidence threshold
        total_strat = conn.execute(
            "SELECT COUNT(*) FROM agent_reasoning WHERE ts >= ? AND agent_name = 'strategist'",
            (cutoff,)
        ).fetchone()[0]
        context["total_strategist_calls"] = total_strat

        # Total prompt tokens for the window
        total_row = conn.execute("""
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens,
                COUNT(*) AS total_calls
            FROM agent_reasoning
            WHERE ts >= ?
        """, (cutoff,)).fetchone()
        context["totals"] = dict(total_row) if total_row else {}

    except Exception as e:
        logger.debug(f"Optimizer context query failed: {e}")
    finally:
        conn.close()

    return deps.sanitize_floats({
        "settings": current_settings,
        "defaults": llm_optimizer.DEFAULTS,
        "param_meta": llm_optimizer.PARAM_META,
        "history": history,
        "context": context,
        "hours": hours,
    })


@router.post("/api/llm-analytics/optimizer/apply", summary="Apply LLM optimizer settings")
def apply_optimizer(body: OptimizerApplyRequest):
    """
    Validate and persist new optimizer settings.  Changes take effect within
    one trading cycle (≤ 30 s cache TTL).
    """
    try:
        changes = llm_optimizer.save_settings(body.settings, changed_by="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Failed to apply optimizer settings")
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(f"Optimizer settings updated: {changes}")
    return {
        "ok": True,
        "applied": body.settings,
        "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()},
    }
