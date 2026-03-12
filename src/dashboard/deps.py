"""
Dashboard shared dependencies — global state, helpers, and FastAPI dependencies.

All route modules import from here to access shared state like StatsDB,
Redis, Temporal client, exchange clients, and utility helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, Query

from src.utils.logger import get_logger

logger = get_logger("dashboard.deps")


# ═══════════════════════════════════════════════════════════════════════════
# Shared state (injected at startup via set_globals / lifespan)
# ═══════════════════════════════════════════════════════════════════════════

stats_db = None           # StatsDB instance
redis_client = None       # redis.Redis instance (optional)
temporal_client = None    # temporalio.client.Client instance (optional)
temporal_host: str = os.environ.get("TEMPORAL_HOST", "localhost:7233")
temporal_namespace: str = os.environ.get("TEMPORAL_NAMESPACE", "default")
config: dict = {}
exchange_client = None    # ExchangeClient instance (optional, for price lookups)
ibkr_exchange_client = None  # IBClient instance (optional, for IBKR price/news)

ws_connections: list = []  # (WebSocket, exchange_filter)

# WebSocket connection limits
MAX_WS_CONNECTIONS: int = 50       # Global cap on concurrent WS connections
MAX_WS_PER_IP: int = 10            # Max concurrent WS connections per IP
WS_AUTH_RATE_WINDOW: int = 60      # Seconds for WS auth attempt rate limiting
WS_AUTH_RATE_MAX: int = 10         # Max failed WS auth attempts per IP per window

# Allowed origins (shared by CORS middleware + WebSocket origin validation)
_cors_origins_raw = os.environ.get("DASHBOARD_CORS_ORIGINS", "")
_cors_origins_explicit = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]


def _build_allowed_origins() -> list[str]:
    """Build the full allowed-origins list.

    Starts with user-configured origins (or localhost defaults), then
    auto-adds the machine's `<hostname>.local` and Tailscale MagicDNS
    origins so LAN / tailnet dashboard access works without manual config.
    """
    import socket

    base = _cors_origins_explicit or ["http://localhost:5173", "http://localhost:8090"]
    if "*" in base:
        base = ["http://localhost:5173", "http://localhost:8090"]

    tailnet = os.environ.get("TAILSCALE_DOMAIN", "tailc4de35.ts.net")
    dashboard_ports = ["5173", "8090"]

    try:
        hostname = socket.gethostname().lower()
    except Exception:
        return base

    extra: list[str] = []
    for suffix in [f"{hostname}.local", f"{hostname}.{tailnet}"]:
        for port in dashboard_ports:
            for scheme in ["http", "https"]:
                origin = f"{scheme}://{suffix}:{port}"
                if origin not in base:
                    extra.append(origin)

    return base + extra


allowed_origins: list[str] = _build_allowed_origins()

rules_instance = None     # AbsoluteRules instance (optional, for runtime push)
llm_client = None         # LLMClient instance (optional, for provider status)


# ═══════════════════════════════════════════════════════════════════════════
# Profile resolution
# ═══════════════════════════════════════════════════════════════════════════

PROFILE_ALIASES: dict[str, str] = {
    "crypto": "coinbase",
    "equity": "ibkr",
}

PROFILE_USE_DEFAULT_DB: set[str] = {"settings"}

PROFILE_CONFIG_FILES: dict[str, str] = {
    "coinbase": "config/coinbase.yaml",
    "ibkr": "config/ibkr.yaml",
}


# ═══════════════════════════════════════════════════════════════════════════
# Profile helpers
# ═══════════════════════════════════════════════════════════════════════════

def resolve_profile(profile: str) -> str:
    """Resolve frontend profile aliases to canonical backend profile names."""
    if not profile:
        return ""
    p = profile.lower().strip()
    return PROFILE_ALIASES.get(p, p)


def quote_currency_for(profile: str) -> list[str] | None:
    """Return the quote currencies for a profile, read dynamically from YAML config.

    Returns None for 'Default / All' (no filtering).
    Returns a list like ``["EUR"]`` or ``["EUR", "USD"]`` for specific profiles.
    """
    resolved = resolve_profile(profile)
    if not resolved:
        return None
    cfg = get_config_for_profile(profile)
    currencies = cfg.get("trading", {}).get("quote_currencies", [])
    if currencies:
        return [c.upper() for c in currencies]
    # Fallback: read singular quote_currency
    single = cfg.get("trading", {}).get("quote_currency")
    if single:
        return [single.upper()]
    return None


def get_config_for_profile(profile: str = "") -> dict:
    """Load the config for a specific profile, falling back to the default config."""
    resolved = resolve_profile(profile)
    config_file = PROFILE_CONFIG_FILES.get(resolved)
    if config_file:
        try:
            from src.utils.settings_manager import load_settings
            return load_settings(config_file)
        except Exception:
            pass
    return get_config()


def get_config() -> dict:
    """Return the current config, reloading from disk to pick up runtime changes."""
    try:
        from src.utils.settings_manager import load_settings
        return load_settings()
    except Exception:
        return config


# ═══════════════════════════════════════════════════════════════════════════
# Confirmation tokens (for sensitive operations)
# ═══════════════════════════════════════════════════════════════════════════

_pending_confirmations: dict[str, dict] = {}
_pending_confirmations_lock = threading.Lock()


def store_confirmation(token: str, data: dict) -> None:
    """Thread-safe store for confirmation tokens."""
    with _pending_confirmations_lock:
        _pending_confirmations[token] = data


def pop_confirmation(token: str) -> dict | None:
    """Thread-safe pop for confirmation tokens."""
    with _pending_confirmations_lock:
        return _pending_confirmations.pop(token, None)


def expire_confirmations() -> None:
    """Remove expired confirmation tokens (thread-safe)."""
    now = time.monotonic()
    with _pending_confirmations_lock:
        expired = [t for t, v in _pending_confirmations.items() if v["expires"] < now]
        for t in expired:
            del _pending_confirmations[t]


# ═══════════════════════════════════════════════════════════════════════════
# Rate-limit helpers for confirmation endpoints
# (separate lock from _pending_confirmations to avoid coupling)
# ═══════════════════════════════════════════════════════════════════════════

_confirmation_attempts: dict[str, list[float]] = {}
_confirmation_attempts_lock = threading.Lock()


def prune_expired_rate_entries() -> None:
    """Remove stale rate-limit buckets (entries where all timestamps are old)."""
    now = time.monotonic()
    with _confirmation_attempts_lock:
        expired_keys = [k for k, v in _confirmation_attempts.items()
                        if all(t < now - 300 for t in v)]
        for k in expired_keys:
            del _confirmation_attempts[k]


def check_confirmation_rate(client_ip: str, max_per_window: int = 5, window_seconds: int = 300) -> bool:
    """Return True if the client is within the rate limit, False if blocked."""
    now = time.monotonic()
    with _confirmation_attempts_lock:
        # L3: Cap dict size to prevent unbounded memory growth
        # Inline pruning to avoid deadlock (Lock is non-reentrant)
        if len(_confirmation_attempts) > 10_000:
            expired_keys = [k for k, v in _confirmation_attempts.items()
                            if all(t < now - 300 for t in v)]
            for k in expired_keys:
                del _confirmation_attempts[k]
            # If still too large after pruning, drop oldest half
            if len(_confirmation_attempts) > 10_000:
                to_drop = sorted(_confirmation_attempts.keys(),
                                 key=lambda k: max(_confirmation_attempts[k], default=0))[:5000]
                for k in to_drop:
                    del _confirmation_attempts[k]
        attempts = _confirmation_attempts.get(client_ip, [])
        attempts = [t for t in attempts if t > now - window_seconds]
        if len(attempts) >= max_per_window:
            _confirmation_attempts[client_ip] = attempts
            return False
        attempts.append(now)
        _confirmation_attempts[client_ip] = attempts
        return True


# ═══════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════

def require_db(profile: str = ""):
    """Return the shared StatsDB singleton.

    All profiles now share a single PostgreSQL database; exchange-level
    filtering is done via the ``exchange`` column in each table.
    """
    if stats_db is None:
        raise HTTPException(status_code=503, detail="Stats DB not initialised")
    return stats_db


def get_profile_db(
    profile: str = Query("", description="Exchange profile (e.g. 'coinbase', 'ibkr')"),
):
    """FastAPI dependency — resolves the StatsDB for the requested profile."""
    return require_db(profile)


# ═══════════════════════════════════════════════════════════════════════════
# Utility helpers
# ═══════════════════════════════════════════════════════════════════════════

def sanitize_floats(obj):
    """Recursively replace inf/nan floats with None so JSON serialisation succeeds."""
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_floats(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return None
    return obj


DASHBOARD_COMMAND_SIGNING_KEY: str = (
    os.environ.get("DASHBOARD_COMMAND_SIGNING_KEY", "")
    or os.environ.get("DASHBOARD_API_KEY", "")
)

if not DASHBOARD_COMMAND_SIGNING_KEY:
    logger.warning(
        "⚠️  DASHBOARD_COMMAND_SIGNING_KEY not set — trade command signing is disabled. "
        "Set this env var to enable authenticated dashboard commands."
    )


def sign_dashboard_command(
    action: str,
    pair: str,
    ts: str,
    source: str,
    nonce: str,
) -> str:
    """Generate an HMAC-SHA256 signature for a dashboard command.

    H13 fix: This function was referenced by commands.py and watchlist.py
    but was never defined, causing AttributeError at runtime.
    """
    import hashlib
    import hmac as _hmac

    if not DASHBOARD_COMMAND_SIGNING_KEY:
        raise RuntimeError("DASHBOARD_COMMAND_SIGNING_KEY is not configured")

    payload = f"{action}|{pair}|{ts}|{source}|{nonce}"
    return _hmac.new(
        DASHBOARD_COMMAND_SIGNING_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()





def utcnow() -> str:
    """Return the current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def langfuse_url(trace_id: str | None = None) -> str | None:
    """Build a Langfuse URL for a trace/span using the Langfuse SDK if available."""
    if not trace_id:
        return None
    # External host for browser-accessible URLs (not internal Docker hostname)
    external_host = get_config().get("dashboard", {}).get("langfuse_host", "http://localhost:3000")
    try:
        from src.utils.tracer import get_llm_tracer
        tracer = get_llm_tracer()
        if tracer:
            url = tracer.get_trace_url(trace_id)
            if url:
                # SDK returns internal Docker URL (e.g. http://langfuse-web:3000/...)
                # Rewrite to external host for browser access
                import re
                url = re.sub(r'^https?://[^/]+', external_host.rstrip('/'), url)
                return url
    except Exception:
        pass
    # Fallback: best-effort URL
    return f"{external_host}/trace/{trace_id}"


def serialize_event_attrs(event) -> dict:
    """Best-effort serialization of a Temporal history event attributes."""
    try:
        attrs = event.attributes
        if hasattr(attrs, "__dict__"):
            raw = {k: str(v) for k, v in attrs.__dict__.items() if not k.startswith("_")}
        elif hasattr(attrs, "DESCRIPTOR"):
            # protobuf message
            raw = {}
            for field in attrs.DESCRIPTOR.fields:
                val = getattr(attrs, field.name, None)
                if val is not None:
                    raw[field.name] = str(val)
            return raw
        else:
            raw = {"raw": str(attrs)}
        return raw
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# Exchange client helpers
# ═══════════════════════════════════════════════════════════════════════════

def ensure_ibkr_client():
    """Lazily initialise the IBKR exchange client on first request."""
    import src.dashboard.deps as _self
    if _self.ibkr_exchange_client is not None:
        return _self.ibkr_exchange_client
    try:
        ib_host = os.environ.get("IBKR_HOST", "127.0.0.1")
        ib_port = int(os.environ.get("IBKR_PORT", "4001"))
        ib_client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))
        from src.core.ib_client import IBClient
        client = IBClient(
            paper_mode=False,
            ib_host=ib_host,
            ib_port=ib_port,
            ib_client_id=ib_client_id + 10,
        )
        _self.ibkr_exchange_client = client
        logger.info("✅ Dashboard lazy-initialised IBKR client")
        return client
    except Exception as e:
        logger.warning(f"⚠️ Could not initialise IBKR client: {e}")
        return None


def client_for_profile(profile: str):
    """Return the exchange client for the given profile."""
    resolved = resolve_profile(profile)
    if resolved == "ibkr":
        return ensure_ibkr_client()
    return exchange_client


def is_equity_profile(profile: str) -> bool:
    """Return True if the profile is an equity exchange."""
    return resolve_profile(profile) == "ibkr"


def get_live_price(pair: str, profile: str = "") -> float | None:
    """Get a live price from the appropriate exchange client."""
    client = client_for_profile(profile)
    if client is None:
        return None
    try:
        price = client.get_current_price(pair)
        return price if price and price > 0 else None
    except Exception:
        return None


