from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from src.dashboard import deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.trades")

router = APIRouter(tags=["Trades"])


@router.get("/api/trades", summary="List raw trades log")
def list_trades(
    pair: Optional[str] = Query(None, description="Filter by pair e.g. BTC-USD"),
    hours: int = Query(24 * 7, ge=1, description="Hours of history to fetch"),
    limit: int = Query(500, ge=1, le=5000),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Returns a list of raw trades from the database, newest first."""
    qc = deps.quote_currency_for(profile)
    exchange = deps.resolve_profile(profile) or None
    trades = db.get_trades(hours=hours, pair=pair, limit=limit, quote_currency=qc, exchange=exchange)
    return {"trades": trades, "count": len(trades)}


@router.get("/api/trades/export", summary="Export trades to CSV")
def export_trades(hours: int = Query(24 * 30, ge=1), profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Exports raw trades to a downloadable CSV file."""
    qc = deps.quote_currency_for(profile)
    exchange = deps.resolve_profile(profile) or None
    trades = db.get_trades(hours=hours, limit=100000, quote_currency=qc, exchange=exchange)

    if not trades:
        return Response(
            content="id,ts,pair,action,quantity,price,quote_amount,pnl,confidence,signal_type\n",
            media_type="text/csv",
        )

    import pandas as pd

    df = pd.DataFrame(trades)
    columns = [
        "id", "ts", "pair", "action", "quantity", "price", "quote_amount",
        "fee_quote", "pnl", "confidence", "signal_type", "stop_loss",
        "take_profit", "reasoning", "is_rotation", "approved_by",
    ]
    existing_cols = [c for c in columns if c in df.columns]
    df = df[existing_cols]

    csv_data = df.to_csv(index=False)
    headers = {
        "Content-Disposition": "attachment; filename=auto_traitor_trades.csv",
    }
    return Response(content=csv_data, media_type="text/csv", headers=headers)


def _get_field(obj, key: str, default=""):
    """Extract a field from either a dict or an object with attributes."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_ts(ts: str) -> str:
    """Normalize a timestamp string to ISO 8601 UTC format."""
    if not ts:
        return ts
    if ts.endswith("Z"):
        return ts
    # Has timezone info — parse and reformat
    from datetime import datetime as dt
    try:
        parsed = dt.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts


def _parse_coinbase_order(order) -> dict | None:
    """Parse a Coinbase order into a normalized dict for DB insertion."""
    order_id = _get_field(order, "order_id")
    if not order_id:
        return None

    product_id = _get_field(order, "product_id", "")
    side = _get_field(order, "side", "").upper()
    action = "buy" if "BUY" in side else "sell"
    filled_size = float(_get_field(order, "filled_size", 0) or 0)
    filled_value = float(_get_field(order, "filled_value", 0) or 0)
    avg_price = float(_get_field(order, "average_filled_price", 0) or 0)
    total_fees = float(_get_field(order, "total_fees", 0) or 0)
    fill_time = _get_field(order, "last_fill_time", "")

    if not product_id or not fill_time:
        return None

    return {
        "external_id": f"coinbase:{order_id}",
        "pair": product_id,
        "action": action,
        "quantity": filled_size,
        "quote_amount": filled_value,
        "price": avg_price,
        "fee_quote": total_fees,
        "ts": _normalize_ts(fill_time),
    }


def _claim_existing_trades(db, orders: list, exchange: str) -> int:
    """Match exchange orders against existing DB trades and set their external_id.

    This prevents the subsequent INSERT from creating duplicates for trades
    that the bot already recorded (with slightly different timestamps).
    """
    from datetime import datetime as dt, timedelta

    claimed = 0
    with db._get_conn() as conn:
        for order in orders:
            parsed = _parse_coinbase_order(order)
            if not parsed:
                continue

            # Check if this external_id is already claimed
            existing = conn.execute(
                "SELECT id FROM trades WHERE external_id = %s",
                (parsed["external_id"],),
            ).fetchone()
            if existing:
                continue

            # Find a matching trade within a 5-second window (limit 1)
            try:
                order_ts = dt.fromisoformat(parsed["ts"].replace("Z", "+00:00"))
            except Exception:
                continue
            ts_lo = (order_ts - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ts_hi = (order_ts + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

            try:
                match = conn.execute(
                    """UPDATE trades SET external_id = %s
                       WHERE id = (
                           SELECT id FROM trades
                           WHERE pair = %s AND action = %s
                             AND ts >= %s AND ts <= %s
                             AND exchange = %s AND external_id IS NULL
                           LIMIT 1
                       )
                       RETURNING id""",
                    (parsed["external_id"], parsed["pair"], parsed["action"],
                     ts_lo, ts_hi, exchange),
                ).fetchone()
                if match:
                    claimed += 1
            except Exception:
                # Unique constraint or other conflict — skip this order
                conn.execute("ROLLBACK")
                conn.execute("BEGIN")
                continue
        conn.commit()
    return claimed


def _sync_coinbase_orders(client, db, exchange: str) -> dict:
    """Fetch all filled orders from Coinbase and insert missing ones."""
    rest = getattr(client, "_rest_client", None)
    if rest is None:
        return {"synced": 0, "total_exchange": 0, "error": "No Coinbase REST client"}

    all_orders = []
    cursor = ""
    for _ in range(20):  # safety limit on pagination
        kwargs = {
            "order_status": ["FILLED"],
            "limit": 250,
            "sort_by": "LAST_FILL_TIME",
        }
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = rest.list_orders(**kwargs)
        except Exception as e:
            logger.warning(f"Coinbase list_orders failed: {e}")
            break
        orders = getattr(resp, "orders", []) or []
        all_orders.extend(orders)
        cursor = getattr(resp, "cursor", "") or ""
        if not cursor or len(orders) == 0:
            break

    # First pass: claim existing DB trades by setting external_id on matches
    _claim_existing_trades(db, all_orders, exchange)

    # Second pass: insert truly new orders (ON CONFLICT skips already-claimed)
    synced = 0
    for order in all_orders:
        parsed = _parse_coinbase_order(order)
        if not parsed:
            continue

        result = db.record_synced_trade(
            ts=parsed["ts"],
            external_id=parsed["external_id"],
            pair=parsed["pair"],
            action=parsed["action"],
            price=parsed["price"],
            quantity=parsed["quantity"],
            quote_amount=parsed["quote_amount"],
            fee_quote=parsed["fee_quote"],
            signal_type="exchange_sync",
            approved_by="exchange",
            exchange=exchange,
        )
        if result is not None:
            synced += 1

    return {"synced": synced, "total_exchange": len(all_orders)}


def _sync_ibkr_orders(client, db, exchange: str) -> dict:
    """Fetch filled orders from IBKR and insert missing ones."""
    ib = getattr(client, "ib", None)
    if ib is None or getattr(client, "paper_mode", False):
        return {"synced": 0, "total_exchange": 0, "error": "IBKR not connected or in paper mode"}

    try:
        fills = ib.fills()
    except Exception as e:
        logger.warning(f"IBKR fills() failed: {e}")
        return {"synced": 0, "total_exchange": 0, "error": str(e)}

    synced = 0
    for fill in fills:
        exec_ = fill.execution
        contract = fill.contract
        exec_id = exec_.execId
        symbol = contract.symbol
        side = exec_.side.upper()
        action = "buy" if side == "BOT" else "sell"
        qty = float(exec_.shares)
        price = float(exec_.price)
        ts = exec_.time.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(exec_.time, "strftime") else str(exec_.time)
        commission = float(fill.commissionReport.commission) if fill.commissionReport else 0.0

        result = db.record_synced_trade(
            ts=ts,
            external_id=f"ibkr:{exec_id}",
            pair=symbol,
            action=action,
            price=price,
            quantity=qty,
            quote_amount=qty * price,
            fee_quote=commission,
            signal_type="exchange_sync",
            approved_by="exchange",
            exchange=exchange,
        )
        if result is not None:
            synced += 1

    return {"synced": synced, "total_exchange": len(fills)}


@router.post("/api/trades/sync", summary="Sync trades from exchange")
def sync_trades(
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """Pull all filled orders from the exchange and insert any missing ones."""
    resolved = deps.resolve_profile(profile)
    client = deps.client_for_profile(profile)
    if client is None:
        return {"synced": 0, "total_exchange": 0, "error": "No exchange client available"}

    exchange = resolved or "coinbase"
    if deps.is_equity_profile(profile):
        result = _sync_ibkr_orders(client, db, exchange)
    else:
        result = _sync_coinbase_orders(client, db, exchange)

    logger.info(f"Trade sync complete for {exchange}: {result}")
    return result
