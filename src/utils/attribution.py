"""
Outcome attribution + counterfactual replay.

Two routines run nightly:

  * ``compute_attribution(db, exchange, lookback_days)``
        Joins ``trading_outcomes`` with the matching ``agent_reasoning``
        rows. For each (trade, feature) pair, writes a row to
        ``feature_attribution`` with the Brier score
        ``(confidence - outcome) ** 2`` so future ensemble tuning can
        prefer well-calibrated features.

  * ``replay_strategist(db, llm, exchange, lookback_days)``
        Re-runs the strategist's *current* prompt-building + LLM call
        on a sample of historical decision contexts and persists the
        replay action/confidence + a comparison vs the actual outcome.
        Lets us answer "would today's brain have done better?".

Both routines are read-mostly and best-effort; failures log and return
a partial summary.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("attribution")


def _fetch_recent_trades_with_reasoning(
    db, exchange: str, *, lookback_days: int, limit: int = 500
) -> list[dict]:
    """Buy-trade reasoning joined with the realised PnL of subsequent sells.

    The current schema stores the agent reasoning against the *buy* trade
    (``agent_reasoning.trade_id`` → ``trades.id`` of the BUY) while
    ``pnl`` is materialised on the matching *sell* row by the FIFO
    reconciler. To attribute outcomes back to the originating reasoning
    we therefore:

      1. Pick all BUY trades that have at least one linked reasoning row.
      2. For each, sum the realised pnl of subsequent SELL trades on the
         same (exchange, pair) within ``lookback_days``.
      3. Skip buys whose total realised pnl is zero / unknown.

    Returned dicts expose ``cycle_id`` (from agent_reasoning), ``pair``
    and ``pnl`` so downstream code can compute Brier scores.
    """
    since = datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
    sql = (
        "SELECT b.id AS trade_id, "
        "       COALESCE(r.cycle_id, '') AS cycle_id, "
        "       b.pair AS pair, "
        "       agg.realised_pnl AS pnl, "
        "       b.action AS action_taken, "
        "       r.agent_name, r.reasoning_json, r.ts AS decided_at "
        "FROM trades b "
        "JOIN agent_reasoning r "
        "  ON r.trade_id = b.id "
        "  AND r.exchange = b.exchange "
        "JOIN ( "
        "    SELECT b2.id AS buy_id, SUM(s.pnl) AS realised_pnl "
        "    FROM trades b2 "
        "    JOIN trades s "
        "      ON s.exchange = b2.exchange "
        "     AND s.pair = b2.pair "
        "     AND s.action = 'sell' "
        "     AND s.pnl IS NOT NULL "
        "     AND s.ts > b2.ts "
        "    WHERE b2.exchange = %s "
        "      AND b2.action = 'buy' "
        "      AND b2.ts >= %s "
        "    GROUP BY b2.id "
        ") agg ON agg.buy_id = b.id "
        "WHERE b.exchange = %s "
        "  AND r.agent_name IN ('strategist','risk_manager','market_analyst') "
        "ORDER BY b.ts DESC LIMIT %s"
    )
    try:
        with db._get_conn() as conn:
            rows = conn.execute(
                sql,
                (exchange, since.isoformat(), exchange, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"attribution: fetch_with_reasoning failed: {e}")
        return []


def compute_attribution(
    db, *, exchange: str, lookback_days: int = 30
) -> dict:
    """Compute Brier-score per (feature, agent) and persist.

    For each trade, we extract:
      * the agent's reported ``confidence`` ∈ [0,1]
      * a binary ``outcome``: 1 if pnl_pct > 0 else 0
      * any numeric features inside the reasoning_json (e.g. ``rsi``,
        ``sentiment``, ``regime_confidence``) so feature-level Brier can
        be computed downstream.

    Returns ``{trades: int, rows: int}``.
    """
    rows_in = _fetch_recent_trades_with_reasoning(
        db, exchange, lookback_days=lookback_days
    )
    if not rows_in:
        return {"trades": 0, "rows": 0}

    out_rows: list[dict] = []
    seen_trades: set[tuple] = set()
    for r in rows_in:
        try:
            j = json.loads(r["reasoning_json"]) if r.get("reasoning_json") else {}
        except Exception:
            j = {}
        conf = j.get("confidence")
        if conf is None:
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        try:
            pnl = float(r.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        outcome = 1.0 if pnl > 0 else 0.0
        brier = (conf - outcome) ** 2
        action = (j.get("action") or j.get("decision") or "hold")
        agent = r.get("agent_name") or "strategist"
        src_key = (r.get("cycle_id") or "", r.get("pair") or "")

        out_rows.append({
            "_src": src_key,
            "feature_name": f"{agent}.confidence",
            "feature_val": conf,
            "confidence": conf,
            "action": str(action),
            "outcome": outcome,
            "brier": brier,
        })
        seen_trades.add(src_key)

        # Feature-level attribution: any numeric child of reasoning_json
        for k, v in (j.items() if isinstance(j, dict) else []):
            if k in ("confidence", "action", "decision", "reasoning"):
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            out_rows.append({
                "_src": src_key,
                "feature_name": f"{agent}.{k}",
                "feature_val": fv,
                "confidence": conf,
                "action": str(action),
                "outcome": outcome,
                "brier": brier,
            })

    written = 0
    if out_rows:
        # Group by (cycle_id, pair) for the bulk write API.
        by_trade: dict[tuple, list[dict]] = {}
        for r in out_rows:
            key = r.pop("_src", ("", ""))
            by_trade.setdefault(key, []).append(r)
        for (cid, pair), batch in by_trade.items():
            try:
                written += db.write_feature_attribution(
                    exchange=exchange,
                    cycle_id=cid or "",
                    pair=pair or "",
                    rows=batch,
                )
            except Exception as e:
                logger.debug(f"attribution: write batch failed: {e}")

    logger.info(
        f"attribution: {exchange} trades={len(seen_trades)} rows={written}"
    )
    return {"trades": len(seen_trades), "rows": written}


async def replay_strategist(
    db,
    llm,
    *,
    exchange: str,
    lookback_days: int = 7,
    sample_size: int = 30,
) -> dict:
    """Re-run the current strategist prompt on historical contexts.

    For each sampled past decision, we reconstruct a minimal context
    (signal + sentiment from the original reasoning blob) and call the
    LLM. We persist actual vs replay action/confidence + actual P&L so
    the dashboard can show "would-today-have-done-better" deltas.

    Best-effort: on any error we log and continue.
    """
    if llm is None:
        return {"sampled": 0, "skipped": True, "reason": "no_llm"}

    rows = _fetch_recent_trades_with_reasoning(
        db, exchange, lookback_days=lookback_days, limit=200
    )
    rows = [r for r in rows if (r.get("agent_name") == "strategist")]
    if not rows:
        return {"sampled": 0}

    rng = random.Random(7)
    rng.shuffle(rows)
    rows = rows[:sample_size]

    out: list[dict] = []
    sem = asyncio.Semaphore(2)

    system_prompt = (
        "You are the strategist agent of an autonomous trading bot. "
        "Given a historical decision context, output the action you would "
        "take TODAY. Respond with strict JSON: "
        '{"action":"buy|sell|hold","confidence":0.0..1.0,"reasoning":"..."}'
    )

    async def _replay_one(r):
        try:
            orig = json.loads(r["reasoning_json"]) if r.get("reasoning_json") else {}
        except Exception:
            orig = {}
        actual_action = orig.get("action") or orig.get("decision") or "hold"
        actual_conf = orig.get("confidence")
        try:
            actual_conf = float(actual_conf)
        except (TypeError, ValueError):
            actual_conf = None

        # Minimal user context from the original reasoning blob.
        user_msg = (
            f"Historical context for {r.get('pair')} on "
            f"{(r.get('decided_at') or '?')!s}:\n{json.dumps(orig)[:3000]}"
        )
        async with sem:
            try:
                resp = await llm.chat_json(
                    system_prompt=system_prompt,
                    user_message=user_msg,
                    temperature=0.2,
                    max_tokens=300,
                    agent_name="counterfactual_replay",
                    priority="low",
                )
            except Exception as e:
                logger.debug(f"replay: llm call failed: {e}")
                return
            replay_action = (resp.get("action") or "hold").lower()
            try:
                replay_conf = float(resp.get("confidence") or 0.0)
            except (TypeError, ValueError):
                replay_conf = 0.0

            try:
                actual_pnl = float(r.get("pnl") or 0.0)
            except (TypeError, ValueError):
                actual_pnl = 0.0

            # If replay says hold but we actually traded → replay PnL = 0.
            # If replay agrees with action → replay PnL = actual.
            # If replay opposite → replay PnL = -actual (rough heuristic).
            if replay_action == actual_action:
                replay_pnl = actual_pnl
            elif replay_action == "hold":
                replay_pnl = 0.0
            else:
                replay_pnl = -actual_pnl

            out.append({
                "cycle_id": r.get("cycle_id"),
                "pair": r.get("pair"),
                "actual_action": actual_action,
                "replay_action": replay_action,
                "actual_conf": actual_conf,
                "replay_conf": replay_conf,
                "actual_pnl_pct": actual_pnl,
                "replay_pnl_pct": replay_pnl,
                "notes": str(resp.get("reasoning") or "")[:500],
            })

    await asyncio.gather(*(_replay_one(r) for r in rows), return_exceptions=True)

    written = 0
    if out:
        try:
            written = db.write_counterfactual(
                exchange=exchange,
                replay_date=datetime.now(timezone.utc),
                rows=out,
            )
        except Exception as e:
            logger.warning(f"replay: write failed: {e}")

    logger.info(
        f"replay: {exchange} sampled={len(rows)} replayed={len(out)} "
        f"written={written}"
    )
    return {"sampled": len(rows), "replayed": len(out), "written": written}
