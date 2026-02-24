"""
Dashboard shared dependencies — global state, helpers, and FastAPI dependencies.

All route modules import from here to access shared state like StatsDB,
Redis, Temporal client, exchange clients, and utility helpers.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import math
import os
import sqlite3
import threading
import time
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

ws_connections: list = []  # (WebSocket, quote_currency_filter)

rules_instance = None     # AbsoluteRules instance (optional, for runtime push)
llm_client = None         # LLMClient instance (optional, for provider status)


# ═══════════════════════════════════════════════════════════════════════════
# Profile resolution
# ═══════════════════════════════════════════════════════════════════════════

PROFILE_ALIASES: dict[str, str] = {
    "crypto": "coinbase",
}

PROFILE_USE_DEFAULT_DB: set[str] = {"settings"}

PROFILE_CONFIG_FILES: dict[str, str] = {
    "coinbase": "config/coinbase.yaml",
    "nordnet": "config/nordnet.yaml",
    "ibkr": "config/ibkr.yaml",
}

PROFILE_CURRENCIES: dict[str, str] = {
    "": "EUR",
    "coinbase": "EUR",
    "nordnet": "SEK",
    "ibkr": "EUR",
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


def quote_currency_for(profile: str) -> str | None:
    """Return the quote currency for a profile, or None for 'Default / All'."""
    resolved = resolve_profile(profile)
    if not resolved:
        return None
    return PROFILE_CURRENCIES.get(resolved)


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
# ═══════════════════════════════════════════════════════════════════════════

_confirmation_attempts: dict[str, list[float]] = {}


def prune_expired_confirmations() -> None:
    """Remove expired confirmation attempts."""
    now = time.monotonic()
    with _pending_confirmations_lock:
        expired_keys = [k for k, v in _confirmation_attempts.items()
                        if all(t < now - 300 for t in v)]
        for k in expired_keys:
            del _confirmation_attempts[k]


def check_confirmation_rate(client_ip: str, max_per_window: int = 5, window_seconds: int = 300) -> bool:
    """Return True if the client is within the rate limit, False if blocked."""
    now = time.monotonic()
    with _pending_confirmations_lock:
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

_MAX_PROFILE_DBS = 16
_profile_db_cache: collections.OrderedDict[str, Any] = collections.OrderedDict()
_profile_db_lock = threading.Lock()


def require_db(profile: str = ""):
    """Return the StatsDB for *profile* (empty string → default / injected)."""
    resolved = resolve_profile(profile)

    if not resolved or resolved in PROFILE_USE_DEFAULT_DB:
        if stats_db is None:
            raise HTTPException(status_code=503, detail="Stats DB not initialised")
        return stats_db

    safe = "".join(c for c in resolved if c.isalnum() or c == "_")
    if not safe:
        raise HTTPException(status_code=400, detail=f"Invalid profile: {profile!r}")

    with _profile_db_lock:
        if safe in _profile_db_cache:
            _profile_db_cache.move_to_end(safe)
            return _profile_db_cache[safe]
        try:
            from src.utils.stats import StatsDB
            db_path = os.path.join("data", f"stats_{safe}.db")
            if not os.path.exists(db_path) or os.path.getsize(db_path) < 8192:
                logger.info(f"📊 Profile DB missing or empty ({db_path}), creating empty StatsDB for '{safe}'")
                db = StatsDB(db_path=db_path)
                _profile_db_cache[safe] = db
                return db
            if len(_profile_db_cache) >= _MAX_PROFILE_DBS:
                evicted_key, evicted_db = _profile_db_cache.popitem(last=False)
                try:
                    evicted_db.close()
                except Exception:
                    pass
            db = StatsDB(db_path=db_path)
            _profile_db_cache[safe] = db
            logger.info(f"📊 Loaded StatsDB for profile '{safe}': {db_path}")
            return db
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Cannot load StatsDB for profile '{safe}': {e}")


def get_profile_db(
    profile: str = Query("", description="Exchange profile (e.g. 'coinbase', 'nordnet', 'ibkr')"),
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


def sign_dashboard_command(action: str, pair: str, ts: str, source: str, nonce: str) -> str:
    """Create a deterministic HMAC signature for dashboard trade commands."""
    payload = f"{action}|{pair}|{ts}|{source}|{nonce}"
    return hmac.new(
        DASHBOARD_COMMAND_SIGNING_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def open_conn(db) -> sqlite3.Connection:
    """Open a fresh SQLite connection for the given StatsDB instance."""
    conn = sqlite3.connect(db.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def fresh_conn(profile: str = "") -> sqlite3.Connection:
    """Open a fresh SQLite connection for the given profile."""
    return open_conn(require_db(profile))


def utcnow() -> str:
    """Return the current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def langfuse_url(trace_id: str | None = None) -> str | None:
    """Build a Langfuse URL for a trace/span using the Langfuse SDK if available."""
    if not trace_id:
        return None
    try:
        from src.utils.tracer import get_llm_tracer
        tracer = get_llm_tracer()
        if tracer:
            url = tracer.get_trace_url(trace_id)
            if url:
                return url
    except Exception:
        pass
    # Fallback: best-effort URL (may not work if project ID is needed)
    host = get_config().get("dashboard", {}).get("langfuse_host", "http://localhost:3000")
    return f"{host}/trace/{trace_id}"


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
    if resolved in ("ibkr", "nordnet"):
        return ensure_ibkr_client()
    return exchange_client


def is_equity_profile(profile: str) -> bool:
    """Return True if the profile is an equity exchange."""
    return resolve_profile(profile) in ("ibkr", "nordnet")


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


# Import datetime at module level for utcnow
from datetime import datetime, timezone
