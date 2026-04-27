"""
Decision-distribution drift detector.

Daily snapshot of strategist confidence + action distribution; alert when
the current snapshot is >2σ from the trailing 30-day baseline.

Run via Temporal nightly. Pure read from ``agent_reasoning`` + write to
``decision_drift``.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("decision_drift")


def _percentile(sorted_vals: list[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _fetch_window(db, exchange: str, *, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=int(days))
    sql = (
        "SELECT cycle_id, agent_name, reasoning_json, ts "
        "FROM agent_reasoning "
        "WHERE exchange = %s AND ts >= %s "
        "AND agent_name IN ('strategist', 'risk_manager') "
        "LIMIT 50000"
    )
    try:
        with db._get_conn() as conn:
            rows = conn.execute(sql, (exchange, since.isoformat())).fetchall()
    except Exception as e:
        logger.warning(f"decision_drift: fetch failed: {e}")
        return []
    out = []
    for r in rows:
        try:
            j = json.loads(r["reasoning_json"]) if r.get("reasoning_json") else {}
        except Exception:
            j = {}
        conf = j.get("confidence")
        action = (j.get("action") or j.get("decision") or "hold")
        if conf is None:
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        out.append({
            "agent": r.get("agent_name"),
            "ts": r.get("ts"),
            "confidence": conf,
            "action": str(action),
        })
    return out


def compute_drift_snapshot(
    db,
    *,
    exchange: str,
    snapshot_window_hours: int = 24,
    baseline_days: int = 30,
    z_alert: float = 2.0,
) -> dict:
    """Compute today's drift vs trailing-30d baseline; persist & return."""
    today_rows = _fetch_window(db, exchange, days=max(1, snapshot_window_hours // 24))
    baseline_rows = _fetch_window(db, exchange, days=baseline_days)

    if not today_rows:
        return {"snapshots": 0, "alerts": 0}

    by_agent: dict[str, list[dict]] = {}
    for r in today_rows:
        by_agent.setdefault(r["agent"], []).append(r)

    baseline_by_agent: dict[str, list[float]] = {}
    for r in baseline_rows:
        baseline_by_agent.setdefault(r["agent"], []).append(r["confidence"])

    out_rows: list[dict] = []
    alerts = 0
    today_date = datetime.now(timezone.utc).date()

    for agent, rows in by_agent.items():
        confs = sorted(r["confidence"] for r in rows)
        action_dist: dict[str, int] = {}
        for r in rows:
            action_dist[r["action"]] = action_dist.get(r["action"], 0) + 1

        baseline = baseline_by_agent.get(agent) or []
        baseline_mean = statistics.mean(baseline) if len(baseline) >= 5 else None
        baseline_std = statistics.pstdev(baseline) if len(baseline) >= 5 else None

        mean_conf = statistics.mean(confs) if confs else None
        z = None
        alert = False
        if (
            baseline_mean is not None
            and baseline_std and baseline_std > 1e-6
            and mean_conf is not None
        ):
            z = (mean_conf - baseline_mean) / baseline_std
            if abs(z) >= z_alert and len(baseline) >= 30:
                alert = True
                alerts += 1

        out_rows.append({
            "snapshot_date": today_date,
            "agent": agent,
            "n_decisions": len(rows),
            "mean_conf": mean_conf,
            "p10_conf": _percentile(confs, 0.10),
            "p50_conf": _percentile(confs, 0.50),
            "p90_conf": _percentile(confs, 0.90),
            "action_dist": action_dist,
            "baseline_mean": baseline_mean,
            "baseline_std": baseline_std,
            "z_score": z,
            "alert": alert,
        })

    written = 0
    if out_rows:
        try:
            written = db.write_decision_drift(exchange, out_rows)
        except Exception as e:
            logger.warning(f"decision_drift: write failed: {e}")

    logger.info(
        f"decision_drift: {exchange} snapshots={len(out_rows)} "
        f"alerts={alerts} rows_written={written}"
    )
    return {"snapshots": len(out_rows), "alerts": alerts, "rows_written": written}
