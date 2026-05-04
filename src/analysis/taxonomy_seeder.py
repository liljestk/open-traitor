"""
Asset taxonomy seeder.

Hydrates the ``asset_taxonomy`` table with a coarse classification per
symbol so the cross-asset engine can group by ecosystem, sector, and
custom tags. Sources are best-effort and deliberately offline-friendly
(no API keys required):

* **Crypto (coinbase / coinbase_paper):** static curated map of major
  ecosystems (BTC-family, ETH-L1, ETH-L2, stablecoin, …). Hand-tuned for
  the assets the bot routinely follows; unknown symbols default to the
  ``crypto-other`` ecosystem so the cluster engine can still group them
  via correlation.
* **Equity (ibkr):** uses ``yfinance.Ticker(sym).info`` to extract sector
  + industry. If yfinance is unavailable or the lookup fails, the symbol
  is recorded with ``ecosystem="equity"`` and no sector.

The seeder is idempotent — re-running merges new tags into existing rows
without dropping prior classifications. Manual operator overrides live in
``config/{profile}.taxonomy.yaml`` (optional) and always win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

import yaml

from src.utils.logger import get_logger
from src.utils.stats import StatsDB

logger = get_logger("analysis.taxonomy_seeder")


# ─── Curated crypto ecosystems ────────────────────────────────────────────

# Map quote-stripped base ticker → (ecosystem, tags_dict). The final
# ``symbol`` we persist always retains the full pair (e.g. "ETH-USD") so
# that lookups by exchange+symbol stay stable.
_CRYPTO_ECOSYSTEMS: dict[str, tuple[str, dict]] = {
    # BTC family
    "BTC":   ("BTC-family", {"layer": 1}),
    "BCH":   ("BTC-family", {"layer": 1, "fork_of": "BTC"}),
    "BSV":   ("BTC-family", {"layer": 1, "fork_of": "BTC"}),
    "LTC":   ("BTC-family", {"layer": 1, "fork_of": "BTC"}),
    # Ethereum L1
    "ETH":   ("ETH-L1", {"layer": 1, "smart_contracts": True}),
    "ETC":   ("ETH-L1", {"layer": 1, "fork_of": "ETH"}),
    # Ethereum-aligned L2 / scaling
    "ARB":   ("ETH-L2", {"layer": 2, "rollup": "optimistic"}),
    "OP":    ("ETH-L2", {"layer": 2, "rollup": "optimistic"}),
    "MATIC": ("ETH-L2", {"layer": 2, "rollup": "zk"}),
    "POL":   ("ETH-L2", {"layer": 2, "rollup": "zk"}),
    "IMX":   ("ETH-L2", {"layer": 2, "rollup": "zk", "category": "gaming"}),
    "STRK":  ("ETH-L2", {"layer": 2, "rollup": "zk"}),
    # High-throughput L1s
    "SOL":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "AVAX":  ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "ADA":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "DOT":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "ATOM":  ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "NEAR":  ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "APT":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "SUI":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "TON":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "XRP":   ("alt-L1", {"layer": 1}),
    "XLM":   ("alt-L1", {"layer": 1}),
    "TRX":   ("alt-L1", {"layer": 1, "smart_contracts": True}),
    "ALGO":  ("alt-L1", {"layer": 1, "smart_contracts": True}),
    # Stablecoins
    "USDT":  ("stablecoin", {"peg": "USD"}),
    "USDC":  ("stablecoin", {"peg": "USD"}),
    "DAI":   ("stablecoin", {"peg": "USD"}),
    "USDS":  ("stablecoin", {"peg": "USD"}),
    "PYUSD": ("stablecoin", {"peg": "USD"}),
    "EURC":  ("stablecoin", {"peg": "EUR"}),
    "GUSD":  ("stablecoin", {"peg": "USD"}),
    # DeFi (Ethereum-native, mostly)
    "UNI":   ("defi", {"layer": 1, "category": "dex"}),
    "AAVE":  ("defi", {"layer": 1, "category": "lending"}),
    "MKR":   ("defi", {"layer": 1, "category": "lending"}),
    "COMP":  ("defi", {"layer": 1, "category": "lending"}),
    "CRV":   ("defi", {"layer": 1, "category": "dex"}),
    "SUSHI": ("defi", {"layer": 1, "category": "dex"}),
    "LDO":   ("defi", {"layer": 1, "category": "staking"}),
    "RPL":   ("defi", {"layer": 1, "category": "staking"}),
    "SNX":   ("defi", {"layer": 1, "category": "derivatives"}),
    "1INCH": ("defi", {"layer": 1, "category": "dex"}),
    # Memes
    "DOGE":  ("meme", {"layer": 1}),
    "SHIB":  ("meme", {"layer": 1}),
    "PEPE":  ("meme", {"layer": 1}),
    "WIF":   ("meme", {"layer": 1}),
    "BONK":  ("meme", {"layer": 1}),
    # Oracle / infra
    "LINK":  ("infra", {"category": "oracle"}),
    "GRT":   ("infra", {"category": "indexer"}),
    "FIL":   ("infra", {"category": "storage"}),
    "AR":    ("infra", {"category": "storage"}),
    # Gaming / NFT
    "SAND":  ("gaming", {}),
    "MANA":  ("gaming", {}),
    "AXS":   ("gaming", {}),
    "GALA":  ("gaming", {}),
    "ENJ":   ("gaming", {}),
    "APE":   ("gaming", {}),
    # Privacy
    "XMR":   ("privacy", {"layer": 1}),
    "ZEC":   ("privacy", {"layer": 1}),
    "DASH":  ("privacy", {"layer": 1}),
}


def _strip_quote(symbol: str) -> str:
    """Return base ticker. Handles both ``ETH-USD`` and ``ETHUSD`` forms."""
    s = symbol.upper().strip()
    if "-" in s:
        return s.split("-", 1)[0]
    for q in ("USDC", "USDT", "USD", "EUR", "GBP", "BTC"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _load_overrides(profile: str) -> list[dict]:
    """Load ``config/{profile}.taxonomy.yaml`` if it exists."""
    safe = "".join(c for c in profile if c.isalnum() or c in "-_")
    path = Path("config") / f"{safe}.taxonomy.yaml"
    if not path.exists():
        return []
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as e:
        logger.warning(f"_load_overrides({path}) failed: {e}")
        return []
    items = data.get("assets", [])
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict) or "symbol" not in it:
            continue
        out.append(it)
    return out


def seed_crypto_taxonomy(
    exchange: str,
    symbols: Iterable[str],
    *,
    stats_db: Optional[StatsDB] = None,
) -> int:
    """Populate ``asset_taxonomy`` for the crypto symbols of an exchange."""
    db = stats_db or StatsDB()
    rows: list[dict] = []
    for sym in symbols:
        base = _strip_quote(sym)
        eco, tags = _CRYPTO_ECOSYSTEMS.get(base, ("crypto-other", {}))
        rows.append({
            "exchange": exchange,
            "symbol": sym,
            "asset_class": "crypto",
            "ecosystem": eco,
            "sector": None,
            "tags": {"base": base, **tags},
            "source": "curated",
        })
    return db.upsert_asset_taxonomy(rows)


def seed_equity_taxonomy(
    exchange: str,
    symbols: Iterable[str],
    *,
    stats_db: Optional[StatsDB] = None,
) -> int:
    """Populate ``asset_taxonomy`` for equity symbols using yfinance.

    Best-effort: any symbol that fails yfinance lookup is still inserted
    with ``ecosystem="equity"`` so the cluster engine has it.
    """
    db = stats_db or StatsDB()
    rows: list[dict] = []
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except Exception:
        yf = None
        logger.warning("yfinance unavailable — equity taxonomy will be coarse")
    consecutive_failures = 0
    for sym in symbols:
        sector: Optional[str] = None
        industry: Optional[str] = None
        ecosystem = "equity"
        if yf is not None:
            try:
                info = yf.Ticker(sym).info or {}
                sector = info.get("sector") or None
                industry = info.get("industry") or None
                if sector:
                    ecosystem = f"equity-{sector.lower().replace(' ', '-')}"
                consecutive_failures = 0
            except Exception as e:
                logger.debug(f"yfinance({sym}) failed: {e}")
                consecutive_failures += 1
                # If the cookie/crumb endpoint (fc.yahoo.com) is blocked,
                # every subsequent ``.info`` call will fail the same way.
                # Bail out after a few consecutive failures and degrade
                # gracefully (ecosystem stays as the coarse "equity" tag).
                if consecutive_failures >= 3:
                    logger.warning(
                        "yfinance taxonomy lookup failing repeatedly — "
                        "degrading remaining symbols to coarse 'equity' tag"
                    )
                    yf = None
        rows.append({
            "exchange": exchange,
            "symbol": sym,
            "asset_class": "equity",
            "ecosystem": ecosystem,
            "sector": sector,
            "tags": {"industry": industry} if industry else {},
            "source": "yfinance" if (yf is not None and sector) else "manual",
        })
    return db.upsert_asset_taxonomy(rows)


def seed_taxonomy_for_profile(
    profile: str,
    symbols: Iterable[str],
    *,
    stats_db: Optional[StatsDB] = None,
) -> int:
    """Convenience: seed taxonomy + apply overrides for one profile."""
    db = stats_db or StatsDB()
    profile_lower = profile.lower()
    is_equity = profile_lower in {"ibkr"}
    exchange = "ibkr" if is_equity else (
        "coinbase" if profile_lower == "crypto" else profile_lower
    )
    syms = list(symbols)
    written = 0
    if syms:
        if is_equity:
            written += seed_equity_taxonomy(exchange, syms, stats_db=db)
        else:
            written += seed_crypto_taxonomy(exchange, syms, stats_db=db)
    overrides = _load_overrides(profile)
    if overrides:
        for row in overrides:
            row.setdefault("exchange", exchange)
            row.setdefault("source", "operator-override")
        written += db.upsert_asset_taxonomy(overrides)
    logger.info(
        f"seed_taxonomy_for_profile({profile}): {written} rows upserted"
    )
    return written
