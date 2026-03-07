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


class AuthStatus(BaseModel):
    authenticated: bool
    auth_configured: bool
    csrf_token: str | None = None
    session_ttl: int = 0


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

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("DASHBOARD_PASSWORD_HASH="):
                    # Sanitize: strip newlines from hash to prevent .env injection
                    safe_hash = pw_hash.replace("\n", "").replace("\r", "")
                    lines.append(f"DASHBOARD_PASSWORD_HASH={safe_hash}\n")
                    found = True
                else:
                    lines.append(line)

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n# Dashboard password (bcrypt hash)\n")
        safe_hash = pw_hash.replace("\n", "").replace("\r", "")
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
