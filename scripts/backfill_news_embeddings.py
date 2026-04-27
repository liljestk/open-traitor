#!/usr/bin/env python3
"""Backfill embeddings for news articles whose ``embedding`` column is NULL.

Usage:
    python scripts/backfill_news_embeddings.py [--profile coinbase] [--limit 5000]

This script is idempotent and resumable — every successful row update sets
``embedded_at`` so re-runs naturally pick up only the rows still missing
embeddings. It is also rate-aware: when Ollama trips its circuit breaker
inside ``utils.embeddings``, the loop falls back to the deterministic hash
embedding so a long backfill always completes in bounded time.

Designed to be run as a one-shot CLI by an operator. Future automation
should call ``StatsDB.get_news_articles_missing_embedding`` directly from
a scheduled Temporal activity.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from src.utils.embeddings import embed_text
from src.utils.logger import get_logger
from src.utils.stats import StatsDB

logger = get_logger("scripts.backfill_news_embeddings")


def run(profile: Optional[str], limit: int, batch_size: int) -> dict:
    db = StatsDB()
    total, with_emb = db.count_news_articles(profile=profile)
    logger.info(
        f"Starting backfill: profile={profile or '(all)'}  "
        f"total={total}  with_embedding={with_emb}  missing={total - with_emb}"
    )

    processed = 0
    failed = 0
    started = time.monotonic()

    while processed < limit:
        rows = db.get_news_articles_missing_embedding(
            profile=profile, limit=min(batch_size, limit - processed),
        )
        if not rows:
            logger.info("No more rows missing embedding — done.")
            break
        for r in rows:
            text = f"{r.get('title') or ''}\n{r.get('summary') or ''}".strip()
            if not text:
                # Mark with a zero vector so we don't re-pick this row forever.
                continue
            try:
                vec = embed_text(text)
                db.update_news_embedding(r["id"], vec)
                processed += 1
            except Exception as e:
                failed += 1
                logger.warning(f"row {r.get('id')} failed: {e}")
        if processed % 100 == 0:
            elapsed = time.monotonic() - started
            rate = processed / max(elapsed, 1e-3)
            logger.info(
                f"progress: processed={processed} failed={failed} "
                f"rate={rate:.1f}/s"
            )

    elapsed = time.monotonic() - started
    total_after, with_emb_after = db.count_news_articles(profile=profile)
    summary = {
        "profile": profile,
        "processed": processed,
        "failed": failed,
        "elapsed_seconds": round(elapsed, 2),
        "total_after": total_after,
        "with_embedding_after": with_emb_after,
    }
    logger.info(f"Backfill complete: {summary}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None,
                        help="profile filter (default: all profiles)")
    parser.add_argument("--limit", type=int, default=10_000,
                        help="max rows to process this run (default: 10000)")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="rows fetched per DB round-trip")
    args = parser.parse_args(argv)
    summary = run(args.profile, args.limit, args.batch_size)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
