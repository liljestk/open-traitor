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
import hmac
import json
import math
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator, Optional

from pathlib import Path
import io

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from src.utils.logger import get_logger

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
_coinbase_client = None   # CoinbaseClient instance (optional, for price lookups)

_ws_connections: list[WebSocket] = []


def set_globals(*, stats_db, redis_client=None, temporal_client=None, config: dict = {}):
    """Inject shared services.  Called from main.py before uvicorn starts."""
    global _stats_db, _redis_client, _temporal_client, _config, _coinbase_client
    _stats_db = stats_db
    _redis_client = redis_client
    _temporal_client = temporal_client
    _config = config
    # Spin up a read-only Coinbase client for live price lookups (market data only)
    try:
        from src.core.coinbase_client import CoinbaseClient
        key_file = os.environ.get("COINBASE_KEY_FILE", "")
        api_key = os.environ.get("COINBASE_API_KEY", "")
        api_secret = os.environ.get("COINBASE_API_SECRET", "")
        _coinbase_client = CoinbaseClient(
            api_key=api_key or None,
            api_secret=api_secret or None,
            key_file=key_file or None,
            paper_mode=True,  # read-only; no real orders from the dashboard
        )
        logger.info("✅ Dashboard Coinbase price client ready")
    except Exception as e:
        logger.warning(f"⚠️ Dashboard Coinbase client not available: {e}")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(*, stats_db=None, redis_client=None, temporal_client=None, config: dict = {}) -> FastAPI:
    set_globals(
        stats_db=stats_db,
        redis_client=redis_client,
        temporal_client=temporal_client,
        config=config,
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

    # --- Initialise Coinbase price client if not already set -----------------
    if _coinbase_client is None:
        try:
            from src.core.coinbase_client import CoinbaseClient
            key_file = os.environ.get("COINBASE_KEY_FILE", "")
            api_key = os.environ.get("COINBASE_API_KEY", "")
            api_secret = os.environ.get("COINBASE_API_SECRET", "")
            globals()["_coinbase_client"] = CoinbaseClient(
                api_key=api_key or None,
                api_secret=api_secret or None,
                key_file=key_file or None,
                paper_mode=True,
            )
            logger.info("✅ Dashboard Coinbase price client ready")
        except Exception as e:
            logger.warning(f"⚠️ Dashboard Coinbase client not available: {e}")

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
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,       # must be False when allow_origins contains "*"
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API Key Authentication (active only when DASHBOARD_API_KEY env var is set)
# ---------------------------------------------------------------------------

_DASHBOARD_API_KEY: str = os.environ.get("DASHBOARD_API_KEY", "")
if not _DASHBOARD_API_KEY:
    logger.warning(
        "⚠️  DASHBOARD_API_KEY not set — the dashboard API is open to all network "
        "clients. Set this env var to require X-API-Key header authentication."
    )


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """Require X-API-Key on /api/* and /ws/* when DASHBOARD_API_KEY is configured.

    WebSocket upgrade requests are also HTTP requests, but browsers cannot set
    custom headers on ``new WebSocket(url)``.  We therefore accept the key as a
    ``?api_key=`` query parameter as a secondary credential path, **only** for
    WebSocket paths where the header mechanism is unavailable.
    """
    if _DASHBOARD_API_KEY and (
        request.url.path.startswith("/api/")
        or request.url.path.startswith("/ws/")
    ):
        # Browsers set Sec-Fetch-Site automatically; same-origin means the
        # request comes from the SPA served by this very server — safe to
        # allow without an explicit API key.
        sec_fetch_site = request.headers.get("Sec-Fetch-Site", "")
        if sec_fetch_site != "same-origin":
            # Primary: X-API-Key header (server-side / curl / fetch clients)
            api_key = request.headers.get("X-API-Key", "")
            if not api_key and request.url.path.startswith("/ws/"):
                # Fallback for browser WebSocket: ?api_key=... query parameter
                api_key = request.query_params.get("api_key", "")
            # Constant-time comparison prevents timing-oracle attacks
            if not hmac.compare_digest(api_key, _DASHBOARD_API_KEY):
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _require_db():
    if _stats_db is None:
        raise HTTPException(status_code=503, detail="Stats DB not initialised")
    return _stats_db


def _sanitize_floats(obj):
    """Recursively replace inf/nan floats with None so JSON serialisation succeeds."""
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return None
    return obj


def _fresh_conn() -> sqlite3.Connection:
    """Open a fresh SQLite connection for this request.

    Avoids relying on the thread-local connection inside StatsDB, which is
    not safe to share across FastAPI's async threadpool workers.
    """
    _require_db()
    conn = sqlite3.connect(_stats_db._db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# REST — Cycles
# ---------------------------------------------------------------------------

@app.get("/api/cycles", summary="List trading cycles (Cycle Explorer)")
def list_cycles(
    pair: Optional[str] = Query(None, description="Filter by pair e.g. BTC-USD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Returns a paginated list of trading cycles with outcome summary.
    Each item represents one unique `cycle_id` across all agent spans.
    """
    db = _require_db()
    cycles = db.get_cycles(pair=pair, limit=limit, offset=offset)
    return {"cycles": cycles, "limit": limit, "offset": offset, "count": len(cycles)}


@app.get("/api/cycles/{cycle_id}", summary="Full span chain for one cycle (Playback)")
def get_cycle(cycle_id: str):
    """
    Returns the complete trace: all agent spans with token counts, latency,
    LLM prompt/output, plus the resulting trade (if any).
    Powers the animated Waterfall timeline on the Playback page.
    """
    db = _require_db()
    cycle = db.get_cycle_full(cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail=f"Cycle {cycle_id!r} not found")
    return cycle


# ---------------------------------------------------------------------------
# REST — Trades & Events
# ---------------------------------------------------------------------------

@app.get("/api/trades", summary="List raw trades log")
def list_trades(
    pair: Optional[str] = Query(None, description="Filter by pair e.g. BTC-USD"),
    hours: int = Query(24 * 7, ge=1, description="Hours of history to fetch"),
    limit: int = Query(500, ge=1, le=5000)
):
    """Returns a list of raw trades from the database, newest first."""
    db = _require_db()
    trades = db.get_trades(hours=hours, pair=pair, limit=limit)
    return {"trades": trades, "count": len(trades)}

@app.get("/api/trades/export", summary="Export trades to CSV")
def export_trades(hours: int = Query(24 * 30, ge=1)):
    """Exports raw trades to a downloadable CSV file."""
    db = _require_db()
    trades = db.get_trades(hours=hours, limit=100000)
    
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
    limit: int = Query(500, ge=1, le=5000)
):
    """Returns a list of system events/logs from the database."""
    db = _require_db()
    events = db.get_events(hours=hours, event_type=event_type, limit=limit)
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
def get_stats_summary():
    """High-level stats: win-rate, PnL, active pairs, recent activity."""
    _require_db()
    conn = _fresh_conn()
    try:
        # Overall trade stats
        trade_row = conn.execute(
            """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(pnl), 2) as total_pnl,
                ROUND(AVG(pnl), 2) as avg_pnl,
                ROUND(MAX(pnl), 2) as best_trade,
                ROUND(MIN(pnl), 2) as worst_trade
               FROM trades
               WHERE pnl IS NOT NULL"""
        ).fetchone()

        # Last 24h
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent_row = conn.execute(
            """SELECT
                COUNT(*) as trades_24h,
                ROUND(SUM(pnl), 2) as pnl_24h
               FROM trades
               WHERE ts >= ? AND pnl IS NOT NULL""",
            (cutoff_24h,),
        ).fetchone()

        # Active pairs
        pairs_row = conn.execute(
            "SELECT COUNT(DISTINCT pair) as active_pairs FROM agent_reasoning WHERE ts >= ?",
            (cutoff_24h,),
        ).fetchone()

        # Cycle count last 24h
        cycle_row = conn.execute(
            "SELECT COUNT(DISTINCT cycle_id) as cycles_24h FROM agent_reasoning WHERE ts >= ?",
            (cutoff_24h,),
        ).fetchone()

        # Latest portfolio snapshot
        snapshot = conn.execute(
            """SELECT portfolio_value, total_pnl, ts
               FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"""
        ).fetchone()

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
        stats["currency"] = _config.get("trading", {}).get("quote_currency", "EUR")
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
    """Fetch the current price for a pair via CoinbaseClient (or return 0 on failure)."""
    if _coinbase_client:
        try:
            return _coinbase_client.get_current_price(pair)
        except Exception:
            pass
    return 0.0


@app.get("/api/products", summary="List tradable Coinbase products")
def list_products():
    """Return all online, tradable products from Coinbase Advanced Trade.

    Response: ``{"products": [{"id": "BTC-EUR", "base": "BTC", "quote": "EUR"}, ...]}``
    Each entry is a product that is *online* and not disabled on the exchange.
    """
    if not _coinbase_client or not _coinbase_client._rest_client:
        return {"products": []}

    try:
        resp = _coinbase_client._rest_client.get_products()
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
def create_simulated_trade(body: SimulatedTradeCreate):
    """
    Opens a new paper simulation. The server fetches the live entry price,
    computes the implied quantity, and persists the record.

    For EUR→Crypto: `from_currency=EUR`, `pair=BTC-EUR`
    For Crypto→Crypto: `from_currency=BTC`, `pair=ETH-BTC` (or similar)
    """
    db = _require_db()
    pair = body.pair.upper().strip()
    from_currency = body.from_currency.upper().strip()

    # Derive to_currency from pair (e.g. BTC-EUR → BTC when buying with EUR)
    parts = pair.split("-")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail=f"Invalid pair format: {pair!r}")
    base, quote = parts
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
):
    """
    Returns all simulated trades. For open ones, the current price is fetched
    live and PnL (absolute + %) is computed on the fly.
    """
    db = _require_db()
    rows = db.get_simulated_trades(include_closed=include_closed)

    # Enrich open rows with live PnL
    for row in rows:
        if row["status"] == "open":
            current_price = _get_live_price(row["pair"])
            if current_price > 0:
                pnl_abs = (current_price - row["entry_price"]) * row["quantity"]
                pnl_pct = ((current_price / row["entry_price"]) - 1) * 100 if row["entry_price"] > 0 else 0.0
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
def close_simulated_trade_route(sim_id: int):
    """
    Closes an open simulation by recording the current live price as the
    close price and computing the final PnL.
    """
    db = _require_db()

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


# ---------------------------------------------------------------------------
# REST — Strategic context (planning)
# ---------------------------------------------------------------------------

@app.get("/api/strategic", summary="Recent strategic plans from Temporal workflows")
def get_strategic(
    horizon: Optional[str] = Query(None, description="daily | weekly | monthly"),
    limit: int = Query(20, ge=1, le=100),
):
    """Returns the most recent planning workflow outputs with Temporal + Langfuse IDs."""
    _require_db()
    conn = _fresh_conn()
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
        events = []
        async for event in await handle.fetch_history():
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
    await websocket.accept()
    _ws_connections.append(websocket)
    logger.info(f"WS client connected ({len(_ws_connections)} total)")
    try:
        while True:
            # Keep connection alive; events are pushed by _redis_subscriber
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping", "ts": _utcnow()})
    except WebSocketDisconnect:
        pass
    finally:
        # Guard: the Redis subscriber may have already removed this socket
        if websocket in _ws_connections:
            _ws_connections.remove(websocket)
        logger.info(f"WS client disconnected ({len(_ws_connections)} remaining)")


async def _redis_subscriber():
    """
    Background task: subscribes to Redis `llm:events` channel and
    broadcasts each message to all connected WebSocket clients.
    """
    if _redis_client is None:
        return

    import redis.asyncio as aioredis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    # Use a separate async connection so we don't block the sync Redis client
    async_redis = aioredis.from_url(redis_url)
    pubsub = async_redis.pubsub()
    await pubsub.subscribe("llm:events")
    logger.info("Subscribed to Redis llm:events")

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                payload = json.loads(message["data"])
            except Exception:
                continue

            dead = []
            for ws in list(_ws_connections):
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in _ws_connections:
                    _ws_connections.remove(ws)
    except asyncio.CancelledError:
        await pubsub.unsubscribe("llm:events")
        await async_redis.aclose()


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
    host = _config.get("dashboard", {}).get("langfuse_host", "http://localhost:3000")
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
