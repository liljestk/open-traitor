"""
Dashboard authentication — session-based auth with bcrypt password hashing.

Provides:
  - Password hashing & verification (bcrypt)
  - Session token management (secure random, server-side store)
  - CSRF token generation & validation
  - Login rate limiting
  - httpOnly cookie helpers

Environment variables:
  DASHBOARD_PASSWORD_HASH   bcrypt hash of the dashboard password
  DASHBOARD_SESSION_SECRET  secret used to derive CSRF tokens (auto-generated if missing)
  DASHBOARD_SESSION_TTL     session lifetime in seconds (default: 3600 = 1h)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("dashboard.auth")

# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════

# bcrypt hash of the dashboard password (set via setup wizard or CLI)
_PASSWORD_HASH: str = os.environ.get("DASHBOARD_PASSWORD_HASH", "")

# Secret for CSRF derivation — auto-generated per process if not set
_SESSION_SECRET: str = os.environ.get("DASHBOARD_SESSION_SECRET", "") or secrets.token_hex(32)

# Session lifetime (seconds); default 1 hour
SESSION_TTL: int = int(os.environ.get("DASHBOARD_SESSION_TTL", "3600"))

# Legacy API key (backward compat — if set, still accepted as bearer auth)
_LEGACY_API_KEY: str = os.environ.get("DASHBOARD_API_KEY", "")


# ═══════════════════════════════════════════════════════════════════════════
# Password hashing (bcrypt)
# ═══════════════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. Returns the hash string."""
    import bcrypt
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def is_auth_configured() -> bool:
    """Return True if any auth mechanism is configured (password or legacy API key)."""
    return bool(_PASSWORD_HASH) or bool(_LEGACY_API_KEY)


def get_password_hash() -> str:
    """Return the configured password hash (may be empty)."""
    return _PASSWORD_HASH


def set_password_hash(h: str) -> None:
    """Update the in-memory password hash (called by setup wizard)."""
    global _PASSWORD_HASH
    _PASSWORD_HASH = h


# ═══════════════════════════════════════════════════════════════════════════
# Session store (server-side, in-memory)
# ═══════════════════════════════════════════════════════════════════════════

_sessions: dict[str, dict] = {}  # token → {created, last_active, ip}
_sessions_lock = threading.Lock()

# Max concurrent sessions — prevent memory exhaustion
_MAX_SESSIONS = 100


def create_session(client_ip: str = "") -> str:
    """Create a new session and return the token."""
    token = secrets.token_urlsafe(48)
    now = time.monotonic()
    with _sessions_lock:
        # Evict expired sessions first
        _evict_expired_sessions_locked()
        # If still at capacity, reject
        if len(_sessions) >= _MAX_SESSIONS:
            # Drop the oldest session
            oldest = min(_sessions, key=lambda k: _sessions[k]["last_active"])
            del _sessions[oldest]
        _sessions[token] = {
            "created": now,
            "last_active": now,
            "ip": client_ip,
        }
    return token


def validate_session(token: str) -> bool:
    """Check if a session token is valid and not expired. Extends TTL on success."""
    if not token:
        return False
    now = time.monotonic()
    with _sessions_lock:
        session = _sessions.get(token)
        if not session:
            return False
        if now - session["created"] > SESSION_TTL:
            del _sessions[token]
            return False
        session["last_active"] = now
        return True


def revoke_session(token: str) -> None:
    """Revoke/delete a session."""
    with _sessions_lock:
        _sessions.pop(token, None)


def revoke_all_sessions() -> None:
    """Revoke all active sessions (e.g. after password change)."""
    with _sessions_lock:
        _sessions.clear()


def active_session_count() -> int:
    """Return count of active sessions."""
    now = time.monotonic()
    with _sessions_lock:
        return sum(1 for s in _sessions.values() if now - s["created"] <= SESSION_TTL)


def _evict_expired_sessions_locked() -> None:
    """Remove expired sessions. Must be called with _sessions_lock held."""
    now = time.monotonic()
    expired = [t for t, s in _sessions.items() if now - s["created"] > SESSION_TTL]
    for t in expired:
        del _sessions[t]


# ═══════════════════════════════════════════════════════════════════════════
# CSRF tokens
# ═══════════════════════════════════════════════════════════════════════════

def generate_csrf_token(session_token: str) -> str:
    """Generate a CSRF token bound to a session."""
    return hmac.new(
        _SESSION_SECRET.encode("utf-8"),
        session_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_csrf_token(session_token: str, csrf_token: str) -> bool:
    """Validate a CSRF token against its session."""
    expected = generate_csrf_token(session_token)
    return hmac.compare_digest(expected, csrf_token)


# ═══════════════════════════════════════════════════════════════════════════
# Login rate limiting
# ═══════════════════════════════════════════════════════════════════════════

_login_attempts: dict[str, list[float]] = {}  # ip → [timestamps]
_login_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 300  # 5 minutes


def check_login_rate(client_ip: str) -> bool:
    """Return True if the IP is allowed to attempt a login. False = rate-limited."""
    now = time.monotonic()
    with _login_lock:
        # Prune if dict grows too large
        if len(_login_attempts) > 10_000:
            stale = [k for k, v in _login_attempts.items()
                     if not v or now - v[-1] > _LOGIN_WINDOW]
            for k in stale:
                del _login_attempts[k]
        attempts = _login_attempts.get(client_ip, [])
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            _login_attempts[client_ip] = attempts
            return False
        attempts.append(now)
        _login_attempts[client_ip] = attempts
        return True


# ═══════════════════════════════════════════════════════════════════════════
# Request helpers (extract tokens from cookies / headers)
# ═══════════════════════════════════════════════════════════════════════════

def get_session_from_request(request) -> Optional[str]:
    """Extract session token from httpOnly cookie or Authorization header."""
    # 1. Cookie (primary)
    token = request.cookies.get("ot_session")
    if token and validate_session(token):
        return token

    # 2. Legacy: X-API-Key header (backward compat)
    if _LEGACY_API_KEY:
        api_key = request.headers.get("X-API-Key", "")
        if api_key and hmac.compare_digest(api_key, _LEGACY_API_KEY):
            return "__legacy_api_key__"

    return None


def is_authenticated(request) -> bool:
    """Check if the request is authenticated via session cookie or legacy API key."""
    if not is_auth_configured():
        # No auth configured — allow all requests (development mode)
        return True
    return get_session_from_request(request) is not None
