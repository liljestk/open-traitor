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
import glob
import os
import sys

import yaml
from dotenv import load_dotenv

# Load .env before reading any environment variables so that local development
# (outside Docker) picks up TEMPORAL_HOST, LANGFUSE_*, REDIS_URL, etc.
load_dotenv(os.path.join("config", ".env"))

import temporalio.client
import temporalio.exceptions
import temporalio.worker

from src.planning.activities import (
    evaluate_previous_plan,
    fetch_trade_history,
    fetch_portfolio_history,
    fetch_backtest_summary,
    fetch_score_divergence,
    fetch_equity_events,
    call_planning_llm,
    write_strategic_context,
    write_daily_plan,
    fetch_pair_universe,
    fetch_universe_scan_summary,
    run_nightly_backtests,
    run_event_regressions,
    run_regressions_for_followed_assets,
    run_price_backfill,
    run_finetune_export,
    run_taxonomy_seed,
    run_correlation_matrix,
    run_cross_event_regressions,
    run_outcome_attribution,
    run_counterfactual_replay,
    run_bandit_update,
    run_lead_lag_matrix,
    run_event_calendar_sync,
    run_decision_drift,
    run_reasoning_judge,
    run_onchain_sync,
)
from src.planning.workflows import (
    DailyPlanWorkflow,
    WeeklyReviewWorkflow,
    MonthlyReviewWorkflow,
    NightlyBacktestWorkflow,
    EventRegressionWorkflow,
    NightlyPriceBackfillWorkflow,
    FinetuneExportWorkflow,
    CrossAssetAnalyticsWorkflow,
    SmartsNightlyWorkflow,
    SmartsHourlyWorkflow,
    SmartsJudgeWorkflow,
)
from src.planning.wfo_workflow import (
    WeeklyWFOWorkflow,
    run_wfo_for_strategies,
)
from src.utils.logger import setup_logger, get_logger

setup_logger(log_level=os.environ.get("LOG_LEVEL", "INFO"))
logger = get_logger("planning.worker")

TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = "planning"


def _discover_profiles() -> list[str]:
    """Discover trading profiles from config/*.yaml files.

    Returns a list of profile names (e.g. ["coinbase", "ibkr"]).
    Skips settings.yaml which is the generic fallback config.
    Falls back to [""] (empty = legacy single-profile mode) if no
    exchange-specific configs are found.
    """
    profiles: list[str] = []
    for path in sorted(glob.glob(os.path.join("config", "*.yaml"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name == "settings":
            continue
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            exchange = cfg.get("trading", {}).get("exchange", "")
            if exchange:
                profiles.append(exchange.lower())
        except Exception as e:
            logger.warning(f"Skipping config {path}: {e}")
    return profiles or [""]


async def start_cron_schedules(client: temporalio.client.Client) -> None:
    """
    Start the three cron-scheduled workflows PER PROFILE.

    Each profile gets its own set of workflow instances (e.g.
    ``daily-plan-coinbase``, ``daily-plan-ibkr``).  The profile
    name is passed as the workflow input arg so activities know
    which stats DB to read from / write to.

    Safe to call on every startup — Temporal deduplicates by workflow ID.
    """
    profiles = _discover_profiles()
    logger.info(f"Discovered profiles for planning: {profiles}")

    base_cron_configs = [
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
        {
            "workflow": NightlyBacktestWorkflow,
            "id": "nightly-backtest",
            "cron": "0 2 * * *",
            "desc": "Nightly backtest runner (2 AM UTC)",
        },
        {
            "workflow": WeeklyWFOWorkflow,
            "id": "weekly-wfo",
            "cron": "0 3 * * 1",
            "desc": "Weekly walk-forward optimisation (Monday 03:00 UTC)",
        },
        {
            "workflow": NightlyPriceBackfillWorkflow,
            "id": "nightly-price-backfill",
            # 01:00 UTC — must finish before EventRegressionWorkflow at 02:30
            # so the regression sees fresh candles.
            "cron": "0 1 * * *",
            "desc": "Nightly OHLCV history backfill (01:00 UTC)",
        },
        {
            "workflow": CrossAssetAnalyticsWorkflow,
            "id": "cross-asset-analytics",
            # 02:15 UTC — between nightly-backtest (02:00) and
            # event-regression (02:30).
            "cron": "15 2 * * *",
            "desc": "Cross-asset taxonomy + correlations + clusters + cross-event regressions (02:00 UTC)",
        },
        {
            "workflow": EventRegressionWorkflow,
            "id": "event-regression",
            "cron": "30 2 * * *",
            "desc": "Nightly event–price regression refit (02:30 UTC)",
        },
        {
            "workflow": FinetuneExportWorkflow,
            "id": "finetune-export",
            "cron": "0 4 1 * *",
            "desc": "Monthly fine-tuning dataset export (1st of month, 04:00 UTC)",
        },
        {
            "workflow": SmartsNightlyWorkflow,
            "id": "smarts-nightly",
            # 03:30 UTC — after backfill (01:00) and analytics (02:15-02:30).
            "cron": "30 3 * * *",
            "desc": "Smarts nightly: attribution + replay + lead-lag + drift",
        },
        {
            "workflow": SmartsHourlyWorkflow,
            "id": "smarts-hourly",
            "cron": "5 * * * *",
            "desc": "Smarts hourly: event calendar + on-chain signals",
        },
        {
            "workflow": SmartsJudgeWorkflow,
            "id": "smarts-judge",
            "cron": "45 */6 * * *",
            "desc": "Smarts every-6h: LLM-judge sample of reasoning outputs",
        },
    ]

    for profile in profiles:
        suffix = f"-{profile}" if profile else ""
        for cfg in base_cron_configs:
            wf_id = f"{cfg['id']}{suffix}"
            try:
                handle = await client.start_workflow(
                    cfg["workflow"].run,
                    arg=profile,
                    id=wf_id,
                    task_queue=TASK_QUEUE,
                    cron_schedule=cfg["cron"],
                )
                logger.info(
                    f"✅ Cron workflow started: {wf_id} ({cfg['cron']}) — {cfg['desc']}"
                )
            except temporalio.exceptions.WorkflowAlreadyStartedError:
                logger.info(f"⏩ Cron workflow already running: {wf_id}")
            except Exception as e:
                logger.warning(f"⚠️  Failed to start cron workflow {wf_id}: {e}")


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
        workflows=[DailyPlanWorkflow, WeeklyReviewWorkflow, MonthlyReviewWorkflow, NightlyBacktestWorkflow, WeeklyWFOWorkflow, EventRegressionWorkflow, NightlyPriceBackfillWorkflow, FinetuneExportWorkflow, CrossAssetAnalyticsWorkflow, SmartsNightlyWorkflow, SmartsHourlyWorkflow, SmartsJudgeWorkflow],
        activities=[
            evaluate_previous_plan,
            fetch_trade_history,
            fetch_portfolio_history,
            fetch_backtest_summary,
            fetch_score_divergence,
            fetch_equity_events,
            call_planning_llm,
            write_strategic_context,
            write_daily_plan,
            fetch_pair_universe,
            fetch_universe_scan_summary,
            run_nightly_backtests,
            run_wfo_for_strategies,
            run_event_regressions,
            run_regressions_for_followed_assets,
            run_price_backfill,
            run_finetune_export,
            run_taxonomy_seed,
            run_correlation_matrix,
            run_cross_event_regressions,
            run_outcome_attribution,
            run_counterfactual_replay,
            run_bandit_update,
            run_lead_lag_matrix,
            run_event_calendar_sync,
            run_decision_drift,
            run_reasoning_judge,
            run_onchain_sync,
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
