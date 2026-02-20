"""
Temporal Worker — entry point for the planning workflow worker process.

Run this as a separate process alongside the main trading bot:
    python -m src.planning.worker

Environment variables:
    TEMPORAL_HOST:    Temporal server address (default: localhost:7233)
    TEMPORAL_NAMESPACE: Temporal namespace (default: default)
    OLLAMA_BASE_URL:  Ollama base URL (default: http://localhost:11434)
    LLM_MODEL / PLANNING_MODEL: model to use for planning (default: llama3.2)

The worker registers all three workflows and their associated activities,
then polls the Temporal task queue for work. Cron-scheduled workflow
instances are started separately (see start_schedules() below or use the
Temporal CLI / docker-compose healthcheck).
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

# Load .env before reading any environment variables so that local development
# (outside Docker) picks up TEMPORAL_HOST, LANGFUSE_*, REDIS_URL, etc.
load_dotenv(os.path.join("config", ".env"))

import temporalio.client
import temporalio.exceptions
import temporalio.worker

from src.planning.activities import (
    fetch_trade_history,
    fetch_portfolio_history,
    call_planning_llm,
    write_strategic_context,
    write_daily_plan,
)
from src.planning.workflows import (
    DailyPlanWorkflow,
    WeeklyReviewWorkflow,
    MonthlyReviewWorkflow,
)
from src.utils.logger import get_logger

logger = get_logger("planning.worker")

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = "planning"


async def start_cron_schedules(client: temporalio.client.Client) -> None:
    """
    Start the three cron-scheduled workflows if they are not already running.
    Safe to call on every startup — Temporal deduplicates by workflow ID.
    """
    cron_configs = [
        {
            "workflow": DailyPlanWorkflow,
            "id": "daily-plan",
            "cron": "0 0 * * *",
            "desc": "Daily strategic plan (midnight UTC)",
        },
        {
            "workflow": WeeklyReviewWorkflow,
            "id": "weekly-review",
            "cron": "0 0 * * 1",
            "desc": "Weekly strategy review (Monday midnight UTC)",
        },
        {
            "workflow": MonthlyReviewWorkflow,
            "id": "monthly-review",
            "cron": "0 0 1 * *",
            "desc": "Monthly portfolio review (1st of month midnight UTC)",
        },
    ]

    for cfg in cron_configs:
        try:
            handle = await client.start_workflow(
                cfg["workflow"].run,
                id=cfg["id"],
                task_queue=TASK_QUEUE,
                cron_schedule=cfg["cron"],
            )
            logger.info(f"✅ Cron workflow started: {cfg['id']} ({cfg['cron']}) — {cfg['desc']}")
        except temporalio.exceptions.WorkflowAlreadyStartedError:
            logger.info(f"⏩ Cron workflow already running: {cfg['id']}")
        except Exception as e:
            logger.warning(f"⚠️  Failed to start cron workflow {cfg['id']}: {e}")


async def main() -> None:
    logger.info(f"🕐 Connecting to Temporal at {TEMPORAL_HOST} (namespace={TEMPORAL_NAMESPACE})")
    client = await temporalio.client.Client.connect(
        TEMPORAL_HOST,
        namespace=TEMPORAL_NAMESPACE,
    )

    # Register cron schedules (idempotent)
    await start_cron_schedules(client)

    logger.info(f"👷 Starting planning worker on task queue: {TASK_QUEUE}")
    async with temporalio.worker.Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DailyPlanWorkflow, WeeklyReviewWorkflow, MonthlyReviewWorkflow],
        activities=[
            fetch_trade_history,
            fetch_portfolio_history,
            call_planning_llm,
            write_strategic_context,
            write_daily_plan,
        ],
    ) as worker:
        logger.info("✅ Planning worker running — waiting for tasks...")
        await asyncio.Future()  # run until cancelled


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Planning worker stopped.")
        sys.exit(0)
