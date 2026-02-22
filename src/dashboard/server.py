"""
Auto-Traitor Dashboard API Server.

FastAPI app running on port 8090 (configurable).  Provides:
  REST
  ───
  GET  /api/cycles               - Paginated cycle list (Cycle Explorer)
  GET  /api/cycles/{cycle_id}    - Full span chain for one cycle (Playback)
  GET  /api/stats/summary        - Portfolio + trade stats overview
  GET  /api/strategic            - Recent strategic context (planning plans)
  GET  /api/temporal/runs        - Temporal planning workflow run list
  GET  /api/temporal/replay/{workflow_id}/{run_id}  - Full Temporal history
  POST /api/temporal/rerun/{workflow_id}/{run_id}   - Trigger a fresh run

  WebSocket
  ─────────
  WS   /ws/live                  - Real-time LLM span events from Redis pub/sub

Start via:
    uvicorn src.dashboard.server:app --host 0.0.0.0 --port 8090

Or programmatically:
    from src.dashboard.server import create_app
    app = create_app(config, stats_db, redis_client, temporal_client)
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator, Optional

from pathlib import Path
import io

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from src.utils.logger import get_logger
from src.utils.pair_format import parse_pair

logger = get_logger("dashboard")

# ---------------------------------------------------------------------------
# Shared state (injected at startup via create_app / set_globals)
# ---------------------------------------------------------------------------

_stats_db = None          # StatsDB instance
_redis_client = None      # redis.Redis instance (optional)
_temporal_client = None   # temporalio.client.Client instance (optional)
_temporal_host: str = os.environ.get("TEMPORAL_HOST", "localhost:7233")
_temporal_namespace: str = os.environ.get("TEMPORAL_NAMESPACE", "default")
_config: dict = {}
_exchange_client = None   # ExchangeClient instance (optional, for price lookups)

_ws_connections: list[tuple[WebSocket, str | None]] = []  # (ws, quote_currency_filter)


_rules_instance = None    # AbsoluteRules instance (optional, for runtime push)
_llm_client = None        # LLMClient instance (optional, for provider status)

# ---------------------------------------------------------------------------
# Profile resolution — maps frontend profile IDs to backend identifiers
# ---------------------------------------------------------------------------

# Frontend sends profile=crypto, but the agent runs as profile=coinbase
PROFILE_ALIASES: dict[str, str] = {
    "crypto": "coinbase",
}

# Profiles whose data lives in the default (injected) stats.db
# (historical crypto data was written before per-profile DBs were introduced)
PROFILE_USE_DEFAULT_DB: set[str] = {"coinbase", "settings"}

# Profile → config file path
PROFILE_CONFIG_FILES: dict[str, str] = {
    "coinbase": "config/coinbase.yaml",
    "nordnet": "config/nordnet.yaml",
    "ibkr": "config/ibkr.yaml",
}

# Profile → quote currency (authoritative fallback when config is unavailable)
PROFILE_CURRENCIES: dict[str, str] = {
    "": "EUR",
    "coinbase": "EUR",
    "nordnet": "SEK",
    "ibkr": "USD",
}


def _resolve_profile(profile: str) -> str:
    """Resolve frontend profile aliases to canonical backend profile names."""
    if not profile:
        return ""
    p = profile.lower().strip()
    return PROFILE_ALIASES.get(p, p)


def _quote_currency_for(profile: str) -> str | None:
    """Return the quote currency for a profile, or None for 'Default / All'."""
    resolved = _resolve_profile(profile)
    if not resolved:
        return None  # Default profile → show all currencies
    return PROFILE_CURRENCIES.get(resolved)


def _get_config_for_profile(profile: str = "") -> dict:
    """Load the config for a specific profile, falling back to the default config."""
    resolved = _resolve_profile(profile)
    config_file = PROFILE_CONFIG_FILES.get(resolved)
    if config_file:
        try:
            from src.utils.settings_manager import load_settings
            return load_settings(config_file)
        except Exception:
            pass
    return _get_config()

# Pending confirmation tokens for sensitive operations (C4/H5 fix)
# Maps token → {action, payload, expires}
_pending_confirmations: dict[str, dict] = {}
_pending_confirmations_lock = threading.Lock()  # M24 fix: thread-safe access


def _store_confirmation(token: str, data: dict) -> None:
    """Thread-safe store for confirmation tokens."""
    with _pending_confirmations_lock:
        _pending_confirmations[token] = data


def _pop_confirmation(token: str) -> dict | None:
    """Thread-safe pop for confirmation tokens."""
    with _pending_confirmations_lock:
        return _pending_confirmations.pop(token, None)


def _expire_confirmations() -> None:
    """Remove expired confirmation tokens (thread-safe)."""
    now = time.monotonic()
    with _pending_confirmations_lock:
        expired = [t for t, v in _pending_confirmations.items() if v["expires"] < now]
        for t in expired:
            del _pending_confirmations[t]


def _get_config() -> dict:
    """Return the current config, reloading from disk to pick up runtime changes."""
    try:
        from src.utils.settings_manager import load_settings
        return load_settings()
    except Exception:
        return _config


def set_globals(*, stats_db, redis_client=None, temporal_client=None, config: dict | None = None, rules_instance=None, llm_client=None):
    """Inject shared services.  Called from main.py before uvicorn starts."""
    global _stats_db, _redis_client, _temporal_client, _config, _exchange_client, _rules_instance, _llm_client
    _stats_db = stats_db
    _redis_client = redis_client
    _temporal_client = temporal_client
    _config = config or {}
    _rules_instance = rules_instance
    _llm_client = llm_client
    # Spin up a read-only Coinbase client for live price lookups (market data only)
    try:
        from src.core.coinbase_client import CoinbaseClient
        key_file = os.environ.get("COINBASE_KEY_FILE", "")
        api_key = os.environ.get("COINBASE_API_KEY", "")
        api_secret = os.environ.get("COINBASE_API_SECRET", "")
        _exchange_client = CoinbaseClient(
            api_key=api_key or None,
            api_secret=api_secret or None,
            key_file=key_file or None,
            paper_mode=True,  # read-only; no real orders from the dashboard
        )
        logger.info("✅ Dashboard Coinbase price client ready")
    except Exception as e:
        logger.warning(f"⚠️ Dashboard Exchange client not available: {e}")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(*, stats_db=None, redis_client=None, temporal_client=None, config: dict | None = None, rules_instance=None, llm_client=None) -> FastAPI:
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
# Lifespan  (background Redis subscriber)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start background tasks on startup: Redis pub/sub listener + Temporal client.

    When the dashboard is started standalone via ``uvicorn src.dashboard.server:app``
    (e.g. inside the Docker container), ``set_globals()`` is never called by
    ``main.py``, so _stats_db / _redis_client are ``None``.  We self-initialise
    them here so the API is functional.
    """
    global _stats_db, _redis_client, _temporal_client

    # --- Self-initialise StatsDB when not injected by main.py ---------------
    if _stats_db is None:
        try:
            from src.utils.stats import StatsDB
            _stats_db = StatsDB()
            logger.info("📊 Dashboard self-initialised StatsDB")
        except Exception as e:
            logger.error(f"❌ Could not initialise StatsDB: {e}")

    # --- Self-initialise Redis when not injected -----------------------------
    if _redis_client is None:
        try:
            import redis as _redis_mod
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            _redis_client = _redis_mod.from_url(redis_url, decode_responses=True)
            _redis_client.ping()
            logger.info(f"📡 Dashboard self-initialised Redis ({redis_url})")
        except Exception as e:
            logger.warning(f"⚠️ Redis not available: {e} — live feed disabled")
            _redis_client = None

    task = None
    if _redis_client:
        task = asyncio.create_task(_redis_subscriber())
        logger.info("📡 Dashboard Redis subscriber started")

    # Connect Temporal here so we use uvicorn's own event loop
    if _temporal_client is None:
        try:
            import temporalio.client as _tc
            _temporal_client = await _tc.Client.connect(
                _temporal_host, namespace=_temporal_namespace
            )
            logger.info(f"✅ Dashboard Temporal client connected ({_temporal_host})")
        except Exception as e:
            logger.warning(f"⚠️ Temporal not available: {e} — replay/rerun disabled")

    if _exchange_client is None:
        try:
            from src.core.coinbase_client import CoinbaseClient
            key_file = os.environ.get("COINBASE_KEY_FILE", "")
            api_key = os.environ.get("COINBASE_API_KEY", "")
            api_secret = os.environ.get("COINBASE_API_SECRET", "")
            globals()["_exchange_client"] = CoinbaseClient(
                api_key=api_key or None,
                api_secret=api_secret or None,
                key_file=key_file or None,
                paper_mode=True,
            )
            logger.info("✅ Dashboard exchange price client ready")
        except Exception as e:
            logger.warning(f"⚠️ Dashboard exchange client not available: {e}")

    yield
    if task:
        task.cancel()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Auto-Traitor Dashboard API",
    description="LLM traceability and playback for the autonomous trading agent",
    version="1.0.0",
    lifespan=lifespan,
)

_cors_origins_raw = os.environ.get("DASHBOARD_CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()] or ["http://localhost:5173", "http://localhost:8090"]


# ---------------------------------------------------------------------------
# API Key Authentication (active only when DASHBOARD_API_KEY env var is set)
# ---------------------------------------------------------------------------

_DASHBOARD_API_KEY: str = os.environ.get("DASHBOARD_API_KEY", "")
_DASHBOARD_COMMAND_SIGNING_KEY: str = (
    os.environ.get("DASHBOARD_COMMAND_SIGNING_KEY", "") or _DASHBOARD_API_KEY
)
if not _DASHBOARD_API_KEY:
    logger.warning(
        "⚠️  DASHBOARD_API_KEY not set — the dashboard API is open to all network "
        "clients. Set this env var to require X-API-Key header authentication."
    )
if not _DASHBOARD_COMMAND_SIGNING_KEY:
    logger.warning(
        "⚠️  DASHBOARD_COMMAND_SIGNING_KEY not set — trade command endpoint will "
        "reject command enqueueing for safety."
    )


# Apply CORS after API key is resolved
if "*" in _cors_origins:
    if not _DASHBOARD_API_KEY:
        # H4 fix: CORS wildcard + no API key = full cross-origin exposure
        logger.error(
            "⚠️ CORS wildcard with no DASHBOARD_API_KEY is dangerous — "
            "restricting to localhost origins"
        )
        _cors_origins = ["http://localhost:5173", "http://localhost:8090"]
    else:
        logger.warning("⚠️ CORS allow_origins contains wildcard — restrict via DASHBOARD_CORS_ORIGINS env var")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,       # must be False when allow_origins contains "*"
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """Require explicit API key auth for dashboard endpoints when configured.

    When DASHBOARD_API_KEY is set, every /api/ request must carry a matching
    X-API-Key header.  When unset, all /api/ requests are allowed — network-
    level access control is handled by the Docker port binding
    (127.0.0.1:8090) which ensures only local traffic reaches the container.
    """
    if _DASHBOARD_API_KEY and request.url.path.startswith("/api/"):
        api_key = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(api_key, _DASHBOARD_API_KEY):
            return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

# Per-profile StatsDB cache — avoids re-opening the same DB file on every request
# Uses OrderedDict for LRU eviction when the cache reaches max size (H4 fix)
_MAX_PROFILE_DBS = 16
_profile_db_cache: collections.OrderedDict[str, Any] = collections.OrderedDict()
_profile_db_lock = threading.Lock()


def _require_db(profile: str = ""):
    """Return the StatsDB for *profile* (empty string → default / injected).

    Resolution order:
    1. Resolve aliases (e.g. crypto → coinbase).
    2. If the resolved profile is in PROFILE_USE_DEFAULT_DB, return the
       injected default DB (stats.db — contains historical crypto data).
    3. Otherwise look up / open stats_{resolved}.db.
    4. If the profile-specific DB doesn't exist or is tiny (<8 KB, empty
       shell), fall back to the default DB gracefully.
    """
    resolved = _resolve_profile(profile)

    if not resolved or resolved in PROFILE_USE_DEFAULT_DB:
        if _stats_db is None:
            raise HTTPException(status_code=503, detail="Stats DB not initialised")
        return _stats_db

    # Sanitise: alphanumeric + underscore only
    safe = "".join(c for c in resolved if c.isalnum() or c == "_")
    if not safe:
        raise HTTPException(status_code=400, detail=f"Invalid profile: {profile!r}")

    with _profile_db_lock:
        if safe in _profile_db_cache:
            _profile_db_cache.move_to_end(safe)  # LRU: mark as recently used
            return _profile_db_cache[safe]
        try:
            from src.utils.stats import StatsDB
            db_path = os.path.join("data", f"stats_{safe}.db")
            if not os.path.exists(db_path) or os.path.getsize(db_path) < 8192:
                # Profile DB doesn't exist or is an empty shell.
                # Create a fresh empty DB so equity profiles return empty data
                # rather than silently falling back to the crypto/default DB.
                logger.info(
                    f"📊 Profile DB missing or empty ({db_path}), "
                    f"creating empty StatsDB for '{safe}'"
                )
                db = StatsDB(db_path=db_path)
                _profile_db_cache[safe] = db
                return db
            # Evict oldest entry if cache is at capacity (H4 fix)
            if len(_profile_db_cache) >= _MAX_PROFILE_DBS:
                evicted_key, evicted_db = _profile_db_cache.popitem(last=False)
                try:
                    evicted_db.close()
                except Exception:
                    pass
                logger.debug(f"Evicted profile DB cache entry: {evicted_key}")
            db = StatsDB(db_path=db_path)
            _profile_db_cache[safe] = db
            logger.info(f"📊 Loaded StatsDB for profile '{safe}': {db_path}")
            return db
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Cannot load StatsDB for profile '{safe}': {e}",
            )


def _sanitize_floats(obj):
    """Recursively replace inf/nan floats with None so JSON serialisation succeeds."""
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return None
    return obj


def _sign_dashboard_command(action: str, pair: str, ts: str, source: str, nonce: str) -> str:
    """Create a deterministic HMAC signature for dashboard trade commands."""
    payload = f"{action}|{pair}|{ts}|{source}|{nonce}"
    return hmac.new(
        _DASHBOARD_COMMAND_SIGNING_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _open_conn(db) -> sqlite3.Connection:
    """Open a fresh SQLite connection for the given StatsDB instance.

    Avoids relying on the thread-local connection inside StatsDB, which is
    not safe to share across FastAPI's async threadpool workers.
    """
    conn = sqlite3.connect(db._db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fresh_conn(profile: str = "") -> sqlite3.Connection:
    """Open a fresh SQLite connection for the given profile."""
    return _open_conn(_require_db(profile))


def _get_profile_db(
    profile: str = Query("", description="Exchange profile (e.g. 'coinbase', 'nordnet', 'ibkr')"),
):
    """FastAPI dependency — resolves the StatsDB for the requested profile."""
    return _require_db(profile)


# ---------------------------------------------------------------------------
# REST — Cycles
# ---------------------------------------------------------------------------

@app.get("/api/cycles", summary="List trading cycles (Cycle Explorer)")
def list_cycles(
    pair: Optional[str] = Query(None, description="Filter by pair e.g. BTC-USD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    profile: str = Query(""),
    db=Depends(_get_profile_db),
):
    """
    Returns a paginated list of trading cycles with outcome summary.
    Each item represents one unique `cycle_id` across all agent spans.
    """
    qc = _quote_currency_for(profile)
    cycles = db.get_cycles(pair=pair, limit=limit, offset=offset, quote_currency=qc)
    for c in cycles:
        c["langfuse_url"] = _langfuse_url(c.get("langfuse_trace_id"))
        # Compute wall-clock duration from first→last agent span timestamps
        try:
            if c.get("started_at") and c.get("finished_at"):
                from datetime import datetime
                _s = datetime.fromisoformat(c["started_at"])
                _f = datetime.fromisoformat(c["finished_at"])
                c["cycle_duration_ms"] = round((_f - _s).total_seconds() * 1000, 1)
            else:
                c["cycle_duration_ms"] = None
        except Exception:
            c["cycle_duration_ms"] = None
    return {"cycles": cycles, "limit": limit, "offset": offset, "count": len(cycles)}


@app.get("/api/cycles/{cycle_id}", summary="Full span chain for one cycle (Playback)")
def get_cycle(cycle_id: str, db=Depends(_get_profile_db)):
    """
    Returns the complete trace: all agent spans with token counts, latency,
    LLM prompt/output, plus the resulting trade (if any).
    Powers the animated Waterfall timeline on the Playback page.
    """
    cycle = db.get_cycle_full(cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail=f"Cycle {cycle_id!r} not found")
    cycle["langfuse_url"] = _langfuse_url(cycle.get("langfuse_trace_id"))
    return cycle


# ---------------------------------------------------------------------------
# REST — Trades & Events
# ---------------------------------------------------------------------------

@app.get("/api/trades", summary="List raw trades log")
def list_trades(
    pair: Optional[str] = Query(None, description="Filter by pair e.g. BTC-USD"),
    hours: int = Query(24 * 7, ge=1, description="Hours of history to fetch"),
    limit: int = Query(500, ge=1, le=5000),
    profile: str = Query(""),
    db=Depends(_get_profile_db),
):
    """Returns a list of raw trades from the database, newest first."""
    qc = _quote_currency_for(profile)
    trades = db.get_trades(hours=hours, pair=pair, limit=limit, quote_currency=qc)
    return {"trades": trades, "count": len(trades)}

@app.get("/api/trades/export", summary="Export trades to CSV")
def export_trades(hours: int = Query(24 * 30, ge=1), profile: str = Query(""), db=Depends(_get_profile_db)):
    """Exports raw trades to a downloadable CSV file."""
    qc = _quote_currency_for(profile)
    trades = db.get_trades(hours=hours, limit=100000, quote_currency=qc)
    
    if not trades:
        return Response(
            content="id,ts,pair,action,quantity,price,quote_amount,pnl,confidence,signal_type\n",
            media_type="text/csv"
        )
    
    import pandas as pd
    df = pd.DataFrame(trades)
    columns = [
        "id", "ts", "pair", "action", "quantity", "price", "quote_amount", 
        "fee_quote", "pnl", "confidence", "signal_type", "stop_loss", 
        "take_profit", "reasoning", "is_rotation", "approved_by"
    ]
    existing_cols = [c for c in columns if c in df.columns]
    df = df[existing_cols]
    
    csv_data = df.to_csv(index=False)
    headers = {
        "Content-Disposition": "attachment; filename=auto_traitor_trades.csv"
    }
    return Response(content=csv_data, media_type="text/csv", headers=headers)

@app.get("/api/events", summary="List system events")
def list_events(
    event_type: Optional[str] = Query(None),
    hours: int = Query(24 * 7, ge=1),
    limit: int = Query(500, ge=1, le=5000),
    profile: str = Query(""),
    db=Depends(_get_profile_db),
):
    """Returns a list of system events/logs from the database."""
    qc = _quote_currency_for(profile)
    events = db.get_events(hours=hours, event_type=event_type, limit=limit, quote_currency=qc)
    # Parse event data json if possible
    for e in events:
        if isinstance(e.get("data"), str):
            try:
                e["data"] = json.loads(e["data"])
            except Exception:
                pass
    return _sanitize_floats({"events": events, "count": len(events)})



# ---------------------------------------------------------------------------
# REST — Stats summary
# ---------------------------------------------------------------------------

@app.get("/api/stats/summary", summary="Portfolio and trade stats overview")
def get_stats_summary(
    profile: str = Query("", description="Exchange profile"),
    db=Depends(_get_profile_db),
):
    """High-level stats: win-rate, PnL, active pairs, recent activity."""
    conn = _open_conn(db)
    qc = _quote_currency_for(profile)
    try:
        # Overall trade stats
        trade_sql = """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl), 2) as total_pnl,
                ROUND(AVG(pnl), 2) as avg_pnl,
                ROUND(MAX(pnl), 2) as best_trade,
                ROUND(MIN(pnl), 2) as worst_trade
               FROM trades
               WHERE pnl IS NOT NULL"""
        if qc:
            trade_row = conn.execute(trade_sql + " AND UPPER(pair) LIKE ?", (f"%-{qc.upper()}",)).fetchone()
        else:
            trade_row = conn.execute(trade_sql).fetchone()

        # Last 24h
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent_sql = """SELECT
                COUNT(*) as trades_24h,
                ROUND(SUM(pnl), 2) as pnl_24h
               FROM trades
               WHERE ts >= ? AND pnl IS NOT NULL"""
        if qc:
            recent_row = conn.execute(recent_sql + " AND UPPER(pair) LIKE ?", (cutoff_24h, f"%-{qc.upper()}")).fetchone()
        else:
            recent_row = conn.execute(recent_sql, (cutoff_24h,)).fetchone()

        # Active pairs
        pairs_sql = "SELECT COUNT(DISTINCT pair) as active_pairs FROM agent_reasoning WHERE ts >= ?"
        if qc:
            pairs_row = conn.execute(pairs_sql + " AND UPPER(pair) LIKE ?", (cutoff_24h, f"%-{qc.upper()}")).fetchone()
        else:
            pairs_row = conn.execute(pairs_sql, (cutoff_24h,)).fetchone()

        # Cycle count last 24h
        cycle_sql = "SELECT COUNT(DISTINCT cycle_id) as cycles_24h FROM agent_reasoning WHERE ts >= ?"
        if qc:
            cycle_row = conn.execute(cycle_sql + " AND UPPER(pair) LIKE ?", (cutoff_24h, f"%-{qc.upper()}")).fetchone()
        else:
            cycle_row = conn.execute(cycle_sql, (cutoff_24h,)).fetchone()

        # Latest portfolio snapshot (filtered by exchange when profile is set)
        snapshot_sql = """SELECT portfolio_value, total_pnl, ts
               FROM portfolio_snapshots"""
        snapshot_params: list = []
        if qc:
            _qc_exchange_map = {"EUR": "coinbase", "SEK": "nordnet", "USD": "ibkr"}
            exchange = _qc_exchange_map.get(qc.upper())
            if exchange:
                snapshot_sql += " WHERE exchange = ?"
                snapshot_params.append(exchange)
        snapshot_sql += " ORDER BY ts DESC LIMIT 1"
        snapshot = conn.execute(snapshot_sql, snapshot_params).fetchone()

        stats = dict(trade_row) if trade_row else {}
        stats.update(dict(recent_row) if recent_row else {})
        stats.update(dict(pairs_row) if pairs_row else {})
        stats.update(dict(cycle_row) if cycle_row else {})
        if stats.get("total_trades", 0) and stats.get("wins") is not None:
            t = stats["total_trades"] or 1
            stats["win_rate"] = round(stats["wins"] / t * 100, 1)
        else:
            stats["win_rate"] = None
        if snapshot:
            stats["portfolio"] = dict(snapshot)
        # Use profile-specific config for currency
        resolved = _resolve_profile(profile)
        cfg = _get_config_for_profile(profile)
        stats["currency"] = cfg.get("trading", {}).get(
            "quote_currency",
            PROFILE_CURRENCIES.get(resolved, "EUR"),
        )
        return stats
    except Exception as exc:
        logger.exception("stats/summary error")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST — Simulated Trades
# ---------------------------------------------------------------------------

def _get_live_price(pair: str) -> float:
    """Fetch the current price for a pair via ExchangeClient (or return 0 on failure)."""
    if _exchange_client:
        try:
            return _exchange_client.get_current_price(pair)
        except Exception:
            pass
    return 0.0


@app.get("/api/products", summary="List tradable Coinbase products")
def list_products():
    """Return all online, tradable products from Coinbase Advanced Trade.

    Response: ``{"products": [{"id": "BTC-EUR", "base": "BTC", "quote": "EUR"}, ...]}``
    Each entry is a product that is *online* and not disabled on the exchange.
    """
    if not getattr(_exchange_client, "_rest_client", None):
        return {"products": []}

    try:
        resp = _exchange_client._rest_client.get_products()
        raw = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
        items = raw.get("products", [])
        products = []
        for p in items:
            if (
                not p.get("trading_disabled", True)
                and not p.get("is_disabled", False)
                and str(p.get("status", "")).lower() == "online"
            ):
                products.append({
                    "id": p.get("product_id", ""),
                    "base": p.get("base_currency_id", ""),
                    "quote": p.get("quote_currency_id", ""),
                })
        products.sort(key=lambda x: x["id"])
        return {"products": products}
    except Exception as e:
        logger.warning(f"⚠️ Failed to list products: {e}")
        return {"products": []}


@app.get("/api/products/search", summary="Search tradable products by keyword")
def search_products(q: str = Query("", min_length=1, description="Search query (symbol or name)")):
    """Search products by base currency / product ID substring.

    Returns up to 25 matches sorted alphabetically.
    Falls back to an empty list when the exchange client is unavailable.
    """
    if not getattr(_exchange_client, "_rest_client", None):
        return {"results": [], "query": q}

    try:
        resp = _exchange_client._rest_client.get_products()
        raw = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
        items = raw.get("products", [])
        query = q.upper().strip()
        results = []
        for p in items:
            if (
                p.get("trading_disabled", True)
                or p.get("is_disabled", False)
                or str(p.get("status", "")).lower() != "online"
            ):
                continue
            pid = (p.get("product_id") or "").upper()
            base = (p.get("base_currency_id") or "").upper()
            display_name = (p.get("base_display_symbol") or base)
            if query in pid or query in base or query in display_name.upper():
                results.append({
                    "id": p.get("product_id", ""),
                    "base": p.get("base_currency_id", ""),
                    "quote": p.get("quote_currency_id", ""),
                    "display_name": display_name,
                    "volume_24h": float(p.get("volume_24h", 0) or 0),
                    "price_change_24h": float(p.get("price_percentage_change_24h", 0) or 0),
                })
        # Sort by 24h volume desc so the most traded pairs appear first
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        return {"results": results[:25], "query": q}
    except Exception as e:
        logger.warning(f"product search error: {e}")
        return {"results": [], "query": q}


@app.get("/api/market/price", summary="Live price for a trading pair")
def get_market_price(pair: str = Query(..., description="e.g. BTC-EUR")):
    """Returns the current best-estimate price for the given pair."""
    price = _get_live_price(pair)
    return {"pair": pair, "price": price, "ts": _utcnow()}


from pydantic import BaseModel as _BaseModel

class SimulatedTradeCreate(_BaseModel):
    pair: str
    from_currency: str
    from_amount: float
    notes: str = ""


@app.post("/api/simulated-trades", summary="Open a new simulated trade")
def create_simulated_trade(body: SimulatedTradeCreate, db=Depends(_get_profile_db)):
    """
    Opens a new paper simulation. The server fetches the live entry price,
    computes the implied quantity, and persists the record.

    For EUR→Crypto: `from_currency=EUR`, `pair=BTC-EUR`
    For Crypto→Crypto: `from_currency=BTC`, `pair=ETH-BTC` (or similar)
    """
    pair = body.pair.upper().strip()
    from_currency = body.from_currency.upper().strip()

    # Derive to_currency from pair (e.g. BTC-EUR → BTC when buying with EUR)
    try:
        base, quote = parse_pair(pair)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid pair format: {pair!r}")
    # If from_currency matches the quote, we're buying the base
    if from_currency == quote:
        to_currency = base
    elif from_currency == base:
        # Selling base for quote (e.g. BTC→EUR)
        to_currency = quote
    else:
        to_currency = base  # Best guess

    entry_price = _get_live_price(pair)
    if entry_price <= 0:
        raise HTTPException(status_code=503, detail=f"Cannot fetch live price for {pair}")

    # Quantity = how much of to_currency we'd get
    if from_currency == quote:
        quantity = body.from_amount / entry_price
    else:
        quantity = body.from_amount * entry_price  # selling crypto → getting quote

    sim_id = db.record_simulated_trade(
        pair=pair,
        from_currency=from_currency,
        from_amount=body.from_amount,
        entry_price=entry_price,
        quantity=quantity,
        to_currency=to_currency,
        notes=body.notes,
    )
    return {
        "id": sim_id,
        "pair": pair,
        "from_currency": from_currency,
        "to_currency": to_currency,
        "from_amount": body.from_amount,
        "entry_price": entry_price,
        "quantity": quantity,
        "notes": body.notes,
        "status": "open",
        "ts": _utcnow(),
    }


@app.get("/api/simulated-trades", summary="List simulated trades with live PnL")
def list_simulated_trades(
    include_closed: bool = Query(False, description="Include closed simulations"),
    profile: str = Query(""),
    db=Depends(_get_profile_db),
):
    """
    Returns all simulated trades. For open ones, the current price is fetched
    live and PnL (absolute + %) is computed on the fly.
    """
    qc = _quote_currency_for(profile)
    rows = db.get_simulated_trades(include_closed=include_closed, quote_currency=qc)

    # Enrich open rows with live PnL
    for row in rows:
        if row["status"] == "open":
            current_price = _get_live_price(row["pair"])
            if current_price > 0 and row["entry_price"] > 0:
                # Determine direction: if from_currency is the quote (e.g. USD),
                # user bought the base (long). Otherwise they sold base (short).
                try:
                    _, quote = parse_pair(row["pair"])
                except ValueError:
                    quote = ""
                is_long = row.get("from_currency", quote) == quote
                if is_long:
                    pnl_abs = (current_price - row["entry_price"]) * row["quantity"]
                    pnl_pct = ((current_price / row["entry_price"]) - 1) * 100
                else:
                    pnl_abs = (row["entry_price"] - current_price) * row["quantity"]
                    pnl_pct = ((row["entry_price"] / current_price) - 1) * 100
            else:
                current_price = row["entry_price"]
                pnl_abs = 0.0
                pnl_pct = 0.0
            row["current_price"] = current_price
            row["pnl_abs"] = round(pnl_abs, 6)
            row["pnl_pct"] = round(pnl_pct, 4)
        else:
            # Closed: use stored values
            row["current_price"] = row.get("close_price") or row["entry_price"]
            row["pnl_abs"] = row.get("close_pnl_abs") or 0.0
            row["pnl_pct"] = row.get("close_pnl_pct") or 0.0

    return {"simulations": rows, "count": len(rows)}


@app.delete("/api/simulated-trades/{sim_id}", summary="Close a simulated trade")
def close_simulated_trade_route(sim_id: int, db=Depends(_get_profile_db)):
    """
    Closes an open simulation by recording the current live price as the
    close price and computing the final PnL.
    """

    # First, look up the sim to get the pair
    rows = db.get_simulated_trades(include_closed=False)
    target = next((r for r in rows if r["id"] == sim_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Open simulation {sim_id} not found")

    close_price = _get_live_price(target["pair"])
    if close_price <= 0:
        close_price = target["entry_price"]  # Fallback to entry price

    result = db.close_simulated_trade(sim_id=sim_id, close_price=close_price)
    if not result:
        raise HTTPException(status_code=404, detail=f"Simulation {sim_id} not found or already closed")
    return result

@app.get("/api/executive_summary", summary="Combined analytics across all profiles")
def get_executive_summary():
    """Returns aggregated high-level stats across all configuration profiles found in 'data/'."""
    profiles = []
    total_pnl = 0.0
    total_trades = 0
    active_pairs = set()

    data_dir = os.path.join(os.getcwd(), "data")
    if os.path.exists(data_dir):
        from src.utils.stats import StatsDB
        for file in os.listdir(data_dir):
            if file.startswith("stats") and file.endswith(".db"):
                # Extract profile name from stats_profile.db or stats.db
                pname = file.replace("stats_", "").replace(".db", "")
                if pname == "stats":
                    pname = "default"
                
                db_path = os.path.join(data_dir, file)
                conn = None
                try:
                    conn = sqlite3.connect(db_path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    
                    row = conn.execute("SELECT COUNT(*) as t, SUM(pnl) as p FROM trades WHERE pnl IS NOT NULL").fetchone()
                    if row and row["t"] > 0:
                        t = row["t"]
                        p = row["p"] or 0.0
                        total_trades += t
                        total_pnl += p
                        
                        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                        pairs = conn.execute("SELECT DISTINCT pair FROM agent_reasoning WHERE ts >= ?", (cutoff,)).fetchall()
                        active_pairs.update(pr["pair"] for pr in pairs)
                        
                        profiles.append({
                            "profile": f"profile_{len(profiles) + 1}",
                            "trades": t,
                            "pnl": round(p, 2),
                            "active_pairs_24h": len(pairs)
                        })
                except Exception as e:
                    logger.warning(f"Error reading DB {file}: {e}")
                finally:
                    if conn:
                        conn.close()

    return {
        "profiles": profiles,
        "combined": {
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "total_active_pairs_24h": len(active_pairs)
        }
    }


# ---------------------------------------------------------------------------
# REST — Strategic context (planning)
# ---------------------------------------------------------------------------

@app.get("/api/strategic", summary="Recent strategic plans from Temporal workflows")
def get_strategic(
    horizon: Optional[str] = Query(None, description="daily | weekly | monthly"),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(_get_profile_db),
):
    """Returns the most recent planning workflow outputs with Temporal + Langfuse IDs."""
    conn = _open_conn(db)
    try:
        if horizon:
            rows = conn.execute(
                """SELECT id, horizon, plan_json, summary_text, ts,
                          langfuse_trace_id, temporal_workflow_id, temporal_run_id
                   FROM strategic_context
                   WHERE horizon = ?
                   ORDER BY ts DESC LIMIT ?""",
                (horizon, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, horizon, plan_json, summary_text, ts,
                          langfuse_trace_id, temporal_workflow_id, temporal_run_id
                   FROM strategic_context
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        result = []
        for r in rows:
            row = dict(r)
            try:
                row["plan_json"] = json.loads(row["plan_json"] or "{}")
            except Exception:
                pass
            row["langfuse_url"] = _langfuse_url(row.get("langfuse_trace_id"))
            result.append(row)
        return {"plans": result, "count": len(result)}
    except Exception as exc:
        logger.exception("strategic error")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST — Temporal workflow replay
# ---------------------------------------------------------------------------

@app.get("/api/temporal/runs", summary="List recent Temporal planning workflow runs")
async def list_temporal_runs(
    limit: int = Query(50, ge=1, le=200),
    workflow_type: Optional[str] = Query(None, description="DailyPlanWorkflow | WeeklyReviewWorkflow | MonthlyReviewWorkflow"),
):
    """Returns recent workflow executions from Temporal with their status."""
    if _temporal_client is None:
        return {"runs": [], "error": "Temporal client not available"}
    try:
        query = " OR ".join(
            f"WorkflowType = '{wt}'"
            for wt in ("DailyPlanWorkflow", "WeeklyReviewWorkflow", "MonthlyReviewWorkflow")
        )
        _ALLOWED_WORKFLOW_TYPES = {"DailyPlanWorkflow", "WeeklyReviewWorkflow", "MonthlyReviewWorkflow"}
        if workflow_type:
            if workflow_type not in _ALLOWED_WORKFLOW_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid workflow_type. Allowed: {sorted(_ALLOWED_WORKFLOW_TYPES)}",
                )
            query = f"WorkflowType = '{workflow_type}'"

        runs = []
        async for wf in _temporal_client.list_workflows(query=query):
            runs.append({
                "workflow_id": wf.id,
                "run_id": wf.run_id,
                "workflow_type": wf.workflow_type,
                "status": str(wf.status),
                "start_time": wf.start_time.isoformat() if wf.start_time else None,
                "close_time": wf.close_time.isoformat() if wf.close_time else None,
            })
            if len(runs) >= limit:
                break
        return {"runs": runs, "count": len(runs)}
    except Exception as exc:
        logger.exception("temporal/runs error")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/temporal/replay/{workflow_id}/{run_id}", summary="Full Temporal workflow event history")
async def get_temporal_replay(workflow_id: str, run_id: str):
    """
    Fetches the complete event history for a Temporal workflow run.
    Each event records input, LLM call, output, timing — enabling full step-by-step replay.
    """
    if _temporal_client is None:
        raise HTTPException(status_code=503, detail="Temporal client not available")
    try:
        handle = _temporal_client.get_workflow_handle(workflow_id, run_id=run_id)
        history = await handle.fetch_history()
        events = []
        for event in history.events:
            events.append({
                "event_id": event.event_id,
                "event_type": str(event.event_type),
                "event_time": event.event_time.isoformat() if event.event_time else None,
                "attributes": _serialize_event_attrs(event),
            })

        # Cross-link with Langfuse trace ID from StatsDB
        langfuse_trace_id = None
        if _stats_db:
            conn = _fresh_conn()
            try:
                row = conn.execute(
                    """SELECT langfuse_trace_id FROM strategic_context
                       WHERE temporal_workflow_id = ? AND temporal_run_id = ?
                       LIMIT 1""",
                    (workflow_id, run_id),
                ).fetchone()
                if row:
                    langfuse_trace_id = row[0]
            finally:
                conn.close()

        return {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "event_count": len(events),
            "langfuse_trace_id": langfuse_trace_id,
            "langfuse_url": _langfuse_url(langfuse_trace_id),
            "events": events,
        }
    except Exception as exc:
        logger.exception("temporal/replay error")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/temporal/rerun/{workflow_id}/{run_id}", summary="Trigger a fresh planning workflow run")
async def rerun_temporal_workflow(workflow_id: str, run_id: str):
    """
    Starts a new execution of the same workflow type with a fresh run ID.
    Useful for debugging or forcing an out-of-schedule planning run.
    """
    if _temporal_client is None:
        raise HTTPException(status_code=503, detail="Temporal client not available")

    # Determine workflow class from the original run
    try:
        handle = _temporal_client.get_workflow_handle(workflow_id, run_id=run_id)
        desc = await handle.describe()
        workflow_type = desc.workflow_type
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Cannot find workflow: {exc}")

    from src.planning.workflows import DailyPlanWorkflow, WeeklyReviewWorkflow, MonthlyReviewWorkflow
    _wf_map = {
        "DailyPlanWorkflow": DailyPlanWorkflow,
        "WeeklyReviewWorkflow": WeeklyReviewWorkflow,
        "MonthlyReviewWorkflow": MonthlyReviewWorkflow,
    }
    wf_cls = _wf_map.get(workflow_type)
    if not wf_cls:
        raise HTTPException(status_code=400, detail=f"Unknown workflow type: {workflow_type!r}")

    import uuid
    new_wf_id = f"manual-rerun-{workflow_type}-{uuid.uuid4().hex[:8]}"
    try:
        new_handle = await _temporal_client.start_workflow(
            wf_cls.run,
            id=new_wf_id,
            task_queue="planning",
        )
        return {
            "status": "started",
            "new_workflow_id": new_wf_id,
            "new_run_id": new_handle.first_execution_run_id,
            "original_workflow_id": workflow_id,
            "original_run_id": run_id,
        }
    except Exception as exc:
        logger.exception("temporal/rerun error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# WebSocket — Live LLM event stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/live")
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
    if _DASHBOARD_API_KEY:
        # Try X-API-Key header first; fall back to Sec-WebSocket-Protocol
        # (browsers can't set custom headers on WS, so the frontend encodes
        # the key as a subprotocol: "apikey.<base64_key>")
        api_key = websocket.headers.get("x-api-key", "")
        if not api_key:
            for proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
                proto = proto.strip()
                if proto.startswith("apikey."):
                    import base64
                    try:
                        api_key = base64.b64decode(proto[7:]).decode("utf-8")
                    except Exception:
                        pass
                    break
        if not hmac.compare_digest(api_key, _DASHBOARD_API_KEY):
            await websocket.close(code=1008, reason="Invalid or missing API key")
            return
    # When no API key is configured, network-level access control is handled
    # by the Docker port binding (127.0.0.1:8090) — same as the HTTP
    # middleware.  Checking websocket.client.host here would break Docker
    # setups where the client appears as the bridge-network gateway IP.

    # L22 fix: echo the auth subprotocol so browsers don't reject per RFC 6455
    _accepted_subprotocol = None
    for _proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
        _proto = _proto.strip()
        if _proto.startswith("apikey."):
            _accepted_subprotocol = _proto
            break
    await websocket.accept(subprotocol=_accepted_subprotocol)

    # Extract profile from query params for event filtering
    from urllib.parse import parse_qs, urlparse
    _qs = parse_qs(urlparse(str(websocket.url)).query)
    _ws_profile = (_qs.get("profile", [""])[0] or "").strip()
    _ws_qc = _quote_currency_for(_ws_profile)

    _ws_connections.append((websocket, _ws_qc))
    logger.info(f"WS client connected (profile={_ws_profile!r}, qc={_ws_qc}) ({len(_ws_connections)} total)")
    try:
        while True:
            # Keep connection alive; events are pushed by _redis_subscriber
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping", "ts": _utcnow()})
    except WebSocketDisconnect:
        pass
    finally:
        # Guard: the Redis subscriber may have already removed this socket
        _ws_connections[:] = [(ws, qc) for ws, qc in _ws_connections if ws is not websocket]
        logger.info(f"WS client disconnected ({len(_ws_connections)} remaining)")


async def _redis_subscriber():
    """
    Background task: subscribes to Redis `llm:events` channel and
    broadcasts each message to all connected WebSocket clients.

    Reconnects with exponential backoff if the Redis connection drops.
    """
    if _redis_client is None:
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

                # Extract pair from event for profile filtering
                event_pair = (payload.get("pair") or "").upper()

                dead = []
                for ws, ws_qc in list(_ws_connections):
                    # Filter: if this WS connection has a quote currency filter,
                    # only send events that match (or have no pair info)
                    if ws_qc and event_pair and not event_pair.endswith(f"-{ws_qc.upper()}"):
                        continue
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _ws_connections[:] = [(w, q) for w, q in _ws_connections if w is not ws]

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


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health(request: Request):
    # Return minimal info when unauthenticated to avoid leaking service topology.
    # When the API key is configured and presented correctly, expose full detail.
    authenticated = (
        not _DASHBOARD_API_KEY
        or hmac.compare_digest(
            request.headers.get("X-API-Key", ""), _DASHBOARD_API_KEY
        )
    )
    base = {"status": "ok", "ts": _utcnow()}
    if authenticated:
        base.update({
            "db": _stats_db is not None,
            "redis": _redis_client is not None,
            "temporal": _temporal_client is not None,
            "ws_clients": len(_ws_connections),
        })
    return base


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _langfuse_url(trace_id: Optional[str]) -> Optional[str]:
    if not trace_id:
        return None
    # Use the Langfuse SDK to build the correct URL (includes project ID)
    from src.utils.tracer import get_llm_tracer
    tracer = get_llm_tracer()
    if tracer:
        url = tracer.get_trace_url(trace_id)
        if url:
            return url
    # Fallback: best-effort URL (may not work if project ID is needed)
    host = _get_config().get("dashboard", {}).get("langfuse_host", "http://localhost:3000")
    return f"{host}/trace/{trace_id}"


def _serialize_event_attrs(event) -> dict:
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
        else:
            raw = {"raw": str(attrs)}
        return raw
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# REST — Setup Wizard (initial configuration)
# ---------------------------------------------------------------------------


class _SetupConfigBody(_BaseModel):
    config_env: dict[str, str]  # env vars for config/.env
    root_env: dict[str, str]  # env vars for root .env (Docker Compose)
    assets: dict | None = None  # {coinbase_pairs: [...], nordnet_pairs: [...], ibkr_pairs: [...]}


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            idx = line.index("=")
            key = line[:idx].strip()
            value = line[idx + 1:].strip()
            result[key] = value
    return result


@app.get("/api/setup", summary="Load current configuration for the setup wizard")
def get_setup_config():
    """Read config/.env, root .env, and YAML configs and return a
    WizardState-compatible JSON so the frontend wizard can pre-populate."""
    try:
        import yaml as _yaml

        config_env = _parse_env_file(os.path.join("config", ".env"))
        # Try config/root.env first (Docker), fall back to .env (host)
        root_env = _parse_env_file(os.path.join("config", "root.env"))
        if not root_env:
            root_env = _parse_env_file(".env")

        if not config_env:
            return {"exists": False}

        env = config_env.get

        # Detect active exchanges from YAML config existence
        exchanges = {"coinbase": False, "nordnet": False, "ibkr": False}
        yaml_pairs: dict[str, list[str]] = {}
        for exch, fname in [("coinbase", "coinbase.yaml"), ("nordnet", "nordnet.yaml"), ("ibkr", "ibkr.yaml")]:
            ypath = os.path.join("config", fname)
            if os.path.exists(ypath):
                exchanges[exch] = True
                try:
                    with open(ypath, "r", encoding="utf-8") as f:
                        ycfg = _yaml.safe_load(f) or {}
                    yaml_pairs[exch] = (ycfg.get("trading") or {}).get("pairs", [])
                except Exception:
                    yaml_pairs[exch] = []

        # Map env vars → WizardState fields
        trading_mode = env("TRADING_MODE", "paper")
        live_confirmed = env("LIVE_TRADING_CONFIRMED", "") != ""

        # Telegram: parse authorized users
        authorized = env("TELEGRAM_AUTHORIZED_USERS", "")
        user_ids = [u.strip() for u in authorized.split(",") if u.strip()]
        primary_user = user_ids[0] if user_ids else ""
        additional_users = ",".join(user_ids[1:]) if len(user_ids) > 1 else ""
        telegram_enabled = bool(primary_user)

        # Infrastructure secrets (passed through so save preserves them)
        infra_secrets = {}
        _INFRA_KEYS = [
            "REDIS_PASSWORD", "REDIS_URL",
            "TEMPORAL_DB_USER", "TEMPORAL_DB_PASSWORD", "TEMPORAL_DB_NAME",
            "LANGFUSE_DB_PASSWORD", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT",
            "LANGFUSE_ADMIN_PASSWORD", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
            "CLICKHOUSE_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
            "LANGFUSE_ENCRYPTION_KEY",
        ]
        for k in _INFRA_KEYS:
            if k in config_env:
                infra_secrets[k] = config_env[k]

        state = {
            "exists": True,
            "exchanges": exchanges,
            "tradingMode": trading_mode,
            "liveConfirmed": live_confirmed,
            "cryptoPairs": yaml_pairs.get("coinbase", []),
            "customCryptoPair": "",
            "stockPairs": yaml_pairs.get("nordnet", []),
            "customStockPair": "",
            "ibkrPairs": yaml_pairs.get("ibkr", []),
            "customIbkrPair": "",
            "coinbaseApiKey": env("COINBASE_API_KEY", ""),
            "coinbaseApiSecret": env("COINBASE_API_SECRET", ""),
            "ibkrHost": env("IBKR_HOST", "127.0.0.1"),
            "ibkrPort": env("IBKR_PORT", "4002"),
            "ibkrClientId": env("IBKR_CLIENT_ID", "1"),
            "ibkrCurrency": env("IBKR_CURRENCY", "USD"),
            "geminiEnabled": env("GEMINI_API_KEY", "") != "",
            "geminiApiKey": env("GEMINI_API_KEY", ""),
            "openrouterEnabled": env("OPENROUTER_API_KEY", "") != "",
            "openrouterApiKey": env("OPENROUTER_API_KEY", ""),
            "openaiEnabled": env("OPENAI_API_KEY", "") != "",
            "openaiApiKey": env("OPENAI_API_KEY", ""),
            "ollamaModel": env("OLLAMA_MODEL", "qwen2.5:14b"),
            "telegramEnabled": telegram_enabled,
            "telegramUserId": primary_user,
            "telegramAdditionalUsers": additional_users,
            "telegramCoinbaseBotToken": env("TELEGRAM_BOT_TOKEN_COINBASE", ""),
            "telegramCoinbaseChatId": env("TELEGRAM_CHAT_ID_COINBASE", ""),
            "telegramNordnetBotToken": env("TELEGRAM_BOT_TOKEN_NORDNET", ""),
            "telegramNordnetChatId": env("TELEGRAM_CHAT_ID_NORDNET", ""),
            "telegramIbkrBotToken": env("TELEGRAM_BOT_TOKEN_IBKR", ""),
            "telegramIbkrChatId": env("TELEGRAM_CHAT_ID_IBKR", ""),
            "redditEnabled": env("REDDIT_CLIENT_ID", "") != "",
            "redditClientId": env("REDDIT_CLIENT_ID", ""),
            "redditClientSecret": env("REDDIT_CLIENT_SECRET", ""),
            "redditUserAgent": env("REDDIT_USER_AGENT", "auto-traitor/1.0"),
            # Infra secrets so the frontend can preserve them on re-save
            "infraSecrets": infra_secrets,
        }
        return state
    except Exception as exc:
        logger.exception("setup GET error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/setup", summary="Save initial configuration from setup wizard")
def setup_config(body: _SetupConfigBody, request: Request):
    """Write config/.env and root .env from the setup wizard.

    Also updates YAML config files with selected trading pairs if provided.
    """
    try:
        source_ip = request.client.host if request.client else "unknown"

        # 1. Write config/.env
        config_env_path = os.path.join("config", ".env")
        os.makedirs("config", exist_ok=True)

        # Backup existing config/.env if present
        if os.path.exists(config_env_path):
            backup_path = f"{config_env_path}.backup.{int(time.time())}"
            import shutil
            shutil.copy2(config_env_path, backup_path)
            logger.info(f"Backed up existing config/.env to {backup_path}")

        # Build file content with comments
        config_lines = [
            "# ===========================================",
            "# Auto-Traitor Environment Configuration",
            f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "# Generated by: Setup Wizard (web)",
            "# ===========================================",
            "",
        ]
        for key, value in body.config_env.items():
            config_lines.append(f"{key}={str(value).replace(chr(10), '').replace(chr(13), '')}")
        config_lines.append("")

        env_dir = os.path.dirname(os.path.abspath(config_env_path))
        fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp", prefix=".env_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(config_lines))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, os.path.abspath(config_env_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # 2. Write root .env (Docker Compose substitution vars)
        # Written to config/root.env because the container FS is read-only;
        # the config/ dir is a bind-mount so this is writable and visible on
        # the host as config/root.env (symlinked or copied to .env by the user
        # or docker-compose override).
        root_env_path = os.path.join("config", "root.env")
        root_lines = [
            "# Docker Compose variable substitution — generated by setup wizard, do not commit",
            "",
        ]
        for key, value in body.root_env.items():
            root_lines.append(f"{key}={str(value).replace(chr(10), '').replace(chr(13), '')}")
        root_lines.append("")

        root_dir = os.path.dirname(os.path.abspath(root_env_path)) or "."
        fd2, tmp2 = tempfile.mkstemp(dir=root_dir, suffix=".env.tmp", prefix=".env_")
        try:
            with os.fdopen(fd2, "w", encoding="utf-8") as f:
                f.write("\n".join(root_lines))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp2, os.path.abspath(root_env_path))
        except BaseException:
            try:
                os.unlink(tmp2)
            except OSError:
                pass
            raise

        # Also try writing the actual root .env for non-containerized runs
        try:
            actual_root = ".env"
            actual_dir = os.path.dirname(os.path.abspath(actual_root)) or "."
            fd3, tmp3 = tempfile.mkstemp(dir=actual_dir, suffix=".env.tmp", prefix=".env_")
            try:
                with os.fdopen(fd3, "w", encoding="utf-8") as f:
                    f.write("\n".join(root_lines))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp3, os.path.abspath(actual_root))
            except BaseException:
                try:
                    os.unlink(tmp3)
                except OSError:
                    pass
                raise
        except OSError:
            # Expected in read-only container — config/root.env is the fallback
            logger.info("Skipped root .env write (read-only filesystem); wrote config/root.env instead")

        # 3. Update trading pairs in YAML configs if provided
        updated_yamls: list[str] = []
        if body.assets:
            import yaml

            coinbase_pairs = body.assets.get("coinbase_pairs")
            if coinbase_pairs and isinstance(coinbase_pairs, list):
                cb_path = os.path.join("config", "coinbase.yaml")
                if os.path.exists(cb_path):
                    with open(cb_path, "r", encoding="utf-8") as f:
                        cb_cfg = yaml.safe_load(f) or {}
                    if "trading" in cb_cfg:
                        cb_cfg["trading"]["pairs"] = coinbase_pairs
                    with open(cb_path, "w", encoding="utf-8") as f:
                        yaml.dump(cb_cfg, f, default_flow_style=False, allow_unicode=True)
                    updated_yamls.append("coinbase.yaml")

            nordnet_pairs = body.assets.get("nordnet_pairs")
            if nordnet_pairs and isinstance(nordnet_pairs, list):
                nn_path = os.path.join("config", "nordnet.yaml")
                if os.path.exists(nn_path):
                    with open(nn_path, "r", encoding="utf-8") as f:
                        nn_cfg = yaml.safe_load(f) or {}
                    if "trading" in nn_cfg:
                        nn_cfg["trading"]["pairs"] = nordnet_pairs
                    with open(nn_path, "w", encoding="utf-8") as f:
                        yaml.dump(nn_cfg, f, default_flow_style=False, allow_unicode=True)
                    updated_yamls.append("nordnet.yaml")

            ibkr_pairs = body.assets.get("ibkr_pairs")
            if ibkr_pairs and isinstance(ibkr_pairs, list):
                ib_path = os.path.join("config", "ibkr.yaml")
                if os.path.exists(ib_path):
                    with open(ib_path, "r", encoding="utf-8") as f:
                        ib_cfg = yaml.safe_load(f) or {}
                    if "trading" in ib_cfg:
                        ib_cfg["trading"]["pairs"] = ibkr_pairs
                    with open(ib_path, "w", encoding="utf-8") as f:
                        yaml.dump(ib_cfg, f, default_flow_style=False, allow_unicode=True)
                    updated_yamls.append("ibkr.yaml")

        # 4. Create data directories
        for d in ["data", "data/trades", "data/news", "data/journal", "data/audit", "logs"]:
            os.makedirs(d, exist_ok=True)

        # 5. Update os.environ with new values (allowlisted keys only)
        _ALLOWED_ENV_KEYS = {
            "COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_KEY_FILE",
            "REDIS_URL", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
            "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_AUTHORIZED_USERS",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
            "TEMPORAL_HOST", "TEMPORAL_NAMESPACE",
            "DASHBOARD_API_KEY", "DASHBOARD_COMMAND_SIGNING_KEY",
            "LOG_LEVEL", "PAPER_MODE",
            "NORDNET_USERNAME", "NORDNET_PASSWORD",
            "IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID", "IBKR_CURRENCY",
        }
        for key, value in body.config_env.items():
            if key in _ALLOWED_ENV_KEYS:
                os.environ[key] = value
            else:
                logger.warning(f"Setup wizard: rejected unknown env key {key!r}")

        # 6. Hot-reload LLM providers so new API keys take effect immediately
        llm_reloaded = False
        if _llm_client:
            try:
                from src.core.llm_client import build_providers
                saved_providers = _sm_get_providers()
                llm_config = _get_config().get("llm", {})
                ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
                fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))
                new_providers = build_providers(
                    saved_providers,
                    fallback_base_url=ollama_url,
                    fallback_model=fallback_model,
                    fallback_timeout=llm_config.get("timeout", 60),
                    fallback_max_retries=llm_config.get("max_retries", 3),
                )
                _llm_client.reload_providers(new_providers)
                # Update stored config so recovery polling picks up new keys
                _llm_client.update_providers_config(
                    saved_providers,
                    fallback_base_url=ollama_url,
                    fallback_model=fallback_model,
                    fallback_timeout=llm_config.get("timeout", 60),
                    fallback_max_retries=llm_config.get("max_retries", 3),
                )
                llm_reloaded = True
            except Exception as _reload_err:
                logger.warning(f"LLM provider hot-reload after setup failed: {_reload_err}")

        logger.warning(
            f"⚙️ Setup wizard config saved: {len(body.config_env)} env vars, "
            f"yamls={updated_yamls}, llm_reloaded={llm_reloaded} (ip={source_ip})"
        )

        return {
            "ok": True,
            "config_env_path": config_env_path,
            "root_env_path": root_env_path,
            "env_vars_count": len(body.config_env),
            "updated_yamls": updated_yamls,
            "llm_reloaded": llm_reloaded,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("setup POST error")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# REST — Settings (read, update, presets)
# ---------------------------------------------------------------------------

from src.utils.settings_manager import (
    get_full_settings as _sm_get_full,
    get_schema_metadata as _sm_get_schema,
    update_section as _sm_update_section,
    apply_preset as _sm_apply_preset,
    push_to_runtime as _sm_push_runtime,
    push_section_to_runtime as _sm_push_section,
    PRESETS as _SM_PRESETS,
    get_preset_summary as _sm_preset_summary,
    is_trading_enabled as _sm_is_trading_enabled,
    is_telegram_allowed as _sm_tg_allowed,
    TELEGRAM_SAFETY_TIERS as _SM_TG_TIERS,
    get_llm_providers as _sm_get_providers,
    update_llm_providers as _sm_update_providers,
)


@app.get("/api/settings", summary="Get all settings with metadata")
def get_settings():
    """Returns the full settings.yaml content, schema metadata, and presets info."""
    try:
        full = _sm_get_full()
        full["schema"] = _sm_get_schema()
        return full
    except Exception as exc:
        logger.exception("settings GET error")
        raise HTTPException(status_code=500, detail=str(exc))


class _SettingsUpdateBody(_BaseModel):
    section: Optional[str] = None
    updates: Optional[dict] = None
    preset: Optional[str] = None
    confirmation_token: Optional[str] = None  # Required for sensitive sections


# Sections that require confirmation before mutation
_SETTINGS_CONFIRM_SECTIONS = frozenset({
    "absolute_rules", "trading", "high_stakes",
})


@app.put("/api/settings", summary="Update settings section or apply preset")
def update_settings(body: _SettingsUpdateBody, request: Request):
    """
    Two modes:
      1. ``{ "preset": "moderate" }`` — apply a named preset
      2. ``{ "section": "risk", "updates": {"stop_loss_pct": 0.05} }`` — update individual fields

    Sensitive sections (absolute_rules, trading, high_stakes) require a
    two-step confirmation flow — first call returns a ``confirmation_token``,
    second call with that token applies the change.

    All mutations are audit-logged.
    """
    try:
        _prune_expired_confirmations()
        source_ip = request.client.host if request.client else "unknown"

        # Mode 1: Apply preset
        if body.preset:
            # Presets always require confirmation
            if not body.confirmation_token:
                if not _check_confirmation_rate(source_ip):
                    raise HTTPException(status_code=429, detail="Too many confirmation requests")
                token = secrets.token_urlsafe(32)
                _store_confirmation(token, {
                    "action": "settings-preset",
                    "preset": body.preset,
                    "expires": time.monotonic() + _CONFIRM_TTL_SECONDS,
                })
                return {
                    "ok": False,
                    "confirmation_required": True,
                    "confirmation_token": token,
                    "message": f"Confirm applying preset '{body.preset}'.",
                    "expires_in_seconds": _CONFIRM_TTL_SECONDS,
                }

            pending = _pop_confirmation(body.confirmation_token)
            if not pending or pending["expires"] < time.monotonic():
                raise HTTPException(status_code=403, detail="Invalid or expired confirmation token")
            if pending.get("preset") != body.preset:
                raise HTTPException(status_code=400, detail="Preset does not match confirmation")

            ok, err, changes = _sm_apply_preset(body.preset)
            if not ok:
                raise HTTPException(status_code=400, detail=err)
            _sm_push_runtime(_rules_instance, _get_config(), changes)
            logger.warning(
                f"⚙️ Settings preset applied: {body.preset} "
                f"({len(changes)} changes, ip={source_ip})"
            )
            return {
                "ok": True,
                "preset": body.preset,
                "changes": changes,
                "trading_enabled": _sm_is_trading_enabled(),
            }

        # Mode 2: Section update
        if not body.section or not body.updates:
            raise HTTPException(
                status_code=400,
                detail="Provide either {preset} or {section, updates}",
            )

        # Require confirmation for sensitive sections
        needs_confirm = body.section in _SETTINGS_CONFIRM_SECTIONS
        if needs_confirm and not body.confirmation_token:
            if not _check_confirmation_rate(source_ip):
                raise HTTPException(status_code=429, detail="Too many confirmation requests")
            token = secrets.token_urlsafe(32)
            # H10 fix: store updates hash so values can’t be swapped on confirmation
            import hashlib as _hl
            _updates_hash = _hl.sha256(json.dumps(body.updates, sort_keys=True).encode()).hexdigest()
            _store_confirmation(token, {
                "action": "settings-section",
                "section": body.section,
                "field_names": sorted(body.updates.keys()),
                "updates_hash": _updates_hash,
                "expires": time.monotonic() + _CONFIRM_TTL_SECONDS,
            })
            return {
                "ok": False,
                "confirmation_required": True,
                "confirmation_token": token,
                "section": body.section,
                "fields_to_update": sorted(body.updates.keys()),
                "message": f"Confirm update to '{body.section}' settings.",
                "expires_in_seconds": _CONFIRM_TTL_SECONDS,
            }

        if needs_confirm:
            pending = _pop_confirmation(body.confirmation_token)
            if not pending or pending["expires"] < time.monotonic():
                raise HTTPException(status_code=403, detail="Invalid or expired confirmation token")
            if pending.get("section") != body.section:
                raise HTTPException(status_code=400, detail="Section does not match confirmation")
            # H10: verify the updates payload hasn't been swapped since confirmation was issued
            import hashlib as _hl
            current_hash = _hl.sha256(json.dumps(body.updates, sort_keys=True).encode()).hexdigest()
            if pending.get("updates_hash") and pending["updates_hash"] != current_hash:
                raise HTTPException(status_code=400, detail="Updates payload changed since confirmation was issued")

        ok, err, changes = _sm_update_section(body.section, body.updates)
        if not ok:
            raise HTTPException(status_code=400, detail=err)

        _sm_push_section(body.section, changes, _rules_instance, _get_config())
        logger.warning(
            f"⚙️ Settings updated: section={body.section}, "
            f"fields={sorted(changes.keys()) if isinstance(changes, dict) else changes} "
            f"(ip={source_ip})"
        )
        return {
            "ok": True,
            "section": body.section,
            "changes": changes,
            "trading_enabled": _sm_is_trading_enabled(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("settings PUT error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/settings/presets", summary="List available presets")
def get_presets():
    """Returns all available presets with their values and human descriptions."""
    result = {}
    for name in _SM_PRESETS:
        result[name] = {
            "values": _SM_PRESETS[name],
            "summary": _sm_preset_summary(name),
        }
    return {"presets": result, "current_enabled": _sm_is_trading_enabled()}


@app.get("/api/settings/telegram-tiers", summary="Telegram safety tier plan")
def get_telegram_tiers():
    """Returns which settings sections are safe/semi-safe/blocked for Telegram."""
    return _SM_TG_TIERS


# ---------------------------------------------------------------------------
# REST — LLM Provider management
# ---------------------------------------------------------------------------

@app.get("/api/settings/llm-providers", summary="Get LLM provider chain with live status")
def get_llm_providers():
    """
    Returns the configured LLM providers with their live status
    (daily tokens used, cooldown state, API key availability).
    """
    try:
        providers_config = _sm_get_providers()

        # Enrich with live status from LLMClient if available
        live_status = {}
        if _llm_client:
            for ps in _llm_client.provider_status():
                live_status[ps["name"]] = ps

        # Fields safe to expose to the dashboard (redact base_url, api_key_env, etc.)
        _SAFE_PROVIDER_FIELDS = {"name", "model", "is_local", "enabled", "priority"}
        result = []
        for pc in providers_config:
            name = pc.get("name", "")
            entry = {k: v for k, v in pc.items() if k in _SAFE_PROVIDER_FIELDS}
            # Add live status if available
            if name in live_status:
                entry["live_status"] = live_status[name]
            # Indicate whether the API key is set (don't expose the env var name)
            api_key_env = pc.get("api_key_env", "")
            if api_key_env:
                entry["api_key_set"] = bool(os.environ.get(api_key_env, ""))
            else:
                entry["api_key_set"] = pc.get("is_local", False)
            result.append(entry)

        return {"providers": result}
    except Exception as exc:
        logger.exception("llm-providers GET error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/settings/openrouter-credits", summary="Check OpenRouter free-tier credits")
async def get_openrouter_credits():
    """Return OpenRouter credit balance and usage info.

    Calls the OpenRouter /api/v1/auth/key endpoint to check remaining credits,
    usage, and whether the key is on a free tier.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "OPENROUTER_API_KEY not set"}

    from src.core.llm_client import check_openrouter_credits
    info = await check_openrouter_credits(api_key)
    return info


class _ProvidersUpdateBody(_BaseModel):
    providers: list[dict]


@app.put("/api/settings/llm-providers", summary="Update LLM provider chain")
def update_llm_providers(body: _ProvidersUpdateBody):
    """
    Accepts a full ordered providers list. Validates, persists to settings.yaml,
    and hot-reloads the LLMClient's provider chain.
    """
    try:
        ok, err, saved = _sm_update_providers(body.providers)
        if not ok:
            raise HTTPException(status_code=400, detail=err)

        # Hot-reload the LLMClient if available
        if _llm_client:
            from src.core.llm_client import build_providers
            llm_config = _get_config().get("llm", {})
            ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))
            new_providers = build_providers(
                saved,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )
            _llm_client.reload_providers(new_providers)
            _llm_client.update_providers_config(
                saved,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )

        return {"ok": True, "providers": saved}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("llm-providers PUT error")
        raise HTTPException(status_code=500, detail=str(exc))


class _ApiKeysUpdateBody(_BaseModel):
    keys: dict[str, str]  # env_var_name → value
    confirmation_token: Optional[str] = None  # Required on second step


def _prune_expired_confirmations() -> None:
    """Remove expired confirmation tokens."""
    _expire_confirmations()  # M24 fix: delegate to thread-safe helper


# M7 fix: Rate limit confirmation token generation (max 10 per IP per 60s)
_confirmation_rate: dict[str, list[float]] = {}
_confirmation_rate_lock = threading.Lock()
_CONFIRM_RATE_LIMIT = 10
_CONFIRM_RATE_WINDOW = 60.0  # seconds


def _check_confirmation_rate(ip: str) -> bool:
    """Return True if the IP is within rate limits for confirmation token generation."""
    now = time.monotonic()
    with _confirmation_rate_lock:
        timestamps = _confirmation_rate.get(ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < _CONFIRM_RATE_WINDOW]
        if len(timestamps) >= _CONFIRM_RATE_LIMIT:
            _confirmation_rate[ip] = timestamps
            return False
        timestamps.append(now)
        _confirmation_rate[ip] = timestamps
        # Evict stale IPs to prevent unbounded growth
        if len(_confirmation_rate) > 1000:
            stale = [k for k, v in _confirmation_rate.items()
                     if not v or now - v[-1] > _CONFIRM_RATE_WINDOW]
            for k in stale:
                del _confirmation_rate[k]
        return True


_CONFIRM_TTL_SECONDS = 120  # 2-minute window to confirm


@app.put("/api/settings/api-keys", summary="Update API keys for LLM providers")
def update_api_keys(body: _ApiKeysUpdateBody, request: Request):
    """
    Two-step confirmation flow for credential updates:

    **Step 1** — Send ``{"keys": {"GEMINI_API_KEY": "AIza..."}}``
      Returns a ``confirmation_token`` and lists the keys that will be updated.

    **Step 2** — Re-send with the token: ``{"keys": {...}, "confirmation_token": "..."}``
      Validates, persists to config/.env (atomic write), and hot-reloads providers.
    """
    try:
        _prune_expired_confirmations()
        source_ip = request.client.host if request.client else "unknown"

        # Validate: only allow env vars referenced by providers' api_key_env
        providers_config = _sm_get_providers()
        allowed_vars: set[str] = set()
        for pc in providers_config:
            env_var = pc.get("api_key_env", "")
            if env_var:
                allowed_vars.add(env_var)

        for var_name in body.keys:
            if var_name not in allowed_vars:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{var_name}' is not a recognized LLM provider API key env var. "
                           f"Allowed: {sorted(allowed_vars)}",
                )

        key_names = sorted(body.keys.keys())

        # ── Step 1: issue confirmation token ──────────────────────────
        if not body.confirmation_token:
            if not _check_confirmation_rate(source_ip):
                raise HTTPException(status_code=429, detail="Too many confirmation requests")
            token = secrets.token_urlsafe(32)
            _store_confirmation(token, {
                "action": "api-keys",
                "key_names": key_names,
                "expires": time.monotonic() + _CONFIRM_TTL_SECONDS,
            })
            logger.info(f"🔑 API key update requested (awaiting confirmation): {key_names}")
            return {
                "ok": False,
                "confirmation_required": True,
                "confirmation_token": token,
                "keys_to_update": key_names,
                "message": f"Confirm update of {len(key_names)} API key(s) by re-sending with confirmation_token.",
                "expires_in_seconds": _CONFIRM_TTL_SECONDS,
            }

        # ── Step 2: validate confirmation token ──────────────────────
        pending = _pop_confirmation(body.confirmation_token)
        if not pending:
            raise HTTPException(status_code=403, detail="Invalid or expired confirmation token")
        if pending["expires"] < time.monotonic():
            raise HTTPException(status_code=403, detail="Confirmation token expired")
        if sorted(pending["key_names"]) != key_names:
            raise HTTPException(
                status_code=400,
                detail="Key names do not match the original confirmation request",
            )

        # Update os.environ immediately
        for var_name, value in body.keys.items():
            os.environ[var_name] = value

        # Persist to config/.env (atomic write)
        env_path = os.path.join("config", ".env")
        _update_env_file(env_path, body.keys)

        # Hot-reload LLMClient providers so new keys take effect
        if _llm_client:
            from src.core.llm_client import build_providers
            saved_providers = _sm_get_providers()
            llm_config = _get_config().get("llm", {})
            ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))
            new_providers = build_providers(
                saved_providers,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )
            _llm_client.reload_providers(new_providers)
            _llm_client.update_providers_config(
                saved_providers,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )

        logger.warning(f"🔑 API keys updated (confirmed): {key_names}")
        return {"ok": True, "updated": key_names}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("api-keys PUT error")
        raise HTTPException(status_code=500, detail=str(exc))


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# REST — Portfolio History & Analytics
# ---------------------------------------------------------------------------

@app.get("/api/portfolio/history", summary="Portfolio value time-series for equity curve")
def get_portfolio_history(hours: int = Query(720, ge=1, le=8760), profile: str = Query(""), db=Depends(_get_profile_db)):
    """Returns portfolio snapshots as time-series data for charting."""
    try:
        qc = _quote_currency_for(profile)
        rows = db.get_portfolio_history(hours=hours, quote_currency=qc)
        return _sanitize_floats({"history": rows, "count": len(rows)})
    except Exception as exc:
        logger.exception("portfolio/history error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/analytics", summary="Comprehensive performance analytics")
def get_analytics(hours: int = Query(720, ge=1, le=8760), profile: str = Query(""), db=Depends(_get_profile_db)):
    """Combined analytics dashboard data: performance, best/worst, daily summaries, win/loss stats."""
    try:
        qc = _quote_currency_for(profile)
        perf = db.get_performance_summary(hours=hours, quote_currency=qc)
        best_worst = db.get_best_worst_trades(hours=hours, quote_currency=qc)
        days = max(1, hours // 24)
        summaries = db.get_daily_summaries(days=days, quote_currency=qc)
        win_loss = db.get_win_loss_stats(hours=hours, quote_currency=qc)
        portfolio_range = db.get_portfolio_range(hours=hours, quote_currency=qc)

        return _sanitize_floats({
            "performance": perf,
            "best_worst": best_worst,
            "daily_summaries": summaries,
            "win_loss": win_loss,
            "portfolio_range": portfolio_range,
        })
    except Exception as exc:
        logger.exception("analytics error")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# REST — Portfolio Exposure (position concentration)
# ---------------------------------------------------------------------------

@app.get("/api/portfolio/exposure", summary="Current portfolio position concentration")
def get_portfolio_exposure(profile: str = Query(""), db=Depends(_get_profile_db)):
    """Returns the latest portfolio snapshot with position breakdown.

    When a profile is active, only the snapshot from the matching exchange is
    returned, and positions are filtered to pairs with the profile's quote
    currency so crypto holdings never bleed into equity views.
    """
    conn = _open_conn(db)
    qc = _quote_currency_for(profile)
    try:
        # Build query: prefer the snapshot for the profile's exchange
        base_sql = """SELECT portfolio_value, cash_balance, return_pct, total_pnl,
                      max_drawdown, open_positions, current_prices, fear_greed_value,
                      high_stakes_active, ts
               FROM portfolio_snapshots"""
        params: list = []
        if qc:
            _qc_exchange_map = {"EUR": "coinbase", "SEK": "nordnet", "USD": "ibkr"}
            exchange = _qc_exchange_map.get(qc.upper())
            if exchange:
                base_sql += " WHERE exchange = ?"
                params.append(exchange)
        base_sql += " ORDER BY ts DESC LIMIT 1"
        row = conn.execute(base_sql, params).fetchone()
        if not row:
            return {"exposure": None}

        data = dict(row)
        # Parse JSON fields
        for field in ("open_positions", "current_prices"):
            if isinstance(data.get(field), str):
                try:
                    data[field] = json.loads(data[field])
                except Exception:
                    pass

        # Compute concentration breakdown
        positions = data.get("open_positions") or {}
        prices = data.get("current_prices") or {}
        portfolio_val = data.get("portfolio_value") or 1

        # Filter positions by quote currency when a profile is active
        if qc:
            positions = {
                pair: pos for pair, pos in positions.items()
                if pair.upper().endswith(f"-{qc.upper()}")
            }
            prices = {
                pair: p for pair, p in prices.items()
                if pair.upper().endswith(f"-{qc.upper()}")
            }

        breakdown = []
        allocated = 0.0
        for pair, pos in positions.items():
            qty = pos.get("quantity", 0) if isinstance(pos, dict) else 0
            price = prices.get(pair, pos.get("entry_price", 0)) if isinstance(pos, dict) else 0
            value = qty * price
            pct = (value / portfolio_val * 100) if portfolio_val else 0
            allocated += value
            entry_price = pos.get("entry_price", 0) if isinstance(pos, dict) else 0
            pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price else 0
            breakdown.append({
                "pair": pair,
                "quantity": qty,
                "entry_price": entry_price,
                "current_price": price,
                "value": round(value, 2),
                "pct_of_portfolio": round(pct, 1),
                "pnl_pct": round(pnl_pct, 2),
            })

        cash_pct = ((portfolio_val - allocated) / portfolio_val * 100) if portfolio_val else 100
        data["breakdown"] = sorted(breakdown, key=lambda x: x["value"], reverse=True)
        data["cash_pct"] = round(cash_pct, 1)
        data["allocated_pct"] = round(100 - cash_pct, 1)

        return _sanitize_floats({"exposure": data})
    except Exception as exc:
        logger.exception("portfolio/exposure error")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST — News Feed
# ---------------------------------------------------------------------------

@app.get("/api/news", summary="Recent news headlines with sentiment")
def get_news(
    count: int = Query(30, ge=1, le=100),
    profile: str = Query("", description="Exchange profile"),
    db=Depends(_get_profile_db),
):
    """Returns recent news articles from Redis cache (populated by news worker).

    When a profile is set, try the profile-specific key first
    (``news:{profile}:latest``), then fall back to the global ``news:latest``
    key and filter articles by tags matching the profile's news sources.

    Human-followed pairs (from the watchlist) boost relevance: articles whose
    tags or title contain the base symbol of a followed pair are included even
    if they don't match the profile's source config.
    """
    if not _redis_client:
        return {"articles": [], "count": 0, "source": "unavailable"}
    try:
        resolved = _resolve_profile(profile)
        qc = _quote_currency_for(profile)

        # 1) Try profile-specific Redis key
        raw = None
        if resolved:
            raw = _redis_client.get(f"news:{resolved}:latest")

        # 2) Fall back to global key
        if not raw:
            raw = _redis_client.get("news:latest")

        if not raw:
            return {"articles": [], "count": 0, "source": "redis_empty"}

        articles = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        if not isinstance(articles, list):
            articles = []

        # Build set of base symbols from human-followed pairs for relevance matching
        followed_symbols: set[str] = set()
        try:
            human_pairs = db.get_followed_pairs_set(followed_by="human", quote_currency=qc)
            for p in human_pairs:
                base = p.split("-")[0].lower() if "-" in p else p.lower()
                followed_symbols.add(base)
        except Exception:
            pass  # non-critical

        # 3) Filter articles by profile's news sources when using global key
        if resolved and articles:
            cfg = _get_config_for_profile(profile)
            news_cfg = cfg.get("news", {})
            # Build a set of expected source identifiers from the profile's config
            expected_subs = {s.lower() for s in news_cfg.get("reddit_subreddits", [])}
            expected_rss = set()
            for url in news_cfg.get("rss_feeds", []):
                # Extract domain-like identifier from RSS URL
                import re as _re
                m = _re.search(r'//(?:www\.)?([^/]+)', url)
                if m:
                    expected_rss.add(m.group(1).lower().replace(".", "_"))

            # CoinGecko trending is crypto-specific
            crypto_profiles = {"coinbase", "crypto"}
            has_coingecko = resolved in crypto_profiles

            def _matches_profile(article: dict) -> bool:
                tags = {t.lower() for t in article.get("tags", [])}
                source = (article.get("source") or "").lower()
                title = (article.get("title") or "").lower()
                # Match by subreddit tag
                if tags & expected_subs:
                    return True
                # Match by RSS source tag
                if tags & expected_rss:
                    return True
                # Match coingecko for crypto profiles
                if has_coingecko and "coingecko" in tags:
                    return True
                # Match by source field containing expected identifiers
                for sub in expected_subs:
                    if sub in source:
                        return True
                # Match by human-followed pair symbols appearing in tags or title
                if followed_symbols:
                    if tags & followed_symbols:
                        return True
                    for sym in followed_symbols:
                        if sym in title:
                            return True
                return False

            # Only filter if we have source config or followed symbols; otherwise show all
            if expected_subs or expected_rss or followed_symbols:
                articles = [a for a in articles if _matches_profile(a)]

        articles = articles[:count]
        return {"articles": articles, "count": len(articles), "source": "redis"}
    except Exception as exc:
        logger.warning(f"news endpoint error: {exc}")
        return {"articles": [], "count": 0, "source": "error"}


# ---------------------------------------------------------------------------
# REST — Watchlist / Scan Results
# ---------------------------------------------------------------------------

@app.get("/api/watchlist", summary="Active pairs watchlist with scan results")
def get_watchlist(
    profile: str = Query("", description="Exchange profile"),
    db=Depends(_get_profile_db),
):
    """Returns the latest universe scan results + active pair configuration."""
    config = _get_config_for_profile(profile)
    qc = _quote_currency_for(profile)
    try:
        scan = db.get_latest_scan_results()
        pairs = config.get("trading", {}).get("pairs", [])

        # Get live prices for active pairs (filled after we know human-followed too)
        live_prices = {}

        # Parse scan results JSON
        scan_data = None
        if scan:
            scan_data = dict(scan)
            for field in ("results_json", "top_movers"):
                if isinstance(scan_data.get(field), str):
                    try:
                        scan_data[field] = json.loads(scan_data[field])
                    except Exception:
                        pass
            # Ensure top_movers is always a list (old data may be a plain string)
            if not isinstance(scan_data.get("top_movers"), list):
                scan_data["top_movers"] = []

            # Filter scan results by quote currency if a specific profile is selected
            if qc:
                suffix = f"-{qc.upper()}"
                if isinstance(scan_data.get("results_json"), dict):
                    scan_data["results_json"] = {
                        k: v for k, v in scan_data["results_json"].items()
                        if k.upper().endswith(suffix)
                    }
                if isinstance(scan_data.get("top_movers"), list):
                    scan_data["top_movers"] = [
                        m for m in scan_data["top_movers"]
                        if isinstance(m, dict) and m.get("pair", "").upper().endswith(suffix)
                    ]

        # Filter active pairs by quote currency
        if qc:
            suffix = f"-{qc.upper()}"
            pairs = [p for p in pairs if p.upper().endswith(suffix)]
            live_prices = {k: v for k, v in live_prices.items() if k.upper().endswith(suffix)}

        # Build follow status for each pair (LLM-chosen pairs come from config)
        follows = db.get_pair_follows(quote_currency=qc)
        # Index follows by pair → set of followed_by values
        follow_map: dict[str, set[str]] = {}
        for f in follows:
            follow_map.setdefault(f["pair"].upper(), set()).add(f["followed_by"])

        # Config pairs are considered LLM-followed
        for p in pairs:
            follow_map.setdefault(p.upper(), set()).add("llm")

        # Human-followed pairs that aren't in the config list
        human_followed = sorted({
            p for p, sources in follow_map.items()
            if "human" in sources and p not in {x.upper() for x in pairs}
        })

        # Build combined pair info list
        all_pairs = list(dict.fromkeys(pairs + human_followed))  # preserve order, dedup

        # Fetch live prices for ALL pairs (config + human-followed), capped at 30
        if _exchange_client and all_pairs:
            for pair in all_pairs[:30]:
                try:
                    live_prices[pair] = _exchange_client.get_current_price(pair)
                except Exception:
                    pass

        pair_info = []
        for p in all_pairs:
            sources = follow_map.get(p.upper(), set())
            pair_info.append({
                "pair": p,
                "followed_by_llm": "llm" in sources,
                "followed_by_human": "human" in sources,
                "price": live_prices.get(p),
            })

        return _sanitize_floats({
            "active_pairs": pairs,
            "human_followed_pairs": human_followed,
            "pair_info": pair_info,
            "live_prices": live_prices,
            "scan": scan_data,
            "pair_count": len(all_pairs),
        })
    except Exception as exc:
        logger.exception("watchlist error")
        raise HTTPException(status_code=500, detail=str(exc))


class _FollowPairBody(_BaseModel):
    pair: str
    exchange: str = ""  # auto-detected from pair when empty


@app.post("/api/watchlist/follow", summary="Follow a pair (human)")
def follow_pair(body: _FollowPairBody, profile: str = Query(""), db=Depends(_get_profile_db)):
    """Add a pair to the human-curated watchlist.

    This does NOT affect the autonomous LLM's trading decisions or pair
    selection — it only controls what the dashboard shows in the watchlist
    and news feed.
    """
    pair = body.pair.upper().strip()
    if not pair or "-" not in pair:
        raise HTTPException(status_code=400, detail=f"Invalid pair format: {pair!r}")

    # Detect exchange from profile or pair suffix
    resolved = _resolve_profile(profile)
    qc = _quote_currency_for(profile)
    _qc_exchange_map = {"EUR": "coinbase", "SEK": "nordnet", "USD": "ibkr"}
    exchange = body.exchange or _qc_exchange_map.get((qc or "").upper(), resolved or "coinbase")

    db.follow_pair(pair=pair, followed_by="human", exchange=exchange)
    return {"ok": True, "pair": pair, "followed_by": "human", "exchange": exchange}


@app.delete("/api/watchlist/follow/{pair}", summary="Unfollow a pair (human)")
def unfollow_pair(pair: str, db=Depends(_get_profile_db)):
    """Remove a pair from the human-curated watchlist.

    Only removes the human follow — LLM follows (config pairs) are unaffected.
    """
    pair = pair.upper().strip()
    deleted = db.unfollow_pair(pair=pair, followed_by="human")
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Not following {pair!r}")
    return {"ok": True, "pair": pair, "unfollowed": True}


# ---------------------------------------------------------------------------
# REST — Prediction Accuracy (Predictions vs Actuals)
# ---------------------------------------------------------------------------

@app.get("/api/predictions/accuracy", summary="Signal prediction accuracy vs actual price movements")
def get_prediction_accuracy(
    days: int = Query(30, ge=1, le=365),
    profile: str = Query(""),
    db=Depends(_get_profile_db),
):
    """Compare market analyst signal predictions with actual price outcomes.

    Automatically filters by the profile's quote currency so that, e.g.,
    the crypto/EUR profile only shows -EUR pairs.
    """
    try:
        qc = _quote_currency_for(profile)
        result = db.get_prediction_accuracy(days=days, quote_currency=qc)
        return _sanitize_floats(result)
    except Exception as exc:
        logger.exception("prediction accuracy error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/predictions/tracked-pairs", summary="Pairs the LLM system actively tracks")
def get_tracked_pairs(profile: str = Query(""), db=Depends(_get_profile_db)):
    """Return pairs the LLM has analyzed recently, grouped by asset class.

    Automatically filters by the profile's quote currency.
    """
    try:
        qc = _quote_currency_for(profile)
        return db.get_tracked_pairs(quote_currency=qc)
    except Exception as exc:
        logger.exception("tracked pairs error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/portfolio/cleanup", summary="One-time cleanup of bad portfolio snapshots")
def cleanup_portfolio(db=Depends(_get_profile_db)):
    """Delete anomalous portfolio snapshots (zero-value and paper-mode bleed-through)."""
    try:
        deleted = db.cleanup_bad_snapshots()
        return {"deleted": deleted, "status": "ok"}
    except Exception as exc:
        logger.exception("portfolio cleanup error")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# REST — Candle data for charts
# ---------------------------------------------------------------------------

@app.get("/api/candles", summary="OHLCV candle data for a trading pair")
def get_candles(
    pair: str = Query(..., description="Trading pair, e.g. BTC-EUR"),
    granularity: str = Query("ONE_HOUR", description="Candle granularity"),
    limit: int = Query(200, ge=10, le=1000),
):
    """Returns OHLCV candle data from the exchange for charting."""
    if not _exchange_client:
        raise HTTPException(status_code=503, detail="Exchange client not available")
    try:
        candles = _exchange_client.get_candles(pair, granularity=granularity, limit=limit)
        if not candles:
            return {"candles": [], "pair": pair}
        return _sanitize_floats({"candles": candles, "pair": pair, "count": len(candles)})
    except Exception as exc:
        logger.warning(f"candles error for {pair}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# REST — HITL (Human-in-the-Loop) Trade Commands
# ---------------------------------------------------------------------------

@app.post("/api/trade/{pair}/command", summary="Send a trading command to the agent")
def send_trade_command(
    pair: str,
    action: str = Query(..., description="Command: liquidate, tighten_stop, pause"),
):
    """Publish a trade command via Redis for the orchestrator to execute.
    
    Supported actions:
    - liquidate: Market sell the entire position
    - tighten_stop: Move stop-loss to breakeven
    - pause: Exclude pair from trading
    """
    if action not in ("liquidate", "tighten_stop", "pause"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    # M10: validate pair format (e.g. BTC-USD)
    import re as _re
    if not _re.match(r'^[A-Z0-9]+-[A-Z0-9]+$', pair.upper()):
        raise HTTPException(status_code=400, detail=f"Invalid pair format: {pair}")
    pair = pair.upper()

    if not _redis_client:
        raise HTTPException(status_code=503, detail="Redis not available — cannot send commands")
    if not _DASHBOARD_COMMAND_SIGNING_KEY:
        raise HTTPException(
            status_code=503,
            detail="Dashboard command signing key not configured",
        )

    try:
        import uuid as _uuid

        ts = datetime.now(timezone.utc).isoformat()
        nonce = _uuid.uuid4().hex
        command = {
            "action": action,
            "pair": pair,
            "ts": ts,
            "source": "dashboard",
            "nonce": nonce,
        }
        command["signature"] = _sign_dashboard_command(
            action=action,
            pair=pair,
            ts=ts,
            source=command["source"],
            nonce=nonce,
        )
        # Push to processing queue (orchestrator polls this)
        _redis_client.rpush("dashboard:commands_queue", json.dumps(command))
        # Also publish for real-time subscribers
        _redis_client.publish("dashboard:commands", json.dumps(command))
        # Audit trail
        _redis_client.lpush("dashboard:command_history", json.dumps(command))
        _redis_client.ltrim("dashboard:command_history", 0, 99)

        logger.info(f"📤 HITL command sent: {action} for {pair}")
        return {"status": "command_sent", "action": action, "pair": pair}
    except Exception as exc:
        logger.exception("HITL command error")
        raise HTTPException(status_code=500, detail="Internal error processing command")


@app.get("/api/trade/commands/history", summary="Recent HITL command history")
def get_command_history(limit: int = Query(20, ge=1, le=100)):
    """Returns recent dashboard-initiated commands."""
    if not _redis_client:
        return {"commands": []}
    try:
        raw_list = _redis_client.lrange("dashboard:command_history", 0, limit - 1)
        commands = []
        for raw in raw_list:
            try:
                commands.append(json.loads(raw))
            except Exception:
                pass
        return {"commands": commands}
    except Exception:
        return {"commands": []}


@app.get("/api/trailing-stops", summary="Active trailing stop states")
def get_trailing_stops(profile: str = Query("")):
    """Returns trailing stop data from Redis (published by the orchestrator).

    When a profile is active, filters trailing stops to only include pairs
    that match the profile's quote currency.
    """
    if not _redis_client:
        return {"stops": {}, "source": "unavailable"}
    try:
        raw = _redis_client.get("trailing_stops:state")
        if not raw:
            return {"stops": {}, "source": "redis_empty"}
        stops = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        # Filter by profile quote currency
        qc = _quote_currency_for(profile)
        if qc and isinstance(stops, dict):
            stops = {
                pair: data for pair, data in stops.items()
                if pair.upper().endswith(f"-{qc.upper()}")
            }
        return _sanitize_floats({"stops": stops, "source": "redis"})
    except Exception as exc:
        logger.warning(f"trailing-stops error: {exc}")
        return {"stops": {}, "source": "error"}


def _update_env_file(env_path: str, updates: dict[str, str]) -> None:
    """Update or append env vars in a .env file, preserving existing content.

    Uses atomic write (write to temp file → rename) to avoid partial writes
    corrupting the .env file on crash or power loss.
    """
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                # Cycle-4 fix: strip newlines to prevent .env injection
                safe_val = str(updates[key]).replace("\n", "").replace("\r", "")
                new_lines.append(f"{key}={safe_val}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append any keys that weren't already in the file
    remaining = set(updates.keys()) - updated_keys
    if remaining:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append("\n# LLM Provider API Keys (added by dashboard)\n")
        for key in sorted(remaining):
            safe_val = str(updates[key]).replace("\n", "").replace("\r", "")
            new_lines.append(f"{key}={safe_val}\n")

    # Atomic write: write to temp file in same directory, then rename
    env_dir = os.path.dirname(os.path.abspath(env_path))
    fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, os.path.abspath(env_path))
    except BaseException:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Static frontend (React/Vite build)
# The Dockerfile copies the built frontend into src/dashboard/static/.
# In local dev the directory won't exist — fall back to API-only mode.
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"

if _STATIC_DIR.is_dir():
    # Serve hashed JS/CSS bundles at /assets (Vite default output dir)
    app.mount("/assets", StaticFiles(directory=str(_STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """Catch-all: return index.html so React Router handles client-side paths."""
        # M5 fix: don't serve SPA HTML for missing API routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        index = _STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend not built")

if __name__ == "__main__":
    import uvicorn
    import yaml
    import os
    from src.utils.stats import StatsDB
    
    config_path = os.path.join("config", "settings.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    else:
        config = {}
        
    db = StatsDB(config.get("database", {}).get("stats_db", "data/stats.db"))
    
    redis_url = os.environ.get("REDIS_URL")
    redis_client = None
    if redis_url:
        import redis
        redis_client = redis.Redis.from_url(redis_url)

    app = create_app(stats_db=db, redis_client=redis_client, temporal_client=None, config=config)
    
    port = int(config.get("dashboard", {}).get("port", 8090))
    print(f"🚀 Starting Dashboard Server on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
