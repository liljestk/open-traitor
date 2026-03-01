from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from src.dashboard import deps

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
