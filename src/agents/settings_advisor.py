"""
Settings Advisor Agent — Autonomously adapts trading parameters
based on market conditions, recent performance, and volatility.

Runs periodically (every N cycles) and proposes parameter adjustments
that are validated against autonomous guardrails, persisted to disk,
pushed to runtime, and audited via Telegram + StatsDB.

OFF LIMITS: Cannot enable/disable trading. Floor guards prevent zeroing
out trade limits or setting confidence above 0.95.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.utils.logger import get_logger
from src.utils import settings_manager as sm

logger = get_logger("agent.settings_advisor")

# Defaults
DEFAULT_REVIEW_INTERVAL = 10   # every 10 pipeline cycles
MAX_CHANGES_PER_REVIEW = 6    # limit fields the LLM can touch per review
MIN_CONFIDENCE_TO_ACT = 0.5   # below this, proposals are logged but skipped


SETTINGS_ADVISOR_SYSTEM_PROMPT = """You are an autonomous trading parameter advisor.
Your job is to review current market conditions, recent trading performance, and the
current configuration — then recommend parameter adjustments to improve performance.

You are part of an automated crypto trading system. You may adjust trading parameters
to adapt to changing market conditions (higher volatility, trending vs ranging markets,
improving or worsening performance).

CRITICAL CONSTRAINTS:
- You CANNOT enable or disable trading. The on/off decision belongs to the human operator only.
- You CANNOT change which pairs are traded, the trading mode (paper/live), or fee rates.
- All numeric values will be clamped to safe ranges automatically. You don't need to worry about breaking things.
- ONLY propose changes when there is a clear reason. If things are working well, respond with NO changes.
- Be conservative. Small, incremental adjustments are better than large swings.
- Capital preservation is always the top priority.
- Do NOT chase losses by dramatically loosening risk parameters.

AVAILABLE PARAMETERS (with allowed ranges):
{schema_summary}

CURRENT SETTINGS:
{current_settings}

Respond with JSON:
{{
    "changes": [
        {{
            "section": "risk",
            "field": "stop_loss_pct",
            "value": 0.04,
            "reason": "Increased volatility warrants wider stops"
        }}
    ],
    "overall_reasoning": "Brief explanation of the market regime and why these changes help",
    "confidence": 0.0-1.0
}}

Rules:
- "changes" should be an array of 0-{max_changes} items. Empty array = no changes needed.
- Only propose changes you are confident about.
- If recent performance is good and market conditions are stable, return an empty changes array.
- Consider risk/reward: tighten stops in choppy markets, loosen in strong trends, etc.
"""


class SettingsAdvisorAgent(BaseAgent):
    """
    Periodically reviews market conditions and adjusts trading parameters.
    Cannot enable/disable trading — guardrails enforce floors on critical fields.
    """

    def __init__(
        self,
        llm,
        state,
        config,
        rules,
        review_interval: int = DEFAULT_REVIEW_INTERVAL,
    ):
        super().__init__("settings_advisor", llm, state, config)
        self.rules = rules
        self.review_interval = review_interval
        self._cycles_since_review = 0
        self._total_adjustments = 0
        self._schema_summary: Optional[str] = None

    def should_run(self) -> bool:
        """Check if enough cycles have passed for a review."""
        self._cycles_since_review += 1
        if self._cycles_since_review >= self.review_interval:
            self._cycles_since_review = 0
            return True
        return False

    # ── Prompt builders ──────────────────────────────────────────────────

    def _get_schema_summary(self) -> str:
        """Formatted summary of what the LLM is allowed to change."""
        if self._schema_summary is None:
            schema = sm.get_autonomous_schema_summary()
            lines: list[str] = []
            for section, fields in schema.items():
                lines.append(f"\n[{section}]")
                for field, info in fields.items():
                    parts: list[str] = []
                    if "min" in info:
                        parts.append(f"min={info['min']}")
                    if "max" in info:
                        parts.append(f"max={info['max']}")
                    range_str = f" ({', '.join(parts)})" if parts else ""
                    if "enum" in info:
                        range_str = f" (options: {info['enum']})"
                    lines.append(f"  {field}: {info['type']}{range_str}")
            self._schema_summary = "\n".join(lines)
        return self._schema_summary

    def _get_current_settings_summary(self) -> str:
        """Current values for all autonomously-adjustable fields."""
        settings = sm.load_settings()
        lines: list[str] = []
        for section in sorted(sm.AUTONOMOUS_ALLOWED_SECTIONS):
            section_cfg = settings.get(section, {})
            section_guards = sm.AUTONOMOUS_FIELD_GUARDS.get(section, {})
            if not section_guards:
                continue
            lines.append(f"\n[{section}]")
            for field in section_guards:
                if (section, field) in sm.AUTONOMOUS_BLOCKED_FIELDS:
                    continue
                val = section_cfg.get(field, "NOT SET")
                lines.append(f"  {field} = {val}")
        return "\n".join(lines)

    # ── Main execution ───────────────────────────────────────────────────

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Analyze market conditions and propose settings adjustments.

        Context expected:
            - fear_greed: str
            - recent_performance: str (win rate, P&L summary)
            - market_volatility: str
            - current_prices: dict
            - cycle_id: str (optional)
            - stats_db: StatsDB (optional)
            - trace_ctx: TraceContext (optional)
        """
        fear_greed = context.get("fear_greed", "unavailable")
        recent_perf = context.get("recent_performance", "unavailable")
        market_vol = context.get("market_volatility", "unavailable")
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        trace_ctx = context.get("trace_ctx")

        system_prompt = SETTINGS_ADVISOR_SYSTEM_PROMPT.format(
            schema_summary=self._get_schema_summary(),
            current_settings=self._get_current_settings_summary(),
            max_changes=MAX_CHANGES_PER_REVIEW,
        )

        user_message = (
            f"MARKET CONDITIONS:\n"
            f"- Fear & Greed: {fear_greed}\n"
            f"- Recent Performance (24h): {recent_perf}\n"
            f"- Market Volatility: {market_vol}\n"
            f"- Current Prices: {json.dumps(context.get('current_prices', {}), default=str)}\n\n"
            f"Based on these conditions, should we adjust any trading parameters?\n"
            f"If everything is working well, return an empty changes array.\n"
            f"Respond with JSON only."
        )

        # Tracing span
        span = None
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                self.name,
                input_data={"system": system_prompt[:500], "user": user_message[:500]},
                model=self.llm.model,
            )

        llm_response = await self.llm.chat_json(
            system_prompt=system_prompt,
            user_message=user_message,
            span=span,
            agent_name=self.name,
        )

        if "error" in llm_response:
            logger.warning(f"Settings advisor LLM failed: {llm_response}")
            return {"changes_applied": 0, "error": llm_response["error"]}

        changes = llm_response.get("changes", [])
        confidence = float(llm_response.get("confidence", 0))
        overall_reasoning = llm_response.get("overall_reasoning", "")

        if not changes:
            logger.info("📋 Settings Advisor: No changes recommended")
            return {
                "changes_applied": 0,
                "reasoning": overall_reasoning,
                "confidence": confidence,
            }

        # Confidence gate
        if confidence < MIN_CONFIDENCE_TO_ACT:
            logger.info(
                f"📋 Settings Advisor: Changes proposed but confidence too low "
                f"({confidence:.0%} < {MIN_CONFIDENCE_TO_ACT:.0%})"
            )
            return {
                "changes_applied": 0,
                "reasoning": f"Low confidence ({confidence:.0%}): {overall_reasoning}",
                "proposed_but_skipped": len(changes),
            }

        # Cap number of changes per review
        changes = changes[:MAX_CHANGES_PER_REVIEW]

        # ── Apply changes section by section ─────────────────────────────
        applied: list[dict] = []
        rejected: list[dict] = []

        # Group by section
        by_section: dict[str, dict[str, Any]] = {}
        change_reasons: dict[str, str] = {}
        for ch in changes:
            sec = ch.get("section", "")
            field = ch.get("field", "")
            value = ch.get("value")
            reason = ch.get("reason", "")
            if sec and field and value is not None:
                by_section.setdefault(sec, {})[field] = value
                change_reasons[f"{sec}.{field}"] = reason

        for section, updates in by_section.items():
            ok, errors, clamped = sm.validate_autonomous_update(section, updates)
            if not ok:
                for err in errors:
                    rejected.append({"error": err})
                    logger.warning(f"🚫 Settings Advisor rejected: {err}")
                continue

            if not clamped:
                continue

            # Persist to settings.yaml
            persist_ok, persist_err, persisted = sm.update_section(section, clamped)
            if not persist_ok:
                rejected.append({"section": section, "error": persist_err})
                logger.warning(f"🚫 Settings Advisor persist failed: {persist_err}")
                continue

            for field, val in persisted.items():
                reason = change_reasons.get(f"{section}.{field}", "")
                applied.append({
                    "section": section,
                    "field": field,
                    "value": val,
                    "reason": reason,
                })

            logger.warning(
                f"🤖 AUTONOMOUS SETTINGS UPDATE | [{section}]: "
                f"{persisted} | Reasoning: {overall_reasoning[:200]}"
            )

        self._total_adjustments += len(applied)

        result = {
            "changes_applied": len(applied),
            "applied": applied,
            "rejected": rejected,
            "reasoning": overall_reasoning,
            "confidence": confidence,
            "total_lifetime_adjustments": self._total_adjustments,
        }

        # Persist reasoning trace
        if stats_db and cycle_id:
            try:
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair="SYSTEM",
                    agent_name="settings_advisor",
                    reasoning_json=result,
                    signal_type="settings_adjustment",
                    confidence=confidence,
                    langfuse_trace_id=span.trace_id if span else None,
                    langfuse_span_id=span.span_id if span else None,
                    prompt_tokens=span.prompt_tokens if span else 0,
                    completion_tokens=span.completion_tokens if span else 0,
                    latency_ms=span.latency_ms if span else 0.0,
                    raw_prompt=user_message[:1000],
                )
            except Exception as e:
                self.logger.debug(f"Failed to save settings_advisor trace: {e}")

        return result


# ═══════════════════════════════════════════════════════════════════════════
# Telegram notification helper
# ═══════════════════════════════════════════════════════════════════════════

def format_advisor_notification(result: dict) -> str:
    """Format a Telegram message for autonomous settings changes."""
    applied = result.get("applied", [])
    if not applied:
        return ""

    lines = ["🤖 **Autonomous Settings Adjustment**\n"]
    lines.append(f"Confidence: {result.get('confidence', 0):.0%}")
    lines.append(f"Reasoning: _{result.get('reasoning', 'N/A')[:200]}_\n")

    for ch in applied:
        lines.append(f"  • `{ch['section']}.{ch['field']}` → **{ch['value']}**")
        if ch.get("reason"):
            lines.append(f"    _{ch['reason']}_")

    rejected = result.get("rejected", [])
    if rejected:
        lines.append(f"\n⚠️ {len(rejected)} change(s) rejected by guardrails")

    return "\n".join(lines)
