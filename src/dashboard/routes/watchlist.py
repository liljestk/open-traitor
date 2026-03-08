"""Watchlist and pair follow/unfollow routes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

import src.dashboard.deps as deps
from src.utils.logger import get_logger
from src.utils.rpm_budget import compute_rpm_entity_cap

logger = get_logger("dashboard.watchlist")

router = APIRouter(tags=["Watchlist"])


def _notify_orchestrator(action: str, pair: str, profile: str = "") -> None:
    """Push a signed watchlist-update command to the orchestrator via Redis.

    Silently no-ops if Redis or the signing key is not configured.
    """
    if not deps.redis_client or not deps.DASHBOARD_COMMAND_SIGNING_KEY:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat()
        nonce = uuid.uuid4().hex
        cmd = {
            "action": action,
            "pair": pair,
            "ts": ts,
            "source": "dashboard",
            "nonce": nonce,
            "signature": deps.sign_dashboard_command(action, pair, ts, "dashboard", nonce),
        }
        _prefix = deps.resolve_profile(profile) or "coinbase"
        deps.redis_client.rpush(f"{_prefix}:dashboard:commands_queue", json.dumps(cmd))
    except Exception as exc:
        logger.debug(f"watchlist: Redis notify failed ({action} {pair}): {exc}")


class _FollowPairBody(BaseModel):
    pair: str
    exchange: str = ""  # auto-detected from pair when empty


@router.get("/api/watchlist", summary="Active pairs watchlist with scan results")
def get_watchlist(
    profile: str = Query("", description="Exchange profile"),
    db=Depends(deps.get_profile_db),
):
    """Returns the latest universe scan results + active pair configuration."""
    config = deps.get_config_for_profile(profile)
    qc = deps.quote_currency_for(profile)
    resolved = deps.resolve_profile(profile)
    try:
        scan = db.get_latest_scan_results(exchange=resolved or None)
        pairs = config.get("trading", {}).get("pairs", [])

        # Fetch all follows once — used for both LLM merge and follow status
        all_follows = db.get_pair_follows(exchange=resolved or None, quote_currency=qc)

        # Also include LLM-followed pairs from the DB (runtime-discovered by the screener)
        llm_db_pairs = [
            f["pair"] for f in all_follows if f.get("followed_by") == "llm"
        ]
        # Merge: config pairs + DB LLM pairs (dedup, preserve order)
        seen = {p.upper() for p in pairs}
        for lp in llm_db_pairs:
            if lp.upper() not in seen:
                pairs.append(lp)
                seen.add(lp.upper())

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
                suffixes = [f"-{c.upper()}" for c in (qc if isinstance(qc, list) else [qc])]
                if isinstance(scan_data.get("results_json"), dict):
                    scan_data["results_json"] = {
                        k: v for k, v in scan_data["results_json"].items()
                        if any(k.upper().endswith(s) for s in suffixes)
                    }
                if isinstance(scan_data.get("top_movers"), list):
                    scan_data["top_movers"] = [
                        m for m in scan_data["top_movers"]
                        if isinstance(m, dict) and any(m.get("pair", "").upper().endswith(s) for s in suffixes)
                    ]

        # Filter active pairs by quote currency
        if qc:
            suffixes = [f"-{c.upper()}" for c in (qc if isinstance(qc, list) else [qc])]
            pairs = [p for p in pairs if any(p.upper().endswith(s) for s in suffixes)]
            live_prices = {k: v for k, v in live_prices.items() if any(k.upper().endswith(s) for s in suffixes)}

        # Build follow status for each pair from the DB
        # Index follows by pair → set of followed_by values
        follow_map: dict[str, set[str]] = {}
        for f in all_follows:
            follow_map.setdefault(f["pair"].upper(), set()).add(f["followed_by"])

        # Config pairs are marked as "config" source — don't fake an LLM badge
        config_pairs_upper = {p.upper() for p in config.get("trading", {}).get("pairs", [])}

        # Human-followed pairs that aren't in the config list
        human_followed = sorted({
            p for p, sources in follow_map.items()
            if "human" in sources and p not in {x.upper() for x in pairs}
        })

        # Build combined pair info list
        all_pairs = list(dict.fromkeys(pairs + human_followed))  # preserve order, dedup

        # Fetch live prices for ALL pairs (config + human-followed), capped at 30
        price_client = deps.client_for_profile(profile)
        if price_client and all_pairs:
            for pair in all_pairs[:30]:
                try:
                    live_prices[pair] = price_client.get_current_price(pair)
                except Exception:
                    pass

        pair_info = []
        for p in all_pairs:
            sources = follow_map.get(p.upper(), set())
            is_config = p.upper() in config_pairs_upper
            pair_info.append({
                "pair": p,
                "followed_by_llm": "llm" in sources,
                "followed_by_human": "human" in sources,
                "is_config_pair": is_config,
                "price": live_prices.get(p),
            })

        # Compute RPM budget to expose limits to the UI
        rpm_budget = None
        try:
            providers = config.get("llm_providers", [])
            interval = config.get("trading", {}).get("interval", 120)
            max_entities, breakdown = compute_rpm_entity_cap(providers, interval)
            configured_max = config.get("trading", {}).get("max_active_pairs", 5)
            rpm_budget = {
                **breakdown,
                "configured_max": configured_max,
                "effective_max": min(configured_max, max_entities),
            }
        except Exception as _rpm_err:
            logger.debug(f"watchlist rpm_budget enrichment skipped: {_rpm_err}")

        return deps.sanitize_floats({
            "active_pairs": pairs,
            "human_followed_pairs": human_followed,
            "pair_info": pair_info,
            "live_prices": live_prices,
            "scan": scan_data,
            "pair_count": len(all_pairs),
            "rpm_budget": rpm_budget,
        })
    except Exception as exc:
        logger.exception("watchlist error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/watchlist/follow", summary="Follow a pair (human)")
def follow_pair(body: _FollowPairBody, profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Add a pair to the human-curated watchlist.

    Watchlist pairs are displayed in the dashboard and are now fully integrated
    into the autonomous trading pipeline. They receive full LLM analysis, trade proposals,
    and risk validation just like system-configured pairs.
    """
    pair = body.pair.upper().strip()
    if not pair or "-" not in pair:
        raise HTTPException(status_code=400, detail=f"Invalid pair format: {pair!r}")

    # Detect exchange from profile or pair suffix
    resolved = deps.resolve_profile(profile)
    exchange = body.exchange or resolved or "coinbase"

    db.follow_pair(pair=pair, followed_by="human", exchange=exchange)
    _notify_orchestrator("add_watchlist_pair", pair, profile=profile)
    return {"ok": True, "pair": pair, "followed_by": "human", "exchange": exchange}


@router.delete("/api/watchlist/follow/{pair}", summary="Unfollow a pair (human)")
def unfollow_pair(pair: str, profile: str = Query(""), db=Depends(deps.get_profile_db)):
    """Remove a pair from the human-curated watchlist.

    Only removes the human follow — LLM follows (config pairs) are unaffected.
    """
    pair = pair.upper().strip()
    resolved = deps.resolve_profile(profile)
    deleted = db.unfollow_pair(pair=pair, followed_by="human", exchange=resolved or None)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Not following {pair!r}")
    _notify_orchestrator("remove_watchlist_pair", pair, profile=profile)
    return {"ok": True, "pair": pair, "unfollowed": True}
