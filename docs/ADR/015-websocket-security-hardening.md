# ADR-015: WebSocket Security Hardening

**Status:** Accepted

## Context

The dashboard WebSocket endpoint (`/ws/live`) streams real-time LLM span events to connected browser clients. While HTTP endpoints had session-based auth, CORS, CSRF, and rate limiting, the WebSocket path was missing several hardening layers:

1. **No origin validation** — FastAPI's `CORSMiddleware` only applies to HTTP, not WebSocket upgrade requests. Any webpage could open a cross-origin WebSocket to `/ws/live` (Cross-Site WebSocket Hijacking / CSWSH).
2. **No connection rate limiting** — unlimited concurrent WebSocket connections per IP enabled resource exhaustion DoS.
3. **No global connection cap** — `ws_connections` was an unbounded list (HTTP sessions were capped at 100, WebSocket was not).
4. **No auth failure rate limiting** — an attacker could brute-force API keys via rapid WebSocket reconnects with no throttle.
5. **Overly permissive CSP** — `connect-src 'self' ws: wss:` allowed browser JS to connect to *any* WebSocket server, not just the dashboard's own.

The Coinbase outbound WebSocket feed (`wss://advanced-trade-ws.coinbase.com`) was already properly secured with HMAC-SHA256 signatures and required no changes.

## Decision

### 1. Origin Validation on WebSocket Upgrade

The `ws_live()` handler now validates the `Origin` header against the same allowed-origins list used by CORS middleware. Connections from disallowed origins are closed with code 1008 ("Policy Violation") before `accept()`.

- Non-browser clients (no `Origin` header) are allowed — only browsers send `Origin` on WebSocket upgrades.
- The allowed-origins list is resolved once in `deps.py` and shared by both CORS middleware and the WS handler.

### 2. Per-IP Connection Limiting

A per-IP counter (`MAX_WS_PER_IP = 10`) prevents any single IP from monopolising server resources. Excess connections are closed with code 1013 ("Try Again Later").

### 3. Global Connection Cap

A global cap (`MAX_WS_CONNECTIONS = 50`) prevents total resource exhaustion regardless of IP distribution. Excess connections are closed with code 1013.

### 4. WS Auth Failure Rate Limiting

Failed WebSocket auth attempts are tracked per-IP (`WS_AUTH_RATE_MAX = 10` per `WS_AUTH_RATE_WINDOW = 60s`). Once the limit is hit, further attempts from that IP are rejected immediately without credential evaluation — preventing brute-force attacks on API keys.

### 5. CSP `connect-src` Tightened

Changed from `connect-src 'self' ws: wss:` to `connect-src 'self'`. Per CSP Level 3 specification, `'self'` matches same-origin WebSocket connections (`ws://` / `wss://` to the same host), so the dashboard's own live feed continues to work while preventing scripts from connecting to arbitrary external WebSocket servers.

## Consequences

- **CSWSH eliminated** — malicious pages can no longer hijack authenticated WebSocket sessions.
- **DoS resistance** — connection floods from a single IP or globally are capped.
- **Brute-force resistance** — API key guessing via rapid WS reconnects is throttled.
- **Tighter CSP** — XSS payloads can no longer exfiltrate data via arbitrary WebSocket servers.
- **Non-browser tools unaffected** — `curl`, Python clients, etc. don't send `Origin` and bypass the check.
- **Defaults are conservative** — 50 global / 10 per-IP is generous for a single-user trading dashboard. Configurable via `deps.py` constants if needed.

## Files Changed

| File | Change |
|------|--------|
| `src/dashboard/routes/websocket.py` | Origin validation, per-IP tracking, global cap, auth rate limiting |
| `src/dashboard/deps.py` | `allowed_origins`, `MAX_WS_CONNECTIONS`, `MAX_WS_PER_IP`, `WS_AUTH_RATE_*` |
| `src/dashboard/server.py` | Use `deps.allowed_origins` for CORS; tighten CSP `connect-src` |
| `tests/test_security_advanced.py` | `TestWebSocketOriginValidation`, `TestWebSocketConnectionLimits`, `TestWebSocketAuthRateLimiting`, `TestCSPConnectSrc` |
