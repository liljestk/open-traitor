# ADR-009: Dashboard Request Signing and Auth

**Status:** Accepted

## Context

The dashboard exposes endpoints that can modify trading parameters, approve trades, and adjust risk settings. Without proper authentication, anyone with network access could alter the system's behavior. The auth model must protect against CSRF, session hijacking, and replay attacks.

## Decision

Implement a **three-layer authentication system**: password + session cookies, CSRF tokens, and optional TOTP 2FA.

### Layer 1: Password + Session (httpOnly cookies)

- Password hash stored in `config/.env` (bcrypt).
- Login verifies password → creates a session token (48-byte random).
- Session tokens stored server-side in a dictionary with TTL (default 3600s).
- Maximum 100 concurrent sessions.
- httpOnly cookies prevent JavaScript access (XSS mitigation).

### Layer 2: CSRF Tokens

Generated per session using HMAC:

```python
def generate_csrf_token(session_token: str) -> str:
    return hmac.new(
        _SESSION_SECRET.encode("utf-8"),
        session_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
```

- Validated on all mutating requests (POST/PUT/DELETE/PATCH).
- Frontend reads CSRF token from `/api/auth/status` response.
- Attached as `X-CSRF-Token` header on every mutating request.
- Timing-safe comparison via `hmac.compare_digest()`.

### Layer 3: Optional TOTP 2FA

- TOTP-based (pyotp library).
- Secret stored in `DASHBOARD_2FA_SECRET` environment variable.
- Backup codes available for recovery.

### Frontend Integration

```typescript
// After login, store CSRF token
setCsrfToken(status.csrf_token);

// On mutating requests, attach header
headers.set('X-CSRF-Token', _csrfToken);

// Auto-refresh on 403 (expired token)
if (res.status === 403 && isMutating) {
    const status = await fetch('/api/auth/status', { credentials: 'include' });
    setCsrfToken(status.csrf_token);
    // retry with refreshed token
}
```

### Legacy API Key (backward compatibility)

If `DASHBOARD_API_KEY` is set, it's accepted as `Authorization: Bearer <key>` with timing-safe comparison. This supports automation and scripting use cases.

### Security Properties

- **No hardcoded secrets**: All credentials come from environment variables or `config/.env`.
- **Session-bound CSRF**: Tokens are derived from session tokens, not global secrets.
- **Timing-safe comparison**: All secret comparisons use `hmac.compare_digest()` to prevent timing attacks.
- **Automatic expiry**: Sessions expire after TTL; CSRF tokens are re-derived on each auth check.

## Consequences

**Benefits:**
- CSRF tokens prevent cross-site request forgery on all state-changing endpoints.
- httpOnly cookies prevent XSS from stealing session tokens.
- Optional 2FA adds defense-in-depth for high-security deployments.
- Legacy API key supports headless/automation workflows.

**Risks:**
- Session store is in-memory; server restart invalidates all sessions (acceptable for single-instance deployment).
- Maximum 100 sessions is a hard limit; exceeded sessions are rejected, not evicted.

**Follow-on:**
- All dashboard routes that modify trading state must validate CSRF tokens (enforced by middleware).
- Frontend must include `profile` in all `useQuery` keys per domain separation rules (ADR-003).
