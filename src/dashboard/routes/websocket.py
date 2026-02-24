from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.websocket")

router = APIRouter(tags=["WebSocket"])

_DASHBOARD_API_KEY: str = os.environ.get("DASHBOARD_API_KEY", "")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    """
    Streams real-time LLM span events from Redis pub/sub channel `llm:events`.
    Clients receive JSON-encoded SpanEvent objects as they happen.

    Message format:
        {
            "type": "span_complete",
            "cycle_id": "...",
            "pair": "BTC-USD",
            "agent_name": "market_analyst",
            "model": "llama3.1:8b",
            "latency_ms": 1234.5,
            "prompt_tokens": 512,
            "completion_tokens": 256,
            "langfuse_trace_id": "...",
            "ts": "2025-01-01T00:00:00Z"
        }
    """
    if _DASHBOARD_API_KEY:
        # Try X-API-Key header first; fall back to Sec-WebSocket-Protocol
        # (browsers can't set custom headers on WS, so the frontend encodes
        # the key as a subprotocol: "apikey.<base64_key>")
        api_key = websocket.headers.get("x-api-key", "")
        if not api_key:
            for proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
                proto = proto.strip()
                if proto.startswith("apikey."):
                    try:
                        api_key = base64.b64decode(proto[7:]).decode("utf-8")
                    except Exception:
                        pass
                    break
        if not hmac.compare_digest(api_key, _DASHBOARD_API_KEY):
            await websocket.close(code=1008, reason="Invalid or missing API key")
            return
    # When no API key is configured, network-level access control is handled
    # by the Docker port binding (127.0.0.1:8090) — same as the HTTP
    # middleware.  Checking websocket.client.host here would break Docker
    # setups where the client appears as the bridge-network gateway IP.

    # L22 fix: echo the auth subprotocol so browsers don't reject per RFC 6455
    _accepted_subprotocol = None
    for _proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
        _proto = _proto.strip()
        if _proto.startswith("apikey."):
            _accepted_subprotocol = _proto
            break
    await websocket.accept(subprotocol=_accepted_subprotocol)

    # Extract profile from query params for event filtering
    _qs = parse_qs(urlparse(str(websocket.url)).query)
    _ws_profile = (_qs.get("profile", [""])[0] or "").strip()
    _ws_qc = deps.quote_currency_for(_ws_profile)

    deps.ws_connections.append((websocket, _ws_qc))
    logger.info(f"WS client connected (profile={_ws_profile!r}, qc={_ws_qc}) ({len(deps.ws_connections)} total)")
    try:
        while True:
            # Keep connection alive; events are pushed by redis_subscriber
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping", "ts": deps.utcnow()})
    except WebSocketDisconnect:
        pass
    finally:
        # Guard: the Redis subscriber may have already removed this socket
        deps.ws_connections[:] = [(ws, qc) for ws, qc in deps.ws_connections if ws is not websocket]
        logger.info(f"WS client disconnected ({len(deps.ws_connections)} remaining)")


# ---------------------------------------------------------------------------
# Redis pub/sub → WebSocket broadcaster
# ---------------------------------------------------------------------------

async def redis_subscriber():
    """
    Background task: subscribes to Redis `llm:events` channel and
    broadcasts each message to all connected WebSocket clients.

    Reconnects with exponential backoff if the Redis connection drops.
    """
    if deps.redis_client is None:
        return

    import redis.asyncio as aioredis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    backoff = 1.0
    max_backoff = 60.0

    while True:
        try:
            async_redis = aioredis.from_url(redis_url)
            pubsub = async_redis.pubsub()
            await pubsub.subscribe("llm:events")
            logger.info("Subscribed to Redis llm:events")
            backoff = 1.0  # Reset on successful connect

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except Exception:
                    continue

                # Extract pair from event for profile filtering
                event_pair = (payload.get("pair") or "").upper()

                dead = []
                for ws, ws_qc in list(deps.ws_connections):
                    # Filter: if this WS connection has a quote currency filter,
                    # only send events that match (or have no pair info)
                    if ws_qc and event_pair and not event_pair.endswith(f"-{ws_qc.upper()}"):
                        continue
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    deps.ws_connections[:] = [(w, q) for w, q in deps.ws_connections if w is not ws]

        except asyncio.CancelledError:
            # Graceful shutdown
            try:
                await pubsub.unsubscribe("llm:events")
                await async_redis.aclose()
            except Exception:
                pass
            return
        except Exception as e:
            logger.warning(f"Redis subscriber disconnected: {e} — reconnecting in {backoff:.0f}s")
            try:
                await async_redis.aclose()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
