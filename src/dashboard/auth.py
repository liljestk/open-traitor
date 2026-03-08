"""
Dashboard authentication — session-based auth with bcrypt password hashing + TOTP 2FA.

Provides:
  - Password hashing & verification (bcrypt)
  - Session token management (secure random, server-side store)
  - CSRF token generation & validation
  - Login rate limiting
  - httpOnly cookie helpers
  - TOTP 2FA (pyotp) with QR code generation
  - Backup codes for 2FA recovery

Environment variables:
  DASHBOARD_PASSWORD_HASH   bcrypt hash of the dashboard password
  DASHBOARD_SESSION_SECRET  secret used to derive CSRF tokens (auto-generated if missing)
  DASHBOARD_SESSION_TTL     session lifetime in seconds (default: 3600 = 1h)
  DASHBOARD_2FA_ENABLED     enable 2FA (default: false)
  DASHBOARD_2FA_SECRET      TOTP secret (base32, auto-generated if missing)
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

# Read config/.env via dotenv (override=True) so that the raw value is used
# instead of the Docker Compose-mangled version (Compose interpolates $ in
# bcrypt hashes like $2b$12$... breaking the hash).
from dotenv import dotenv_values as _dotenv_values

_config_env_path = os.path.join("config", ".env")
_dotenv_cfg = _dotenv_values(_config_env_path) if os.path.isfile(_config_env_path) else {}

# bcrypt hash of the dashboard password (set via setup wizard or CLI)
_PASSWORD_HASH: str = _dotenv_cfg.get("DASHBOARD_PASSWORD_HASH", "") or os.environ.get("DASHBOARD_PASSWORD_HASH", "")

# Secret for CSRF derivation — auto-generated per process if not set
_SESSION_SECRET_FROM_ENV = os.environ.get("DASHBOARD_SESSION_SECRET", "")
_SESSION_SECRET: str = _SESSION_SECRET_FROM_ENV or secrets.token_hex(32)
if not _SESSION_SECRET_FROM_ENV:
    logger.warning(
        "DASHBOARD_SESSION_SECRET not set — auto-generated per process. "
        "Set this env var for multi-worker / multi-container deployments."
    )

# Session lifetime (seconds); default 1 hour
SESSION_TTL: int = int(os.environ.get("DASHBOARD_SESSION_TTL", "3600"))

# Legacy API key (backward compat — if set, still accepted as bearer auth)
_LEGACY_API_KEY: str = os.environ.get("DASHBOARD_API_KEY", "")

# 2FA configuration
_2FA_ENABLED: bool = os.environ.get("DASHBOARD_2FA_ENABLED", "").lower() in ("1", "true", "yes")
_2FA_SECRET: str = os.environ.get("DASHBOARD_2FA_SECRET", "")
_2FA_ISSUER: str = os.environ.get("DASHBOARD_2FA_ISSUER", "Auto-Traitor")
_2FA_ACCOUNT: str = os.environ.get("DASHBOARD_2FA_ACCOUNT", "dashboard@auto-traitor")

# Backup codes (in-memory storage; persisted via env or config file in production)
_backup_codes: set[str] = set()
_backup_codes_lock = threading.Lock()


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
    if not hashed or not isinstance(hashed, str):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError):
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
# TOTP 2FA (Time-based One-Time Password)
# ═══════════════════════════════════════════════════════════════════════════

def generate_totp_secret() -> str:
    """Generate a new TOTP secret (base32 encoded)."""
    import pyotp
    return pyotp.random_base32()


def get_totp_secret() -> str:
    """Return the configured TOTP secret (may be empty)."""
    return _2FA_SECRET


def set_totp_secret(secret: str) -> None:
    """Update the in-memory TOTP secret."""
    global _2FA_SECRET
    _2FA_SECRET = secret


def is_2fa_enabled() -> bool:
    """Return True if 2FA is enabled and configured."""
    return _2FA_ENABLED and bool(_2FA_SECRET)


def enable_2fa() -> None:
    """Enable 2FA."""
    global _2FA_ENABLED
    _2FA_ENABLED = True


def disable_2fa() -> None:
    """Disable 2FA."""
    global _2FA_ENABLED
    _2FA_ENABLED = False


def verify_totp(code: str, secret: str | None = None) -> bool:
    """Verify a TOTP code against the configured secret.
    
    Args:
        code: 6-digit TOTP code
        secret: Override secret (for setup verification), uses global if None
        
    Returns:
        True if code is valid, False otherwise
    """
    import pyotp
    
    secret = secret or _2FA_SECRET
    if not secret:
        return False
    
    try:
        totp = pyotp.TOTP(secret)
        # Accept codes within ±1 time window (30s each) = 90s tolerance
        return totp.verify(code, valid_window=1)
    except Exception as e:
        logger.warning(f"TOTP verification error: {e}")
        return False


def generate_totp_qr_uri(secret: str | None = None, account: str | None = None) -> str:
    """Generate a TOTP provisioning URI for QR code generation.
    
    Args:
        secret: Base32 TOTP secret (uses global if None)
        account: Account identifier for authenticator app
        
    Returns:
        otpauth:// URI string
    """
    import pyotp
    
    secret = secret or _2FA_SECRET
    account = account or _2FA_ACCOUNT
    
    if not secret:
        raise ValueError("No TOTP secret configured")
    
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=account, issuer_name=_2FA_ISSUER)


def generate_totp_qr_code_data_uri(secret: str | None = None, account: str | None = None) -> str:
    """Generate a QR code as a data URI for embedding in HTML/JSON.
    
    Args:
        secret: Base32 TOTP secret (uses global if None)
        account: Account identifier
        
    Returns:
        data:image/png;base64,... URI string
    """
    import base64
    import io
    import qrcode
    
    uri = generate_totp_qr_uri(secret, account)
    
    # Generate QR code
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to data URI
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    
    return f"data:image/png;base64,{b64}"


# ═══════════════════════════════════════════════════════════════════════════
# Backup codes (for 2FA recovery)
# ═══════════════════════════════════════════════════════════════════════════

def generate_backup_codes(count: int = 8) -> list[str]:
    """Generate backup codes for 2FA recovery.
    
    Args:
        count: Number of codes to generate (default: 8)
        
    Returns:
        List of 10-character alphanumeric codes
    """
    codes = []
    for _ in range(count):
        # Generate 10-char alphanumeric code (format: XXXX-XXXX-XX)
        code = secrets.token_hex(5).upper()  # 10 hex chars
        codes.append(code)
    return codes


def set_backup_codes(codes: list[str]) -> None:
    """Store backup codes (hashed for security)."""
    global _backup_codes
    with _backup_codes_lock:
        _backup_codes = {hashlib.sha256(c.encode()).hexdigest() for c in codes}


def verify_backup_code(code: str) -> bool:
    """Verify and consume a backup code (one-time use).
    
    Args:
        code: Backup code to verify
        
    Returns:
        True if code is valid and unused, False otherwise
    """
    code_hash = hashlib.sha256(code.upper().encode()).hexdigest()
    with _backup_codes_lock:
        if code_hash in _backup_codes:
            _backup_codes.remove(code_hash)
            logger.info("✅ Backup code used successfully")
            return True
        return False


def get_backup_codes_count() -> int:
    """Return the number of remaining backup codes."""
    with _backup_codes_lock:
        return len(_backup_codes)


# ═══════════════════════════════════════════════════════════════════════════
# Pending 2FA sessions (intermediate state during 2-step login)
# ═══════════════════════════════════════════════════════════════════════════

_pending_2fa: dict[str, dict] = {}  # temp_token → {created, ip}
_pending_2fa_lock = threading.Lock()
_PENDING_2FA_TTL = 300  # 5 minutes to complete 2FA


def create_pending_2fa_session(client_ip: str = "") -> str:
    """Create a pending 2FA session (password verified, awaiting TOTP)."""
    token = secrets.token_urlsafe(48)
    now = time.monotonic()
    with _pending_2fa_lock:
        # Evict expired
        expired = [t for t, s in _pending_2fa.items() if now - s["created"] > _PENDING_2FA_TTL]
        for t in expired:
            del _pending_2fa[t]
        
        _pending_2fa[token] = {
            "created": now,
            "ip": client_ip,
        }
    return token


def validate_pending_2fa_session(token: str) -> bool:
    """Check if a pending 2FA session is valid."""
    if not token:
        return False
    now = time.monotonic()
    with _pending_2fa_lock:
        session = _pending_2fa.get(token)
        if not session:
            return False
        if now - session["created"] > _PENDING_2FA_TTL:
            del _pending_2fa[token]
            return False
        return True


def revoke_pending_2fa_session(token: str) -> None:
    """Revoke a pending 2FA session."""
    with _pending_2fa_lock:
        _pending_2fa.pop(token, None)


def upgrade_pending_2fa_to_full_session(pending_token: str, client_ip: str = "") -> str:
    """Convert a pending 2FA session to a full authenticated session.
    
    Args:
        pending_token: The pending 2FA token
        client_ip: Client IP address
        
    Returns:
        Full session token
    """
    revoke_pending_2fa_session(pending_token)
    return create_session(client_ip)


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
