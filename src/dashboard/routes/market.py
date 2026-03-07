from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.dashboard import deps
from src.utils.logger import get_logger
from src.utils.pair_format import parse_pair

logger = get_logger("dashboard.market")

router = APIRouter(tags=["Market"])


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _get_products_for_profile(profile: str) -> list[dict]:
    """Return tradable products for the given profile.

    For equity profiles (IBKR), returns pairs from the config file
    since these exchanges don't have a Coinbase-style product catalog.
    For crypto profiles, queries the Coinbase REST API.
    """
    if deps.is_equity_profile(profile):
        cfg = deps.get_config_for_profile(profile)
        pairs = cfg.get("trading", {}).get("pairs", [])
        qc = cfg.get("trading", {}).get("quote_currency", "EUR")
        products = []
        for p in pairs:
            parts = p.split("-")
            if len(parts) == 2:
                products.append({"id": p, "base": parts[0], "quote": parts[1]})

        # Also include any human-followed pairs from the DB
        try:
            db = deps.require_db(profile)
            follows = db.get_pair_follows(quote_currency=qc)
            existing_ids = {prod["id"].upper() for prod in products}
            for f in follows:
                fpair = f["pair"].upper()
                if fpair not in existing_ids:
                    fparts = fpair.split("-")
                    if len(fparts) == 2:
                        products.append({"id": fpair, "base": fparts[0], "quote": fparts[1]})
                        existing_ids.add(fpair)
        except Exception:
            pass
        products.sort(key=lambda x: x["id"])
        return products

    # Coinbase / default: query REST API
    client = deps.exchange_client
    if not getattr(client, "_rest_client", None):
        return []
    try:
        resp = client._rest_client.get_products()
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
        return products
    except Exception as e:
        logger.warning(f"⚠️ Failed to list products: {e}")
        return []


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SimulatedTradeCreate(BaseModel):
    pair: str
    from_currency: str
    from_amount: float
    notes: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/products", summary="List tradable products for the active profile")
def list_products(profile: str = Query("", description="Exchange profile")):
    """Return tradable products for the active exchange profile.

    For crypto profiles: queries Coinbase Advanced Trade.
    For equity profiles (IBKR): returns configured pairs from the config file.
    """
    return {"products": _get_products_for_profile(profile)}


@router.get("/api/products/search", summary="Search tradable products by keyword")
def search_products(
    q: str = Query("", min_length=1, description="Search query (symbol or name)"),
    profile: str = Query("", description="Exchange profile"),
):
    """Search products by base currency / product ID substring.

    Returns up to 25 matches sorted alphabetically.
    For equity profiles, searches the config pair list.
    For crypto profiles, queries Coinbase.
    """
    query = q.upper().strip()

    if deps.is_equity_profile(profile):
        all_products = _get_products_for_profile(profile)
        # 1) Local config / followed pairs matching the query
        config_results = []
        config_ids: set[str] = set()
        for p in all_products:
            pid = p["id"].upper()
            base = p["base"].upper()
            if query in pid or query in base:
                config_results.append({
                    "id": p["id"],
                    "base": p["base"],
                    "quote": p["quote"],
                    "display_name": p["base"],
                    "volume_24h": 0,
                    "price_change_24h": 0,
                })
                config_ids.add(pid)

        # 2) Live search via IBKR Gateway (or Yahoo Finance fallback)
        live_results: list[dict] = []
        try:
            client = deps.client_for_profile(profile)
            if client and hasattr(client, "search_symbols"):
                live_results = client.search_symbols(query, limit=25)
        except Exception as e:
            logger.debug(f"Live equity search failed: {e}")

        # 3) Merge, dedup by pair ID (config results first)
        merged = list(config_results)
        for lr in live_results:
            if lr["id"].upper() not in config_ids:
                merged.append(lr)
                config_ids.add(lr["id"].upper())

        return {"results": merged[:25], "query": q}

    # Coinbase search
    client = deps.exchange_client
    if not getattr(client, "_rest_client", None):
        return {"results": [], "query": q}

    try:
        resp = client._rest_client.get_products()
        raw = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
        items = raw.get("products", [])
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
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        return {"results": results[:25], "query": q}
    except Exception as e:
        logger.warning(f"product search error: {e}")
        return {"results": [], "query": q}


@router.get("/api/market/price", summary="Live price for a trading pair")
def get_market_price(
    pair: str = Query(..., description="e.g. BTC-EUR"),
    profile: str = Query("", description="Exchange profile"),
):
    """Returns the current best-estimate price for the given pair."""
    price = deps.get_live_price(pair, profile=profile) or 0.0
    return {"pair": pair, "price": price, "ts": deps.utcnow()}


@router.post("/api/simulated-trades", summary="Open a new simulated trade")
def create_simulated_trade(body: SimulatedTradeCreate, profile: str = Query(""), db=Depends(deps.get_profile_db)):
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

    entry_price = deps.get_live_price(pair, profile=profile) or 0.0
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
        "ts": deps.utcnow(),
    }


@router.get("/api/simulated-trades", summary="List simulated trades with live PnL")
def list_simulated_trades(
    include_closed: bool = Query(False, description="Include closed simulations"),
    profile: str = Query(""),
    db=Depends(deps.get_profile_db),
):
    """
    Returns all simulated trades. For open ones, the current price is fetched
    live and PnL (absolute + %) is computed on the fly.
    """
    qc = deps.quote_currency_for(profile)
    rows = db.get_simulated_trades(include_closed=include_closed, quote_currency=qc)

    # Enrich open rows with live PnL
    for row in rows:
        if row["status"] == "open":
            current_price = deps.get_live_price(row["pair"], profile=profile) or 0.0
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


@router.delete("/api/simulated-trades/{sim_id}", summary="Close a simulated trade")
def close_simulated_trade_route(sim_id: int, profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """
    Closes an open simulation by recording the current live price as the
    close price and computing the final PnL.
    """

    # First, look up the sim to get the pair
    rows = db.get_simulated_trades(include_closed=False)
    target = next((r for r in rows if r["id"] == sim_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Open simulation {sim_id} not found")

    close_price = deps.get_live_price(target["pair"], profile=profile) or 0.0
    if close_price <= 0:
        close_price = target["entry_price"]  # Fallback to entry price

    result = db.close_simulated_trade(sim_id=sim_id, close_price=close_price)
    if not result:
        raise HTTPException(status_code=404, detail=f"Simulation {sim_id} not found or already closed")
    return result


@router.get("/api/candles", summary="OHLCV candle data for a trading pair")
def get_candles(
    pair: str = Query(..., description="Trading pair, e.g. BTC-EUR"),
    granularity: str = Query("ONE_HOUR", description="Candle granularity"),
    limit: int = Query(200, ge=10, le=1000),
    profile: str = Query("", description="Exchange profile"),
):
    """Returns OHLCV candle data from the exchange for charting."""
    client = deps.client_for_profile(profile)
    if not client:
        raise HTTPException(status_code=503, detail="Exchange client not available")
    try:
        candles = client.get_candles(pair, granularity=granularity, limit=limit)
        if not candles:
            return {"candles": [], "pair": pair}
        return deps.sanitize_floats({"candles": candles, "pair": pair, "count": len(candles)})
    except Exception as exc:
        logger.warning(f"candles error for {pair}: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")
