"""
LLM Advisor (Phase 6).

LLMs are demoted from the critical alpha path to an *advisory* role.
They never directly execute trades, never bypass AbsoluteRules, and
never silently mutate live parameters. Instead they:

  • classify news/events into structured signals;
  • explain anomalies in human-readable form for ops/postmortems;
  • author postmortems on losing trades or circuit-breaker trips;
  • PROPOSE parameter deltas that go through a 24h shadow-test gate
    before they can possibly become live.

The advisor itself is transport-agnostic — it accepts a callable
``llm_client(prompt: str) -> str`` so production can wire any
provider (Ollama, OpenAI, Anthropic, etc.) and tests use a stub.

ShadowTester is the safety mechanism: it holds pending deltas in an
on-disk queue, accumulates shadow PnL vs live PnL, and only marks a
delta "promotable" once shadow has outperformed live by
``min_shadow_edge`` for ``min_observation_seconds``. Even then,
promotion is a SEPARATE call (so the orchestrator owns the final
decision). This preserves autonomy while adding a hard veto.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# --------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class NewsClassification:
    sentiment: str                # "bullish" | "bearish" | "neutral"
    severity: float               # [0, 1]
    affected_assets: tuple[str, ...]
    reasoning: str
    confidence: float


@dataclass(frozen=True)
class AnomalyExplanation:
    summary: str
    likely_causes: tuple[str, ...]
    suggested_actions: tuple[str, ...]


@dataclass(frozen=True)
class Postmortem:
    title: str
    timeline: str
    root_cause: str
    lessons: tuple[str, ...]
    recommendations: tuple[str, ...]


@dataclass
class PendingDelta:
    """A proposed parameter change waiting in the shadow queue."""
    delta_id: str
    strategy: str
    proposed_params: dict
    rationale: str
    created_at: float
    shadow_pnl: float = 0.0
    live_pnl: float = 0.0
    observations: int = 0
    last_observed_at: float = 0.0
    status: str = "pending"          # "pending" | "promotable" | "rejected"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingDelta":
        return cls(**d)


# --------------------------------------------------------------------- #
# LLM advisor
# --------------------------------------------------------------------- #

class LLMAdvisor:
    """Stateless façade over a pluggable LLM transport."""

    def __init__(
        self,
        llm_client: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.llm_client = llm_client or _null_llm

    # ----- News --------------------------------------------------------- #

    def classify_news(self, headline: str, body: str = "") -> NewsClassification:
        prompt = (
            "Classify the following news as bullish, bearish, or neutral for "
            "crypto markets. Reply STRICTLY in JSON with keys: "
            "sentiment, severity (0-1), affected_assets (list), reasoning, confidence (0-1).\n\n"
            f"HEADLINE: {headline}\nBODY: {body}\n"
        )
        raw = self._safe_call(prompt)
        data = _json_or_default(raw, {
            "sentiment": "neutral",
            "severity": 0.0,
            "affected_assets": [],
            "reasoning": "llm unavailable",
            "confidence": 0.0,
        })
        return NewsClassification(
            sentiment=str(data.get("sentiment", "neutral")).lower(),
            severity=_clip01(data.get("severity", 0.0)),
            affected_assets=tuple(str(a) for a in (data.get("affected_assets") or [])),
            reasoning=str(data.get("reasoning", "")),
            confidence=_clip01(data.get("confidence", 0.0)),
        )

    # ----- Anomaly ------------------------------------------------------ #

    def explain_anomaly(self, context: dict[str, Any]) -> AnomalyExplanation:
        prompt = (
            "An automated trading system observed an anomaly. Given the JSON "
            "context, explain (a) what likely happened, (b) likely root causes, "
            "(c) recommended ops actions. Reply JSON: "
            "{summary, likely_causes:[...], suggested_actions:[...]}.\n\n"
            f"CONTEXT: {json.dumps(context, default=str)}"
        )
        raw = self._safe_call(prompt)
        data = _json_or_default(raw, {
            "summary": "Anomaly observed; LLM unavailable.",
            "likely_causes": [],
            "suggested_actions": ["manual_review"],
        })
        return AnomalyExplanation(
            summary=str(data.get("summary", "")),
            likely_causes=tuple(str(x) for x in (data.get("likely_causes") or [])),
            suggested_actions=tuple(str(x) for x in (data.get("suggested_actions") or [])),
        )

    # ----- Postmortem --------------------------------------------------- #

    def write_postmortem(self, event: dict[str, Any]) -> Postmortem:
        prompt = (
            "Write a brief postmortem (incident report) for this trading event. "
            "Reply JSON: {title, timeline, root_cause, lessons:[...], recommendations:[...]}.\n\n"
            f"EVENT: {json.dumps(event, default=str)}"
        )
        raw = self._safe_call(prompt)
        data = _json_or_default(raw, {
            "title": "Postmortem (auto)",
            "timeline": "",
            "root_cause": "unknown",
            "lessons": [],
            "recommendations": [],
        })
        return Postmortem(
            title=str(data.get("title", "Postmortem")),
            timeline=str(data.get("timeline", "")),
            root_cause=str(data.get("root_cause", "")),
            lessons=tuple(str(x) for x in (data.get("lessons") or [])),
            recommendations=tuple(str(x) for x in (data.get("recommendations") or [])),
        )

    # ----- Internals ---------------------------------------------------- #

    def _safe_call(self, prompt: str) -> str:
        try:
            return str(self.llm_client(prompt) or "")
        except Exception:
            return ""


# --------------------------------------------------------------------- #
# Shadow tester for proposed parameter deltas
# --------------------------------------------------------------------- #

class ShadowTester:
    """Hold proposed parameter deltas behind a shadow-PnL gate."""

    def __init__(
        self,
        *,
        state_path: Optional[str] = None,
        min_observation_seconds: int = 24 * 3600,
        min_observations: int = 20,
        min_shadow_edge: float = 0.005,  # +0.5pp shadow vs. live cumulative PnL
    ) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.min_observation_seconds = int(min_observation_seconds)
        self.min_observations = int(min_observations)
        self.min_shadow_edge = float(min_shadow_edge)
        self._pending: dict[str, PendingDelta] = self._load()

    # ------------------------------------------------------------------ #

    def propose(self, strategy: str, params: dict, rationale: str = "") -> str:
        delta_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._pending[delta_id] = PendingDelta(
            delta_id=delta_id,
            strategy=strategy,
            proposed_params=dict(params),
            rationale=rationale,
            created_at=now,
        )
        self._save()
        return delta_id

    def observe(self, delta_id: str, *, shadow_pnl: float, live_pnl: float) -> None:
        d = self._pending.get(delta_id)
        if d is None or d.status != "pending":
            return
        d.shadow_pnl += float(shadow_pnl)
        d.live_pnl += float(live_pnl)
        d.observations += 1
        d.last_observed_at = time.time()
        # Promotability check.
        elapsed = d.last_observed_at - d.created_at
        if (
            d.observations >= self.min_observations
            and elapsed >= self.min_observation_seconds
            and (d.shadow_pnl - d.live_pnl) >= self.min_shadow_edge
        ):
            d.status = "promotable"
        self._save()

    def get(self, delta_id: str) -> Optional[PendingDelta]:
        return self._pending.get(delta_id)

    def list_promotable(self) -> list[PendingDelta]:
        return [d for d in self._pending.values() if d.status == "promotable"]

    def reject(self, delta_id: str, reason: str = "") -> None:
        d = self._pending.get(delta_id)
        if d is not None:
            d.status = "rejected"
            self._save()

    def consume(self, delta_id: str) -> Optional[PendingDelta]:
        """Atomically remove and return a delta (used after promotion)."""
        d = self._pending.pop(delta_id, None)
        if d is not None:
            self._save()
        return d

    # ------------------------------------------------------------------ #

    def _load(self) -> dict[str, PendingDelta]:
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text())
            return {k: PendingDelta.from_dict(v) for k, v in (raw or {}).items()}
        except Exception:
            return {}

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {k: v.to_dict() for k, v in self._pending.items()},
            indent=2, sort_keys=True,
        ))
        os.replace(tmp, self.state_path)


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #

def _null_llm(_: str) -> str:
    return ""


def _json_or_default(s: str, default: dict) -> dict:
    if not s:
        return default
    s = s.strip()
    # Strip markdown fences if present.
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.startswith("```")]
        s = "\n".join(lines).strip()
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return default


def _clip01(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


__all__ = [
    "LLMAdvisor", "ShadowTester",
    "NewsClassification", "AnomalyExplanation", "Postmortem",
    "PendingDelta",
]
