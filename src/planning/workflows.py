"""
Temporal Workflows for multi-horizon strategic planning.

Three cron-scheduled workflows:
  - DailyPlanWorkflow:    runs at midnight UTC every day
  - WeeklyReviewWorkflow: runs at midnight UTC every Monday
  - MonthlyReviewWorkflow: runs at midnight UTC on the 1st of each month

Each workflow:
  1. Fetches trade + portfolio history from StatsDB via activities
  2. Calls the LLM to produce a structured plan
  3. Writes the plan back to StatsDB (strategic_context + daily_summaries)

The fast orchestrator loop reads strategic_context on each cycle and injects
it as soft prompt context for agents — no hard overrides.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.planning.activities import (
        evaluate_previous_plan,
        fetch_trade_history,
        fetch_portfolio_history,
        call_planning_llm,
        write_strategic_context,
        write_daily_plan,
    )


_ACTIVITY_TIMEOUT = timedelta(minutes=10)
_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10))


@workflow.defn
class DailyPlanWorkflow:
    """
    Daily strategic plan: reviews last 7 days, produces a regime assessment
    and focus areas for today. Writes to strategic_context (horizon='daily')
    and daily_summaries.plan_text.

    Cron: '0 0 * * *' (midnight UTC, every day)
    """

    @workflow.run
    async def run(self) -> dict:
        workflow.logger.info("DailyPlanWorkflow: starting daily review")

        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id

        # Evaluate how well yesterday's plan performed
        evaluation = await workflow.execute_activity(
            evaluate_previous_plan,
            args=["daily"],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Fetch last 7 days of trade + portfolio data
        portfolio_data = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[7],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Inject evaluation into portfolio data so the LLM can learn from it
        portfolio_data["previous_plan_evaluation"] = evaluation

        # Call LLM for daily plan
        plan = await workflow.execute_activity(
            call_planning_llm,
            args=["daily", portfolio_data],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        summary = plan.get("summary", "No summary generated.")

        # Persist to strategic_context (with Temporal + Langfuse IDs)
        await workflow.execute_activity(
            write_strategic_context,
            args=["daily", plan, summary, workflow_id, run_id],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Also write to daily_summaries.plan_text
        today = str(workflow.now().date())
        plan_text = (
            f"[{today}] DAILY PLAN | Regime: {plan.get('regime', '?')} | "
            f"Risk: {plan.get('risk_posture', '?')} | "
            f"Focus: {plan.get('today_focus', summary)}"
        )
        await workflow.execute_activity(
            write_daily_plan,
            args=[today, plan_text],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        workflow.logger.info(f"DailyPlanWorkflow: complete --- {summary[:100]}")
        return plan


@workflow.defn
class WeeklyReviewWorkflow:
    """
    Weekly strategy review: reviews last 30 days, produces strategy adjustments,
    pair preferences, and risk posture. Writes to strategic_context (horizon='weekly').

    Cron: '0 0 * * 1' (midnight UTC, every Monday)
    """

    @workflow.run
    async def run(self) -> dict:
        workflow.logger.info("WeeklyReviewWorkflow: starting weekly review")

        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id

        # Evaluate how well last week's plan performed
        evaluation = await workflow.execute_activity(
            evaluate_previous_plan,
            args=["weekly"],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Fetch last 30 days of data
        portfolio_data = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[30],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        trade_history = await workflow.execute_activity(
            fetch_trade_history,
            args=[30, None],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Merge reasoning sample from portfolio data with trade list + evaluation
        review_data = {
            **portfolio_data,
            "recent_trades": trade_history[:50],
            "previous_plan_evaluation": evaluation,
        }

        plan = await workflow.execute_activity(
            call_planning_llm,
            args=["weekly", review_data],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        summary = plan.get("summary", "No summary generated.")

        await workflow.execute_activity(
            write_strategic_context,
            args=["weekly", plan, summary, workflow_id, run_id],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        workflow.logger.info(f"WeeklyReviewWorkflow: complete --- {summary[:100]}")
        return plan


@workflow.defn
class MonthlyReviewWorkflow:
    """
    Monthly portfolio review: reviews last 90 days + YTD, produces macro regime
    assessment, portfolio allocation targets, and strategic themes.
    Writes to strategic_context (horizon='monthly').

    Cron: '0 0 1 * *' (midnight UTC, 1st of each month)
    """

    @workflow.run
    async def run(self) -> dict:
        workflow.logger.info("MonthlyReviewWorkflow: starting monthly review")

        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id

        # Primary: last 90 days
        portfolio_90d = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[90],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # YTD: approximate as last 365 days (sufficient for cycle analysis)
        portfolio_ytd = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[365],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        review_data = {
            **portfolio_90d,
            "ytd_stats": portfolio_ytd.get("trade_stats", {}),
            "ytd_pair_breakdown": portfolio_ytd.get("pair_breakdown", []),
        }

        plan = await workflow.execute_activity(
            call_planning_llm,
            args=["monthly", review_data],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        summary = plan.get("summary", "No summary generated.")

        await workflow.execute_activity(
            write_strategic_context,
            args=["monthly", plan, summary, workflow_id, run_id],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        workflow.logger.info(f"MonthlyReviewWorkflow: complete --- {summary[:100]}")
        return plan