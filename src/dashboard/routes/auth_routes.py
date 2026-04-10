"""Dashboard authentication routes — login, logout, session status, password setup."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from src.dashboard import auth
from src.utils.logger import get_logger

logger = get_logger("dashboard.auth_routes")

router = APIRouter(tags=["Auth"])


# ═══════════════════════════════════════════════════════════════════════════
# Request / response models
# ═══════════════════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    password: str


class SetPasswordRequest(BaseModel):
    password: str
    current_password: str | None = None  # Required if changing, not for initial setup


class TotpVerifyRequest(BaseModel):
    code: str
    pending_token: str | None = None  # For login flow
    use_backup_code: bool = False


class TotpSetupResponse(BaseModel):
    secret: str
    qr_code: str
    backup_codes: list[str]


class TotpStatusResponse(BaseModel):
    enabled: bool
    backup_codes_remaining: int


class AuthStatus(BaseModel):
    authenticated: bool
    auth_configured: bool
    csrf_token: str | None = None
    session_ttl: int = 0
    requires_2fa: bool = False
    twofa_enabled: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Cookie helpers
# ═══════════════════════════════════════════════════════════════════════════

def _set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie with security flags."""
    # Determine if we should set Secure flag (HTTPS)
    use_https = os.environ.get("DASHBOARD_HTTPS", "").lower() in ("1", "true", "yes")
    response.set_cookie(
        key="ot_session",
        value=token,
        httponly=True,
        secure=use_https,
        samesite="strict",
        max_age=auth.SESSION_TTL,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(key="ot_session", path="/")


# ═══════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/auth/status", summary="Check authentication status")
def auth_status(request: Request):
    """Return whether the user is authenticated and whether auth is configured."""
    authenticated = auth.is_authenticated(request)
    session_token = auth.get_session_from_request(request)
    csrf = None
    if session_token and session_token != "__legacy_api_key__":
        csrf = auth.generate_csrf_token(session_token)
    return AuthStatus(
        authenticated=authenticated,
        auth_configured=auth.is_auth_configured(),
        csrf_token=csrf,
        session_ttl=auth.SESSION_TTL if authenticated else 0,
        twofa_enabled=auth.is_2fa_enabled(),
    )


@router.post("/api/auth/login", summary="Log in with password")
def login(body: LoginRequest, request: Request, response: Response):
    """Authenticate with password and receive a session cookie."""
    client_ip = request.client.host if request.client else "unknown"

    # Rate limiting
    if not auth.check_login_rate(client_ip):
        logger.warning(f"🚫 Login rate limit exceeded for {client_ip}")
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in 5 minutes.",
        )

    pw_hash = auth.get_password_hash()
    if not pw_hash:
        # No password configured — check if legacy API key matches
        legacy_key = os.environ.get("DASHBOARD_API_KEY", "")
        if legacy_key:
            import hmac
            if hmac.compare_digest(body.password, legacy_key):
                token = auth.create_session(client_ip)
                _set_session_cookie(response, token)
                logger.info(f"✅ Login via legacy API key from {client_ip}")
                return {
                    "status": "ok",
                    "csrf_token": auth.generate_csrf_token(token),
                    "session_ttl": auth.SESSION_TTL,
                }
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not auth.verify_password(body.password, pw_hash):
        logger.warning(f"🚫 Failed login attempt from {client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Password is correct — check if 2FA is enabled
    if auth.is_2fa_enabled():
        # Create pending 2FA session (user must provide TOTP next)
        pending_token = auth.create_pending_2fa_session(client_ip)
        logger.info(f"⏳ Password verified from {client_ip}, awaiting 2FA")
        return {
            "status": "requires_2fa",
            "pending_token": pending_token,
            "message": "Password correct. Please provide your 2FA code.",
        }

    # No 2FA — create full session immediately
    token = auth.create_session(client_ip)
    _set_session_cookie(response, token)
    logger.info(f"✅ Login from {client_ip}")
    return {
        "status": "ok",
        "csrf_token": auth.generate_csrf_token(token),
        "session_ttl": auth.SESSION_TTL,
    }


@router.post("/api/auth/logout", summary="Log out (revoke session)")
def logout(request: Request, response: Response):
    """Revoke the current session and clear the cookie."""
    token = request.cookies.get("ot_session")
    if token:
        auth.revoke_session(token)
    _clear_session_cookie(response)
    return {"status": "ok"}


@router.post("/api/auth/set-password", summary="Set or change the dashboard password")
def set_password(body: SetPasswordRequest, request: Request, response: Response):
    """Set the initial password or change existing password.

    - If no password is configured yet: sets the initial password (no auth required).
    - If a password already exists: requires current_password for verification.
    """
    client_ip = request.client.host if request.client else "unknown"
    current_hash = auth.get_password_hash()

    if current_hash:
        # Changing password — must be authenticated AND provide current password
        if not auth.is_authenticated(request):
            raise HTTPException(status_code=401, detail="Authentication required")
        if not body.current_password:
            raise HTTPException(status_code=400, detail="current_password is required to change password")
        if not auth.verify_password(body.current_password, current_hash):
            raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Validate new password strength
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    new_hash = auth.hash_password(body.password)
    auth.set_password_hash(new_hash)

    # Persist to config/.env so it survives restarts
    _persist_password_hash(new_hash)

    # Revoke all existing sessions (force re-login)
    auth.revoke_all_sessions()
    _clear_session_cookie(response)

    logger.info(f"🔑 Dashboard password {'changed' if current_hash else 'set'} by {client_ip}")
    return {"status": "ok", "message": "Password set. Please log in."}


def _persist_password_hash(pw_hash: str) -> None:
    """Write the password hash to config/.env for persistence across restarts."""
    import tempfile
    env_path = os.path.join("config", ".env")
    lines: list[str] = []
    found = False

    # Sanitize newlines and escape $ as $$ so docker-compose env_file
    # parsing doesn't treat bcrypt cost markers ($2b$12$...) as variables.
    # Both python-dotenv (interpolate=True) and compose unescape $$ → $.
    safe_hash = pw_hash.replace("\n", "").replace("\r", "").replace("$", "$$")

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("DASHBOARD_PASSWORD_HASH="):
                    lines.append(f"DASHBOARD_PASSWORD_HASH={safe_hash}\n")
                    found = True
                else:
                    lines.append(line)

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n# Dashboard password (bcrypt hash)\n")
        lines.append(f"DASHBOARD_PASSWORD_HASH={safe_hash}\n")

    # Atomic write
    env_dir = os.path.dirname(os.path.abspath(env_path))
    os.makedirs(env_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, os.path.abspath(env_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ═══════════════════════════════════════════════════════════════════════════
# 2FA / TOTP Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/auth/2fa/status", summary="Check 2FA status")
def twofa_status(request: Request):
    """Return whether 2FA is enabled and backup codes remaining."""
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    return TotpStatusResponse(
        enabled=auth.is_2fa_enabled(),
        backup_codes_remaining=auth.get_backup_codes_count(),
    )


@router.post("/api/auth/2fa/setup", summary="Initialize 2FA setup")
def twofa_setup(request: Request):
    """Generate TOTP secret and QR code for 2FA setup (does not enable yet)."""
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    # Generate new secret
    secret = auth.generate_totp_secret()
    
    # Generate QR code
    try:
        qr_code = auth.generate_totp_qr_code_data_uri(secret)
    except Exception as e:
        logger.error(f"Failed to generate QR code: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate QR code")
    
    # Generate backup codes
    backup_codes = auth.generate_backup_codes(count=8)
    
    # Store secret temporarily (user must verify before enabling)
    auth.set_totp_secret(secret)
    
    logger.info(f"🔐 2FA setup initiated")
    return TotpSetupResponse(
        secret=secret,
        qr_code=qr_code,
        backup_codes=backup_codes,
    )


@router.post("/api/auth/2fa/enable", summary="Enable 2FA after verification")
def twofa_enable(body: TotpVerifyRequest, request: Request):
    """Enable 2FA after verifying TOTP code and store backup codes."""
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    secret = auth.get_totp_secret()
    if not secret:
        raise HTTPException(status_code=400, detail="No 2FA setup in progress. Call /api/auth/2fa/setup first")
    
    # Verify the TOTP code
    if not auth.verify_totp(body.code, secret):
        logger.warning("🚫 2FA enable failed: invalid TOTP code")
        raise HTTPException(status_code=401, detail="Invalid or expired 2FA code")
    
    # Enable 2FA
    auth.enable_2fa()
    
    # Persist 2FA secret and status
    _persist_2fa_config(secret, enabled=True)
    
    logger.info("✅ 2FA enabled successfully")
    return {"status": "ok", "message": "2FA enabled successfully"}


@router.post("/api/auth/2fa/disable", summary="Disable 2FA")
def twofa_disable(body: TotpVerifyRequest, request: Request, response: Response):
    """Disable 2FA (requires TOTP code or backup code for verification)."""
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    if not auth.is_2fa_enabled():
        raise HTTPException(status_code=400, detail="2FA is not enabled")
    
    # Verify code
    verified = False
    if body.use_backup_code:
        verified = auth.verify_backup_code(body.code)
    else:
        verified = auth.verify_totp(body.code)
    
    if not verified:
        logger.warning("🚫 2FA disable failed: invalid code")
        raise HTTPException(status_code=401, detail="Invalid 2FA code")
    
    # Disable 2FA
    auth.disable_2fa()
    auth.set_totp_secret("")
    auth.set_backup_codes([])
    
    # Persist
    _persist_2fa_config("", enabled=False)
    
    # Revoke all sessions (force re-login without 2FA)
    auth.revoke_all_sessions()
    _clear_session_cookie(response)
    
    logger.info("⚠️ 2FA disabled")
    return {"status": "ok", "message": "2FA disabled. Please log in again."}


@router.post("/api/auth/2fa/verify", summary="Verify 2FA code during login")
def twofa_verify(body: TotpVerifyRequest, request: Request, response: Response):
    """Complete 2FA login by verifying TOTP code against pending session."""
    if not body.pending_token:
        raise HTTPException(status_code=400, detail="pending_token is required")
    
    client_ip = request.client.host if request.client else "unknown"
    
    # Validate pending session
    if not auth.validate_pending_2fa_session(body.pending_token):
        logger.warning(f"🚫 Invalid or expired pending 2FA session from {client_ip}")
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")
    
    # Verify code
    verified = False
    if body.use_backup_code:
        verified = auth.verify_backup_code(body.code)
        if verified:
            logger.info(f"✅ 2FA login via backup code from {client_ip}")
    else:
        verified = auth.verify_totp(body.code)
        if verified:
            logger.info(f"✅ 2FA login via TOTP from {client_ip}")
    
    if not verified:
        logger.warning(f"🚫 2FA verification failed from {client_ip}")
        raise HTTPException(status_code=401, detail="Invalid or expired 2FA code")
    
    # Upgrade to full session
    token = auth.upgrade_pending_2fa_to_full_session(body.pending_token, client_ip)
    _set_session_cookie(response, token)
    
    return {
        "status": "ok",
        "csrf_token": auth.generate_csrf_token(token),
        "session_ttl": auth.SESSION_TTL,
    }


@router.post("/api/auth/2fa/regenerate-backup-codes", summary="Regenerate backup codes")
def twofa_regenerate_backup_codes(body: TotpVerifyRequest, request: Request):
    """Regenerate backup codes (requires TOTP verification)."""
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    if not auth.is_2fa_enabled():
        raise HTTPException(status_code=400, detail="2FA is not enabled")
    
    # Verify current TOTP code
    if not auth.verify_totp(body.code):
        logger.warning("🚫 Backup code regeneration failed: invalid TOTP")
        raise HTTPException(status_code=401, detail="Invalid 2FA code")
    
    # Generate new backup codes
    backup_codes = auth.generate_backup_codes(count=8)
    auth.set_backup_codes(backup_codes)
    
    # Persist (backup codes are hashed, so we only persist count info)
    _persist_backup_codes()
    
    logger.info("🔄 Backup codes regenerated")
    return {"backup_codes": backup_codes}


def _persist_2fa_config(secret: str, enabled: bool) -> None:
    """Write 2FA config to config/.env for persistence."""
    import tempfile
    env_path = os.path.join("config", ".env")
    lines: list[str] = []
    secret_found = False
    enabled_found = False
    
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("DASHBOARD_2FA_SECRET="):
                    safe_secret = secret.replace("\n", "").replace("\r", "")
                    lines.append(f"DASHBOARD_2FA_SECRET={safe_secret}\n")
                    secret_found = True
                elif line.strip().startswith("DASHBOARD_2FA_ENABLED="):
                    lines.append(f"DASHBOARD_2FA_ENABLED={'true' if enabled else 'false'}\n")
                    enabled_found = True
                else:
                    lines.append(line)
    
    if not secret_found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n# Dashboard 2FA (TOTP)\n")
        safe_secret = secret.replace("\n", "").replace("\r", "")
        lines.append(f"DASHBOARD_2FA_SECRET={safe_secret}\n")
    
    if not enabled_found:
        lines.append(f"DASHBOARD_2FA_ENABLED={'true' if enabled else 'false'}\n")
    
    # Atomic write
    env_dir = os.path.dirname(os.path.abspath(env_path))
    os.makedirs(env_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, os.path.abspath(env_path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _persist_backup_codes() -> None:
    """Mark backup codes as regenerated (we don't persist the codes themselves — they're shown once)."""
    # In a production system, you might persist hashed backup codes to a database
    # For now, backup codes are in-memory only (matches session model)
    logger.debug("Backup codes regenerated (stored in-memory)")

