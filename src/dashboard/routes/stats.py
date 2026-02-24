from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.dashboard import deps
from src.utils.logger import get_logger
from src.utils.qc_filter import qc_where

logger = get_logger("dashboard.routes.stats")

router = APIRouter(tags=["Stats"])


# ---------------------------------------------------------------------------
# REST — Stats summary
# ---------------------------------------------------------------------------

@router.get("/api/stats/summary", summary="Portfolio and trade stats overview")
def get_stats_summary(
    profile: str = Query("", description="Exchange profile"),
    db=Depends(deps.get_profile_db),
):
    """High-level stats: win-rate, PnL, active pairs, recent activity."""
    conn = deps.open_conn(db)
    qc = deps.quote_currency_for(profile)
    qc_frag, qc_params = qc_where(qc)
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
        trade_row = conn.execute(trade_sql + qc_frag, qc_params).fetchone()

        # Last 24h
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent_sql = """SELECT
                COUNT(*) as trades_24h,
                ROUND(SUM(pnl), 2) as pnl_24h
               FROM trades
               WHERE ts >= ? AND pnl IS NOT NULL"""
        recent_row = conn.execute(recent_sql + qc_frag, (cutoff_24h, *qc_params)).fetchone()

        # Active pairs
        pairs_sql = "SELECT COUNT(DISTINCT pair) as active_pairs FROM agent_reasoning WHERE ts >= ?"
        pairs_row = conn.execute(pairs_sql + qc_frag, (cutoff_24h, *qc_params)).fetchone()

        # Cycle count last 24h
        cycle_sql = "SELECT COUNT(DISTINCT cycle_id) as cycles_24h FROM agent_reasoning WHERE ts >= ?"
        cycle_row = conn.execute(cycle_sql + qc_frag, (cutoff_24h, *qc_params)).fetchone()

        # Latest portfolio snapshot (filtered by exchange when profile is set)
        snapshot_sql = """SELECT portfolio_value, total_pnl, ts
               FROM portfolio_snapshots"""
        snapshot_params: list = []
        resolved_p = deps.resolve_profile(profile)
        if resolved_p:
            # Use profile name directly as exchange filter
            snapshot_sql += " WHERE exchange = ?"
            snapshot_params.append(resolved_p)
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
        resolved = deps.resolve_profile(profile)
        cfg = deps.get_config_for_profile(profile)
        stats["currency"] = cfg.get("trading", {}).get("quote_currency", "EUR")

        # For equity profiles, override portfolio value with live data from the
        # exchange client — the DB snapshots may contain stale/default values.
        if deps.is_equity_profile(profile):
            try:
                client = deps.client_for_profile(profile)
                if client:
                    live_pv = client.get_portfolio_value()
                    if live_pv > 0:
                        if "portfolio" not in stats or not stats["portfolio"]:
                            stats["portfolio"] = {"portfolio_value": live_pv, "total_pnl": 0.0, "ts": deps.utcnow()}
                        else:
                            stats["portfolio"]["portfolio_value"] = live_pv
            except Exception as _pv_err:
                logger.debug(f"Live portfolio value fetch failed for {profile}: {_pv_err}")

        return stats
    except Exception as exc:
        logger.exception("stats/summary error")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST — Executive summary (cross-profile)
# ---------------------------------------------------------------------------

@router.get("/api/executive_summary", summary="Combined analytics across all profiles")
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
# REST — Portfolio History & Analytics
# ---------------------------------------------------------------------------

@router.get("/api/portfolio/history", summary="Portfolio value time-series for equity curve")
def get_portfolio_history(hours: int = Query(720, ge=1, le=8760), profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Returns portfolio snapshots as time-series data for charting."""
    try:
        qc = deps.quote_currency_for(profile)
        resolved = deps.resolve_profile(profile)
        rows = db.get_portfolio_history(hours=hours, quote_currency=qc, exchange=resolved or None)
        return deps.sanitize_floats({"history": rows, "count": len(rows)})
    except Exception as exc:
        logger.exception("portfolio/history error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/analytics", summary="Comprehensive performance analytics")
def get_analytics(hours: int = Query(720, ge=1, le=8760), profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Combined analytics dashboard data: performance, best/worst, daily summaries, win/loss stats."""
    try:
        qc = deps.quote_currency_for(profile)
        resolved = deps.resolve_profile(profile)
        perf = db.get_performance_summary(hours=hours, quote_currency=qc)
        best_worst = db.get_best_worst_trades(hours=hours, quote_currency=qc)
        days = max(1, hours // 24)
        summaries = db.get_daily_summaries(days=days, quote_currency=qc, exchange=resolved or None)
        win_loss = db.get_win_loss_stats(hours=hours, quote_currency=qc)
        portfolio_range = db.get_portfolio_range(hours=hours, quote_currency=qc)

        return deps.sanitize_floats({
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

@router.get("/api/portfolio/exposure", summary="Current portfolio position concentration")
def get_portfolio_exposure(profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Returns the latest portfolio snapshot with position breakdown.

    When a profile is active, only the snapshot from the matching exchange is
    returned, and positions are filtered to pairs with the profile's quote
    currency so crypto holdings never bleed into equity views.
    """
    conn = deps.open_conn(db)
    qc = deps.quote_currency_for(profile)
    try:
        # Build query: prefer the snapshot for the profile's exchange
        base_sql = """SELECT portfolio_value, cash_balance, return_pct, total_pnl,
                      max_drawdown, open_positions, current_prices, fear_greed_value,
                      high_stakes_active, ts
               FROM portfolio_snapshots"""
        params: list = []
        resolved_p = deps.resolve_profile(profile)
        if resolved_p:
            base_sql += " WHERE exchange = ?"
            params.append(resolved_p)
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

        # For equity profiles, override portfolio value with live data
        if deps.is_equity_profile(profile):
            try:
                client = deps.client_for_profile(profile)
                if client:
                    live_pv = client.get_portfolio_value()
                    if live_pv > 0:
                        data["portfolio_value"] = live_pv
                        portfolio_val = live_pv
            except Exception:
                pass

        # Filter positions by quote currency when a profile is active
        if qc:
            suffixes = [f"-{c.upper()}" for c in (qc if isinstance(qc, list) else [qc])]
            positions = {
                pair: pos for pair, pos in positions.items()
                if any(pair.upper().endswith(s) for s in suffixes)
            }
            prices = {
                pair: p for pair, p in prices.items()
                if any(pair.upper().endswith(s) for s in suffixes)
            }

        breakdown = []
        allocated = 0.0
        for pair, pos in positions.items():
            # Handle both formats:
            #   dict  → {"quantity": ..., "entry_price": ...}  (full position)
            #   float → just the quantity (legacy / orchestrator format)
            if isinstance(pos, dict):
                qty = pos.get("quantity", 0)
                entry_price = pos.get("entry_price", 0)
                price = prices.get(pair, entry_price)
            else:
                qty = float(pos) if pos else 0
                entry_price = prices.get(pair, 0)  # no stored entry, use current
                price = prices.get(pair, 0)
            value = qty * price
            pct = (value / portfolio_val * 100) if portfolio_val else 0
            allocated += value
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

        return deps.sanitize_floats({"exposure": data})
    except Exception as exc:
        logger.exception("portfolio/exposure error")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST — Prediction Accuracy (Predictions vs Actuals)
# ---------------------------------------------------------------------------

@router.get("/api/predictions/accuracy", summary="Signal prediction accuracy vs actual price movements")
def get_prediction_accuracy(
    days: int = Query(30, ge=1, le=365),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Compare market analyst signal predictions with actual price outcomes.

    Automatically filters by the profile's quote currency so that, e.g.,
    the crypto/EUR profile only shows -EUR pairs.
    """
    try:
        qc = deps.quote_currency_for(profile)
        result = db.get_prediction_accuracy(days=days, quote_currency=qc)
        return deps.sanitize_floats(result)
    except Exception as exc:
        logger.exception("prediction accuracy error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/predictions/tracked-pairs", summary="Pairs the LLM system actively tracks")
def get_tracked_pairs(profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Return pairs the LLM has analyzed recently, grouped by asset class.

    Automatically filters by the profile's quote currency.
    """
    try:
        qc = deps.quote_currency_for(profile)
        return db.get_tracked_pairs(quote_currency=qc)
    except Exception as exc:
        logger.exception("tracked pairs error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/predictions/pair-history", summary="Price history with prediction overlay for a single pair")
def get_pair_prediction_history(
    pair: str = Query(..., description="Trading pair, e.g. BTC-EUR"),
    days: int = Query(30, ge=1, le=365),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Return actual price time-series with prediction markers overlaid for a single pair.

    Used by the Prediction Overlay chart to show predicted vs actual prices.
    """
    try:
        result = db.get_pair_prediction_history(pair=pair.upper(), days=days)
        return deps.sanitize_floats(result)
    except Exception as exc:
        logger.exception("pair prediction history error")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/portfolio/cleanup", summary="One-time cleanup of bad portfolio snapshots")
def cleanup_portfolio(db=Depends(deps.get_profile_db)):
    """Delete anomalous portfolio snapshots (zero-value and paper-mode bleed-through)."""
    try:
        deleted = db.cleanup_bad_snapshots()
        return {"deleted": deleted, "status": "ok"}
    except Exception as exc:
        logger.exception("portfolio cleanup error")
        raise HTTPException(status_code=500, detail=str(exc))
