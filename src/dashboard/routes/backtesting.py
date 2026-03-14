"""
Backtesting dashboard API routes.

Exposes historical backtest results, WFO optimization history,
parameter promotions, and on-demand backtest triggering.
All endpoints respect domain separation via the ``exchange`` column.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, field_validator

import src.dashboard.deps as deps
from src.dashboard import auth
from src.utils.logger import get_logger

logger = get_logger("dashboard.backtesting")

router = APIRouter()

# ---------------------------------------------------------------------------
# DB schema — created lazily on first request
# ---------------------------------------------------------------------------

_SCHEMA_CREATED = False

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id SERIAL PRIMARY KEY,
    run_ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    pair TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 60,
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    total_return_pct REAL DEFAULT 0,
    sharpe_ratio REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    alpha REAL DEFAULT 0,
    is_wfo BOOLEAN DEFAULT FALSE,
    wfo_wfe REAL DEFAULT NULL
)
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_bt_runs_exchange ON backtest_runs(exchange)",
    "CREATE INDEX IF NOT EXISTS idx_bt_runs_pair ON backtest_runs(pair)",
    "CREATE INDEX IF NOT EXISTS idx_bt_runs_ts ON backtest_runs(run_ts)",
]


def _ensure_schema(db) -> bool:
    """Create backtest_runs table if it doesn't exist (idempotent).

    Returns True if the table is ready, False if creation failed.
    """
    global _SCHEMA_CREATED
    if _SCHEMA_CREATED:
        return True
    try:
        with db._get_conn() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            for idx_sql in _CREATE_INDEXES_SQL:
                conn.execute(idx_sql)
            conn.commit()
        _SCHEMA_CREATED = True
        return True
    except Exception as e:
        logger.warning(f"Schema creation skipped: {e}")
        return False


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

# Pair must be a valid crypto pair (XXX-YYY) or equity ticker (AAPL-USD,
# ABB.ST-SEK, VOLV-B.ST-SEK, AAPL@SMART).  Allows alphanumerics, dots,
# dashes, and @ for IB routing hints.
_VALID_PAIR_RE = re.compile(r'^[A-Z0-9][A-Z0-9.\-@]{0,29}$')


def _validate_pair_format(pair: str) -> str:
    """Validate and normalise a trading pair. Rejects injection payloads."""
    cleaned = pair.strip().upper()
    if not cleaned or not _VALID_PAIR_RE.match(cleaned):
        raise ValueError(f"Invalid trading pair format: must match TICKER or TICKER-QUOTE")
    return cleaned


class BacktestTriggerRequest(BaseModel):
    pair: str = Field(..., min_length=1, max_length=30)
    days: int = Field(60, ge=7, le=365)
    position_size_pct: float = Field(0.10, ge=0.01, le=0.50)
    trailing_stop_pct: float = Field(0.03, ge=0.005, le=0.20)
    entry_threshold: float = Field(0.4, ge=0.1, le=0.9)
    fee_pct: float = Field(0.006, ge=0.0, le=0.05)
    slippage_pct: float = Field(0.001, ge=0.0, le=0.05)

    @field_validator('pair')
    @classmethod
    def pair_must_be_valid(cls, v: str) -> str:
        return _validate_pair_format(v)


# ---------------------------------------------------------------------------
# GET /api/backtesting/history — list past backtest runs
# ---------------------------------------------------------------------------

@router.get("/api/backtesting/history", summary="List past backtest runs")
def get_backtest_history(
    days: int = Query(90, ge=1, le=365),
    pair: str = Query("", description="Optional pair filter"),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Return a list of past backtest run summaries, filtered by exchange."""
    try:
        if not _ensure_schema(db):
            return {"runs": []}
        resolved = deps.resolve_profile(profile) or None

        sql = """
            SELECT id, run_ts, pair, exchange, days,
                   total_return_pct, sharpe_ratio, win_rate,
                   total_trades, max_drawdown_pct, alpha,
                   is_wfo, wfo_wfe
            FROM backtest_runs
            WHERE run_ts >= to_char((now() AT TIME ZONE 'UTC' - interval '1 day' * %s), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        """
        params: list = [days]

        if resolved:
            sql += " AND exchange = %s"
            params.append(resolved)

        if pair:
            validated = _validate_pair_format(pair)
            sql += " AND pair = %s"
            params.append(validated)

        sql += " ORDER BY run_ts DESC LIMIT 200"

        with db._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        return {"runs": [dict(r) for r in rows]}
    except Exception as exc:
        if "does not exist" in str(exc):
            return {"runs": []}
        logger.exception("backtest history error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# GET /api/backtesting/run/{run_id} — detailed results for one run
# ---------------------------------------------------------------------------

@router.get("/api/backtesting/run/{run_id}", summary="Get detailed backtest results")
def get_backtest_run(
    run_id: int,
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Return full backtest results including equity curve, trades, and cost sensitivity."""
    try:
        _ensure_schema(db)
        resolved = deps.resolve_profile(profile) or None

        sql = """SELECT id, run_ts, pair, exchange, days,
                       params_json, result_json,
                       total_return_pct, sharpe_ratio, win_rate,
                       total_trades, max_drawdown_pct, alpha,
                       is_wfo, wfo_wfe
                FROM backtest_runs WHERE id = %s"""
        params: list = [run_id]
        if resolved:
            sql += " AND exchange = %s"
            params.append(resolved)

        with db._get_conn() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Backtest run not found")

        result = dict(row)
        # Parse stored JSON fields
        for json_field in ("params_json", "result_json"):
            if json_field in result and isinstance(result[json_field], str):
                try:
                    result[json_field] = json.loads(result[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass

        return deps.sanitize_floats(result)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("backtest run detail error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# DELETE /api/backtesting/run/{run_id} — delete a backtest run
# ---------------------------------------------------------------------------

@router.delete("/api/backtesting/run/{run_id}", summary="Delete a backtest run")
def delete_backtest_run(
    run_id: int,
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Permanently delete a single backtest run by ID."""
    try:
        _ensure_schema(db)
        resolved = deps.resolve_profile(profile) or None

        sql = "DELETE FROM backtest_runs WHERE id = %s"
        params: list = [run_id]
        if resolved:
            sql += " AND exchange = %s"
            params.append(resolved)

        with db._get_conn() as conn:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Run not found")

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("backtest delete error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# GET /api/backtesting/wfo-history — WFO optimization results timeline
# ---------------------------------------------------------------------------

@router.get("/api/backtesting/wfo-history", summary="WFO optimization history")
def get_wfo_history(
    days: int = Query(90, ge=1, le=365),
    pair: str = Query("", description="Optional pair filter"),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Return WFO optimization results from backtest_runs where is_wfo=true."""
    try:
        if not _ensure_schema(db):
            return deps.sanitize_floats({"runs": []})
        resolved = deps.resolve_profile(profile) or None

        sql = """
            SELECT id, run_ts, pair, exchange, days,
                   total_return_pct, sharpe_ratio, win_rate,
                   total_trades, max_drawdown_pct, alpha,
                   wfo_wfe, result_json
            FROM backtest_runs
            WHERE is_wfo = TRUE
              AND run_ts >= to_char((now() AT TIME ZONE 'UTC' - interval '1 day' * %s), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        """
        params: list = [days]

        if resolved:
            sql += " AND exchange = %s"
            params.append(resolved)

        if pair:
            validated = _validate_pair_format(pair)
            sql += " AND pair = %s"
            params.append(validated)

        sql += " ORDER BY run_ts DESC LIMIT 100"

        with db._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        results = []
        for r in rows:
            entry = dict(r)
            # Parse result_json to extract WFO-specific fields
            if isinstance(entry.get("result_json"), str):
                try:
                    entry["result_json"] = json.loads(entry["result_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(entry)

        return deps.sanitize_floats({"runs": results})
    except Exception as exc:
        if "does not exist" in str(exc):
            return deps.sanitize_floats({"runs": []})
        logger.exception("wfo history error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# GET /api/backtesting/promotions — parameter promotion/rollback timeline
# ---------------------------------------------------------------------------

@router.get("/api/backtesting/promotions", summary="Parameter promotion history")
def get_backtest_promotions(
    limit: int = Query(30, ge=1, le=200),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Return parameter promotions with optional exchange filtering.

    Pairs are implicitly exchange-scoped (crypto pairs contain -EUR/-USD,
    equity pairs are ticker symbols). When a profile is active, only
    show promotions for pairs belonging to that exchange domain.
    """
    try:
        resolved = deps.resolve_profile(profile) or None

        # parameter_promotions table doesn't have an exchange column,
        # so we filter by joining to known pairs or by pair pattern.
        # For safety, return all when no profile — users see their domain's data.
        sql = """
            SELECT run_ts, pair, param_name, old_value, new_value,
                   wfe, oos_sharpe, promoted, rolled_back,
                   rollback_ts, rollback_reason,
                   pre_promotion_accuracy, post_promotion_accuracy
            FROM parameter_promotions
            ORDER BY run_ts DESC
            LIMIT %s
        """
        params: list = [limit]

        with db._get_conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        promotions = [dict(r) for r in rows]

        # Client-side filter by domain heuristic if profile is set
        if resolved:
            if resolved.startswith("ibkr"):
                promotions = [p for p in promotions if _is_equity_pair(p.get("pair", ""))]
            else:
                promotions = [p for p in promotions if not _is_equity_pair(p.get("pair", ""))]

        return deps.sanitize_floats({"promotions": promotions})
    except Exception as exc:
        logger.exception("backtest promotions error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# POST /api/backtesting/trigger — run an on-demand backtest
# ---------------------------------------------------------------------------

@router.post("/api/backtesting/trigger", summary="Trigger an on-demand backtest")
def trigger_backtest(
    req: BacktestTriggerRequest,
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Run a backtest on historical candles for the given pair and save results.

    Uses the appropriate exchange client based on the active profile:
    - Crypto (coinbase): CoinbaseClient.get_candles()
    - Equity (ibkr): Yahoo Finance via equity_feed.get_candles()
    """
    try:
        _ensure_schema(db)
        resolved = deps.resolve_profile(profile) or "coinbase"
        is_equity = resolved.startswith("ibkr")

        # Fetch candles from the appropriate source
        candles = _fetch_candles_for_backtest(req.pair.upper(), req.days, is_equity)
        if not candles or len(candles) < 100:
            got = len(candles) if candles else 0
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No historical data found for {req.pair}. "
                    "The exchange may not support this pair, or the trading pair may be delisted."
                ) if got == 0 else (
                    f"Not enough data for {req.pair}: found {got} candles but need at least 100. "
                    f"Try increasing the backtest period beyond {req.days} days."
                )
            )

        # Run backtest
        config = deps.get_config_for_profile(profile)
        from src.backtesting.engine import BacktestEngine

        engine = BacktestEngine(
            config=config,
            initial_balance=10000.0,
            position_size_pct=req.position_size_pct,
            max_positions=3,
            trailing_stop_pct=req.trailing_stop_pct,
            fee_pct=req.fee_pct,
            slippage_pct=req.slippage_pct,
        )

        # Override entry threshold if the engine supports it
        engine._entry_threshold = req.entry_threshold

        result = engine.run(candles, pair=req.pair.upper())

        # Serialize result for storage
        result_dict = {
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_balance": result.initial_balance,
            "final_balance": result.final_balance,
            "total_return_pct": result.total_return_pct,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": result.win_rate,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "sortino_ratio": result.sortino_ratio,
            "calmar_ratio": result.calmar_ratio,
            "profit_factor": result.profit_factor,
            "largest_win": result.largest_win,
            "largest_loss": result.largest_loss,
            "avg_hold_time_hours": result.avg_hold_time_hours,
            "benchmark_return_pct": result.benchmark_return_pct,
            "alpha": result.alpha,
            "trades": result.trades,
            "equity_curve": result.equity_curve,
            "cost_sensitivity": result.cost_sensitivity,
        }

        params_dict = {
            "position_size_pct": req.position_size_pct,
            "trailing_stop_pct": req.trailing_stop_pct,
            "entry_threshold": req.entry_threshold,
            "fee_pct": req.fee_pct,
            "slippage_pct": req.slippage_pct,
        }

        # Persist to DB
        insert_sql = """
            INSERT INTO backtest_runs
                (pair, exchange, days, params_json, result_json,
                 total_return_pct, sharpe_ratio, win_rate,
                 total_trades, max_drawdown_pct, alpha)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, run_ts
        """
        with db._get_conn() as conn:
            row = conn.execute(insert_sql, (
                req.pair.upper(),
                resolved,
                req.days,
                json.dumps(params_dict),
                json.dumps(result_dict, default=str),
                result.total_return_pct,
                result.sharpe_ratio,
                result.win_rate,
                result.total_trades,
                result.max_drawdown_pct,
                result.alpha,
            )).fetchone()
            conn.commit()

        run_id = row["id"] if row else None
        run_ts = row["run_ts"] if row else None

        return deps.sanitize_floats({
            "id": run_id,
            "run_ts": run_ts,
            "pair": req.pair.upper(),
            "exchange": resolved,
            **result_dict,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("backtest trigger error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COINBASE_MAX_CANDLES = 300  # Coinbase API hard limit is 350; use 300 for safety


def _fetch_candles_for_backtest(pair: str, days: int, is_equity: bool) -> list[dict]:
    """Fetch historical candles from the appropriate source.

    For Coinbase, paginates in chunks of _COINBASE_MAX_CANDLES because the
    API rejects requests for more than 350 candles per call.
    """
    limit = days * 24  # hourly candles

    if is_equity:
        try:
            from src.core.equity_feed import get_candles as yf_candles
            candles = yf_candles(pair, granularity="ONE_HOUR", limit=limit)
            candles.sort(key=lambda c: int(c.get("time") or c.get("start") or 0) if isinstance(c, dict) else 0)
            return candles
        except Exception as e:
            logger.warning(f"Yahoo Finance candle fetch failed for {pair}: {e}")
            return []

    # Crypto — use Coinbase client, paginating to stay under API limit
    client = deps.exchange_client
    if client is None:
        raise HTTPException(status_code=503, detail="Exchange client not available")
    try:
        if limit <= _COINBASE_MAX_CANDLES:
            candles = client.get_candles(pair, granularity="ONE_HOUR", limit=limit)
            candles.sort(key=lambda c: int(c.get("start") or c.get("time") or 0) if isinstance(c, dict) else 0)
            return candles

        # Paginate: fetch chunks walking backwards from now
        all_candles: list[dict] = []
        remaining = limit
        end_ts = int(time.time())
        while remaining > 0:
            chunk_size = min(remaining, _COINBASE_MAX_CANDLES)
            start_ts = end_ts - (chunk_size * 3600)
            candles = client.get_candles(
                pair,
                granularity="ONE_HOUR",
                limit=chunk_size,
                start_time=start_ts,
                end_time=end_ts,
            )
            if not candles:
                break
            all_candles.extend(candles)
            remaining -= len(candles)
            end_ts = start_ts  # slide window back
            if len(candles) < chunk_size:
                break  # exchange returned less than requested → no more data

        # Sort by timestamp ascending (oldest first) for the engine
        all_candles.sort(key=lambda c: int(c.get("start") or c.get("time") or 0) if isinstance(c, dict) else 0)
        return all_candles
    except Exception as e:
        logger.warning(f"Coinbase candle fetch failed for {pair}: {e}")
        return []


def _is_equity_pair(pair: str) -> bool:
    """Heuristic: equity pairs are plain ticker symbols without a dash+crypto quote."""
    if not pair:
        return False
    crypto_quotes = {"USD", "EUR", "GBP", "USDT", "USDC", "BTC", "ETH"}
    parts = pair.split("-")
    if len(parts) == 2 and parts[1] in crypto_quotes:
        return False  # Crypto pair like BTC-EUR
    return True  # Likely equity ticker


# ---------------------------------------------------------------------------
# GET /api/backtesting/pairs — followed pairs available for backtesting
# ---------------------------------------------------------------------------

@router.get("/api/backtesting/pairs", summary="Pairs available for backtesting")
def get_backtest_pairs(
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Return pairs the user/LLM/config actively follows for the current profile.

    Merges config pairs, human-followed, and LLM-followed. Each pair includes
    its follow source and latest price when available.
    Domain-separated: crypto profile → crypto pairs only, equity → equity only.
    """
    try:
        config = deps.get_config_for_profile(profile)
        resolved = deps.resolve_profile(profile) or "coinbase"
        qc = deps.quote_currency_for(profile)

        # Config pairs
        config_pairs = config.get("trading", {}).get("pairs", [])

        # DB follows (human + LLM)
        all_follows = db.get_pair_follows(
            exchange=resolved, quote_currency=qc
        )

        # Build follow map: pair → set of sources
        follow_map: dict[str, set[str]] = {}
        for f in all_follows:
            follow_map.setdefault(f["pair"].upper(), set()).add(f["followed_by"])

        # Universe scan pairs (actively scanned by the agent)
        scan_pairs: list[str] = []
        try:
            scan = db.get_latest_scan_results(exchange=resolved)
            if scan:
                import json as _json
                rj = scan.get("results_json", {})
                if isinstance(rj, str):
                    rj = _json.loads(rj)
                if isinstance(rj, dict):
                    scan_pairs = list(rj.keys())
        except Exception:
            pass

        # Merge unique pairs: config → DB follows → scan universe
        config_upper = {p.upper() for p in config_pairs}
        follow_upper = {f["pair"].upper() for f in all_follows}
        merged = config_pairs + [
            f["pair"] for f in all_follows if f["pair"].upper() not in config_upper
        ]
        seen = config_upper | follow_upper
        for sp in scan_pairs:
            if sp.upper() not in seen:
                merged.append(sp)
                seen.add(sp.upper())
        all_pair_names = list(dict.fromkeys(merged))

        # Filter by quote currency
        if qc:
            suffixes = [f"-{c.upper()}" for c in (qc if isinstance(qc, list) else [qc])]
            if not deps.is_equity_profile(resolved):
                all_pair_names = [
                    p for p in all_pair_names
                    if any(p.upper().endswith(s) for s in suffixes)
                ]

        # Fetch last-run info from backtest_runs for these pairs
        _ensure_schema(db)
        last_runs: dict[str, dict] = {}
        if all_pair_names:
            placeholders = ", ".join(["%s"] * len(all_pair_names))
            sql = f"""
                SELECT DISTINCT ON (pair) pair, run_ts, total_return_pct, sharpe_ratio
                FROM backtest_runs
                WHERE pair IN ({placeholders}) AND exchange = %s
                ORDER BY pair, run_ts DESC
            """
            params = [p.upper() for p in all_pair_names] + [resolved]
            try:
                with db._get_conn() as conn:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    last_runs[r["pair"]] = {
                        "last_run_ts": r["run_ts"],
                        "last_return_pct": r["total_return_pct"],
                        "last_sharpe": r["sharpe_ratio"],
                    }
            except Exception:
                pass  # table may not exist yet

        # Build result
        scan_upper = {sp.upper() for sp in scan_pairs}
        pairs_out = []
        for p in all_pair_names:
            sources = follow_map.get(p.upper(), set())
            is_config = p.upper() in config_upper
            in_scan = p.upper() in scan_upper
            entry = {
                "pair": p,
                "source": (
                    "config" if is_config
                    else "human" if "human" in sources
                    else "llm" if "llm" in sources
                    else "scan" if in_scan
                    else "config"
                ),
                "followed_by_human": "human" in sources,
                "followed_by_llm": "llm" in sources,
                "is_config_pair": is_config,
                **last_runs.get(p.upper(), {}),
            }
            pairs_out.append(entry)

        return deps.sanitize_floats({"pairs": pairs_out, "exchange": resolved})
    except Exception as exc:
        logger.exception("backtest pairs error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# WebSocket /ws/backtest — live backtest progress streaming
# ---------------------------------------------------------------------------

# In-flight backtest sessions: token → state dict
_backtest_sessions: dict[str, dict] = {}
_backtest_lock = threading.Lock()

_MAX_BACKTEST_WS = 5  # max concurrent backtest WS connections


# ---------------------------------------------------------------------------
# GET /api/backtesting/run/{run_id}/interpretation — LLM analysis of results
# ---------------------------------------------------------------------------

@router.get("/api/backtesting/run/{run_id}/interpretation", summary="AI interpretation of backtest results")
async def get_backtest_interpretation(
    run_id: int,
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Generate an LLM-powered plain-language interpretation of a backtest run."""
    try:
        _ensure_schema(db)
        resolved = deps.resolve_profile(profile) or None

        sql = """SELECT id, pair, days, params_json, result_json,
                       total_return_pct, sharpe_ratio, win_rate,
                       total_trades, max_drawdown_pct, alpha
                FROM backtest_runs WHERE id = %s"""
        params: list = [run_id]
        if resolved:
            sql += " AND exchange = %s"
            params.append(resolved)

        with db._get_conn() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Backtest run not found")

        result = dict(row)
        for json_field in ("params_json", "result_json"):
            if json_field in result and isinstance(result[json_field], str):
                try:
                    result[json_field] = json.loads(result[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass

        r = result.get("result_json", {})
        p = result.get("params_json", {})
        trades = r.get("trades", [])

        # Build a structured data block for the LLM
        exit_reasons: dict[str, int] = {}
        for t in trades:
            reason = t.get("exit_reason", "unknown")
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        cost_sens = r.get("cost_sensitivity", [])
        profitable_configs = sum(1 for c in cost_sens if c.get("profitable"))
        total_configs = len(cost_sens)

        round_trip_cost = ((p.get("fee_pct", 0) * 2) + p.get("slippage_pct", 0)) * 100

        data_block = f"""Backtest Results for {result['pair']} ({result['days']}-day period)

PERFORMANCE:
  Return: {r.get('total_return_pct', 0):.2f}%
  P&L: ${r.get('final_balance', 10000) - r.get('initial_balance', 10000):.2f} (${r.get('initial_balance', 10000):.0f} → ${r.get('final_balance', 10000):.0f})
  Buy & Hold Return: {r.get('benchmark_return_pct', 0):.2f}%
  Alpha (strategy - benchmark): {r.get('alpha', 0):.2f}%

RISK METRICS:
  Sharpe Ratio: {r.get('sharpe_ratio', 0):.3f}
  Sortino Ratio: {r.get('sortino_ratio', 0):.3f}
  Calmar Ratio: {r.get('calmar_ratio', 0):.3f}
  Max Drawdown: {r.get('max_drawdown_pct', 0):.2f}%
  Profit Factor: {r.get('profit_factor', 0):.2f}

TRADE STATISTICS:
  Total Trades: {r.get('total_trades', 0)}
  Win Rate: {r.get('win_rate', 0):.1f}%
  Winners: {r.get('winning_trades', 0)} | Losers: {r.get('losing_trades', 0)}
  Avg Win: ${r.get('avg_win', 0):.2f} | Avg Loss: ${r.get('avg_loss', 0):.2f}
  Largest Win: ${r.get('largest_win', 0):.2f} | Largest Loss: ${r.get('largest_loss', 0):.2f}
  Avg Hold Time: {r.get('avg_hold_time_hours', 0):.1f} hours
  Exit Reasons: {', '.join(f'{k}: {v}' for k, v in exit_reasons.items())}

PARAMETERS:
  Position Size: {p.get('position_size_pct', 0) * 100:.0f}%
  Trailing Stop: {p.get('trailing_stop_pct', 0) * 100:.1f}%
  Entry Threshold: {p.get('entry_threshold', 0):.2f}
  Fee: {p.get('fee_pct', 0) * 100:.2f}%
  Slippage: {p.get('slippage_pct', 0) * 100:.2f}%
  Estimated Round-Trip Cost: {round_trip_cost:.2f}%

COST SENSITIVITY:
  {profitable_configs}/{total_configs} fee/slippage combinations were profitable"""

        interpretation = ""
        try:
            from src.core.llm_client import LLMClient, build_providers

            providers_cfg = deps.get_config().get("llm_providers", [])
            providers = build_providers(providers_cfg)

            llm = LLMClient(
                providers=providers,
                temperature=0.3,
                max_tokens=800,
            )

            system_prompt = (
                "You are a quantitative trading analyst explaining backtest results to a retail trader. "
                "Given the backtest metrics below, write a clear, actionable interpretation covering:\n"
                "1) **Overall Verdict** — Was this strategy profitable? How did it compare to simply holding?\n"
                "2) **Risk Assessment** — What do the Sharpe, Sortino, Calmar, and drawdown numbers tell us?\n"
                "3) **Trade Quality** — Are the win rate, profit factor, and avg win/loss ratios healthy?\n"
                "4) **Cost Impact** — How much are fees and slippage eating into returns?\n"
                "5) **Actionable Suggestions** — 2-3 specific things the user could try to improve results.\n\n"
                "Rules:\n"
                "- Be concise (under 250 words)\n"
                "- Use plain language, avoid jargon without explanation\n"
                "- Be honest about poor results — don't sugarcoat\n"
                "- Reference specific numbers from the data\n"
                "- Use markdown formatting (bold, bullet points) for readability"
            )

            interpretation = await llm.chat(
                system_prompt=system_prompt,
                user_message=f"Interpret these backtest results:\n\n{data_block}",
                agent_name="backtest_analyst",
                priority="low",
            )
        except Exception as e:
            logger.warning(f"LLM interpretation failed for run {run_id}: {e}")
            # Deterministic fallback when LLM is unavailable
            pnl = r.get("final_balance", 10000) - r.get("initial_balance", 10000)
            verdict = "profitable" if pnl > 0 else "unprofitable"
            vs_hold = "outperformed" if r.get("alpha", 0) > 0 else "underperformed"
            interpretation = (
                f"**{result['pair']}** was **{verdict}** over {result['days']} days "
                f"with a return of {r.get('total_return_pct', 0):.2f}% "
                f"(P&L: ${pnl:.2f}). "
                f"The strategy **{vs_hold}** buy-and-hold by {abs(r.get('alpha', 0)):.2f}%. "
                f"Sharpe ratio of {r.get('sharpe_ratio', 0):.3f} "
                f"{'indicates acceptable risk-adjusted returns' if r.get('sharpe_ratio', 0) >= 1 else 'suggests poor risk-adjusted performance'}. "
                f"Win rate: {r.get('win_rate', 0):.1f}% across {r.get('total_trades', 0)} trades. "
                f"Round-trip costs of ~{round_trip_cost:.2f}% per trade may be impacting profitability."
            )

        return {"interpretation": interpretation, "run_id": run_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("backtest interpretation error")
        raise HTTPException(status_code=500, detail="Internal server error")


def _ws_get_client_ip(websocket: WebSocket) -> str:
    if websocket.client:
        return websocket.client.host
    return "unknown"


@router.websocket("/ws/backtest")
async def ws_backtest(websocket: WebSocket):
    """WebSocket endpoint for live backtest progress streaming.

    Protocol:
    1. Client connects with ?profile=<profile>
    2. Client sends JSON: {pair, days, position_size_pct, ...}
    3. Server streams:
       - {type: "status", phase: "fetching_candles"}
       - {type: "progress", pct: 30, candles_processed: 300, total_candles: 1000}
       - {type: "metrics", sharpe: ..., return_pct: ..., trades: ...}
       - {type: "complete", ...full_result}
       - {type: "error", detail: "..."}
    4. Connection closes after complete/error
    """
    client_ip = _ws_get_client_ip(websocket)

    # Origin validation
    origin = websocket.headers.get("origin", "")
    if origin and origin not in deps.allowed_origins:
        logger.warning(f"Backtest WS rejected: disallowed origin {origin!r} from {client_ip}")
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    # Connection capacity
    active = sum(1 for s in _backtest_sessions.values() if s.get("active"))
    if active >= _MAX_BACKTEST_WS:
        await websocket.close(code=1013, reason="Too many concurrent backtests")
        return

    # Auth check
    if auth.is_auth_configured():
        session_token = websocket.cookies.get("ot_session", "")
        api_key = ""
        _auth_subprotocol = None
        if not session_token:
            for proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
                proto = proto.strip()
                if proto.startswith("apikey."):
                    try:
                        raw = proto[7:]
                        if len(raw) > 256:
                            break
                        import base64
                        api_key = base64.b64decode(raw).decode("utf-8")
                        _auth_subprotocol = proto
                    except Exception:
                        pass
                    break
            api_key = api_key or websocket.headers.get("x-api-key", "")

        import hmac as _hmac
        authenticated = False
        if session_token and auth.validate_session(session_token):
            authenticated = True
        elif api_key and auth._LEGACY_API_KEY and _hmac.compare_digest(api_key, auth._LEGACY_API_KEY):
            authenticated = True
        if not authenticated:
            await websocket.close(code=1008, reason="Authentication required")
            return

    # Accept connection
    _accepted_subprotocol = None
    for _proto in (websocket.headers.get("sec-websocket-protocol", "")).split(","):
        _proto = _proto.strip()
        if _proto.startswith("apikey."):
            _accepted_subprotocol = _proto
            break
    await websocket.accept(subprotocol=_accepted_subprotocol)

    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(str(websocket.url)).query)
    profile = (qs.get("profile", [""])[0] or "").strip()

    session_id = uuid.uuid4().hex[:12]

    try:
        # Wait for trigger message from client
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        params = json.loads(raw)

        # Validate
        pair_raw = params.get("pair", "")
        if not pair_raw:
            await websocket.send_json({"type": "error", "detail": "pair is required"})
            return
        pair = _validate_pair_format(pair_raw)
        bt_days = max(7, min(365, int(params.get("days", 60))))
        position_size_pct = max(0.01, min(0.50, float(params.get("position_size_pct", 0.10))))
        trailing_stop_pct = max(0.005, min(0.20, float(params.get("trailing_stop_pct", 0.03))))
        entry_threshold = max(0.1, min(0.9, float(params.get("entry_threshold", 0.4))))
        fee_pct = max(0.0, min(0.05, float(params.get("fee_pct", 0.006))))
        slippage_pct = max(0.0, min(0.05, float(params.get("slippage_pct", 0.001))))

        resolved = deps.resolve_profile(profile) or "coinbase"
        is_equity = resolved.startswith("ibkr")

        with _backtest_lock:
            _backtest_sessions[session_id] = {"active": True, "pair": pair}

        # Phase 1: Fetch candles
        await websocket.send_json({"type": "status", "phase": "fetching_candles", "pair": pair})

        loop = asyncio.get_event_loop()
        candles = await loop.run_in_executor(
            None, _fetch_candles_for_backtest, pair, bt_days, is_equity
        )

        if not candles or len(candles) < 100:
            got = len(candles) if candles else 0
            await websocket.send_json({
                "type": "error",
                "code": "no_candles" if got == 0 else "insufficient_candles",
                "detail": f"Insufficient candle data for {pair}: got {got} candles (need 100+)",
                "pair": pair,
                "candles_found": got,
                "candles_needed": 100,
                "days": bt_days,
            })
            return

        await websocket.send_json({
            "type": "status", "phase": "running_backtest",
            "total_candles": len(candles), "pair": pair,
        })

        # Phase 2: Run backtest with progress callbacks
        config = deps.get_config_for_profile(profile)

        progress_state = {"last_pct": 0}

        async def _send_progress(pct: int, interim: dict | None = None):
            """Send progress + optional interim metrics."""
            msg: dict = {"type": "progress", "pct": pct}
            if interim:
                msg["interim"] = interim
            try:
                await websocket.send_json(msg)
            except Exception:
                pass

        def _run_backtest_with_progress():
            """Run the backtest engine in a thread, posting progress."""
            from src.backtesting.engine import BacktestEngine

            engine = BacktestEngine(
                config=config,
                initial_balance=10000.0,
                position_size_pct=position_size_pct,
                max_positions=3,
                trailing_stop_pct=trailing_stop_pct,
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
            )
            engine._entry_threshold = entry_threshold

            # Monkey-patch the run loop to emit progress
            warmup = 50
            total = len(candles) - warmup
            original_run = engine.run

            def instrumented_run(cndls, pair=pair, warmup_val=warmup):
                # Let the engine run normally but emit progress from equity_curve growth
                result = original_run(cndls, pair=pair, warmup=warmup_val)
                return result

            return engine.run(candles, pair=pair)

        result = await loop.run_in_executor(None, _run_backtest_with_progress)

        # Send progress milestones (simulated since engine doesn't have callbacks)
        for pct in [25, 50, 75, 100]:
            await _send_progress(pct)
            await asyncio.sleep(0.05)  # small delay for UI feel

        # Phase 3: Save to DB and send complete result
        result_dict = {
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_balance": result.initial_balance,
            "final_balance": result.final_balance,
            "total_return_pct": result.total_return_pct,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": result.win_rate,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "sortino_ratio": result.sortino_ratio,
            "calmar_ratio": result.calmar_ratio,
            "profit_factor": result.profit_factor,
            "largest_win": result.largest_win,
            "largest_loss": result.largest_loss,
            "avg_hold_time_hours": result.avg_hold_time_hours,
            "benchmark_return_pct": result.benchmark_return_pct,
            "alpha": result.alpha,
            "trades": result.trades,
            "equity_curve": result.equity_curve,
            "cost_sensitivity": result.cost_sensitivity,
        }

        params_dict = {
            "position_size_pct": position_size_pct,
            "trailing_stop_pct": trailing_stop_pct,
            "entry_threshold": entry_threshold,
            "fee_pct": fee_pct,
            "slippage_pct": slippage_pct,
        }

        # Persist
        db = deps.get_profile_db()
        _ensure_schema(db)
        insert_sql = """
            INSERT INTO backtest_runs
                (pair, exchange, days, params_json, result_json,
                 total_return_pct, sharpe_ratio, win_rate,
                 total_trades, max_drawdown_pct, alpha)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, run_ts
        """
        run_id = None
        run_ts = None
        try:
            with db._get_conn() as conn:
                row = conn.execute(insert_sql, (
                    pair, resolved, bt_days,
                    json.dumps(params_dict),
                    json.dumps(result_dict, default=str),
                    result.total_return_pct, result.sharpe_ratio,
                    result.win_rate, result.total_trades,
                    result.max_drawdown_pct, result.alpha,
                )).fetchone()
                conn.commit()
            run_id = row["id"] if row else None
            run_ts = row["run_ts"] if row else None
        except Exception as db_err:
            logger.warning(f"Backtest DB save failed: {db_err}")

        await websocket.send_json(deps.sanitize_floats({
            "type": "complete",
            "id": run_id,
            "run_ts": run_ts,
            "pair": pair,
            "exchange": resolved,
            "days": bt_days,
            "params_json": params_dict,
            "result_json": result_dict,
        }))

    except asyncio.TimeoutError:
        try:
            await websocket.send_json({"type": "error", "detail": "Timeout waiting for parameters"})
        except Exception:
            pass
    except ValueError as ve:
        try:
            await websocket.send_json({"type": "error", "detail": str(ve)})
        except Exception:
            pass
    except WebSocketDisconnect:
        logger.debug(f"Backtest WS {session_id} disconnected")
    except Exception as exc:
        logger.exception(f"Backtest WS error: {exc}")
        try:
            await websocket.send_json({"type": "error", "detail": "Internal server error"})
        except Exception:
            pass
    finally:
        with _backtest_lock:
            _backtest_sessions.pop(session_id, None)
