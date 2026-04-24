"""
Pattern Agent — Catalyst Pattern Engine signal producer.

For each pair the agent:
  1. Looks up upcoming catalyst events within ``horizon_days`` (default 21).
  2. For the *nearest* catalyst, builds a current pre-window fingerprint
     and finds nearest-neighbour historical analogs via pgvector.
  3. Aggregates forward-return distribution into a ``PatternOutcome``.
  4. Optionally fuses with the current ``news_bias`` sentiment score.
  5. Emits a structured ``pattern_signal`` dict that the strategist + risk
     manager consume as advisory context — never overrides AbsoluteRules.

The agent is purely deterministic (no LLM call) so it is cheap to run
every cycle. When no upcoming catalyst is in range, it returns a benign
``{"available": False}`` payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.analysis.pattern_engine import (
    PRE_WINDOW_BARS,
    PatternOutcome,
    predict_for_upcoming,
)
from src.utils.logger import get_logger

logger = get_logger("agent.pattern")


class PatternAgent(BaseAgent):
    """Catalyst Pattern Engine agent — emits a deterministic pattern_signal."""

    def __init__(self, llm, state, config):
        super().__init__("pattern_agent", llm, state, config)
        pcfg = (config.get("pattern_engine") or {})
        self.horizon_days: int = int(pcfg.get("horizon_days", 21))
        self.granularity: str = pcfg.get("granularity", "ONE_DAY")
        self.k_neighbours: int = int(pcfg.get("k", 20))
        self.min_matches: int = int(pcfg.get("min_matches", 3))
        self.sentiment_weight: float = float(pcfg.get("sentiment_weight", 0.4))
        self.enabled: bool = bool(pcfg.get("enabled", True))

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"pattern_signal": {"available": False, "reason": "disabled"}}
        pair: str = context.get("pair", "")
        exchange: str = context.get("exchange", "coinbase")
        stats_db = context.get("stats_db")
        cycle_id: str = context.get("cycle_id", "")
        sentiment_score: Optional[float] = context.get("sentiment_score")
        if stats_db is None or not pair:
            return {"pattern_signal": {"available": False, "reason": "missing_context"}}

        # 1. Find nearest upcoming catalyst.
        try:
            upcoming = stats_db.get_upcoming_catalysts(
                exchange=exchange,
                horizon_days=self.horizon_days,
                symbol=pair,
            )
        except Exception as e:
            self.logger.debug(f"get_upcoming_catalysts({pair}) failed: {e}")
            return {"pattern_signal": {"available": False, "reason": "lookup_error"}}
        if not upcoming:
            return {"pattern_signal": {"available": False, "reason": "no_upcoming_catalyst"}}
        next_event = upcoming[0]
        anchor_ts: datetime = next_event["event_ts"]
        if anchor_ts.tzinfo is None:
            anchor_ts = anchor_ts.replace(tzinfo=timezone.utc)

        # 2/3/4. Predict.
        try:
            outcome: PatternOutcome = predict_for_upcoming(
                db=stats_db,
                exchange=exchange,
                symbol=pair,
                upcoming_event_ts=anchor_ts,
                event_type=next_event["event_type"],
                granularity=self.granularity,
                sentiment_score=sentiment_score,
                k=self.k_neighbours,
            )
        except Exception as e:
            self.logger.debug(f"predict_for_upcoming({pair}) failed: {e}")
            return {"pattern_signal": {"available": False, "reason": "predict_error"}}

        if outcome.n_matches < self.min_matches:
            payload = {
                "available": False,
                "reason": "insufficient_matches",
                "n_matches": outcome.n_matches,
                "upcoming_event": _serialise_event(next_event),
            }
            self._maybe_persist(stats_db, cycle_id, pair, exchange, payload)
            return {"pattern_signal": payload}

        payload = {
            "available": True,
            "upcoming_event": _serialise_event(next_event),
            "horizon_days": self.horizon_days,
            "pre_window_bars": PRE_WINDOW_BARS,
            "direction": outcome.direction,
            "expected_drift": outcome.expected_drift,
            "dispersion": outcome.dispersion,
            "n_matches": outcome.n_matches,
            "confidence": outcome.confidence,
            "matches": outcome.matches,
            "sentiment_score": sentiment_score,
        }
        self._maybe_persist(stats_db, cycle_id, pair, exchange, payload)
        self.logger.info(
            f"🧬 {pair}: pattern={outcome.direction} drift_5d="
            f"{outcome.expected_drift.get('5d', 0.0):+.4f} "
            f"matches={outcome.n_matches} conf={outcome.confidence:.2f} "
            f"(next: {next_event['event_type']} @ {anchor_ts.date()})"
        )
        return {"pattern_signal": payload}

    def _maybe_persist(
        self,
        stats_db,
        cycle_id: str,
        pair: str,
        exchange: str,
        payload: dict,
    ) -> None:
        if not cycle_id:
            return
        try:
            confidence = float(payload.get("confidence", 0.0))
            signal_type = payload.get("direction", "neutral")
            stats_db.save_reasoning(
                cycle_id=cycle_id,
                pair=pair,
                agent_name="pattern_agent",
                reasoning_json=payload,
                signal_type=signal_type,
                confidence=confidence,
                exchange=exchange,
            )
        except Exception as e:
            self.logger.debug(f"persist pattern reasoning failed: {e}")


def _serialise_event(ev: dict) -> dict:
    """Convert a catalyst-event row into a JSON-safe dict."""
    out = dict(ev)
    ts = out.get("event_ts")
    if isinstance(ts, datetime):
        out["event_ts"] = ts.isoformat()
    ins = out.get("inserted_at")
    if isinstance(ins, datetime):
        out["inserted_at"] = ins.isoformat()
    return out
