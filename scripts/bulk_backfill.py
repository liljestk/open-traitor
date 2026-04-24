#!/usr/bin/env python3
"""Bulk historical OHLCV backfill CLI for the Catalyst Pattern Engine.

Usage:
    python scripts/bulk_backfill.py --profile coinbase \
        --granularity ONE_HOUR,ONE_DAY --since 2018-01-01

If --symbols is omitted, the union of (config trading.pairs ∪ pair_follows
in the DB) for the profile is used.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow ``python scripts/bulk_backfill.py`` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.history_bulk_backfill import bulk_backfill_universe  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402
from src.utils.settings_manager import load_settings  # noqa: E402
from src.utils.stats import StatsDB  # noqa: E402

logger = get_logger("scripts.bulk_backfill")


def _resolve_symbols(profile: str, override: list[str] | None) -> list[str]:
    if override:
        return override
    cfg = load_settings()
    pairs = list(cfg.get("trading", {}).get("pairs", []) or [])
    db = StatsDB()
    try:
        from src.analysis.history_bulk_backfill import _profile_exchange
        exchange = _profile_exchange(profile)
        with db._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT pair FROM pair_follows WHERE exchange = %s",
                (exchange,),
            ).fetchall()
        followed = [r["pair"] for r in rows]
    except Exception:
        followed = []
    return sorted(set(pairs) | set(followed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True,
                        choices=["coinbase", "coinbase_paper", "ibkr"])
    parser.add_argument("--granularity", default="ONE_HOUR,ONE_DAY",
                        help="Comma-separated granularities")
    parser.add_argument("--since", default="2018-01-01",
                        help="ISO date or datetime (UTC)")
    parser.add_argument("--symbols", default="",
                        help="Optional comma-separated symbol override")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan and exit without fetching")
    args = parser.parse_args()

    os.environ["AUTO_TRAITOR_PROFILE"] = args.profile
    grans = [g.strip() for g in args.granularity.split(",") if g.strip()]
    overrides = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    symbols = _resolve_symbols(args.profile, overrides)
    try:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.error(f"Invalid --since: {args.since!r}")
        return 2

    if not symbols:
        logger.error("No symbols resolved — provide --symbols or populate trading.pairs")
        return 1

    plan = {
        "profile": args.profile,
        "granularities": grans,
        "since": since.isoformat(),
        "symbols": symbols,
    }
    print(f"Plan: {plan}")
    if args.dry_run:
        return 0

    summary = bulk_backfill_universe(
        profile=args.profile,
        symbols=symbols,
        granularities=grans,
        since=since,
    )
    total = sum(sum(v.values()) for v in summary.values())
    print(f"Backfill complete: {total} rows written")
    for sym, by_g in summary.items():
        for g, n in by_g.items():
            if n:
                print(f"  {sym} {g}: +{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
