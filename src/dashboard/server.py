"""
OpenTraitor Dashboard API Server (slim core).

FastAPI app running on port 8090 (configurable).  All REST endpoints live in
``src.dashboard.routes.*`` sub-modules — this file contains only:

  - Shared-state injection (``set_globals``)
  - The ASGI lifespan (Redis subscriber, Temporal connect, self-init)
  - CORS + API-key middleware
  - Router registration
  - Static-file / SPA serving

Start via:
    uvicorn src.dashboard.server:app --host 0.0.0.0 --port 8090

Or programmatically:
    from src.dashboard.server import create_app
    app = create_app(config, stats_db, redis_client, temporal_client)
"""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import src.dashboard.deps as deps
from src.dashboard import auth
from src.utils.logger import get_logger

logger = get_logger("dashboard")

# ---------------------------------------------------------------------------
# Route imports
# ---------------------------------------------------------------------------
from src.dashboard.routes.cycles import router as cycles_router
from src.dashboard.routes.trades import router as trades_router
from src.dashboard.routes.stats import router as stats_router
from src.dashboard.routes.market import router as market_router
from src.dashboard.routes.planning import router as planning_router
from src.dashboard.routes.websocket import router as ws_router, redis_subscriber
from src.dashboard.routes.settings import router as settings_router
from src.dashboard.routes.news import router as news_router
from src.dashboard.routes.watchlist import router as watchlist_router
from src.dashboard.routes.commands import router as commands_router
from src.dashboard.routes.llm_analytics import router as llm_analytics_router
from src.dashboard.routes.learning import router as learning_router
from src.dashboard.routes.auth_routes import router as auth_router
from src.dashboard.routes.financial_calendar import router as financial_calendar_router


# ---------------------------------------------------------------------------
# set_globals — inject shared services before uvicorn starts
# ---------------------------------------------------------------------------

def set_globals(
    *,
    stats_db,
    redis_client=None,
    temporal_client=None,
    config: dict | None = None,
    rules_instance=None,
    llm_client=None,
):
    """Inject shared services.  Called from main.py before uvicorn starts."""
    deps.stats_db = stats_db
    deps.redis_client = redis_client
    deps.temporal_client = temporal_client
    deps.config = config or {}
    deps.rules_instance = rules_instance
    deps.llm_client = llm_client

    # Spin up a read-only Coinbase client for live price lookups (market data only)
    try:
        from src.core.coinbase_client import CoinbaseClient
        key_file = os.environ.get("COINBASE_KEY_FILE", "")
        api_key = os.environ.get("COINBASE_API_KEY", "")
        api_secret = os.environ.get("COINBASE_API_SECRET", "")
        deps.exchange_client = CoinbaseClient(
            api_key=api_key or None,
            api_secret=api_secret or None,
            key_file=key_file or None,
            paper_mode=False,  # real API for balance/price reads; dashboard never places orders
        )
        logger.info("✅ Dashboard Coinbase price client ready")
    except Exception as e:
        logger.warning(f"⚠️ Dashboard Exchange client not available: {e}")

    # Also try to create an IBKR client for IBKR profile price lookups
    try:
        ib_host = os.environ.get("IBKR_HOST", "127.0.0.1")
        ib_port = int(os.environ.get("IBKR_PORT", "4001"))
        ib_client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))
        from src.core.ib_client import IBClient
        deps.ibkr_exchange_client = IBClient(
            paper_mode=False,
            ib_host=ib_host,
            ib_port=ib_port,
            ib_client_id=ib_client_id + 10,
        )
        logger.info("✅ Dashboard IBKR price client ready")
    except Exception as e:
        logger.info(f"ℹ️ Dashboard IBKR client not available: {e}")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    *,
    stats_db=None,
    redis_client=None,
    temporal_client=None,
    config: dict | None = None,
    rules_instance=None,
    llm_client=None,
) -> FastAPI:
    set_globals(
        stats_db=stats_db,
        redis_client=redis_client,
        temporal_client=temporal_client,
        config=config,
        rules_instance=rules_instance,
        llm_client=llm_client,
    )
    return app


# ---------------------------------------------------------------------------
# Lifespan (background Redis subscriber + self-init)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start background tasks on startup: Redis pub/sub listener + Temporal client.

    When the dashboard is started standalone via ``uvicorn src.dashboard.server:app``
    (e.g. inside the Docker container), ``set_globals()`` is never called by
    ``main.py``, so deps.stats_db / deps.redis_client are ``None``.
    We self-initialise them here so the API is functional.
    """

    # --- Self-initialise StatsDB when not injected by main.py ---------------
    if deps.stats_db is None:
        try:
            from src.utils.stats import StatsDB
            deps.stats_db = StatsDB()
            logger.info("📊 Dashboard self-initialised StatsDB (PostgreSQL)")
        except Exception as e:
            logger.error(f"❌ Could not initialise StatsDB: {e}")

    # --- Self-initialise Redis when not injected -----------------------------
    if deps.redis_client is None:
        try:
            import redis as _redis_mod
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            deps.redis_client = _redis_mod.from_url(redis_url, decode_responses=True)
            deps.redis_client.ping()
            logger.info(f"📡 Dashboard self-initialised Redis ({redis_url})")
        except Exception as e:
            logger.warning(f"⚠️ Redis not available: {e} — live feed disabled")
            deps.redis_client = None

    task = None
    if deps.redis_client:
        task = asyncio.create_task(redis_subscriber())
        logger.info("📡 Dashboard Redis subscriber started")

    # Connect Temporal here so we use uvicorn's own event loop
    if deps.temporal_client is None:
        try:
            import temporalio.client as _tc
            deps.temporal_client = await _tc.Client.connect(
                deps.temporal_host, namespace=deps.temporal_namespace,
            )
            logger.info(f"✅ Dashboard Temporal client connected ({deps.temporal_host})")
        except Exception as e:
            logger.warning(f"⚠️ Temporal not available: {e} — replay/rerun disabled")

    if deps.exchange_client is None:
        try:
            from src.core.coinbase_client import CoinbaseClient
            key_file = os.environ.get("COINBASE_KEY_FILE", "")
            api_key = os.environ.get("COINBASE_API_KEY", "")
            api_secret = os.environ.get("COINBASE_API_SECRET", "")
            deps.exchange_client = CoinbaseClient(
                api_key=api_key or None,
                api_secret=api_secret or None,
                key_file=key_file or None,
                paper_mode=False,  # real API for balance/price reads; dashboard never places orders
            )
            logger.info("✅ Dashboard exchange price client ready")
        except Exception as e:
            logger.warning(f"⚠️ Dashboard exchange client not available: {e}")

    # Try to initialise IBKR client for equity price lookups (IB Gateway on host)
    if deps.ibkr_exchange_client is None:
        try:
            ib_host = os.environ.get("IBKR_HOST", "127.0.0.1")
            ib_port = int(os.environ.get("IBKR_PORT", "4001"))
            ib_client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))
            from src.core.ib_client import IBClient
            deps.ibkr_exchange_client = IBClient(
                paper_mode=False,
                ib_host=ib_host,
                ib_port=ib_port,
                ib_client_id=ib_client_id + 10,
            )
            logger.info("✅ Dashboard IBKR price client ready")
        except Exception as e:
            logger.info(f"ℹ️ Dashboard IBKR client not available: {e}")

    yield
    if task:
        task.cancel()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

# Auth configuration check
_AUTH_CONFIGURED: bool = auth.is_auth_configured()

app = FastAPI(
    title="OpenTraitor Dashboard API",
    description="LLM traceability and playback for the autonomous trading agent",
    version="1.0.0",
    lifespan=lifespan,
    # Disable OpenAPI docs when auth is configured (production mode)
    docs_url=None if _AUTH_CONFIGURED else "/docs",
    redoc_url=None if _AUTH_CONFIGURED else "/redoc",
    openapi_url=None if _AUTH_CONFIGURED else "/openapi.json",
)

# --- CORS ---------------------------------------------------------------

_cors_origins_raw = os.environ.get("DASHBOARD_CORS_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    or ["http://localhost:5173", "http://localhost:8090"]
)

if not _AUTH_CONFIGURED:
    logger.warning(
        "⚠️  No authentication configured — the dashboard API is open to all network "
        "clients. Set DASHBOARD_PASSWORD_HASH or DASHBOARD_API_KEY to enable auth."
    )

# CORS hardening: never allow wildcard origin (prevents credential leakage)
if "*" in _cors_origins:
    logger.error(
        "⚠️ CORS wildcard '*' is not allowed — restricting to localhost origins. "
        "Set DASHBOARD_CORS_ORIGINS to specific origins instead."
    )
    _cors_origins = ["http://localhost:5173", "http://localhost:8090"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,  # Required for httpOnly session cookies
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "X-CSRF-Token", "Authorization"],
)

# --- API key middleware ---------------------------------------------------


# Mutating methods that require CSRF validation
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Endpoints that are always public (login flow, health)
_PUBLIC_ENDPOINTS = frozenset({
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/set-password",
    "/api/auth/2fa/verify",  # Needed during login flow
    "/api/system/status",
    "/api/setup",
    "/health",
})


@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    """Add comprehensive security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Cache-Control"] = "no-store"
    # Content Security Policy — restrict script/style sources
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    # HSTS — instruct browsers to always use HTTPS (1 year)
    if os.environ.get("DASHBOARD_HTTPS", "").lower() in ("1", "true", "yes"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Session-based auth middleware for /api/ endpoints.

    - Public endpoints (login, status, health) always pass through.
    - When auth is configured: all other /api/ endpoints require valid session.
    - When auth is NOT configured: read-only (GET/HEAD/OPTIONS) are open,
      mutating methods are blocked.
    - Mutating requests from session-authenticated users require CSRF token.
    """
    path = request.url.path

    if path.startswith("/api/"):
        # Always allow public endpoints
        if path in _PUBLIC_ENDPOINTS:
            return await call_next(request)

        if auth.is_auth_configured():
            # Check session-based auth or legacy API key
            if not auth.is_authenticated(request):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)

            # CSRF protection for mutating requests (skip for legacy API key auth)
            if request.method in _MUTATING_METHODS:
                session_token = auth.get_session_from_request(request)
                if session_token and session_token != "__legacy_api_key__":
                    csrf_token = request.headers.get("X-CSRF-Token", "")
                    if not csrf_token or not auth.validate_csrf_token(session_token, csrf_token):
                        return JSONResponse({"detail": "Invalid or missing CSRF token"}, status_code=403)
        else:
            # No auth configured — block mutating methods
            if request.method in _MUTATING_METHODS:
                return JSONResponse(
                    {"detail": "Authentication must be configured to use mutating endpoints. "
                     "Set a password via POST /api/auth/set-password."},
                    status_code=403,
                )

    return await call_next(request)


# --- Register routers ----------------------------------------------------

app.include_router(auth_router)
app.include_router(cycles_router)
app.include_router(trades_router)
app.include_router(stats_router)
app.include_router(market_router)
app.include_router(planning_router)
app.include_router(ws_router)
app.include_router(settings_router)
app.include_router(news_router)
app.include_router(watchlist_router)
app.include_router(commands_router)
app.include_router(llm_analytics_router)
app.include_router(learning_router)
app.include_router(financial_calendar_router)


# ---------------------------------------------------------------------------
# Static frontend (React/Vite build)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"

if _STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """Catch-all: serve static files first, then fall back to index.html for SPA routing."""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        # Serve actual static files (logo.png, favicon.ico, etc.)
        static_file = _STATIC_DIR / full_path
        if full_path and static_file.is_file() and _STATIC_DIR in static_file.resolve().parents:
            return FileResponse(str(static_file))
        index = _STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend not built")


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    import yaml
    from src.utils.stats import StatsDB

    config_path = os.path.join("config", "settings.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    db = StatsDB()

    redis_url = os.environ.get("REDIS_URL")
    redis_client = None
    if redis_url:
        import redis
        redis_client = redis.Redis.from_url(redis_url)

    app = create_app(
        stats_db=db,
        redis_client=redis_client,
        temporal_client=None,
        config=config,
    )

    port = int(config.get("dashboard", {}).get("port", 8090))
    print(f"🚀 Starting Dashboard Server on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
