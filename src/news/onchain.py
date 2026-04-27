"""
Free-tier on-chain signal aggregator.

Pulls a small basket of high-signal on-chain metrics from no-API-key sources
and persists them to ``onchain_signals`` for the strategist to consume.

Sources (all free, public, rate-limited politely):
  * Blockchain.info charts API → BTC exchange-balance proxy, hash rate
  * Defillama → stablecoin total supply (USDT, USDC) + dominance
  * CoinGecko global → total crypto market-cap dominance shifts

Designed to run from a Temporal activity hourly. Best-effort: every fetcher
returns ``[]`` on failure, so partial outages never block the worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import requests

from src.utils.logger import get_logger

logger = get_logger("onchain")

_HTTP_TIMEOUT = 10
_USER_AGENT = "OpenTraitor-OnChain/1.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_btc_hashrate() -> list[dict]:
    """7-day-avg hash rate from blockchain.info (public, no key)."""
    url = "https://api.blockchain.info/charts/hash-rate"
    params = {"timespan": "7days", "format": "json", "rollingAverage": "1day"}
    try:
        r = requests.get(
            url, params=params, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(f"onchain: hash-rate fetch failed: {e}")
        return []
    out = []
    for pt in (data.get("values") or [])[-24:]:
        ts = pt.get("x")
        v = pt.get("y")
        if ts is None or v is None:
            continue
        out.append({
            "asset": "BTC",
            "metric": "hash_rate_th",
            "ts": datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(),
            "value": float(v),
            "source": "blockchain_info",
        })
    return out


def _fetch_stablecoin_supply() -> list[dict]:
    """Total stablecoin supply (USD) from Defillama (public)."""
    url = "https://stablecoins.llama.fi/stablecoins"
    try:
        r = requests.get(
            url, params={"includePrices": "false"},
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(f"onchain: stablecoin fetch failed: {e}")
        return []
    out = []
    now = _now_iso()
    for coin in (data.get("peggedAssets") or [])[:20]:
        sym = (coin.get("symbol") or "").upper()
        if not sym:
            continue
        circ = coin.get("circulating") or {}
        total = circ.get("peggedUSD") or circ.get("peggedVAR")
        if total is None:
            continue
        try:
            out.append({
                "asset": sym,
                "metric": "supply_usd",
                "ts": now,
                "value": float(total),
                "source": "defillama",
            })
        except (TypeError, ValueError):
            continue
    return out


def _fetch_btc_dominance() -> list[dict]:
    """BTC dominance % from CoinGecko global endpoint (no key, rate-limited)."""
    url = "https://api.coingecko.com/api/v3/global"
    try:
        r = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(f"onchain: btc dominance fetch failed: {e}")
        return []
    out = []
    market_cap_pct = (data.get("data") or {}).get("market_cap_percentage") or {}
    now = _now_iso()
    for sym, pct in market_cap_pct.items():
        try:
            out.append({
                "asset": sym.upper(),
                "metric": "market_cap_dominance_pct",
                "ts": now,
                "value": float(pct),
                "source": "coingecko",
            })
        except (TypeError, ValueError):
            continue
    return out


def fetch_and_persist_onchain(db, *, exchange: str) -> dict:
    """Pull all on-chain feeds and upsert to ``onchain_signals``."""
    if exchange not in ("coinbase", "coinbase_paper"):
        # Equity profiles don't consume on-chain signals.
        return {"persisted": 0, "skipped": True, "reason": "non-crypto profile"}

    rows: list[dict] = []
    sources_count: dict[str, int] = {}

    for fetcher, name in (
        (_fetch_btc_hashrate, "blockchain_info"),
        (_fetch_stablecoin_supply, "defillama"),
        (_fetch_btc_dominance, "coingecko"),
    ):
        try:
            r = fetcher()
        except Exception as e:
            logger.warning(f"onchain: {name} raised: {e}")
            r = []
        sources_count[name] = len(r)
        rows.extend(r)

    persisted = 0
    if rows:
        try:
            persisted = db.upsert_onchain(exchange, rows)
        except Exception as e:
            logger.warning(f"onchain: upsert failed: {e}")

    logger.info(
        f"onchain: {exchange} persisted={persisted} sources={sources_count}"
    )
    return {"persisted": persisted, "sources": sources_count}
