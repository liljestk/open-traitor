from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.dashboard import deps
from src.utils.logger import get_logger
from src.utils.qc_filter import qc_where

logger = get_logger("dashboard.routes.stats")

router = APIRouter(tags=["Stats"])


# ---------------------------------------------------------------------------
# REST - Stats summary
# ---------------------------------------------------------------------------

@router.get("/api/stats/summary", summary="Portfolio and trade stats overview")
def get_stats_summary(
    profile: str = Query("", description="Exchange profile"),
    db=Depends(deps.get_profile_db),
):
    """High-level stats: win-rate, PnL, active pairs, recent activity."""
    qc = deps.quote_currency_for(profile)
    qc_frag, qc_params = qc_where(qc)
    with db._get_conn() as conn:
        try:
            # Build exchange filter early — applied to ALL queries
            resolved_exch = deps.resolve_profile(profile) or None
            exch_frag = " AND (exchange = %s OR exchange = %s)" if resolved_exch else ""
            exch_params_ar = [resolved_exch, f"{resolved_exch}_paper"] if resolved_exch else []

            # Overall trade stats
            trade_sql = """SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    ROUND(CAST(SUM(pnl) AS numeric), 2) as total_pnl,
                    ROUND(CAST(AVG(pnl) AS numeric), 2) as avg_pnl,
                    ROUND(CAST(MAX(pnl) AS numeric), 2) as best_trade,
                    ROUND(CAST(MIN(pnl) AS numeric), 2) as worst_trade
                   FROM trades
                   WHERE pnl IS NOT NULL"""
            trade_row = conn.execute(trade_sql + qc_frag + exch_frag, (*qc_params, *exch_params_ar)).fetchone()

            # Last 24h
            cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            recent_sql = """SELECT
                    COUNT(*) as trades_24h,
                    ROUND(CAST(SUM(pnl) AS numeric), 2) as pnl_24h
                   FROM trades
                   WHERE ts >= %s AND pnl IS NOT NULL"""
            recent_row = conn.execute(recent_sql + qc_frag + exch_frag, (cutoff_24h, *qc_params, *exch_params_ar)).fetchone()

            # Active pairs
            exch_frag_single = " AND exchange = %s" if resolved_exch else ""
            exch_params_single = [resolved_exch] if resolved_exch else []
            pairs_sql = "SELECT COUNT(DISTINCT pair) as active_pairs FROM agent_reasoning WHERE ts >= %s"
            pairs_row = conn.execute(pairs_sql + qc_frag + exch_frag_single, (cutoff_24h, *qc_params, *exch_params_single)).fetchone()

            # Cycle count last 24h
            cycle_sql = "SELECT COUNT(DISTINCT cycle_id) as cycles_24h FROM agent_reasoning WHERE ts >= %s"
            cycle_row = conn.execute(cycle_sql + qc_frag + exch_frag_single, (cutoff_24h, *qc_params, *exch_params_single)).fetchone()

            # Latest portfolio snapshot (filtered by exchange when profile is set)
            snapshot_sql = """SELECT portfolio_value, total_pnl, ts
                   FROM portfolio_snapshots"""
            snapshot_params: list = []
            resolved_p = deps.resolve_profile(profile)
            if resolved_p:
                snapshot_sql += " WHERE exchange = %s"
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
            # exchange client - the DB snapshots may contain stale/default values.
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


# ---------------------------------------------------------------------------
# REST - Executive summary (cross-profile)
# ---------------------------------------------------------------------------

@router.get("/api/executive_summary", summary="Combined analytics across all profiles")
def get_executive_summary():
    """Returns aggregated high-level stats across all configuration profiles."""
    profiles = []
    total_pnl = 0.0
    total_trades = 0
    active_pairs: set = set()

    db = deps.require_db()
    with db._get_conn() as conn:
        try:
            # Get per-exchange breakdown from the single PG database
            exch_rows = conn.execute("""
                SELECT exchange,
                       COUNT(*) as t,
                       COALESCE(SUM(pnl), 0) as p
                FROM trades
                WHERE pnl IS NOT NULL
                GROUP BY exchange
            """).fetchall()

            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

            for row in exch_rows:
                exch = row["exchange"] or "default"
                t = row["t"]
                p = float(row["p"] or 0)
                total_trades += t
                total_pnl += p

                pairs = conn.execute(
                    "SELECT DISTINCT pair FROM agent_reasoning WHERE ts >= %s AND exchange = %s",
                    (cutoff, exch),
                ).fetchall()
                active_pairs.update(pr["pair"] for pr in pairs)

                profiles.append({
                    "profile": exch,
                    "trades": t,
                    "pnl": round(p, 2),
                    "active_pairs_24h": len(pairs),
                })
        except Exception as e:
            logger.warning(f"Executive summary error: {e}")

    return {
        "profiles": profiles,
        "combined": {
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "total_active_pairs_24h": len(active_pairs),
        },
    }


# ---------------------------------------------------------------------------
# REST - Portfolio History & Analytics
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
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/analytics", summary="Comprehensive performance analytics")
def get_analytics(hours: int = Query(720, ge=1, le=8760), profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Combined analytics dashboard data: performance, best/worst, daily summaries, win/loss stats."""
    try:
        qc = deps.quote_currency_for(profile)
        resolved = deps.resolve_profile(profile) or None
        perf = db.get_performance_summary(hours=hours, quote_currency=qc, exchange=resolved)
        best_worst = db.get_best_worst_trades(hours=hours, quote_currency=qc, exchange=resolved)
        days = max(1, hours // 24)
        summaries = db.get_daily_summaries(days=days, quote_currency=qc, exchange=resolved)
        win_loss = db.get_win_loss_stats(hours=hours, quote_currency=qc, exchange=resolved)
        portfolio_range = db.get_portfolio_range(hours=hours, quote_currency=qc, exchange=resolved)

        return deps.sanitize_floats({
            "performance": perf,
            "best_worst": best_worst,
            "daily_summaries": summaries,
            "win_loss": win_loss,
            "portfolio_range": portfolio_range,
        })
    except Exception as exc:
        logger.exception("analytics error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# REST - Portfolio Exposure (position concentration)
# ---------------------------------------------------------------------------

@router.get("/api/portfolio/exposure", summary="Current portfolio position concentration")
def get_portfolio_exposure(profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Returns the latest portfolio snapshot with position breakdown.

    When a profile is active, only the snapshot from the matching exchange is
    returned, and positions are filtered to pairs with the profile's quote
    currency so crypto holdings never bleed into equity views.
    """
    qc = deps.quote_currency_for(profile)
    with db._get_conn() as conn:
        try:
            # Build query: prefer the snapshot for the profile's exchange
            base_sql = """SELECT portfolio_value, cash_balance, return_pct, total_pnl,
                          max_drawdown, open_positions, current_prices, fear_greed_value,
                          high_stakes_active, ts
                   FROM portfolio_snapshots"""
            params: list = []
            resolved_p = deps.resolve_profile(profile)
            if resolved_p:
                base_sql += " WHERE exchange = %s"
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

            client = deps.client_for_profile(profile)

            # For equity profiles, override portfolio value with live data
            if deps.is_equity_profile(profile):
                try:
                    if client:
                        live_pv = client.get_portfolio_value()
                        if live_pv > 0:
                            data["portfolio_value"] = live_pv
                            portfolio_val = live_pv
                except Exception:
                    pass
            else:
                # For crypto profiles, override positions with live balances from
                # the exchange so holdings outside the bot's tracking are visible.
                try:
                    if client:
                        live_balances = client.balance  # {currency: total_qty}
                        qc_list = qc if isinstance(qc, list) else ([qc] if qc else [])
                        fiat = {"USD", "EUR", "GBP", "CHF", "SEK", "NOK", "DKK",
                                "CAD", "AUD", "JPY", "USDC", "USDT"}
                        live_positions: dict[str, float] = {}
                        for currency, qty in live_balances.items():
                            if currency.upper() in fiat or qty <= 0:
                                continue
                            # Match to a pair using the profile's quote currencies
                            for q in qc_list:
                                pair = f"{currency.upper()}-{q.upper()}"
                                live_positions[pair] = qty
                                break
                            else:
                                # No quote currency filter — keep as-is
                                live_positions[currency] = qty
                        if live_positions:
                            positions = live_positions
                            # Update cash balance from live EUR/fiat balance
                            for q in qc_list:
                                if q.upper() in live_balances:
                                    data["cash_balance"] = live_balances[q.upper()]
                                    break
                except Exception:
                    pass  # fall back to snapshot positions

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

            # For legacy float positions, look up the weighted avg entry price
            # from the last BUY trades per pair (single query, all pairs at once).
            legacy_pairs = [p for p, v in positions.items() if not isinstance(v, dict)]
            avg_entry_by_pair: dict[str, float] = {}
            if legacy_pairs:
                resolved_exch = deps.resolve_profile(profile) or None
                exch_frag_ep = " AND exchange = %s" if resolved_exch else ""
                exch_params_ep = [resolved_exch] if resolved_exch else []
                placeholders = ",".join(["%s"] * len(legacy_pairs))
                try:
                    ep_rows = conn.execute(
                        f"""SELECT pair,
                                   SUM(price * quantity) / NULLIF(SUM(quantity), 0) AS avg_price
                            FROM trades
                            WHERE action = 'buy' AND pair IN ({placeholders}){exch_frag_ep}
                            GROUP BY pair""",
                        (*legacy_pairs, *exch_params_ep),
                    ).fetchall()
                    avg_entry_by_pair = {r["pair"]: float(r["avg_price"]) for r in ep_rows if r["avg_price"]}
                except Exception:
                    pass  # leave empty; fallback to current price below

            breakdown = []
            allocated = 0.0
            for pair, pos in positions.items():
                # Handle both formats:
                #   dict  -> {"quantity": ..., "entry_price": ...}  (full position)
                #   float -> just the quantity (legacy / orchestrator format)
                if isinstance(pos, dict):
                    qty = pos.get("quantity", 0)
                    entry_price = pos.get("entry_price", 0)
                    price = prices.get(pair, entry_price)
                else:
                    qty = float(pos) if pos else 0
                    price = prices.get(pair, 0)
                    entry_price = avg_entry_by_pair.get(pair, price)
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
            raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# REST - Prediction Accuracy (Predictions vs Actuals)
# ---------------------------------------------------------------------------

_ACCURACY_CACHE_TTL = 300  # seconds


@router.get("/api/predictions/accuracy", summary="Signal prediction accuracy vs actual price movements")
def get_prediction_accuracy(
    days: int = Query(30, ge=1, le=365),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Compare market analyst signal predictions with actual price outcomes.

    Automatically filters by the profile's quote currency so that, e.g.,
    the crypto/EUR profile only shows -EUR pairs.
    Result is cached in Redis for 5 minutes to avoid expensive recomputation.
    """
    try:
        qc = deps.quote_currency_for(profile)
        resolved = deps.resolve_profile(profile) or None
        cache_key = f"predictions:accuracy:{resolved or 'all'}:{days}"

        if deps.redis_client:
            cached = deps.redis_client.get(cache_key)
            if cached:
                import json as _json
                return _json.loads(cached)

        result = db.get_prediction_accuracy(days=days, quote_currency=qc, exchange=resolved)
        result = deps.sanitize_floats(result)

        if deps.redis_client:
            import json as _json
            deps.redis_client.setex(cache_key, _ACCURACY_CACHE_TTL, _json.dumps(result))

        return result
    except Exception as exc:
        logger.exception("prediction accuracy error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/predictions/tracked-pairs", summary="Pairs the LLM system actively tracks")
def get_tracked_pairs(profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Return pairs the LLM has analyzed recently, grouped by asset class.

    Automatically filters by the profile's quote currency.
    """
    try:
        qc = deps.quote_currency_for(profile)
        resolved = deps.resolve_profile(profile) or None
        return db.get_tracked_pairs(quote_currency=qc, exchange=resolved)
    except Exception as exc:
        logger.exception("tracked pairs error")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        resolved = deps.resolve_profile(profile) or None
        result = db.get_pair_prediction_history(pair=pair.upper(), days=days, exchange=resolved)
        return deps.sanitize_floats(result)
    except Exception as exc:
        logger.exception("pair prediction history error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/portfolio/cleanup", summary="One-time cleanup of bad portfolio snapshots")
def cleanup_portfolio(db=Depends(deps.get_profile_db)):
    """Delete anomalous portfolio snapshots (zero-value and paper-mode bleed-through)."""
    try:
        deleted = db.cleanup_bad_snapshots()
        return {"deleted": deleted, "status": "ok"}
    except Exception as exc:
        logger.exception("portfolio cleanup error")
        raise HTTPException(status_code=500, detail="Internal server error")
