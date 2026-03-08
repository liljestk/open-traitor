"""
Prompt Evolver — Generates meta-lessons from prediction patterns.

Weekly batch: clusters winning vs losing predictions by market_condition,
signal_type, and key_factors → generates concise behavioral corrections
via LLM call → stores as prompt supplements injected into agent prompts.

New DB table: ``prompt_supplements``
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("utils.prompt_evolver")

# Guardrails
_MAX_ACTIVE_SUPPLEMENTS = 5      # per agent
_MAX_SUPPLEMENT_CHARS = 2000     # total supplement text length cap
_MIN_SAMPLES_FOR_LESSON = 20    # minimum scored predictions to generate lessons

_EVOLUTION_SYSTEM_PROMPT = """You are a meta-learning engine for a cryptocurrency trading AI.
You analyze patterns in past prediction accuracy to generate behavioral corrections.

You will receive:
1. Factor attribution data showing which reasoning factors correlate with correct vs incorrect predictions
2. Regime accuracy data showing how well the agent performs in different market conditions
3. Confidence calibration data showing whether the agent is too confident or not confident enough

Generate 3-5 concise behavioral corrections — specific, actionable rules the trading agent
should follow to improve its accuracy. Each correction should be 1-2 sentences.

Focus on:
- Factors with negative lift (below baseline accuracy) — things the agent should be skeptical about
- Factors with high positive lift — things the agent should weight more heavily
- Regimes where accuracy is particularly low — conditions requiring caution
- Confidence bands where the agent is systematically miscalibrated

Output JSON:
{
    "lessons": [
        {
            "text": "When RSI divergence is cited as bullish, actual outcomes are bearish 62% of the time — treat RSI divergence with skepticism unless confirmed by volume.",
            "source_factor": "rsi divergence",
            "source_accuracy_pct": 38.0,
            "priority": 1
        }
    ],
    "summary": "Brief overall assessment of prediction patterns"
}
"""


class PromptEvolver:
    """Generates and manages prompt supplements from prediction patterns.

    Lifecycle:
        evolver = PromptEvolver(stats_db, scorecard, llm_client)
        evolver.load_active_supplements()      # on startup
        evolver.evolve_prompts()               # weekly
        supplements = evolver.get_supplements("market_analyst")
    """

    def __init__(self, stats_db, scorecard, llm_client=None, audit=None):
        self._db = stats_db
        self._scorecard = scorecard
        self._llm = llm_client
        self._audit = audit
        # In-memory: {agent_name: [supplement_dicts]}
        self._supplements: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS prompt_supplements (
            id SERIAL PRIMARY KEY,
            version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            agent_name TEXT NOT NULL,
            supplement_text TEXT NOT NULL,
            source_factor TEXT DEFAULT '',
            source_accuracy_pct REAL DEFAULT NULL,
            priority INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            deactivated_at TEXT DEFAULT NULL
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_supplements_agent ON prompt_supplements(agent_name, is_active)",
            "CREATE INDEX IF NOT EXISTS idx_supplements_version ON prompt_supplements(version)",
        ]

    # ------------------------------------------------------------------
    # Evolution (weekly)
    # ------------------------------------------------------------------

    async def evolve_prompts(self, window_days: int = 30) -> dict[str, Any]:
        """Generate new prompt supplements from recent prediction patterns.

        Returns summary of generated lessons.
        """
        if not self._llm:
            return {"skipped": True, "reason": "no_llm_client"}

        # Gather analysis data
        factor_attr = self._scorecard.get_factor_attribution(
            window_days=window_days, horizon_hours=24, min_occurrences=5
        )
        regime_accuracy = self._scorecard.get_regime_accuracy(
            window_days=window_days, horizon_hours=24
        )

        # Check we have enough data
        total_scored = sum(r.get("total", 0) for r in regime_accuracy.values())
        if total_scored < _MIN_SAMPLES_FOR_LESSON:
            return {
                "skipped": True,
                "reason": "insufficient_data",
                "total_scored": total_scored,
            }

        # Get calibration data summary
        calibration_summary = self._scorecard.get_calibration_data(
            window_days=window_days, horizon_hours=24
        )
        cal_buckets = self._summarize_calibration(calibration_summary)

        # Build LLM prompt
        analysis_context = self._format_analysis_context(
            factor_attr, regime_accuracy, cal_buckets
        )

        try:
            response = await self._llm.chat_json(
                system_prompt=_EVOLUTION_SYSTEM_PROMPT,
                user_message=analysis_context,
                response_format={"type": "json_object"},
                temperature=0.3,
            )

            lessons = response.get("lessons", [])
            if not lessons:
                return {"skipped": True, "reason": "no_lessons_generated"}

            # Generate version ID
            version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

            # Store lessons for both market_analyst and strategist
            results = {}
            for agent_name in ("market_analyst", "strategist"):
                stored = self._store_supplements(agent_name, version, lessons)
                results[agent_name] = stored

            # Reload active supplements
            self.load_active_supplements()

            # Audit
            if self._audit:
                self._audit.log("prompt_evolution", {
                    "version": version,
                    "lessons_count": len(lessons),
                    "agents": list(results.keys()),
                })

            logger.info(
                f"🧬 Prompt evolution: generated {len(lessons)} lessons (v{version})"
            )
            return {
                "version": version,
                "lessons": lessons,
                "agents": results,
            }

        except Exception as e:
            logger.warning(f"Prompt evolution failed: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Supplement access
    # ------------------------------------------------------------------

    def get_supplements(self, agent_name: str) -> list[dict]:
        """Get active prompt supplements for an agent."""
        return self._supplements.get(agent_name, [])

    def format_supplements(self, agent_name: str) -> str:
        """Format active supplements as prompt text for injection.

        Returns empty string if no supplements are active.
        """
        supplements = self.get_supplements(agent_name)
        if not supplements:
            return ""

        lines = ["\n📚 LEARNED BEHAVIORAL CORRECTIONS (from prediction accuracy analysis):"]
        total_chars = 0
        for s in supplements[:_MAX_ACTIVE_SUPPLEMENTS]:
            text = s.get("supplement_text", "")
            if total_chars + len(text) > _MAX_SUPPLEMENT_CHARS:
                break
            lines.append(f"  • {text}")
            total_chars += len(text)

        lines.append(
            "Apply these corrections when they are relevant to the current analysis."
        )
        return "\n".join(lines)

    def get_active_version(self) -> str | None:
        """Get the version ID of the currently active supplements."""
        for agent_supplements in self._supplements.values():
            if agent_supplements:
                return agent_supplements[0].get("version")
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_active_supplements(self) -> bool:
        """Load active supplements from the database."""
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT id, version, agent_name, supplement_text,
                           source_factor, source_accuracy_pct, priority
                    FROM prompt_supplements
                    WHERE is_active = TRUE
                    ORDER BY priority ASC, created_at DESC
                    """,
                ).fetchall()

            self._supplements = {}
            for row in rows:
                agent = row["agent_name"]
                if agent not in self._supplements:
                    self._supplements[agent] = []
                self._supplements[agent].append(dict(row))

            total = sum(len(v) for v in self._supplements.values())
            if total:
                logger.info(f"🧬 Loaded {total} active prompt supplements")
            return total > 0
        except Exception as e:
            logger.warning(f"Failed to load prompt supplements: {e}")
            return False

    def _store_supplements(
        self, agent_name: str, version: str, lessons: list[dict]
    ) -> dict:
        """Store new supplements, deactivating oldest if over limit."""
        stored = 0
        try:
            with self._db._get_conn() as conn:
                # Count active supplements for this agent
                active_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM prompt_supplements WHERE agent_name = %s AND is_active = TRUE",
                    (agent_name,),
                ).fetchone()["cnt"]

                # Deactivate oldest if adding new ones would exceed limit
                excess = (active_count + len(lessons)) - _MAX_ACTIVE_SUPPLEMENTS
                if excess > 0:
                    oldest = conn.execute(
                        """
                        SELECT id FROM prompt_supplements
                        WHERE agent_name = %s AND is_active = TRUE
                        ORDER BY created_at ASC
                        LIMIT %s
                        """,
                        (agent_name, excess),
                    ).fetchall()
                    for old in oldest:
                        conn.execute(
                            """
                            UPDATE prompt_supplements
                            SET is_active = FALSE,
                                deactivated_at = (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
                            WHERE id = %s
                            """,
                            (old["id"],),
                        )

                # Insert new lessons
                for lesson in lessons:
                    conn.execute(
                        """
                        INSERT INTO prompt_supplements
                            (version, agent_name, supplement_text,
                             source_factor, source_accuracy_pct, priority)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            version,
                            agent_name,
                            lesson.get("text", ""),
                            lesson.get("source_factor", ""),
                            lesson.get("source_accuracy_pct"),
                            lesson.get("priority", 0),
                        ),
                    )
                    stored += 1

                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to store supplements for {agent_name}: {e}")

        return {"stored": stored, "agent": agent_name}

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _format_analysis_context(
        factor_attr: list[dict],
        regime_accuracy: dict[str, dict],
        calibration_buckets: list[dict],
    ) -> str:
        """Format analysis data into a prompt for the evolution LLM call."""
        lines = []

        # Factor attribution
        lines.append("## Factor Attribution (24h horizon)")
        if factor_attr:
            lines.append("Factors sorted by lift vs baseline accuracy:")
            for f in factor_attr[:20]:
                marker = "✅" if f["lift_vs_baseline"] > 0 else "⚠️"
                lines.append(
                    f"  {marker} \"{f['factor']}\" — accuracy: {f['accuracy_pct']}%, "
                    f"lift: {f['lift_vs_baseline']:+.1f}pp, n={f['total']}"
                )
        else:
            lines.append("  No factor attribution data available.")

        # Regime accuracy
        lines.append("\n## Regime Accuracy (24h horizon)")
        for regime, data in regime_accuracy.items():
            total = data.get("total", 0)
            correct = data.get("correct", 0)
            acc = data.get("accuracy_pct", "N/A")
            lines.append(f"  {regime}: {acc}% ({correct}/{total})")

        # Calibration
        lines.append("\n## Confidence Calibration")
        if calibration_buckets:
            for b in calibration_buckets:
                lines.append(
                    f"  Confidence {b['range']}: predicted ~{b['avg_predicted']:.0%}, "
                    f"actual {b['avg_actual']:.0%} (n={b['count']})"
                )
        else:
            lines.append("  No calibration data available.")

        return "\n".join(lines)

    @staticmethod
    def _summarize_calibration(
        data: list[tuple[float, bool]]
    ) -> list[dict]:
        """Bucket calibration data into summary bins."""
        if not data:
            return []

        bins = [
            {"range": "0-20%", "sum_pred": 0, "sum_actual": 0, "count": 0},
            {"range": "20-40%", "sum_pred": 0, "sum_actual": 0, "count": 0},
            {"range": "40-60%", "sum_pred": 0, "sum_actual": 0, "count": 0},
            {"range": "60-80%", "sum_pred": 0, "sum_actual": 0, "count": 0},
            {"range": "80-100%", "sum_pred": 0, "sum_actual": 0, "count": 0},
        ]

        for conf, correct in data:
            idx = min(int(conf * 5), 4)
            bins[idx]["sum_pred"] += conf
            bins[idx]["sum_actual"] += float(correct)
            bins[idx]["count"] += 1

        result = []
        for b in bins:
            if b["count"] > 0:
                result.append({
                    "range": b["range"],
                    "avg_predicted": b["sum_pred"] / b["count"],
                    "avg_actual": b["sum_actual"] / b["count"],
                    "count": b["count"],
                })
        return result

    def get_supplements_summary(self) -> dict[str, Any]:
        """Summary for dashboard display."""
        result = {}
        for agent, supplements in self._supplements.items():
            result[agent] = {
                "count": len(supplements),
                "version": supplements[0].get("version") if supplements else None,
                "lessons": [
                    {
                        "text": s.get("supplement_text", ""),
                        "factor": s.get("source_factor", ""),
                        "accuracy": s.get("source_accuracy_pct"),
                    }
                    for s in supplements
                ],
            }
        return result
