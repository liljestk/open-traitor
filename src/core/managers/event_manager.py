"""
EventManager — WebSocket ticker callback, news pub/sub, and emergency replanning.

Extracted from Orchestrator for maintainability.  Takes an orchestrator reference
in its constructor (same pattern as PipelineManager / StateManager).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING

from src.utils.logger import get_logger
from src.utils.helpers import format_percentage

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.event_manager")


class EventManager:
    """Handles event-driven triggers: WS price moves, news pub/sub, emergency replans."""

    _REPLAN_COOLDOWN_S: float = 1800.0  # min 30 min between emergency replans
    _replan_last_ts: float = 0.0

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator

    # =========================================================================
    # WebSocket Ticker Callback
    # =========================================================================

    def on_ws_ticker(self, data: dict) -> None:
        """Called by the WS feed on every ticker tick (runs in WS thread).

        Compares the new price against the snapshot recorded at the last pipeline
        start for this pair.  If the move exceeds *_ws_trigger_pct* the pair is
        queued for an early pipeline run during the idle sleep period.
        """
        orch = self.orchestrator
        pair = data.get("product_id", "")
        price = float(data.get("price", 0))
        if not pair or price <= 0 or pair not in orch.pairs:
            return

        with orch._ws_trigger_lock:
            last = orch._ws_last_prices.get(pair, 0)
            if last > 0:
                change_pct = abs(price - last) / last
                if change_pct >= orch._ws_trigger_pct:
                    orch._ws_trigger_pairs.add(pair)
                    logger.info(
                        f"📡 WS trigger: {pair} moved {change_pct:+.2%} "
                        f"(${last:,.2f} → ${price:,.2f}) — early pipeline queued"
                    )
                # Extreme move (≥3%) → trigger emergency replan
                if change_pct >= 0.03:
                    threading.Thread(
                        target=self.trigger_emergency_replan,
                        args=(f"{pair} moved {change_pct:+.2%} in a single tick",),
                        daemon=True,
                        name="ws-emergency-replan",
                    ).start()
            # Always keep the running WS price current so the next check is fresh
            orch._ws_last_prices[pair] = price

    # =========================================================================
    # News Pub/Sub Subscriber
    # =========================================================================

    def start_news_subscriber(self) -> None:
        """Subscribe to Redis *news:updates* pub/sub channel in a daemon thread.

        When the news worker publishes a fresh batch all active pairs are added
        to the news-trigger set so the main loop fetches up-to-date headlines
        during the next early-pipeline check rather than waiting a full interval.
        No-op if Redis is not configured.
        """
        orch = self.orchestrator
        if not orch.redis:
            return

        def _listener() -> None:
            try:
                pubsub = orch.redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe("news:updates")
                for message in pubsub.listen():
                    if not orch.state.is_running:
                        break
                    if message and message.get("type") == "message":
                        with orch._ws_trigger_lock:
                            orch._news_trigger_pairs.update(orch.pairs)
                        logger.debug(
                            "📰 Breaking news detected via pub/sub — early pipeline queued"
                        )
            except Exception as e:
                logger.debug(f"News pub/sub subscriber error: {e}")

        t = threading.Thread(target=_listener, daemon=True, name="news-sub")
        t.start()
        logger.info("📰 News pub/sub subscriber started")

    # =========================================================================
    # Emergency Re-Planning
    # =========================================================================

    def trigger_emergency_replan(self, reason: str) -> None:
        """Write an emergency conservative strategic context and attempt a Temporal replan.

        Called when the circuit breaker fires or an extreme WS price move (≥3%)
        is detected.  Works even if Temporal is down — the local DB write is
        immediate and the orchestrator picks it up on the next cache refresh.
        """
        orch = self.orchestrator
        now = time.time()
        if now - self._replan_last_ts < self._REPLAN_COOLDOWN_S:
            logger.debug("Emergency replan skipped — cooldown active")
            return
        self._replan_last_ts = now

        logger.warning(f"🚨 Emergency replan triggered: {reason}")

        # 1. Write a conservative emergency context to StatsDB immediately
        try:
            emergency_plan = {
                "regime": "volatile",
                "confidence": 0.3,
                "risk_posture": "conservative",
                "preferred_pairs": [],
                "avoid_pairs": list(orch.pairs),  # avoid all pairs until next plan
                "key_observations": [
                    f"EMERGENCY: {reason}",
                    "All pairs set to avoid — waiting for next scheduled plan evaluation",
                ],
                "today_focus": "Capital preservation — emergency mode active",
                "summary": (
                    f"Emergency replan: {reason}. "
                    "Switched to conservative posture, all pairs on avoid. "
                    "Next scheduled plan will re-evaluate."
                ),
            }
            orch.stats_db.save_strategic_context(
                horizon="daily",
                plan_json=emergency_plan,
                summary_text=emergency_plan["summary"],
            )
            # Invalidate the cache so the next cycle picks up the emergency plan
            orch._strategic_context_ts = 0.0

            if orch.telegram:
                orch.telegram.send_alert(
                    f"🚨 *Emergency Replan*\n\n"
                    f"Reason: {reason}\n"
                    f"Action: Switched to conservative posture, all pairs on avoid.\n"
                    f"Next scheduled plan will re-evaluate."
                )
        except Exception as e:
            logger.error(f"Failed to write emergency context: {e}")

        # 2. Optionally trigger a Temporal DailyPlanWorkflow
        def _try_temporal_replan() -> None:
            try:
                import temporalio.client as _tc

                temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
                temporal_ns = os.environ.get("TEMPORAL_NAMESPACE", "default")

                async def _start_workflow():
                    client = await _tc.Client.connect(temporal_host, namespace=temporal_ns)
                    from src.planning.workflows import DailyPlanWorkflow
                    await client.start_workflow(
                        DailyPlanWorkflow.run,
                        id=f"emergency-replan-{uuid.uuid4().hex[:8]}",
                        task_queue="planning-queue",
                    )
                    logger.info("📋 Emergency Temporal replan workflow started")

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_start_workflow())
                finally:
                    loop.close()
            except Exception as e:
                logger.debug(
                    f"Temporal emergency replan unavailable (local context already written): {e}"
                )

        # Run Temporal attempt in background thread to avoid blocking
        threading.Thread(
            target=_try_temporal_replan, daemon=True, name="emergency-replan"
        ).start()
