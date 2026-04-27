"""
Shadow strategist — parallel logger of an alternative variant.

Runs alongside the live strategist on the SAME context but with a different
temperature / system-prompt twist, so we can A/B test prompt or weight
changes against the live agent risk-free.

Variants are configured in ``config/{profile}.yaml`` under
``shadow_strategists`` as a list of dicts::

    shadow_strategists:
      - name: "high_temp"
        temperature: 0.8
      - name: "conservative_prompt"
        system_suffix: "Prefer hold over buy when uncertain."

The shadow agent NEVER produces an executable trade. It only writes to
``shadow_decisions`` for later analysis.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("shadow_strategist")


_SHADOW_SYSTEM = (
    "You are a shadow variant of the live trading strategist. "
    "Given the same context, output the action you would take. "
    "Respond strictly as JSON: "
    '{"action":"buy|sell|hold","confidence":0.0..1.0,"reasoning":"one sentence"}'
)


class ShadowStrategist:
    """Logs a variant strategist's decisions in parallel."""

    def __init__(
        self,
        llm,
        stats_db,
        *,
        exchange: str,
        variants: list[dict] | None = None,
    ):
        self.llm = llm
        self.db = stats_db
        self.exchange = exchange
        self.variants = list(variants or [])

    async def shadow(
        self,
        *,
        cycle_id: Optional[str],
        pair: str,
        live_action: Optional[str],
        live_confidence: Optional[float],
        context: dict,
    ) -> None:
        if self.llm is None or not self.variants:
            return
        try:
            blob = json.dumps({
                k: context.get(k)
                for k in (
                    "signal", "strategy_signals", "sentiment",
                    "pattern_signal", "cross_asset_signal",
                    "news_knn_prior", "lead_lag_signals",
                    "upcoming_events", "onchain_now",
                )
                if context.get(k) is not None
            }, default=str)[:6000]
        except Exception:
            blob = str(context)[:4000]

        async def _one(variant: dict):
            name = str(variant.get("name") or "default")
            temp = float(variant.get("temperature", 0.5))
            sys_suffix = str(variant.get("system_suffix") or "")
            sys_prompt = _SHADOW_SYSTEM + ("\n" + sys_suffix if sys_suffix else "")
            try:
                resp = await self.llm.chat_json(
                    system_prompt=sys_prompt,
                    user_message=f"Pair: {pair}\nContext:\n{blob}",
                    temperature=temp,
                    max_tokens=200,
                    agent_name=f"shadow_{name}",
                    priority="low",
                )
            except Exception as e:
                logger.debug(f"shadow {name} llm failed: {e}")
                return
            action = (resp.get("action") or "hold").lower()
            try:
                conf = float(resp.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            try:
                self.db.write_shadow_decision(self.exchange, {
                    "cycle_id": cycle_id,
                    "variant": name,
                    "pair": pair,
                    "action": action,
                    "confidence": max(0.0, min(1.0, conf)),
                    "live_action": live_action,
                    "live_confidence": live_confidence,
                    "diff_action": (live_action and action != live_action),
                    "reasoning": str(resp.get("reasoning") or "")[:1000],
                })
            except Exception as e:
                logger.debug(f"shadow {name} write failed: {e}")

        await asyncio.gather(*(_one(v) for v in self.variants), return_exceptions=True)
