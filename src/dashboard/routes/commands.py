"""HITL trade commands and trailing-stop routes."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.commands")

router = APIRouter(tags=["Commands"])


@router.post("/api/trade/{pair}/command", summary="Send a trading command to the agent")
def send_trade_command(
    pair: str,
    action: str = Query(..., description="Command: liquidate, tighten_stop, pause"),
):
    """Publish a trade command via Redis for the orchestrator to execute.

    Supported actions:
    - liquidate: Market sell the entire position
    - tighten_stop: Move stop-loss to breakeven
    - pause: Exclude pair from trading
    """
    if action not in ("liquidate", "tighten_stop", "pause"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # M10: validate pair format (e.g. BTC-USD)
    if not re.match(r'^[A-Z0-9]+-[A-Z0-9]+$', pair.upper()):
        raise HTTPException(status_code=400, detail=f"Invalid pair format: {pair}")
    pair = pair.upper()

    if not deps.redis_client:
        raise HTTPException(status_code=503, detail="Redis not available — cannot send commands")
    if not deps.DASHBOARD_COMMAND_SIGNING_KEY:
        raise HTTPException(
            status_code=503,
            detail="Dashboard command signing key not configured",
        )

    try:
        ts = datetime.now(timezone.utc).isoformat()
        nonce = uuid.uuid4().hex
        command = {
            "action": action,
            "pair": pair,
            "ts": ts,
            "source": "dashboard",
            "nonce": nonce,
        }
        command["signature"] = deps.sign_dashboard_command(
            action=action,
            pair=pair,
            ts=ts,
            source=command["source"],
            nonce=nonce,
        )
        # Push to processing queue (orchestrator polls this)
        deps.redis_client.rpush("dashboard:commands_queue", json.dumps(command))
        # Also publish for real-time subscribers
        deps.redis_client.publish("dashboard:commands", json.dumps(command))
        # Audit trail
        deps.redis_client.lpush("dashboard:command_history", json.dumps(command))
        deps.redis_client.ltrim("dashboard:command_history", 0, 99)

        logger.info(f"📤 HITL command sent: {action} for {pair}")
        return {"status": "command_sent", "action": action, "pair": pair}
    except Exception as exc:
        logger.exception("HITL command error")
        raise HTTPException(status_code=500, detail="Internal error processing command")


@router.get("/api/trade/commands/history", summary="Recent HITL command history")
def get_command_history(limit: int = Query(20, ge=1, le=100)):
    """Returns recent dashboard-initiated commands."""
    if not deps.redis_client:
        return {"commands": []}
    try:
        raw_list = deps.redis_client.lrange("dashboard:command_history", 0, limit - 1)
        commands = []
        for raw in raw_list:
            try:
                commands.append(json.loads(raw))
            except Exception:
                pass
        return {"commands": commands}
    except Exception:
        return {"commands": []}


@router.get("/api/trailing-stops", summary="Active trailing stop states")
def get_trailing_stops(profile: str = Query("")):
    """Returns trailing stop data from Redis (published by the orchestrator).

    When a profile is active, filters trailing stops to only include pairs
    that match the profile's quote currency.
    """
    if not deps.redis_client:
        return {"stops": {}, "source": "unavailable"}
    try:
        raw = deps.redis_client.get("trailing_stops:state")
        if not raw:
            return {"stops": {}, "source": "redis_empty"}
        stops = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        # Filter by profile quote currency
        qc = deps.quote_currency_for(profile)
        if qc and isinstance(stops, dict):
            stops = {
                pair: data for pair, data in stops.items()
                if pair.upper().endswith(f"-{qc.upper()}")
            }
        return deps.sanitize_floats({"stops": stops, "source": "redis"})
    except Exception as exc:
        logger.warning(f"trailing-stops error: {exc}")
        return {"stops": {}, "source": "error"}
