"""
LLM-as-judge sampler for reasoning quality.

Samples a small fraction of recent ``agent_reasoning`` rows, prompts a
local LLM (Ollama) to score each on actionable / generic / confused, and
persists scores into ``reasoning_judge``.

Cheap by design: small sample, low temperature, capped input, <=200 tokens. Skipped if the
LLM is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("llm_judge")

_JUDGE_SYSTEM = (
    "You are an impartial reasoning auditor for an autonomous trading agent. "
    "You judge a single reasoning blob on three axes:\n"
    "  - actionable: cites specific data / numbers / rules and reaches a clear decision.\n"
    "  - generic: vague / boilerplate / no specific evidence.\n"
    "  - confused: contradicts itself / hallucinates fields / off-topic.\n"
    "Respond ONLY with strict JSON: "
    '{"verdict":"actionable|generic|confused","score":0.0..1.0,"rationale":"one sentence"}'
)


def _fetch_sample(db, exchange: str, *, lookback_hours: int, sample_pct: float) -> list[dict]:
    if sample_pct <= 0:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=int(lookback_hours))
    sql = (
        "SELECT cycle_id, agent_name, pair, signal_type, confidence, reasoning_json, ts "
        "FROM agent_reasoning "
        "WHERE exchange = %s AND ts >= %s "
        "AND reasoning_json IS NOT NULL "
        "ORDER BY ts DESC LIMIT 5000"
    )
    try:
        with db._get_conn() as conn:
            rows = conn.execute(sql, (exchange, since.isoformat())).fetchall()
    except Exception as e:
        logger.warning(f"llm_judge: fetch failed: {e}")
        return []
    rows = [dict(r) for r in rows]
    rng = random.Random(42)
    sampled = [r for r in rows if rng.random() < float(sample_pct)]
    anomalies = [r for r in rows if _reasoning_is_anomaly(r)]
    merged: list[dict] = []
    seen: set[tuple] = set()
    for row in anomalies + sampled:
        key = (row.get("cycle_id"), row.get("agent_name"), row.get("pair"), row.get("ts"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _reasoning_is_anomaly(row: dict) -> bool:
    blob = str(row.get("reasoning_json") or "")
    if any(marker in blob.lower() for marker in ("error", "fallback", "schema", "invalid")):
        return True
    try:
        confidence = float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    signal = str(row.get("signal_type") or "").lower()
    if signal in {"strong_buy", "strong_sell"} and confidence < 0.65:
        return True
    if signal == "neutral" and confidence > 0.80:
        return True
    return False


async def _judge_one(llm, row: dict, *, reasoning_max_chars: int = 1200) -> Optional[dict]:
    blob = row.get("reasoning_json") or "{}"
    user_msg = (
        f"Agent: {row.get('agent_name')}\n"
        f"Pair: {row.get('pair') or 'N/A'}\n"
        f"Reasoning JSON (truncated):\n{blob[:reasoning_max_chars]}"
    )
    try:
        resp = await llm.chat_json(
            system_prompt=_JUDGE_SYSTEM,
            user_message=user_msg,
            temperature=0.1,
            max_tokens=200,
            agent_name="reasoning_judge",
            priority="low",
        )
    except Exception as e:
        logger.debug(f"llm_judge: call failed: {e}")
        return None
    verdict = str(resp.get("verdict") or "").lower()
    if verdict not in ("actionable", "generic", "confused"):
        verdict = "generic"
    try:
        score = float(resp.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return {
        "cycle_id": row.get("cycle_id"),
        "agent": row.get("agent_name"),
        "pair": row.get("pair"),
        "verdict": verdict,
        "score": max(0.0, min(1.0, score)),
        "rationale": str(resp.get("rationale") or "")[:1500],
    }


async def sample_and_judge(
    db,
    llm,
    *,
    exchange: str,
    lookback_hours: int = 24,
    sample_pct: float = 0.01,
    max_judgments: int = 50,
    reasoning_max_chars: int = 1200,
) -> dict:
    """Sample reasoning rows and score with the LLM."""
    if llm is None:
        return {"sampled": 0, "judged": 0, "skipped": True, "reason": "no_llm"}
    sample = _fetch_sample(db, exchange, lookback_hours=lookback_hours, sample_pct=sample_pct)
    if not sample:
        return {"sampled": 0, "judged": 0}
    sample = sample[:max_judgments]
    judgments: list[dict] = []
    sem = asyncio.Semaphore(2)  # limit concurrent LLM calls

    async def _bounded(row):
        async with sem:
            j = await _judge_one(llm, row, reasoning_max_chars=reasoning_max_chars)
            if j:
                judgments.append(j)

    await asyncio.gather(*(_bounded(r) for r in sample), return_exceptions=True)

    written = 0
    if judgments:
        try:
            written = db.write_reasoning_judge(exchange, judgments)
        except Exception as e:
            logger.warning(f"llm_judge: write failed: {e}")

    logger.info(
        f"llm_judge: {exchange} sampled={len(sample)} judged={len(judgments)} "
        f"written={written}"
    )
    return {"sampled": len(sample), "judged": len(judgments), "written": written}
