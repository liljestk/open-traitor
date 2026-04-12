from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

import src.dashboard.deps as deps
from src.dashboard import auth
from src.utils.logger import get_logger

logger = get_logger("dashboard.websocket")

router = APIRouter(tags=["WebSocket"])


# ---------------------------------------------------------------------------
# Per-IP WS connection tracking & auth-failure rate limiting
# ---------------------------------------------------------------------------

_ws_ip_connections: dict[str, int] = {}   # ip → active connection count
_ws_ip_lock = threading.Lock()

_ws_auth_failures: dict[str, list[float]] = {}  # ip → [timestamps]
_ws_auth_lock = threading.Lock()


def _get_client_ip(websocket: WebSocket) -> str:
    """Extract client IP from the WebSocket connection."""
    if websocket.client:
        return websocket.client.host
    return "unknown"


def _check_ws_auth_rate(client_ip: str) -> bool:
    """Return True if the IP is allowed another WS auth attempt."""
    now = time.monotonic()
    window = deps.WS_AUTH_RATE_WINDOW
    max_attempts = deps.WS_AUTH_RATE_MAX
    with _ws_auth_lock:
        if len(_ws_auth_failures) > 10_000:
            stale = [k for k, v in _ws_auth_failures.items()
                     if not v or now - v[-1] > window]
            for k in stale:
                del _ws_auth_failures[k]
        attempts = _ws_auth_failures.get(client_ip, [])
        attempts = [t for t in attempts if now - t < window]
        if len(attempts) >= max_attempts:
            _ws_auth_failures[client_ip] = attempts
            return False
        _ws_auth_failures[client_ip] = attempts
        return True


def _record_ws_auth_failure(client_ip: str) -> None:
    """Record a failed WS auth attempt for rate limiting."""
    now = time.monotonic()
    with _ws_auth_lock:
        attempts = _ws_auth_failures.get(client_ip, [])
        attempts.append(now)
        _ws_auth_failures[client_ip] = attempts


def _acquire_ws_slot(client_ip: str) -> str | None:
    """Try to acquire a WS connection slot. Returns an error reason or None on success."""
    with _ws_ip_lock:
        if len(deps.ws_connections) >= deps.MAX_WS_CONNECTIONS:
            return "Server at maximum WebSocket capacity"
        count = _ws_ip_connections.get(client_ip, 0)
        if count >= deps.MAX_WS_PER_IP:
            return f"Too many connections from {client_ip}"
        _ws_ip_connections[client_ip] = count + 1
    return None


def _release_ws_slot(client_ip: str) -> None:
    """Release a WS connection slot."""
    with _ws_ip_lock:
        count = _ws_ip_connections.get(client_ip, 0)
        if count <= 1:
            _ws_ip_connections.pop(client_ip, None)
        else:
            _ws_ip_connections[client_ip] = count - 1


def _validate_origin(websocket: WebSocket) -> bool:
    """Validate the Origin header against allowed origins.

    Returns True if the origin is allowed or absent (non-browser clients).
    Browser WebSocket requests always include an Origin header.
    """
    origin = websocket.headers.get("origin", "")
    if not origin:
        # Non-browser clients (curl, Python, etc.) don't send Origin — allow
        return True
    return origin in deps.allowed_origins


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
    client_ip = _get_client_ip(websocket)

    # --- Origin validation (CSWSH protection) ---
    if not _validate_origin(websocket):
        origin = websocket.headers.get("origin", "")
        logger.warning(f"WS rejected: disallowed origin {origin!r} from {client_ip}")
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    # --- Connection capacity checks ---
    slot_error = _acquire_ws_slot(client_ip)
    if slot_error:
        logger.warning(f"WS rejected: {slot_error}")
        await websocket.close(code=1013, reason=slot_error)
        return

    slot_acquired = True
    try:
        # --- Auth rate limiting (brute-force protection) ---
        if auth.is_auth_configured():
            if not _check_ws_auth_rate(client_ip):
                logger.warning(f"WS auth rate-limited: {client_ip}")
                await websocket.close(code=1008, reason="Too many auth attempts")
                return

            # Check session cookie or legacy API key via subprotocol
            session_token = websocket.cookies.get("ot_session", "")
            api_key = ""
            _auth_subprotocol = None
            if not session_token:
                # Browsers can't set custom headers on WS, so the frontend encodes
                # the key as a subprotocol: "apikey.<base64_key>"
                for proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
                    proto = proto.strip()
                    if proto.startswith("apikey."):
                        try:
                            raw = proto[7:]
                            if len(raw) > 256:
                                logger.warning("WebSocket auth: base64 payload too large, rejected")
                                break
                            api_key = base64.b64decode(raw).decode("utf-8")
                            _auth_subprotocol = proto
                        except Exception:
                            logger.warning("WebSocket auth: invalid base64 in subprotocol")
                        break
                api_key = api_key or websocket.headers.get("x-api-key", "")

            authenticated = False
            if session_token and auth.validate_session(session_token):
                authenticated = True
            elif api_key and auth._LEGACY_API_KEY and hmac.compare_digest(api_key, auth._LEGACY_API_KEY):
                authenticated = True

            if not authenticated:
                _record_ws_auth_failure(client_ip)
                logger.warning(
                    f"WS auth failed from {client_ip}"
                    f" (cookie={'yes' if session_token else 'no'},"
                    f" apikey={'yes' if api_key else 'no'})"
                )
                await websocket.close(code=1008, reason="Authentication required")
                return

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
        _ws_exchange = deps.resolve_profile(_ws_profile)

        deps.ws_connections.append((websocket, _ws_exchange))
        logger.info(f"WS client connected (profile={_ws_profile!r}, exchange={_ws_exchange}) ({len(deps.ws_connections)} total)")
        try:
            while True:
                # Keep connection alive; events are pushed by redis_subscriber
                await asyncio.sleep(30)
                await websocket.send_json({"type": "ping", "ts": deps.utcnow()})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            # Guard: the Redis subscriber may have already removed this socket
            deps.ws_connections[:] = [(ws, ex) for ws, ex in deps.ws_connections if ws is not websocket]
            logger.info(f"WS client disconnected ({len(deps.ws_connections)} remaining)")
    finally:
        if slot_acquired:
            _release_ws_slot(client_ip)


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

                # Extract exchange from event for profile filtering
                event_exchange = (payload.get("exchange") or "").lower()

                dead = []
                for ws, ws_exchange in list(deps.ws_connections):
                    # Filter: if this WS connection has an exchange filter,
                    # only send events that match (or have no exchange info)
                    if ws_exchange and event_exchange:
                        if event_exchange != ws_exchange and not event_exchange.startswith(ws_exchange + "_"):
                            continue
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    deps.ws_connections[:] = [(w, e) for w, e in deps.ws_connections if w is not ws]

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
